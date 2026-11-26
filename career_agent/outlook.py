"""Delegated Microsoft Graph mail access with Windows-protected token caching."""

from __future__ import annotations

import base64
import http.client
import json
import re
import socket
import sys
import urllib.error
import urllib.request
from email.headerregistry import Address
from importlib import import_module
from pathlib import Path
from uuid import UUID

from .client import _NoRedirect


GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Send", "User.Read"]
MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
_WINDOWS = sys.platform == "win32"


class MailSendError(RuntimeError):
    """A safe user-facing error and whether delivery may have been accepted."""

    def __init__(self, message: str, outcome: str = "failed"):
        if outcome not in {"failed", "unknown"}:
            raise ValueError("邮件错误状态必须是 failed 或 unknown。")
        super().__init__(message)
        self.outcome = outcome


class OutlookAuth:
    def __init__(
        self,
        client_id: str,
        tenant: str = "consumers",
        cache_path: Path = Path(".local/outlook-token-cache.bin"),
        username: str | None = None,
    ):
        try:
            self.client_id = str(UUID(client_id))
        except (ValueError, TypeError, AttributeError):
            raise MailSendError("Outlook 应用 Client ID 必须是有效的 UUID。") from None
        if isinstance(tenant, str) and tenant in {"consumers", "common", "organizations"}:
            self.tenant = tenant
        else:
            try:
                self.tenant = str(UUID(tenant))
            except (ValueError, TypeError, AttributeError):
                raise MailSendError("Outlook tenant 必须是 consumers、common、organizations 或租户 UUID。") from None
        if username is not None and (
            not isinstance(username, str) or not username.strip()
            or len(username) > 320 or "\r" in username or "\n" in username
        ):
            raise MailSendError("Outlook 登录邮箱格式无效。")
        self.username = username.strip() if username else None
        self.cache_path = Path(cache_path)

    def acquire(self, interactive: bool = False, notify=print) -> str:
        if not _WINDOWS:
            raise MailSendError("当前 Outlook 持久登录使用 Windows 加密存储，请在 Windows 中运行。")
        try:
            msal = import_module("msal")
            extensions = import_module("msal_extensions")
        except ImportError:
            raise MailSendError("Outlook 登录需要可选依赖，请运行 python -m pip install -e '.[outlook]'。") from None
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            persistence = extensions.FilePersistenceWithDataProtection(str(self.cache_path.resolve()))
            cache = extensions.PersistedTokenCache(persistence)
            app = msal.PublicClientApplication(
                self.client_id,
                authority="https://login.microsoftonline.com/" + self.tenant,
                token_cache=cache,
            )
            accounts = app.get_accounts(username=self.username)
            if self.username is None and len(accounts) > 1:
                raise MailSendError("缓存包含多个 Outlook 账户，请指定发件邮箱后运行 mail-login。")
            for account in accounts:
                result = app.acquire_token_silent(SCOPES, account=account)
                if isinstance(result, dict) and isinstance(result.get("access_token"), str) and result["access_token"]:
                    return result["access_token"]
            if not interactive:
                raise MailSendError("Outlook 尚未登录或授权已失效，请先运行 mail-login。")
            flow = app.initiate_device_flow(scopes=SCOPES)
            if not isinstance(flow, dict) or not all(
                isinstance(flow.get(key), str) and flow[key]
                for key in ("user_code", "verification_uri", "device_code")
            ):
                raise MailSendError("无法启动 Outlook 登录，请检查 Client ID、账户类型和公共客户端设置。")
            notify("请在浏览器打开 " + flow["verification_uri"])
            notify("输入登录代码 " + flow["user_code"] + "，选择发件账户并授权。")
            result = app.acquire_token_by_device_flow(flow)
            if isinstance(result, dict) and isinstance(result.get("access_token"), str) and result["access_token"]:
                return result["access_token"]
            raise MailSendError("Outlook 登录未完成，请检查登录与授权页面后重试 mail-login。")
        except MailSendError:
            raise
        except Exception:
            # MSAL/network/persistence errors may contain token or response details.
            raise MailSendError("Outlook 登录或加密缓存访问失败，请检查网络、应用配置及 Windows 账户。") from None


def _single_address(value) -> str:
    if not isinstance(value, str) or len(value) > 320 or any(char.isspace() for char in value):
        raise MailSendError("每次投递需要一个有效的邮箱地址。")
    try:
        parsed = Address(addr_spec=value)
        value.encode("ascii")
    except (ValueError, UnicodeError):
        raise MailSendError("每次投递需要一个有效的邮箱地址。") from None
    if not parsed.username or not parsed.domain or parsed.addr_spec != value:
        raise MailSendError("每次投递需要一个有效的邮箱地址。")
    return value


