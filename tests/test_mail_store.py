import hashlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from career_agent.mail_store import MailStore
from career_agent.storage import Store


class MailStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "career.db"
        self.store = MailStore(self.db)
        self.payload = {"subject": "申请 AI Agent 实习", "recipient": "hr@example.com"}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def draft(self, key="candidate@example.com|job-1"):
        return self.store.create(self.payload, b"original resume snapshot", key)

    def test_dedup_keeps_original_body_and_attachment_snapshot(self):
        draft = self.draft()
        self.payload["subject"] = "changed after creation"
        duplicate = self.store.create(self.payload, b"changed attachment", draft["dedupe_key"])
        self.assertTrue(draft["created"])
        self.assertFalse(duplicate["created"])
        self.assertEqual(duplicate["draft_id"], draft["draft_id"])
        self.assertEqual(duplicate["message_id"], draft["message_id"])
        self.assertEqual(duplicate["payload"]["subject"], "申请 AI Agent 实习")
        self.assertEqual(duplicate["attachment"], b"original resume snapshot")
        self.assertEqual(duplicate["attachment_sha256"], hashlib.sha256(duplicate["attachment"]).hexdigest())
        self.assertTrue(duplicate["message_id"].startswith("<"))
        self.assertTrue(duplicate["message_id"].endswith(">"))

    def test_claim_and_finish_require_matching_send_attempt(self):
        draft = self.draft()
        sending = self.store.claim(draft["draft_id"])
        self.assertEqual(sending["status"], "sending")
        with self.assertRaises(ValueError):
            self.store.finish(draft["draft_id"], "wrong-attempt", "accepted")
        self.assertEqual(self.store.get(draft["draft_id"])["status"], "sending")
        accepted = self.store.finish(draft["draft_id"], sending["attempt_id"], "accepted", provider_reference="request-123")
        self.assertEqual(accepted["provider_reference"], "request-123")
        with self.assertRaises(ValueError):
            self.store.claim(draft["draft_id"])
        with self.assertRaises(ValueError):
            self.store.finish(draft["draft_id"], sending["attempt_id"], "failed")

    def test_failed_attempt_can_retry_with_new_attempt_id(self):
        draft = self.draft()
        first = self.store.claim(draft["draft_id"])
        self.store.finish(draft["draft_id"], first["attempt_id"], "failed", error="Authentication failed")
        second = self.store.claim(draft["draft_id"])
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(first["message_id"], second["message_id"])
        with self.assertRaises(ValueError):
            self.store.finish(draft["draft_id"], first["attempt_id"], "accepted")

    def test_uncertain_and_interrupted_sends_require_explicit_resolution(self):
        draft = self.draft()
        attempt = self.store.claim(draft["draft_id"])
        self.store.finish(draft["draft_id"], attempt["attempt_id"], "unknown", error="Response not received")
        with self.assertRaises(ValueError):
            self.store.claim(draft["draft_id"])
        resolved = self.store.resolve(draft["draft_id"], False, "已查看发件箱，确认未发送")
        self.assertEqual(resolved["status"], "failed")
        self.store.claim(draft["draft_id"])
        confirmed = self.store.resolve(draft["draft_id"], True, "已发送记录有此封邮件")
        self.assertEqual(confirmed["status"], "accepted")
        self.assertIn("已发送记录", confirmed["provider_reference"])
        with self.assertRaises(ValueError):
            self.store.resolve(draft["draft_id"], False, "不能改写最终状态")

    def test_existing_evidence_tables_and_reopened_mail_state_are_preserved(self):
        with Store(self.db) as evidence:
            document = evidence.add_text("课程 Python 脚本", "resume", "简历")
        draft = self.draft()
        self.store.claim(draft["draft_id"])
        self.store.close()
        self.store = MailStore(self.db)
        self.assertEqual(self.store.get(draft["draft_id"])["status"], "sending")
        with self.assertRaises(ValueError):
            self.store.claim(draft["draft_id"])
        with Store(self.db) as evidence:
            self.assertEqual(evidence.search("Python")[0]["document_id"], document["document_id"])

    def test_concurrent_process_style_connections_only_claim_once(self):
        draft = self.draft()
        barrier = threading.Barrier(2)

        def claim():
            with MailStore(self.db) as independent:
                barrier.wait(timeout=5)
                try:
                    independent.claim(draft["draft_id"])
                    return "claimed"
                except ValueError:
                    return "blocked"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(claim) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sorted(results), ["blocked", "claimed"])

    def test_invalid_inputs_do_not_create_partial_records(self):
        invalid = [
            lambda: self.store.create([], b"resume", "key"),
            lambda: self.store.create({"bad": float("nan")}, b"resume", "key"),
            lambda: self.store.create({}, b"", "key"),
            lambda: self.store.create({}, b"resume", " "),
            lambda: self.store.claim("missing"),
        ]
        for operation in invalid:
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(self.store.list(), [])

    def test_update_draft_changes_snapshot_and_resets_failed_attempt(self):
        draft = self.draft()
        changed = {**draft["payload"], "subject": "更新主题", "body": "更新正文", "resume_name": "更新简历.pdf"}
        updated = self.store.update_draft(draft["draft_id"], changed, b"updated resume snapshot")
        self.assertEqual(updated["payload"], changed)
        self.assertEqual(updated["attachment"], b"updated resume snapshot")
        self.assertNotEqual(updated["attachment_sha256"], draft["attachment_sha256"])
        self.assertEqual(updated["attachment_sha256"], hashlib.sha256(updated["attachment"]).hexdigest())
        for field in ("draft_id", "dedupe_key", "message_id", "created_at"):
            self.assertEqual(updated[field], draft[field])
        self.assertGreaterEqual(updated["updated_at"], draft["updated_at"])
        attempt = self.store.claim(draft["draft_id"])
        self.store.finish(draft["draft_id"], attempt["attempt_id"], "failed", error="Rejected", provider_reference="rejection-id")
        reset = self.store.update_draft(draft["draft_id"], changed, b"second updated resume")
        self.assertEqual(reset["status"], "draft")
        self.assertIsNone(reset["error"])
        self.assertIsNone(reset["attempt_id"])
        self.assertIsNone(reset["provider_reference"])

    def test_update_draft_rejects_changes_to_identity_fields(self):
        draft = self.draft()
        for field in ("sender_email", "recipient", "company", "position", "job_id", "source_url"):
            changed = {**draft["payload"], field: "different-value"}
            with self.assertRaises(ValueError):
                self.store.update_draft(draft["draft_id"], changed, b"updated resume")
        unchanged = self.store.get(draft["draft_id"])
        self.assertEqual(unchanged["payload"], draft["payload"])
        self.assertEqual(unchanged["attachment"], draft["attachment"])
        compatible = {**draft["payload"], "job_id": None, "source_url": None}
        self.assertEqual(self.store.update_draft(draft["draft_id"], compatible, draft["attachment"])["status"], "draft")

    def test_update_draft_blocks_sending_accepted_and_uncertain_records(self):
        for state in ("sending", "accepted", "unknown"):
            draft = self.draft("job-" + state)
            attempt = self.store.claim(draft["draft_id"])
            if state != "sending":
                self.store.finish(draft["draft_id"], attempt["attempt_id"], state)
            with self.assertRaises(ValueError):
                self.store.update_draft(draft["draft_id"], {**draft["payload"], "body": "updated"}, b"new resume")
            stored = self.store.get(draft["draft_id"])
            self.assertEqual(stored["status"], state)
            self.assertEqual(stored["payload"], draft["payload"])


if __name__ == "__main__":
    unittest.main()
