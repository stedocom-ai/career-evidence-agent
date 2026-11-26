import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from career_agent.mail_cli import send_draft
from career_agent.mail_store import MailStore
from career_agent.mailing import compose
from career_agent.outlook import MailSendError


class FakeMailClient:
    def __init__(self, error=None, sender="candidate@example.com"):
        self.error = error
        self.sender = sender
        self.sent = []

    def get_sender(self):
        return {"mail": self.sender}

    def send_message(self, payload, attachment, correlation_id):
        self.sent.append((payload, attachment, correlation_id))
        if self.error:
            raise self.error
        return {"status": "accepted", "provider_reference": "fake-accepted"}


class MailCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = str(self.root / "career.db")
        self.pdf = self.root / "resume.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\nexample\n%%EOF")
        self.profile = {"email": "candidate@example.com", "name": "示例", "resume_path": str(self.pdf), "transport": "graph"}
        self.output = io.StringIO()

    def draft(self, store):
        payload, attachment, key = compose(self.profile, "hr@example.com", "公司", "实习岗位")
        return store.create(payload, attachment, key)

    def call(self, *args):
        return subprocess.run([sys.executable, "-m", "career_agent", "--db", self.db, *args],
                              capture_output=True, text=True, encoding="utf-8")

    def test_normal_cli_profile_draft_preview_list_is_offline(self):
        configured = self.call("mail-profile", "--email", self.profile["email"], "--name", "示例", "--resume", str(self.pdf))
        self.assertEqual(configured.returncode, 0, configured.stderr)
        drafted = self.call("mail-draft", "--to", "hr@example.com", "--company", "公司", "--position", "岗位")
        self.assertEqual(drafted.returncode, 0, drafted.stderr)
        listed = json.loads(self.call("mail-list").stdout)
        self.assertEqual(listed[0]["status"], "draft")
        preview = self.call("mail-preview", "--id", listed[0]["draft_id"])
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn("hr@example.com", preview.stdout)
        self.assertTrue((self.root / "mail" / listed[0]["draft_id"] / "message.eml").exists())
        edited = self.call("mail-edit", "--id", listed[0]["draft_id"], "--subject", "更新主题")
        self.assertEqual(edited.returncode, 0, edited.stderr)
        self.assertIn("更新主题", edited.stdout)

    def test_accepted_draft_cannot_be_sent_again(self):
        client = FakeMailClient()
        with MailStore(self.db) as store, patch("career_agent.mail_cli._client", return_value=client), contextlib.redirect_stdout(self.output):
            record = self.draft(store)
            self.assertEqual(send_draft(store, record, self.profile, self.db), 0)
            self.assertEqual(send_draft(store, store.get(record["draft_id"]), self.profile, self.db), 0)
            self.assertEqual(len(client.sent), 1)
            self.assertEqual(store.get(record["draft_id"])["status"], "accepted")

    def test_uncertain_send_blocks_retry_until_manually_resolved(self):
        client = FakeMailClient(MailSendError("timeout", outcome="unknown"))
        with MailStore(self.db) as store, patch("career_agent.mail_cli._client", return_value=client), contextlib.redirect_stdout(self.output):
            record = self.draft(store)
            self.assertEqual(send_draft(store, record, self.profile, self.db), 1)
            with self.assertRaises(ValueError):
                send_draft(store, store.get(record["draft_id"]), self.profile, self.db)
            self.assertEqual(len(client.sent), 1)
            store.resolve(record["draft_id"], False, "测试确认模拟服务没有接受消息")
            client.error = None
            self.assertEqual(send_draft(store, store.get(record["draft_id"]), self.profile, self.db), 0)

    def test_wrong_logged_in_sender_stops_before_claim_and_send(self):
        client = FakeMailClient(sender="other@example.com")
        with MailStore(self.db) as store, patch("career_agent.mail_cli._client", return_value=client):
            record = self.draft(store)
            with self.assertRaises(ValueError):
                send_draft(store, record, self.profile, self.db)
            self.assertEqual(store.get(record["draft_id"])["status"], "draft")
            self.assertEqual(client.sent, [])


if __name__ == "__main__":
    unittest.main()
