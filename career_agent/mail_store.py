"""Durable mail drafts and send attempts in the existing local database.

An accepted request means the mail provider accepted it. A process interruption
or uncertain network result stays blocked until the user checks Sent Items.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import make_msgid
from pathlib import Path
from uuid import uuid4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value, name: str, maximum: int, *, allow_empty=False) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{name} 必须是长度不超过 {maximum} 的字符串。")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name} 不能为空。")
    return value


def _snapshot(payload: dict, attachment: bytes) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("邮件内容必须是对象。")
    if not isinstance(attachment, bytes) or not attachment:
        raise ValueError("附件必须是非空 bytes。")
    try:
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("邮件内容不能转换为有效 JSON。") from exc
    return serialized, hashlib.sha256(attachment).hexdigest()


class MailStore:
    def __init__(self, db_path: str | Path):
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), timeout=5)
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS mail_outbox (
                draft_id TEXT PRIMARY KEY,
                dedupe_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK(status IN
                    ('draft','sending','accepted','failed','unknown')),
                payload TEXT NOT NULL,
                attachment BLOB NOT NULL,
                attachment_sha256 TEXT NOT NULL,
                message_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                attempt_id TEXT,
                error TEXT,
                provider_reference TEXT
            )"""
        )
        self._db.commit()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._db.close()

    @contextmanager
    def _write(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    @staticmethod
    def _record(row) -> dict | None:
        if row is None:
            return None
        record = dict(row)
        record["payload"] = json.loads(record["payload"])
        record["attachment"] = bytes(record["attachment"])
        return record

    def create(self, payload: dict, attachment: bytes, dedupe_key: str) -> dict:
        _text(dedupe_key, "dedupe_key", 2048)
        serialized, digest = _snapshot(payload, attachment)
        timestamp = _now()
        with self._write():
            cursor = self._db.execute(
                """INSERT INTO mail_outbox
                   (draft_id,dedupe_key,status,payload,attachment,attachment_sha256,
                    message_id,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(dedupe_key) DO NOTHING""",
                ("mail_" + uuid4().hex, dedupe_key, "draft", serialized,
                 attachment, digest,
                 make_msgid(domain="career-agent.local"), timestamp, timestamp),
            )
            row = self._db.execute(
                "SELECT * FROM mail_outbox WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
            result = self._record(row)
            assert result is not None
            result["created"] = cursor.rowcount == 1
        return result

    def update_draft(self, draft_id: str, payload: dict, attachment: bytes) -> dict:
        _text(draft_id, "draft_id", 200)
        serialized, digest = _snapshot(payload, attachment)
        identity_fields = ("sender_email", "recipient", "company", "position", "job_id", "source_url")
        with self._write():
            row = self._db.execute(
                "SELECT * FROM mail_outbox WHERE draft_id=?", (draft_id,)
            ).fetchone()
            if row is None:
                raise ValueError("邮件草稿不存在。")
            if row["status"] not in {"draft", "failed"}:
                raise ValueError(f"草稿状态为 {row['status']}，无法修改。请先核对发送结果。")
            original = json.loads(row["payload"])
            for field in identity_fields:
                if original.get(field) != payload.get(field):
                    raise ValueError(f"无法修改草稿的 {field}，请为新的岗位或收件人创建草稿。")
            self._db.execute(
                """UPDATE mail_outbox SET payload=?,attachment=?,attachment_sha256=?,
                   updated_at=?,status='draft',error=NULL,attempt_id=NULL,provider_reference=NULL
                   WHERE draft_id=?""",
                (serialized, attachment, digest, _now(), draft_id),
            )
            result = self._record(self._db.execute(
                "SELECT * FROM mail_outbox WHERE draft_id=?", (draft_id,)
            ).fetchone())
        assert result is not None
        return result

    def get(self, draft_id: str) -> dict | None:
        _text(draft_id, "draft_id", 200)
        row = self._db.execute(
            "SELECT * FROM mail_outbox WHERE draft_id=?", (draft_id,)
        ).fetchone()
        return self._record(row)

    def list(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM mail_outbox ORDER BY created_at,draft_id"
        ).fetchall()
        return [self._record(row) for row in rows]

    def claim(self, draft_id: str) -> dict:
        _text(draft_id, "draft_id", 200)
        with self._write():
            row = self._db.execute(
                "SELECT * FROM mail_outbox WHERE draft_id=?", (draft_id,)
            ).fetchone()
            if row is None:
                raise ValueError("邮件草稿不存在。")
            if row["status"] not in {"draft", "failed"}:
                raise ValueError(
                    f"草稿状态为 {row['status']}，无法重复发送。"
                    "如上次发送结果不确定，请先核对邮箱的已发送记录。"
                )
            self._db.execute(
                """UPDATE mail_outbox SET status='sending',attempt_id=?,updated_at=?,
                   error=NULL,provider_reference=NULL WHERE draft_id=?""",
                (uuid4().hex, _now(), draft_id),
            )
            result = self._record(self._db.execute(
                "SELECT * FROM mail_outbox WHERE draft_id=?", (draft_id,)
            ).fetchone())
        assert result is not None
        return result

    def finish(
        self, draft_id: str, attempt_id: str, status: str,
        error: str | None = None, provider_reference: str | None = None,
    ) -> dict:
        _text(draft_id, "draft_id", 200)
        _text(attempt_id, "attempt_id", 200)
        if status not in {"accepted", "failed", "unknown"}:
            raise ValueError("发送结束状态必须是 accepted、failed 或 unknown。")
        if error is not None:
            _text(error, "error", 4000, allow_empty=True)
        if provider_reference is not None:
            _text(provider_reference, "provider_reference", 2000, allow_empty=True)
        with self._write():
            cursor = self._db.execute(
                """UPDATE mail_outbox SET status=?,updated_at=?,error=?,provider_reference=?
                   WHERE draft_id=? AND status='sending' AND attempt_id=?""",
                (status, _now(), error, provider_reference, draft_id, attempt_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("发送记录状态或 attempt_id 不匹配，未更新。")
            result = self._record(self._db.execute(
                "SELECT * FROM mail_outbox WHERE draft_id=?", (draft_id,)
            ).fetchone())
        assert result is not None
        return result

    def resolve(self, draft_id: str, was_sent: bool, note: str) -> dict:
        _text(draft_id, "draft_id", 200)
        _text(note, "人工核对说明", 1000)
        if type(was_sent) is not bool:
            raise ValueError("was_sent 必须是 bool。")
        status = "accepted" if was_sent else "failed"
        with self._write():
            cursor = self._db.execute(
                """UPDATE mail_outbox SET status=?,updated_at=?,error=?,provider_reference=?
                   WHERE draft_id=? AND status IN ('unknown','sending')""",
                (status, _now(), None if was_sent else note,
                 "人工核对：" + note if was_sent else None, draft_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("只有 sending 或 unknown 状态能进行人工核对。")
            result = self._record(self._db.execute(
                "SELECT * FROM mail_outbox WHERE draft_id=?", (draft_id,)
            ).fetchone())
        assert result is not None
        return result
