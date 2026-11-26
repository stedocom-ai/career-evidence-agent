"""Classic Outlook transport using the signed-in Windows desktop profile.

Only explicit check/send commands invoke Outlook. Unit tests mock the helper
process. Outlook accepting Send means queued, rather than recipient delivery.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .outlook import MailSendError


_EMAIL = re.compile(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+")


def _address(value) -> str:
    if not isinstance(value, str) or not _EMAIL.fullmatch(value) or len(value) > 254 or any(
        ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise MailSendError("请提供一个完整的邮箱地址。", outcome="failed")
    return value


class DesktopOutlookClient:
    def __init__(self, sender_email: str, timeout=60):
        self.sender_email = _address(sender_email)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 300:
            raise MailSendError("Outlook 超时必须在 1 到 300 秒之间。", outcome="failed")
        self.timeout = timeout

    def _invoke(self, operation, payload_path=None, attachment_path=None):
        if sys.platform != "win32":
            raise MailSendError("经典 Outlook 投递仅支持 Windows。", outcome="failed")
        executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        helper = Path(__file__).with_suffix(".ps1")
        if not executable.is_file() or not helper.is_file():
            raise MailSendError("未找到 Windows PowerShell 或 Outlook helper，请使用完整项目目录。", outcome="failed")
        args = [str(executable), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(helper), "-Operation", operation, "-AccountEmail", self.sender_email]
        if payload_path is not None:
            args.extend(["-PayloadPath", str(payload_path), "-AttachmentPath", str(attachment_path)])
        uncertain = "unknown" if operation == "send" else "failed"
        try:
            process = subprocess.run(
                args, shell=False, capture_output=True, encoding="utf-8-sig",
                timeout=self.timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            raise MailSendError(
                "经典 Outlook 响应超时，请检查 Outlook 的账户设置、提示框和已发送记录。",
                outcome=uncertain,
            ) from None
        except OSError:
            raise MailSendError("无法启动 Outlook helper。", outcome="failed") from None
        except UnicodeError:
            raise MailSendError("Outlook helper 返回了无法读取的响应，请核对邮箱记录。", outcome=uncertain) from None
        try:
            result = json.loads(process.stdout)
            if not isinstance(result, dict):
                raise ValueError
        except (ValueError, TypeError):
            raise MailSendError("Outlook helper 响应无效，请核对邮箱记录。", outcome=uncertain) from None
        if result.get("status") == "error":
            outcome = result.get("outcome")
            if outcome not in {"failed", "unknown"}:
                outcome = uncertain
            error = result.get("error")
            if not isinstance(error, str) or not error:
                error = "经典 Outlook 操作失败，请查看 Outlook 账户状态。"
            raise MailSendError(error[:2000], outcome=outcome)
        if process.returncode != 0:
            raise MailSendError("Outlook helper 未正常结束，请核对邮箱记录。", outcome=uncertain)
        return result

    def get_sender(self) -> dict:
        result = self._invoke("check")
        address = result.get("mail")
        if result.get("status") != "ready" or not isinstance(address, str) or address.casefold() != self.sender_email.casefold():
            raise MailSendError("Outlook 检测到的发件账户与配置不一致。", outcome="failed")
        return result

    def send_message(self, payload: dict, attachment: bytes, correlation_id: str) -> dict:
        if not isinstance(payload, dict) or not isinstance(payload.get("sender_email"), str) or payload["sender_email"].casefold() != self.sender_email.casefold():
            raise MailSendError("邮件草稿的发件账户与 Outlook 配置不一致。", outcome="failed")
        _address(payload.get("recipient"))
        for key in ("subject", "body", "resume_name"):
            if not isinstance(payload.get(key), str) or not payload[key].strip():
                raise MailSendError(f"邮件草稿缺少有效 {key}。", outcome="failed")
        if any(char in payload["subject"] for char in "\r\n"):
            raise MailSendError("邮件主题不能包含换行。", outcome="failed")
        filename = payload["resume_name"]
        if len(filename) > 200 or filename in {".", ".."} or any(
            char in '<>:"/\\|?*' or ord(char) < 32 for char in filename
        ):
            raise MailSendError("附件名称必须是有效的文件名。", outcome="failed")
        if not isinstance(attachment, bytes) or not attachment:
            raise MailSendError("简历附件不能为空。", outcome="failed")
        if not isinstance(correlation_id, str) or not 1 <= len(correlation_id) <= 200 or any(
            char in correlation_id for char in "\r\n"
        ):
            raise MailSendError("邮件关联编号格式无效。", outcome="failed")
        helper_invoked = False
        try:
            with tempfile.TemporaryDirectory(prefix="career-agent-mail-") as temporary:
                root = Path(temporary)
                payload_path = root / "payload.json"
                attachment_folder = root / "attachments"
                attachment_folder.mkdir()
                attachment_path = attachment_folder / filename
                data = {**payload, "correlation_id": correlation_id}
                payload_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                attachment_path.write_bytes(attachment)
                helper_invoked = True
                result = self._invoke("send", payload_path, attachment_path)
        except OSError:
            outcome = "unknown" if helper_invoked else "failed"
            raise MailSendError("无法准备或清理 Outlook 的临时附件文件。", outcome=outcome) from None
        if result.get("status") != "accepted" or result.get("provider_reference") != "classic-outlook-queued":
            raise MailSendError("Outlook 发送响应无效，请核对邮箱的已发送记录。", outcome="unknown")
        return result
