"""Transport and configuration tests: fake opener only, no network or real keys."""

from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
import urllib.request
from unittest import mock

from career_agent.client import ChatModel, ModelError, _NoRedirect


FAKE_KEY = "unit-test-secret-not-a-real-key"
BASE_URL = "https://provider.example/v1"
MODEL_NAME = "unit-test-tool-model"


def response_body(message=None, *, usage=None, finish_reason="stop"):
    data = {
        "choices": [{
            "message": message or {"role": "assistant", "content": "完成"},
            "finish_reason": finish_reason,
        }],
    }
    if usage is not None:
        data["usage"] = usage
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


class ChatModelTests(unittest.TestCase):
    def setUp(self):
        # Patch construction itself so an unexpected code path still cannot use
        # a real urllib opener or read a developer's existing environment.
        self.opener = mock.MagicMock()
        patcher = mock.patch(
            "career_agent.client.urllib.request.build_opener",
            return_value=self.opener,
        )
        self.build_opener = patcher.start()
        self.addCleanup(patcher.stop)
        env_patcher = mock.patch.dict(os.environ, {}, clear=True)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def model(self, **kwargs):
        config = {"api_key": FAKE_KEY, "base_url": BASE_URL, "model": MODEL_NAME}
        config.update(kwargs)
        return ChatModel(**config)

    def set_response(self, body):
        response = mock.MagicMock()
        response.read.return_value = body
        self.opener.open.return_value.__enter__.return_value = response
        return response

    def test_request_contains_json_tools_and_bearer_header(self):
        self.set_response(response_body())
        model = self.model(
            timeout=17,
            max_tokens=2048,
            extra_body={"thinking": {"type": "disabled"}},
        )
        messages = [{"role": "user", "content": "检查 Python 经历"}]
        tools = [{"type": "function", "function": {"name": "search_evidence"}}]
        result = model.complete(messages, tools)

        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, BASE_URL + "/chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + FAKE_KEY)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload, {
            "model": MODEL_NAME,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "stream": False,
            "max_tokens": 2048,
            "thinking": {"type": "disabled"},
        })
        self.assertNotIn(FAKE_KEY, request.data.decode("utf-8"))
        self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 17})
        self.assertEqual(result, {"role": "assistant", "content": "完成"})

    def test_usage_accumulates_across_all_model_rounds(self):
        model = self.model()
        rounds = [
            {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
            {"prompt_tokens": 240, "completion_tokens": 40, "total_tokens": 280},
        ]
        for usage in rounds:
            self.set_response(response_body(usage=usage))
            model.complete([], [])
        self.assertEqual(model.usage, {
            "prompt_tokens": 360,
            "completion_tokens": 70,
            "total_tokens": 430,
        })

    def test_invalid_usage_counters_do_not_pollute_totals(self):
        model = self.model()
        self.set_response(response_body(usage={
            "prompt_tokens": True, "completion_tokens": -1, "total_tokens": "100",
        }))
        model.complete([], [])
        self.assertEqual(model.usage, {})
        self.set_response(response_body(usage={"prompt_tokens": 0}))
        model.complete([], [])
        self.assertEqual(model.usage, {"prompt_tokens": 0})

    def test_reasoning_content_can_be_returned_to_provider_in_next_round(self):
        model = self.model()
        message = {
            "role": "assistant",
            "content": None,
            "reasoning_content": "provider-required-context",
            "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "search_evidence", "arguments": '{"query":"Python"}'},
            }],
            "provider_private_field": "must-not-be-forwarded",
        }
        self.set_response(response_body(message))
        first = model.complete([], [])
        self.assertEqual(first["reasoning_content"], message["reasoning_content"])
        self.assertNotIn("provider_private_field", first)

        self.set_response(response_body())
        model.complete([first], [])
        request = self.opener.open.call_args.args[0]
        sent = json.loads(request.data)["messages"][0]
        self.assertEqual(sent["reasoning_content"], "provider-required-context")
        self.assertEqual(sent["tool_calls"], message["tool_calls"])

    def test_http_errors_never_expose_body_key_or_request_details(self):
        model = self.model()
        for code, expected_hint in [(401, "API Key"), (429, "请求受限")]:
            with self.subTest(code=code):
                body = mock.MagicMock(wraps=io.BytesIO((FAKE_KEY + " private-body").encode()))
                error = urllib.error.HTTPError(
                    BASE_URL, code, FAKE_KEY + " private-error", {}, body,
                )
                self.opener.open.side_effect = error
                with self.assertRaises(ModelError) as caught:
                    model.complete([], [])
                text = str(caught.exception)
                self.assertIn(str(code), text)
                self.assertIn(expected_hint, text)
                self.assertNotIn(FAKE_KEY, text)
                self.assertNotIn("private-body", text)
                self.assertNotIn("private-error", text)
                body.read.assert_not_called()

    def test_network_error_details_are_hidden(self):
        self.opener.open.side_effect = urllib.error.URLError(FAKE_KEY)
        with self.assertRaises(ModelError) as caught:
            self.model().complete([], [])
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        self.assertIn("网络", str(caught.exception))

    def test_invalid_json_and_utf8_are_rejected(self):
        model = self.model()
        for body in (b"not json", b'{"choices":', b"\xff\xfe\x80"):
            with self.subTest(body=body):
                self.set_response(body)
                with self.assertRaisesRegex(ModelError, "JSON"):
                    model.complete([], [])

    def test_missing_or_invalid_response_structure_is_rejected(self):
        model = self.model()
        invalid = [
            None, [], {}, {"choices": []}, {"choices": None},
            {"choices": [{}]}, {"choices": [{"message": None}]},
            {"choices": [{"message": []}]},
            {"choices": [{"message": {"content": "missing role"}}]},
            {"choices": [{"message": {"role": "user", "content": "wrong role"}}]},
        ]
        for data in invalid:
            with self.subTest(data=data):
                self.set_response(json.dumps(data).encode())
                with self.assertRaisesRegex(ModelError, "choices"):
                    model.complete([], [])

    def test_truncated_output_is_not_accepted_as_report_input(self):
        model = self.model()
        self.set_response(response_body(finish_reason="length"))
        with self.assertRaisesRegex(ModelError, "截断"):
            model.complete([], [])

    def test_response_size_is_bounded_before_json_decode(self):
        model = self.model()
        response = self.set_response(b"x" * 1_000_001)
        with self.assertRaisesRegex(ModelError, "1 MB"):
            model.complete([], [])
        response.read.assert_called_once_with(1_000_001)

    def test_endpoint_normalizes_trailing_slash(self):
        self.assertEqual(
            self.model(base_url=BASE_URL + "/").endpoint,
            BASE_URL + "/chat/completions",
        )

    def test_https_and_local_http_urls_are_allowed(self):
        for base_url in (
            "https://provider.example/v1", "http://localhost:8000/v1",
            "http://127.0.0.1:8000/v1", "http://[::1]:8000/v1",
        ):
            with self.subTest(base_url=base_url):
                self.model(base_url=base_url)

    def test_unsafe_or_incomplete_urls_are_rejected(self):
        for base_url in (
            "", "provider.example/v1", "http://provider.example/v1",
            "ftp://provider.example/v1", "https:///v1", "https://:443",
            "https://user:password@provider.example/v1",
            "https://provider.example/v1?key=private",
            "https://provider.example/v1#fragment",
            "https://example.invalid:bad/v1",
            "https://exa mple.invalid/v1",
            "https://[::1/v1",
        ):
            with self.subTest(base_url=base_url):
                with self.assertRaises(ModelError):
                    self.model(base_url=base_url)

    def test_redirect_handler_does_not_forward_credentials(self):
        request = urllib.request.Request(
            BASE_URL, headers={"Authorization": "Bearer " + FAKE_KEY},
        )
        handler = _NoRedirect()
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.assertIsNone(handler.redirect_request(
                    request, None, status, "redirect", {}, "https://other.example/",
                ))

    def test_blank_key_or_model_and_invalid_request_limits_are_rejected(self):
        for config in (
            {"api_key": " "}, {"model": "\t"}, {"timeout": 0},
            {"timeout": 121}, {"max_tokens": 255}, {"max_tokens": 16385},
            {"max_tokens": True}, {"max_tokens": 1024.0},
        ):
            with self.subTest(config=config):
                with self.assertRaises(ModelError):
                    self.model(**config)

    def test_extra_body_cannot_override_request_control_fields(self):
        for key in (
            "model", "messages", "tools", "tool_choice", "stream", "max_tokens",
            "api_key", "unexpected",
        ):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ModelError, "EXTRA_BODY"):
                    self.model(extra_body={key: "override"})

    def test_extra_body_non_object_values_are_rejected(self):
        for value in ([], "", 0, False, ["thinking"]):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ModelError, "EXTRA_BODY"):
                    self.model(extra_body=value)

    def test_from_env_requires_all_explicit_connection_settings(self):
        with self.assertRaises(ModelError) as caught:
            ChatModel.from_env()
        for name in ("AGENT_API_KEY", "AGENT_BASE_URL", "AGENT_MODEL"):
            self.assertIn(name, str(caught.exception))
        with mock.patch.dict(os.environ, {"AGENT_API_KEY": FAKE_KEY}, clear=True):
            with self.assertRaises(ModelError) as caught:
                ChatModel.from_env()
            self.assertNotIn(FAKE_KEY, str(caught.exception))
            self.assertNotIn("AGENT_API_KEY", str(caught.exception))

    def test_from_env_loads_optional_parameters_and_trims_key_model(self):
        values = {
            "AGENT_API_KEY": " " + FAKE_KEY + " ",
            "AGENT_BASE_URL": BASE_URL,
            "AGENT_MODEL": " " + MODEL_NAME + " ",
            "AGENT_EXTRA_BODY": '{"thinking":{"type":"disabled"}}',
            "AGENT_TIMEOUT": "15.5",
            "AGENT_MAX_TOKENS": "2048",
        }
        with mock.patch.dict(os.environ, values, clear=True):
            model = ChatModel.from_env()
        self.assertEqual(model.name, MODEL_NAME)
        self.assertEqual(model.timeout, 15.5)
        self.assertEqual(model.max_tokens, 2048)
        self.assertEqual(model.extra_body, {"thinking": {"type": "disabled"}})
        self.set_response(response_body())
        model.complete([], [])
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer " + FAKE_KEY)

    def test_from_env_rejects_malformed_json_and_numeric_configuration(self):
        required = {
            "AGENT_API_KEY": FAKE_KEY,
            "AGENT_BASE_URL": BASE_URL,
            "AGENT_MODEL": MODEL_NAME,
        }
        for override in (
            {"AGENT_EXTRA_BODY": "{invalid"},
            {"AGENT_MAX_TOKENS": "not-an-int"},
            {"AGENT_TIMEOUT": "not-a-number"},
        ):
            with self.subTest(override=override):
                with mock.patch.dict(os.environ, {**required, **override}, clear=True):
                    with self.assertRaises(ModelError) as caught:
                        ChatModel.from_env()
                    self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_from_env_extra_body_requires_json_object(self):
        required = {
            "AGENT_API_KEY": FAKE_KEY,
            "AGENT_BASE_URL": BASE_URL,
            "AGENT_MODEL": MODEL_NAME,
        }
        for raw in ("[]", "null", "false", "0", '""', '["thinking"]'):
            with self.subTest(raw=raw):
                with mock.patch.dict(os.environ, {
                    **required, "AGENT_EXTRA_BODY": raw,
                }, clear=True):
                    with self.assertRaisesRegex(ModelError, "EXTRA_BODY"):
                        ChatModel.from_env()


if __name__ == "__main__":
    unittest.main()
