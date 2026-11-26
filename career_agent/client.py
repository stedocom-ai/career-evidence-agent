"""Minimal Chat Completions transport. No agent framework or SDK required."""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ModelError(RuntimeError):
    """A configuration, transport or protocol error safe to show in the CLI."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a Bearer credential to a redirect target.
        return None


class ChatModel:
    mode = "live"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        timeout: float = 30,
        max_tokens: int = 4096,
        extra_body: dict[str, Any] | None = None,
    ):
        if not isinstance(base_url, str) or not base_url.isascii() or any(char.isspace() for char in base_url):
            raise ModelError("服务地址必须是无空白字符的 ASCII URL。")
        try:
            parsed = urllib.parse.urlsplit(base_url)
            port = parsed.port
            if port is not None and port == 0:
                raise ValueError
        except ValueError:
            raise ModelError("服务地址或端口格式无效。") from None
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if not parsed.hostname or any(char.isspace() for char in parsed.hostname) or not (
            parsed.scheme == "https" or (parsed.scheme == "http" and local)
        ):
            raise ModelError("AGENT_BASE_URL 必须使用 HTTPS；本机测试可用 HTTP。")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ModelError("服务地址不能含用户名、密码、查询参数或锚点。")
        if not api_key.strip() or not model.strip():
            raise ModelError("API Key 和模型名称不能为空。")
        if not 1 <= timeout <= 120 or type(max_tokens) is not int or not 256 <= max_tokens <= 16384:
            raise ModelError("请求超时必须在 1–120 秒，max_tokens 在 256–16384。")
        extra_body = {} if extra_body is None else extra_body
        if not isinstance(extra_body, dict) or set(extra_body) - {"thinking", "reasoning_effort"}:
            raise ModelError("AGENT_EXTRA_BODY 只允许 thinking 和 reasoning_effort 字段。")
        self._api_key = api_key.strip()
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.name = model.strip()
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.extra_body = extra_body
        self.usage: dict[str, int] = {}
        self._opener = urllib.request.build_opener(_NoRedirect())

    @classmethod
    def from_env(cls) -> "ChatModel":
        required = ("AGENT_API_KEY", "AGENT_BASE_URL", "AGENT_MODEL")
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        if missing:
            raise ModelError("请先设置环境变量：" + ", ".join(missing) + "。可先运行 demo。")
        try:
            extra = json.loads(os.environ.get("AGENT_EXTRA_BODY", "{}"))
            max_tokens = int(os.environ.get("AGENT_MAX_TOKENS", "4096"))
            timeout = float(os.environ.get("AGENT_TIMEOUT", "30"))
        except (ValueError, TypeError) as exc:
            raise ModelError("API 配置格式有误：请检查 EXTRA_BODY JSON 和数字配置。") from exc
        if not isinstance(extra, dict):
            raise ModelError("AGENT_EXTRA_BODY 必须是 JSON 对象。")
        return cls(
            os.environ["AGENT_API_KEY"], os.environ["AGENT_BASE_URL"],
            os.environ["AGENT_MODEL"], timeout=timeout,
            max_tokens=max_tokens, extra_body=extra,
        )

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        payload = {
            "model": self.name, "messages": messages, "tools": tools,
            "tool_choice": "auto", "stream": False,
            "max_tokens": self.max_tokens, **self.extra_body,
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self._api_key},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ModelError("模型响应超过 1 MB；已停止，请减少输出长度。")
            data = json.loads(raw)
        except urllib.error.HTTPError as exc:
            hints = {401: "检查 API Key", 402: "检查账户余额", 403: "检查账户与地区权限",
                     404: "检查服务地址和模型名称", 429: "请求受限，请稍后重试"}
            # Do not echo the response body or request headers: they can contain secrets.
            raise ModelError(f"模型服务返回 HTTP {exc.code}；{hints.get(exc.code, '请查服务商错误文档')}。") from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise ModelError("模型请求失败或超时；检查网络和服务地址。") from None
        except (ValueError, UnicodeError):
            raise ModelError("模型服务没有返回有效 JSON。") from None
        try:
            message = data["choices"][0]["message"]
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError
            if data["choices"][0].get("finish_reason") == "length":
                raise ModelError("模型输出被长度上限截断；请检查 AGENT_MAX_TOKENS。")
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                count = data.get("usage", {}).get(key)
                if type(count) is int and count >= 0:
                    self.usage[key] = self.usage.get(key, 0) + count
            # Some compatible providers require reasoning_content to be passed back.
            # It stays in memory, but is not written into the execution trace.
            return {key: message[key] for key in ("role", "content", "tool_calls", "reasoning_content") if key in message}
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ModelError("模型响应缺少有效的 choices[0].message。") from None
