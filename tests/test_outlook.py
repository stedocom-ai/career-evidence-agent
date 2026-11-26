"""Outlook tests use fake MSAL modules and a fake HTTP opener only."""

import base64
import http.client
import io
import json
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from career_agent.outlook import GraphClient, MailSendError, OutlookAuth, SCOPES


FAKE_TOKEN = "not-a-real-outlook-access-token"
CLIENT_ID = "11111111-2222-4333-8444-555555555555"
PDF = b"%PDF-1.4\nexample attachment"
PAYLOAD = {
    "sender_email": "student@outlook.com", "sender_name": "示例学生",
    "recipient": "hr@example.com", "company": "示例公司", "position": "Agent 实习",
    "subject": "应聘 Agent 实习", "body": "您好，附件为我的简历。\n谢谢。",
    "resume_name": "示例简历.pdf", "private_note": "do not upload this field",
}


class GraphClientTests(unittest.TestCase):
    def setUp(self):
        self.opener = mock.MagicMock()
        patcher = mock.patch("career_agent.outlook.urllib.request.build_opener", return_value=self.opener)
        patcher.start()
        self.addCleanup(patcher.stop)

    def response(self, status, data=b"", headers=None):
        response = mock.MagicMock()
        response.status = status
        response.headers = headers or {}
        response.read.return_value = data
        self.opener.open.return_value.__enter__.return_value = response
        return response

    def test_one_recipient_pdf_and_only_explicit_message_fields_are_submitted(self):
        response = self.response(202, headers={"request-id": "provider-request-123"})
        result = GraphClient(FAKE_TOKEN).send_message(PAYLOAD, PDF, "mail_unit_test")
        self.assertEqual(result, {"status": "accepted", "provider_reference": "provider-request-123"})
        self.opener.open.assert_called_once()
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://graph.microsoft.com/v1.0/me/sendMail")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + FAKE_TOKEN)
        data = json.loads(request.data)
        self.assertTrue(data["saveToSentItems"])
        self.assertEqual(data["message"]["toRecipients"], [{"emailAddress": {"address": "hr@example.com"}}])
        self.assertEqual(base64.b64decode(data["message"]["attachments"][0]["contentBytes"]), PDF)
        self.assertEqual(data["message"]["internetMessageHeaders"][0]["value"], "mail_unit_test")
        self.assertNotIn("do not upload", request.data.decode())
        self.assertNotIn(FAKE_TOKEN, request.data.decode())
        response.read.assert_not_called()

    def test_http_refusals_are_failed_and_uncertain_results_are_unknown_without_retry(self):
        for status in (400, 401, 403, 413, 429, 408, 500, 503):
            with self.subTest(status=status):
                self.opener.open.reset_mock()
                secret_body = io.BytesIO((FAKE_TOKEN + " response secret").encode())
                self.opener.open.side_effect = urllib.error.HTTPError(
                    "https://graph.microsoft.com/v1.0/me/sendMail", status, "error", {}, secret_body,
                )
                with self.assertRaises(MailSendError) as raised:
                    GraphClient(FAKE_TOKEN).send_message(PAYLOAD, PDF, "mail_unit_test")
                expected = "unknown" if status == 408 or status >= 500 else "failed"
                self.assertEqual(raised.exception.outcome, expected)
                self.assertNotIn(FAKE_TOKEN, str(raised.exception))
                self.assertEqual(secret_body.tell(), 0)
                self.opener.open.assert_called_once()

    def test_timeout_is_unknown_and_is_not_retried(self):
        for error in (TimeoutError("details " + FAKE_TOKEN), http.client.IncompleteRead(b"partial")):
            with self.subTest(error_type=type(error).__name__):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = error
                with self.assertRaises(MailSendError) as raised:
                    GraphClient(FAKE_TOKEN).send_message(PAYLOAD, PDF, "mail_unit_test")
                self.assertEqual(raised.exception.outcome, "unknown")
                self.assertNotIn(FAKE_TOKEN, str(raised.exception))
                self.opener.open.assert_called_once()

    def test_unexpected_success_status_requires_manual_reconciliation(self):
        self.response(200, b"{}")
        with self.assertRaises(MailSendError) as raised:
            GraphClient(FAKE_TOKEN).send_message(PAYLOAD, PDF, "mail_unit_test")
        self.assertEqual(raised.exception.outcome, "unknown")

    def test_get_sender_requests_only_account_identity_fields(self):
        self.response(200, json.dumps({"mail": "student@outlook.com", "displayName": "Student"}).encode())
        result = GraphClient(FAKE_TOKEN).get_sender()
        self.assertEqual(result, {"mail": "student@outlook.com", "userPrincipalName": None, "displayName": "Student"})
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIn("/me?$select=mail,userPrincipalName,displayName", request.full_url)

    def test_get_sender_rejects_invalid_or_oversized_responses(self):
        for raw in (b"not json", b"[]", b'{"mail":12}', b"{}", b"x" * (1024 * 1024 + 1)):
            with self.subTest(raw_length=len(raw)):
                self.response(200, raw)
                with self.assertRaises(MailSendError):
                    GraphClient(FAKE_TOKEN).get_sender()

    def test_invalid_content_cannot_issue_a_request(self):
        cases = [
            ({**PAYLOAD, "recipient": "a@example.com,b@example.com"}, PDF, "mail_x"),
            ({**PAYLOAD, "recipient": "hr@example.com\r\nBcc:x@example.com"}, PDF, "mail_x"),
            ({**PAYLOAD, "subject": "Hi\nBcc"}, PDF, "mail_x"),
            ({**PAYLOAD, "resume_name": "../resume.pdf"}, PDF, "mail_x"),
            (PAYLOAD, b"not a PDF", "mail_x"),
            (PAYLOAD, b"%PDF-" + b"x" * (2 * 1024 * 1024), "mail_x"),
            (PAYLOAD, PDF, "mail_x\r\nheader"),
        ]
        for payload, attachment, identifier in cases:
            with self.subTest(identifier=identifier, recipient=payload.get("recipient")):
                with self.assertRaises(MailSendError):
                    GraphClient(FAKE_TOKEN).send_message(payload, attachment, identifier)
        self.opener.open.assert_not_called()

    def test_invalid_tokens_and_timeouts_are_rejected(self):
        for token in ("", "token\nheader", "token\U0001f600", None):
            with self.subTest(token=token), self.assertRaises(MailSendError):
                GraphClient(token)
        for timeout in (True, 0, 121, float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(MailSendError):
                GraphClient(FAKE_TOKEN, timeout=timeout)


class OutlookAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache_path = Path(self.temp.name) / "oauth" / "cache.bin"
        self.app = mock.MagicMock()
        self.app.get_accounts.return_value = []
        self.msal = types.SimpleNamespace(PublicClientApplication=mock.Mock(return_value=self.app))
        self.extensions = types.SimpleNamespace(
            FilePersistenceWithDataProtection=mock.Mock(return_value="encrypted-persistence"),
            PersistedTokenCache=mock.Mock(return_value="protected-token-cache"),
        )
        modules = {"msal": self.msal, "msal_extensions": self.extensions}
        patcher = mock.patch("career_agent.outlook.import_module", side_effect=modules.__getitem__)
        patcher.start()
        self.addCleanup(patcher.stop)
        windows_patcher = mock.patch("career_agent.outlook._WINDOWS", True)
        windows_patcher.start()
        self.addCleanup(windows_patcher.stop)

    def auth(self, **kwargs):
        return OutlookAuth(CLIENT_ID, cache_path=self.cache_path, username="student@outlook.com", **kwargs)

    def test_existing_account_uses_silent_token_and_windows_encrypted_cache(self):
        account = {"username": "student@outlook.com"}
        self.app.get_accounts.return_value = [account]
        self.app.acquire_token_silent.return_value = {"access_token": FAKE_TOKEN}
        self.assertEqual(self.auth().acquire(), FAKE_TOKEN)
        self.app.get_accounts.assert_called_once_with(username="student@outlook.com")
        self.app.acquire_token_silent.assert_called_once_with(SCOPES, account=account)
        self.extensions.FilePersistenceWithDataProtection.assert_called_once_with(str(self.cache_path.resolve()))
        self.msal.PublicClientApplication.assert_called_once_with(
            CLIENT_ID, authority="https://login.microsoftonline.com/consumers", token_cache="protected-token-cache",
        )
        self.app.initiate_device_flow.assert_not_called()

    def test_missing_or_expired_session_requires_login_without_implicit_auth(self):
        self.app.get_accounts.return_value = [{"username": "student@outlook.com"}]
        self.app.acquire_token_silent.return_value = {"error": "interaction_required", "error_description": FAKE_TOKEN}
        with self.assertRaises(MailSendError) as raised:
            self.auth().acquire()
        self.assertIn("mail-login", str(raised.exception))
        self.assertNotIn(FAKE_TOKEN, str(raised.exception))
        self.app.initiate_device_flow.assert_not_called()

    def test_interactive_login_displays_device_code_without_access_token(self):
        self.app.initiate_device_flow.return_value = {
            "verification_uri": "https://microsoft.com/devicelogin",
            "user_code": "CODE123", "device_code": "private-device-secret",
        }
        self.app.acquire_token_by_device_flow.return_value = {"access_token": FAKE_TOKEN}
        output = []
        token = self.auth().acquire(interactive=True, notify=output.append)
        self.assertEqual(token, FAKE_TOKEN)
        self.assertIn("CODE123", " ".join(output))
        self.assertNotIn(FAKE_TOKEN, " ".join(output))
        self.assertNotIn("private-device-secret", " ".join(output))
        self.app.initiate_device_flow.assert_called_once_with(scopes=["Mail.Send", "User.Read"])

    def test_authentication_errors_do_not_echo_server_secrets(self):
        self.app.initiate_device_flow.return_value = {"error": "invalid_client", "error_description": FAKE_TOKEN}
        with self.assertRaises(MailSendError) as raised:
            self.auth().acquire(interactive=True)
        self.assertNotIn(FAKE_TOKEN, str(raised.exception))
        self.app.get_accounts.side_effect = RuntimeError("sensitive " + FAKE_TOKEN)
        with self.assertRaises(MailSendError) as raised:
            self.auth().acquire()
        self.assertNotIn(FAKE_TOKEN, str(raised.exception))

    def test_missing_dependencies_and_non_windows_have_actionable_errors(self):
        with mock.patch("career_agent.outlook.import_module", side_effect=ImportError):
            with self.assertRaises(MailSendError) as raised:
                self.auth().acquire()
            self.assertIn(".[outlook]", str(raised.exception))
        with mock.patch("career_agent.outlook._WINDOWS", False):
            with self.assertRaises(MailSendError) as raised:
                self.auth().acquire()
            self.assertIn("Windows", str(raised.exception))

    def test_configuration_cannot_change_authority_to_an_arbitrary_host(self):
        for client in ("", "not-an-id", None):
            with self.subTest(client=client), self.assertRaises(MailSendError):
                OutlookAuth(client)
        for tenant in ("https://malicious.example", "consumers/other", "example.com", {}):
            with self.subTest(tenant=tenant), self.assertRaises(MailSendError):
                self.auth(tenant=tenant)


if __name__ == "__main__":
    unittest.main()