class GraphClient:
    """Only this client's send_message method performs a mail submission."""

    def __init__(self, access_token: str, timeout: float = 30):
        if (not isinstance(access_token, str) or not access_token or not access_token.isascii()
                or any(char.isspace() for char in access_token)):
            raise MailSendError("Outlook 访问令牌无效，请重新运行 mail-login。")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120:
            raise MailSendError("Outlook 请求超时必须在 1 至 120 秒之间。")
        self._access_token = access_token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _request(self, method: str, path: str, body: bytes | None = None):
        request = urllib.request.Request(
            GRAPH_ROOT + path,
            data=body,
            headers={
                "Authorization": "Bearer " + self._access_token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.status
                headers = response.headers
                # Once a 202 response arrives, Graph has accepted the request.
                # There is no response body, and acceptance is not final delivery.
                raw = b"" if method == "POST" and status == 202 else response.read(MAX_RESPONSE_BYTES + 1)
            return status, headers, raw
        except urllib.error.HTTPError as exc:
            outcome = "failed" if 400 <= exc.code < 500 and exc.code != 408 else "unknown"
            hints = {
                400: "检查邮件内容和附件",
                401: "重新运行 mail-login",
                403: "检查 Mail.Send 授权和组织策略",
                413: "缩小简历附件",
                429: "稍后再试",
            }
            raise MailSendError(
                f"Outlook 返回 HTTP {exc.code}，{hints.get(exc.code, '请核对发送记录后再处理')}。",
                outcome=outcome,
            ) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, http.client.HTTPException):
            raise MailSendError("Outlook 请求中断或超时，请核对已发送邮件后再处理。", outcome="unknown") from None

    def get_sender(self) -> dict:
        status, _, raw = self._request("GET", "/me?$select=mail,userPrincipalName,displayName")
        if status != 200 or len(raw) > MAX_RESPONSE_BYTES:
            raise MailSendError("无法确认 Outlook 发件账户，请重新登录。")
        try:
            data = json.loads(raw)
            fields = ("mail", "userPrincipalName", "displayName")
            if not isinstance(data, dict) or any(
                data.get(field) is not None and not isinstance(data[field], str) for field in fields
            ):
                raise ValueError
            result = {field: data.get(field) for field in fields}
            if not result["mail"] and not result["userPrincipalName"]:
                raise ValueError
            return result
        except (ValueError, UnicodeError, TypeError):
            raise MailSendError("Outlook 发件账户响应无效，请重新登录。") from None

    def send_message(self, payload: dict, attachment: bytes, correlation_id: str) -> dict:
        if not isinstance(payload, dict):
            raise MailSendError("邮件内容格式无效。")
        recipient = _single_address(payload.get("recipient"))
        _single_address(payload.get("sender_email"))
        subject, body, resume_name = (payload.get(key) for key in ("subject", "body", "resume_name"))
        if (not isinstance(subject, str) or not subject.strip() or len(subject) > 300
                or "\r" in subject or "\n" in subject
                or not isinstance(body, str) or not body.strip() or len(body) > 30_000):
            raise MailSendError("邮件标题或正文为空、过长或包含无效字符。")
        if (not isinstance(resume_name, str) or not resume_name.lower().endswith(".pdf")
                or len(resume_name) > 240 or any(char in resume_name for char in ("/", "\\", "\r", "\n"))):
            raise MailSendError("简历附件需要有效的 PDF 文件名。")
        if not isinstance(attachment, bytes) or not attachment.startswith(b"%PDF-") or len(attachment) > MAX_ATTACHMENT_BYTES:
            raise MailSendError("简历必须为不超过 2 MiB 的 PDF 附件。")
        if not isinstance(correlation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", correlation_id):
            raise MailSendError("邮件投递记录编号无效。")
        message = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
            "attachments": [{
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": resume_name,
                "contentType": "application/pdf",
                "contentBytes": base64.b64encode(attachment).decode("ascii"),
            }],
            "internetMessageHeaders": [{"name": "X-Career-Agent-ID", "value": correlation_id}],
        }
        raw = json.dumps({"message": message, "saveToSentItems": True}, ensure_ascii=False).encode("utf-8")
        status, headers, _ = self._request("POST", "/me/sendMail", raw)
        if status != 202:
            raise MailSendError("Outlook 返回了意外的发送状态，请核对已发送邮件后再处理。", outcome="unknown")
        reference = headers.get("request-id")
        if not isinstance(reference, str) or len(reference) > 200 or "\r" in reference or "\n" in reference:
            reference = None
        return {"status": "accepted", "provider_reference": reference}
