"""Gmail transport regression tests with mocked SMTP connections only."""

import smtplib
import unittest
from unittest import mock

from career_agent.gmail import GmailSmtpClient
from career_agent.outlook import MailSendError


SECRET = "abcdefghijklmnop"
PDF = b"%PDF-1.4\nexact saved resume\n%%EOF"
PAYLOAD = {
    "sender_email": "candidate@gmail.com", "sender_name": "示例候选人",
    "recipient": "hr@example.com", "subject": "应聘 AI Agent 实习",
    "body": "招聘团队，您好！\n附件是我的简历。\n谢谢！",
    "resume_name": "示例简历.pdf", "message_id": "<saved-id@career-agent.local>",
    "created_at": "2026-11-26T12:00:00+00:00",
}


class GmailTests(unittest.TestCase):
    def setUp(self):
        self.connection = mock.MagicMock()
        self.connection.send_message.return_value = {}
        patcher = mock.patch("career_agent.gmail.smtplib.SMTP_SSL", return_value=self.connection)
        self.smtp = patcher.start()
        self.addCleanup(patcher.stop)
        ssl_patcher = mock.patch("career_agent.gmail.ssl.create_default_context", return_value="default-verified-tls")
        ssl_patcher.start()
        self.addCleanup(ssl_patcher.stop)

    def client(self):
        return GmailSmtpClient("candidate@gmail.com", "abcd efgh ijkl mnop")

    def test_credential_check_uses_tls465_and_never_submits_mail(self):
        self.assertEqual(self.client().get_sender(), {"mail": "candidate@gmail.com"})
        self.smtp.assert_called_once_with("smtp.gmail.com", 465, timeout=30, context="default-verified-tls")
        self.connection.login.assert_called_once_with("candidate@gmail.com", SECRET)
        self.connection.send_message.assert_not_called()
        self.connection.quit.assert_called_once()

    def test_explicit_single_recipient_and_original_attachment_are_preserved(self):
        receipt = self.client().send_message(PAYLOAD, PDF, "mail_saved_id")
        self.assertEqual(receipt, {"status": "accepted", "provider_reference": PAYLOAD["message_id"]})
        message = self.connection.send_message.call_args.args[0]
        self.assertEqual(self.connection.send_message.call_args.kwargs,
                         {"from_addr": "candidate@gmail.com", "to_addrs": ["hr@example.com"]})
        self.assertEqual(message["From"].addresses[0].addr_spec, "candidate@gmail.com")
        self.assertEqual(message["To"].addresses[0].addr_spec, "hr@example.com")
        self.assertIn("示例候选人", str(message["From"]))
        self.assertEqual(str(message["Subject"]), PAYLOAD["subject"])
        self.assertEqual(str(message["Message-ID"]), PAYLOAD["message_id"])
        self.assertEqual(str(message["X-Career-Agent-ID"]), "mail_saved_id")
        self.assertEqual(message["Date"].datetime.isoformat(), PAYLOAD["created_at"])
        attached = list(message.iter_attachments())
        self.assertEqual(len(attached), 1)
        self.assertEqual(attached[0].get_filename(), PAYLOAD["resume_name"])
        self.assertEqual(attached[0].get_payload(decode=True), PDF)
        self.assertNotIn(SECRET.encode(), message.as_bytes())

    def test_transport_error_before_submission_is_failed_and_after_submission_is_unknown(self):
        for error in (TimeoutError("raw " + SECRET), smtplib.SMTPServerDisconnected("raw " + SECRET)):
            with self.subTest(error_type=type(error).__name__):
                self.connection.reset_mock()
                self.connection.login.side_effect = error
                with self.assertRaises(MailSendError) as raised:
                    self.client().send_message(PAYLOAD, PDF, "mail_saved_id")
                self.assertEqual(raised.exception.outcome, "failed")
                self.assertNotIn(SECRET, str(raised.exception))
                self.connection.send_message.assert_not_called()
                self.connection.login.side_effect = None
                self.connection.send_message.side_effect = error
                with self.assertRaises(MailSendError) as raised:
                    self.client().send_message(PAYLOAD, PDF, "mail_saved_id")
                self.assertEqual(raised.exception.outcome, "unknown")
                self.assertNotIn(SECRET, str(raised.exception))
                self.connection.send_message.assert_called_once()
                self.connection.send_message.side_effect = None

    def test_explicit_smtp_rejections_are_failed_and_not_retried(self):
        errors = (
            smtplib.SMTPAuthenticationError(535, SECRET.encode()),
            smtplib.SMTPRecipientsRefused({"hr@example.com": (550, SECRET.encode())}),
            smtplib.SMTPDataError(554, SECRET.encode()),
        )
        for error in errors:
            with self.subTest(error_type=type(error).__name__):
                self.connection.reset_mock()
                self.connection.send_message.side_effect = error
                with self.assertRaises(MailSendError) as raised:
                    self.client().send_message(PAYLOAD, PDF, "mail_saved_id")
                self.assertEqual(raised.exception.outcome, "failed")
                self.assertNotIn(SECRET, str(raised.exception))
                self.connection.send_message.assert_called_once()

    def test_single_recipient_refusal_dictionary_is_failed(self):
        self.connection.send_message.return_value = {"hr@example.com": (550, b"refused")}
        with self.assertRaises(MailSendError) as raised:
            self.client().send_message(PAYLOAD, PDF, "mail_saved_id")
        self.assertEqual(raised.exception.outcome, "failed")

    def test_quit_failure_after_acceptance_preserves_accepted_result(self):
        self.connection.quit.side_effect = smtplib.SMTPServerDisconnected("raw " + SECRET)
        receipt = self.client().send_message(PAYLOAD, PDF, "mail_saved_id")
        self.assertEqual(receipt["status"], "accepted")
        self.connection.close.assert_called_once()

    def test_invalid_mailboxes_headers_and_attachment_cannot_open_a_connection(self):
        for changes in (
            {"sender_email": "other@gmail.com"},
            {"recipient": "first@example.com,second@example.com"},
            {"recipient": "hr@example.com\r\nBcc:x@example.com"},
            {"subject": "hello\r\nBcc:x@example.com"},
            {"message_id": "<id@x>\r\nBcc:x@example.com"},
            {"resume_name": "../resume.pdf"},
        ):
            with self.subTest(changes=changes), self.assertRaises(MailSendError):
                self.client().send_message({**PAYLOAD, **changes}, PDF, "mail_saved_id")
        with self.assertRaises(MailSendError):
            self.client().send_message(PAYLOAD, b"not a PDF", "mail_saved_id")
        self.smtp.assert_not_called()

    def test_missing_saved_message_id_gets_correlated_fallback(self):
        payload = {key: value for key, value in PAYLOAD.items() if key not in {"message_id", "created_at"}}
        result = self.client().send_message(payload, PDF, "mail_fallback")
        self.assertEqual(result["provider_reference"], "<mail_fallback@career-agent.local>")

    def test_configuration_errors_do_not_leak_password(self):
        for password in (None, "", "login-password", "abcd\nefghijklmnop", "x" * 17):
            with self.subTest(password_length=len(password) if isinstance(password, str) else None):
                with self.assertRaises(MailSendError) as raised:
                    GmailSmtpClient("candidate@gmail.com", password)
                if isinstance(password, str) and password:
                    self.assertNotIn(password, str(raised.exception))
        for timeout in (True, 0, 121, float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(MailSendError):
                GmailSmtpClient("candidate@gmail.com", SECRET, timeout=timeout)

    def test_failed_login_check_does_not_send_or_expose_service_reply(self):
        self.connection.login.side_effect = smtplib.SMTPAuthenticationError(535, SECRET.encode())
        with self.assertRaises(MailSendError) as raised:
            self.client().get_sender()
        self.assertEqual(raised.exception.outcome, "failed")
        self.assertNotIn(SECRET, str(raised.exception))
        self.connection.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
