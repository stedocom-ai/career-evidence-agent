import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from career_agent.outlook import MailSendError
from career_agent.outlook_desktop import DesktopOutlookClient


class DesktopOutlookTests(unittest.TestCase):
    def setUp(self):
        self.client = DesktopOutlookClient('sender@example.com')
        self.payload = {
            'sender_email': 'sender@example.com', 'sender_name': '示例候选人',
            'recipient': 'hr@example.com', 'company': '示例公司', 'position': 'AI Agent 实习',
            'subject': '申请 AI Agent 实习', 'body': '您好，附件是我的简历。\n谢谢。',
            'resume_name': '简历.pdf',
        }
        patcher = mock.patch('career_agent.outlook_desktop.subprocess.run')
        self.run = patcher.start()
        self.addCleanup(patcher.stop)

    def response(self, result, code=0):
        self.run.return_value = subprocess.CompletedProcess([], code, json.dumps(result), '')

    def test_check_uses_fixed_helper_and_separate_safe_arguments(self):
        self.response({'status': 'ready', 'mail': 'SENDER@example.com', 'display_name': 'Sender'})
        result = self.client.get_sender()
        self.assertEqual(result['mail'], 'SENDER@example.com')
        args = self.run.call_args.args[0]
        self.assertTrue(args[0].lower().endswith('windowspowershell\\v1.0\\powershell.exe'))
        self.assertEqual(args[args.index('-Operation') + 1], 'check')
        self.assertEqual(args[args.index('-AccountEmail') + 1], 'sender@example.com')
        self.assertTrue(args[args.index('-File') + 1].endswith('outlook_desktop.ps1'))
        self.assertFalse(self.run.call_args.kwargs['shell'])
        self.assertEqual(self.run.call_args.kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)

    def test_send_snapshots_utf8_payload_and_attachment_then_removes_temporary_files(self):
        observed = {}

        def completed(args, **kwargs):
            payload_path = Path(args[args.index('-PayloadPath') + 1])
            attachment_path = Path(args[args.index('-AttachmentPath') + 1])
            observed['payload_path'] = payload_path
            observed['attachment_path'] = attachment_path
            observed['payload'] = json.loads(payload_path.read_text(encoding='utf-8'))
            observed['attachment'] = attachment_path.read_bytes()
            return subprocess.CompletedProcess(args, 0, json.dumps({'status': 'accepted', 'provider_reference': 'classic-outlook-queued'}), '')

        self.payload['subject'] = '申请 $(Write-Output ignored) `literal`'
        self.run.side_effect = completed
        result = self.client.send_message(self.payload, b'%PDF-snapshot', '<mail-id@example.local>')
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(observed['payload']['subject'], self.payload['subject'])
        self.assertEqual(observed['payload']['correlation_id'], '<mail-id@example.local>')
        self.assertEqual(observed['attachment'], b'%PDF-snapshot')
        self.assertEqual(observed['attachment_path'].name, '简历.pdf')
        self.assertFalse(observed['payload_path'].exists())
        self.assertFalse(observed['attachment_path'].exists())

    def test_wrong_sender_and_extra_recipient_reject_before_helper(self):
        invalid = [
            {**self.payload, 'sender_email': 'other@example.com'},
            {**self.payload, 'recipient': 'hr@example.com;other@example.com'},
            {**self.payload, 'recipient': 'hr@example.com\r\nBcc: other@example.com'},
            {**self.payload, 'resume_name': '..\\resume.pdf'},
            {**self.payload, 'subject': 'subject\r\nextra'},
        ]
        for payload in invalid:
            with self.assertRaises(MailSendError) as error:
                self.client.send_message(payload, b'resume', 'id')
            self.assertEqual(error.exception.outcome, 'failed')
        self.run.assert_not_called()

    def test_timeout_is_uncertain_for_send_and_failed_for_check(self):
        self.run.side_effect = subprocess.TimeoutExpired(['helper'], 60)
        with self.assertRaises(MailSendError) as error:
            self.client.send_message(self.payload, b'resume', 'id')
        self.assertEqual(error.exception.outcome, 'unknown')
        with self.assertRaises(MailSendError) as error:
            self.client.get_sender()
        self.assertEqual(error.exception.outcome, 'failed')

    def test_script_error_outcome_and_unexpected_process_failure(self):
        for outcome in ('failed', 'unknown'):
            self.response({'status': 'error', 'outcome': outcome, 'error': 'Outlook reported a problem'}, code=1)
            with self.assertRaises(MailSendError) as error:
                self.client.send_message(self.payload, b'resume', 'id')
            self.assertEqual(error.exception.outcome, outcome)
        self.response({'status': 'accepted', 'provider_reference': 'classic-outlook-queued'}, code=1)
        with self.assertRaises(MailSendError) as error:
            self.client.send_message(self.payload, b'resume', 'id')
        self.assertEqual(error.exception.outcome, 'unknown')

    def test_invalid_helper_output_never_marks_send_accepted(self):
        for output in ('not-json', '[]', '{}', '{"status":"accepted"}'):
            self.run.return_value = subprocess.CompletedProcess([], 0, output, '')
            with self.assertRaises(MailSendError) as error:
                self.client.send_message(self.payload, b'resume', 'id')
            self.assertEqual(error.exception.outcome, 'unknown')

    def test_check_rejects_wrong_account_and_unsupported_system(self):
        self.response({'status': 'ready', 'mail': 'other@example.com'})
        with self.assertRaises(MailSendError) as error:
            self.client.get_sender()
        self.assertEqual(error.exception.outcome, 'failed')
        self.run.reset_mock()
        with mock.patch('career_agent.outlook_desktop.sys.platform', 'linux'):
            with self.assertRaises(MailSendError) as error:
                self.client.get_sender()
        self.assertEqual(error.exception.outcome, 'failed')
        self.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
