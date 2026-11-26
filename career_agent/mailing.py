"""Create immutable, reviewable email applications from user-specified inputs."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

MAX_RESUME_BYTES = 2 * 1024 * 1024
_BAD_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def text_field(value: str, name: str, maximum=200, *, multiline=False) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} 必须是1至{maximum}字符的非空文本。")
    if _BAD_CONTROLS.search(value) or (not multiline and any(c in value for c in "\r\n\t")):
        raise ValueError(f"{name} 含不支持的控制字符。")
    return value.strip()


def email_address(value: str) -> str:
    value = text_field(value, "邮箱地址", 254)
    if not value.isascii() or any(c.isspace() for c in value) or any(c in value for c in ",;<>\\"):
        raise ValueError("请填写一个完整的邮箱地址，例如 name@example.com。")
    try:
        address = Address(addr_spec=value)
        if address.addr_spec != value or not address.username or not address.domain or "." not in address.domain:
            raise ValueError
        if address.username.startswith(".") or address.username.endswith(".") or ".." in address.username:
            raise ValueError
        if any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", part)
               for part in address.domain.split(".")):
            raise ValueError
    except (ValueError, IndexError):
        raise ValueError("邮箱地址格式有误，只支持一个收件地址。") from None
    return value


def read_resume(path: str | Path) -> tuple[Path, bytes]:
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise ValueError("邮件简历附件请使用 PDF 文件。")
    if path.stat().st_size > MAX_RESUME_BYTES:
        raise ValueError("当前邮件版本支持不超过 2 MiB 的 PDF 附件。")
    data = path.read_bytes()
    if not data.startswith(b"%PDF-") or len(data) > MAX_RESUME_BYTES:
        raise ValueError("简历文件缺少 PDF 标识或超过附件大小限制。")
    return path, data


def profile_path(db_path: str) -> Path:
    if db_path == ":memory:":
        raise ValueError("邮箱命令需要文件数据库以保存发送状态。")
    return Path(db_path).parent / "mail-profile.json"


def read_profile(db_path: str) -> dict:
    path = profile_path(db_path)
    if not path.is_file():
        raise ValueError("请先运行 mail-profile，设置发件邮箱、署名和简历文件。")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("邮件配置格式有误，请重新运行 mail-profile。")
    email_address(result.get("email"))
    text_field(result.get("name"), "署名")
    if result.get("transport") not in {"gmail-smtp", "outlook-desktop", "graph"}:
        raise ValueError("发信方式必须为 gmail-smtp、outlook-desktop 或 graph。")
    return result


def write_profile(db_path: str, data: dict):
    import os
    import tempfile

    path = profile_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def compose(profile: dict, recipient: str, company: str, position: str, *,
            job_id: str | None = None, source_url: str | None = None,
            subject: str | None = None, body: str | None = None) -> tuple[dict, bytes, str]:
    sender = email_address(profile["email"])
    name = text_field(profile["name"], "署名")
    recipient = email_address(recipient)
    company = text_field(company, "公司名称")
    position = text_field(position, "岗位名称")
    if job_id:
        job_id = text_field(job_id, "岗位材料ID")
    if source_url:
        source_url = text_field(source_url, "岗位来源链接", 2000)
        parsed = urlsplit(source_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("岗位来源链接必须是有效的 HTTP(S) 地址。")
    path, attachment = read_resume(profile["resume_path"])
    subject = text_field(subject if subject is not None else f"应聘{position}｜{name}", "邮件主题", 300)
    body = text_field(body if body is not None else (
        f"{company}招聘团队，您好！\n\n"
        f"我是{name}，希望申请贵公司的{position}岗位。附件是我的简历，供您参考。\n\n"
        f"期待有机会进一步介绍我的经历。感谢您的时间！\n\n{name}\n{sender}"
    ), "邮件正文", 20_000, multiline=True)
    payload = {
        "sender_email": sender, "sender_name": name, "recipient": recipient,
        "company": company, "position": position, "subject": subject, "body": body,
        "resume_name": path.name, "job_id": job_id, "source_url": source_url,
    }
    # Optional source metadata can be added later without creating a second send.
    normalized_company = " ".join(unicodedata.normalize("NFKC", company).casefold().split())
    normalized_position = " ".join(unicodedata.normalize("NFKC", position).casefold().split())
    identity = [sender.casefold(), recipient.casefold(), normalized_company, normalized_position]
    dedupe_key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()
    return payload, attachment, dedupe_key


def validate_snapshot(record: dict):
    p = record["payload"]
    email_address(p["sender_email"])
    email_address(p["recipient"])
    for key in ("sender_name", "company", "position", "resume_name"):
        text_field(p[key], key)
    text_field(p["subject"], "邮件主题", 300)
    text_field(p["body"], "邮件正文", 20_000, multiline=True)
    if Path(p["resume_name"]).name != p["resume_name"] or any(c in p["resume_name"] for c in "/\\"):
        raise ValueError("附件文件名无效。")
    attachment = record["attachment"]
    if not attachment.startswith(b"%PDF-") or len(attachment) > MAX_RESUME_BYTES:
        raise ValueError("已保存的 PDF 附件无效。")
    if hashlib.sha256(attachment).hexdigest() != record["attachment_sha256"]:
        raise ValueError("附件快照校验失败，请核对本地数据库。")


def preview_text(record: dict) -> str:
    p = record["payload"]
    return (
        f"草稿编号：{record['draft_id']}\n状态：{record['status']}\n"
        f"发件人：{p['sender_name']} <{p['sender_email']}>\n收件人：{p['recipient']}\n"
        f"公司：{p['company']}\n岗位：{p['position']}\n"
        f"主题：{p['subject']}\n附件：{p['resume_name']}（{len(record['attachment'])} bytes）\n"
        f"附件 SHA256：{record['attachment_sha256']}\n\n{p['body']}\n"
    )


def build_message(payload: dict, attachment: bytes, message_id: str, created_at: str | None = None) -> EmailMessage:
    p = payload
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = Address(display_name=p["sender_name"], addr_spec=p["sender_email"])
    message["To"] = p["recipient"]
    message["Subject"] = p["subject"]
    message["Message-ID"] = message_id
    message["Date"] = format_datetime(datetime.fromisoformat(created_at) if created_at else datetime.now(timezone.utc))
    message.set_content(p["body"])
    message.add_attachment(attachment, maintype="application", subtype="pdf", filename=p["resume_name"])
    return message


def export_preview(record: dict, output: Path) -> Path:
    validate_snapshot(record)
    output.mkdir(parents=True, exist_ok=True)
    message = build_message(record["payload"], record["attachment"], record["message_id"], record["created_at"])
    (output / "message.eml").write_bytes(message.as_bytes())
    (output / "preview.txt").write_text(preview_text(record), encoding="utf-8")
    return output
