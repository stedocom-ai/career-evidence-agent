import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path

from career_agent.mail_store import MailStore
from career_agent.mailing import compose, email_address, export_preview, read_resume


class MailingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.resume = self.root / "我的简历.pdf"
        self.pdf = b"%PDF-1.4\nsource snapshot\n%%EOF"
        self.resume.write_bytes(self.pdf)
        self.profile = {"email": "candidate@example.com", "name": "示例同学", "resume_path": str(self.resume)}

    def test_preview_preserves_chinese_and_original_attachment(self):
        payload, attachment, key = compose(self.profile, "hr@example.com", "示例公司", "Agent 实习")
        with MailStore(":memory:") as store:
            record = store.create(payload, attachment, key)
            self.resume.write_bytes(b"%PDF-1.4\nchanged")
            export_preview(record, self.root / "preview")
        message = BytesParser(policy=policy.default).parsebytes((self.root / "preview/message.eml").read_bytes())
        self.assertEqual(str(message["To"]), "hr@example.com")
        self.assertIn("Agent 实习", str(message["Subject"]))
        self.assertIn("示例同学", message.get_body(preferencelist=("plain",)).get_content())
        attached = list(message.iter_attachments())
        self.assertEqual(len(attached), 1)
        self.assertEqual(attached[0].get_filename(), "我的简历.pdf")
        self.assertEqual(attached[0].get_payload(decode=True), self.pdf)

    def test_addresses_and_headers_cannot_add_extra_recipients(self):
        for address in ("first@example.com,second@example.com", "name <x@example.com>", "x@example.com\r\nBcc:y@example.com", "a..b@example.com"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                email_address(address)
        for field in ({"subject": "申请\r\nBcc: another@example.com"}, {"body": "hello\x1b[2J"}):
            with self.subTest(field=field), self.assertRaises(ValueError):
                compose(self.profile, "hr@example.com", "示例公司", "Agent 实习", **field)

    def test_deduplication_uses_job_identity_and_mailboxes(self):
        one = compose(self.profile, "hr@example.com", "公司", "岗位")
        same = compose(self.profile, "HR@example.com", "公司", "岗位", job_id="doc_one", subject="改主题")
        same_with_url = compose(self.profile, "hr@example.com", "公司", "岗位", source_url="https://example.com/job")
        different = compose(self.profile, "hr@example.com", "公司", "另一岗位", job_id="doc_two")
        self.assertEqual(one[2], same[2])
        self.assertEqual(one[2], same_with_url[2])
        self.assertNotEqual(one[2], different[2])

    def test_resume_must_be_pdf_and_fit_snapshot_limit(self):
        self.resume.write_bytes(b"not a pdf")
        with self.assertRaises(ValueError):
            read_resume(self.resume)
        self.resume.write_bytes(b"%PDF-" + b"x" * (2 * 1024 * 1024))
        with self.assertRaises(ValueError):
            read_resume(self.resume)


if __name__ == "__main__":
    unittest.main()
