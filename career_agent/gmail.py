"""Gmail SMTP delivery using a user-supplied application password."""

from __future__ import annotations

import re
import smtplib
import ssl
from email.message import EmailMessage

from .mailing import MAX_RESUME_BYTES, build_message, email_address, text_field
from .outlook import MailSendError


def _close_connection(connection):
    if connection is None:
        return
    try:
        connection.quit()
    except (OSError, smtplib.SMTPException):
        # A cleanup error cannot undo an acknowledged SMTP DATA transaction.
        try:
            connection.close()
        except (OSError, smtplib.SMTPException):
            pass


def _build_message(payload: dict, attachment: bytes, correlation_id: str) -> EmailMessage:
    if not isinstance(payload, dict):
        raise ValueError("邮件草稿内容格式有误。")
    email_address(payload.get("sender_email"))
    email_address(payload.get("recipient"))
    text_field(payload.get("sender_name"), "发件署名")
    text_field(payload.get("subject"), "邮件主题", 300)
    text_field(payload.get("body"), "邮件正文", 20_000, multiline=True)
    filename = text_field(payload.get("resume_name"), "简历文件名")
    if not filename.lower().endswith(".pdf") or any(char in filename for char in "/\\"):
        raise ValueError("简历需要有效的 PDF 文件名。")
    if not isinstance(attachment, bytes) or not attachment.startswith(b"%PDF-") or len(attachment) > MAX_RESUME_BYTES:
        raise ValueError("简历需要不超过 2 MiB 的 PDF 附件。")
    if not isinstance(correlation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", correlation_id):
        raise ValueError("邮件投递记录编号无效。")
    message_id = payload.get("message_id", f"<{correlation_id}@career-agent.local>")
    if (not isinstance(message_id, str) or not message_id.isascii()
            or not re.fullmatch(r"<[^\s<>@]+@[^\s<>@]+>", message_id)):
        raise ValueError("邮件 Message-ID 格式有误。")
    created_at = payload.get("created_at")
    if created_at is not None:
        text_field(created_at, "邮件创建时间", 100)
    message = build_message(payload, attachment, message_id, created_at)
    message["X-Career-Agent-ID"] = correlation_id
    return message


class GmailSmtpClient:
    def __init__(self, sender_email: str, app_password: str, timeout=30):
        try:
            self.sender_email = email_address(sender_email)
        except ValueError:
            raise MailSendError("Gmail 发件邮箱地址格式有误。", outcome="failed") from None
        if not isinstance(app_password, str):
            raise MailSendError("请在本地设置 GMAIL_APP_PASSWORD，使用 Google 应用专用密码。", outcome="failed")
        secret = app_password.replace(" ", "").strip()
        if len(secret) != 16 or any(char.isspace() for char in secret) or not secret.isascii():
            raise MailSendError("GMAIL_APP_PASSWORD 需要填写 Google 生成的 16 位应用专用密码。", outcome="failed")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120:
            raise MailSendError("Gmail 请求超时必须在 1 到 120 秒之间。", outcome="failed")
        self._app_password = secret
        self.timeout = timeout

    def _connect(self):
        return smtplib.SMTP_SSL(
            "smtp.gmail.com", 465, timeout=self.timeout, context=ssl.create_default_context(),
        )

    def get_sender(self) -> dict:
        connection = None
        try:
            connection = self._connect()
            connection.login(self.sender_email, self._app_password)
            return {"mail": self.sender_email}
        except smtplib.SMTPAuthenticationError:
            raise MailSendError("Gmail 登录失败，请检查应用专用密码和账户的两步验证设置。", outcome="failed") from None
        except (OSError, smtplib.SMTPException):
            raise MailSendError("Gmail 连接或登录未完成，请检查网络和邮箱设置。", outcome="failed") from None
        finally:
            _close_connection(connection)

    def send_message(self, payload: dict, attachment: bytes, correlation_id: str) -> dict:
        try:
            message = _build_message(payload, attachment, correlation_id)
        except (ValueError, TypeError, KeyError):
            raise MailSendError("邮件内容或简历附件无效，请检查预览并修正草稿。", outcome="failed") from None
        if payload["sender_email"].casefold() != self.sender_email.casefold():
            raise MailSendError("草稿发件邮箱与 Gmail 登录账户不一致。", outcome="failed")
        recipient = email_address(payload["recipient"])
        connection = None
        dispatched = False
        try:
            connection = self._connect()
            connection.login(self.sender_email, self._app_password)
            dispatched = True
            rejected = connection.send_message(message, from_addr=self.sender_email, to_addrs=[recipient])
            if rejected:
                raise MailSendError("Gmail 拒绝了指定收件人，邮件未被接受。", outcome="failed")
            return {"status": "accepted", "provider_reference": message["Message-ID"]}
        except smtplib.SMTPAuthenticationError:
            raise MailSendError("Gmail 登录失败，请检查应用专用密码和账户的两步验证设置。", outcome="failed") from None
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            raise MailSendError("Gmail 明确拒绝了本次邮件，请核对发件账户、收件人和内容。", outcome="failed") from None
        except (OSError, smtplib.SMTPException):
            outcome = "unknown" if dispatched else "failed"
            message_text = ("Gmail 发送结果待核实，请检查已发送邮件后再处理。" if dispatched
                            else "Gmail 连接或登录失败，邮件尚未提交。")
            raise MailSendError(message_text, outcome=outcome) from None
        finally:
            _close_connection(connection)
