"""LLM client abstraction (spec section 36).

OpenRouter is wrapped behind a small client so the model/provider is
replaceable without changing the PT architecture. Credentials come from
the environment; nothing is hard-coded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ..config import LLMConfig

logger = logging.getLogger("personal_trainer.llm")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class LLMClient(Protocol):
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        ...


class OpenRouterClient:
    """OpenAI-compatible chat completions via OpenRouter."""

    def __init__(self, config: LLMConfig, http_client: httpx.Client | None = None):
        self._config = config
        self._http = http_client or httpx.Client(timeout=180.0)
        self._spent = 0.0

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        api_key = self._config.api_key
        if not api_key:
            raise RuntimeError(
                f"LLM API key not found: set {self._config.api_key_env} in the environment."
            )
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
        }
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
        if response_format:
            payload["response_format"] = response_format

        response = self._http.post(
            f"{self._config.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost:8000",
                "X-Title": "Personal Trainer",
            },
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        result = _parse_response(data)
        if usage := data.get("usage"):
            prompt_tok = usage.get("prompt_tokens", 0)
            completion_tok = usage.get("completion_tokens", 0)
            # Config prices are USD per million tokens.
            cost = (
                prompt_tok * self._config.price_in
                + completion_tok * self._config.price_out
            ) / 1_000_000
            self._spent += cost
            # Accumulate before logging, so the running total shown includes the
            # call being reported. Logging first made every line under-report by
            # that call's own cost, which understated a full workout turn.
            logger.info(
                "llm %s: %d in / %d out tokens, ~$%.4f (session total ~$%.4f)",
                self._config.model, prompt_tok, completion_tok, cost, self._spent,
            )
        return result


class StubLLMClient:
    """Test double: returns queued responses, records calls."""

    def __init__(self, responses: list[LLMResponse] | None = None):
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def complete(self, messages, tools=None, tool_choice=None, response_format=None) -> LLMResponse:
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "response_format": response_format,
        })
        if not self.responses:
            raise RuntimeError("StubLLMClient has no responses left")
        return self.responses.pop(0)


def _parse_response(data: dict[str, Any]) -> LLMResponse:
    choice = data["choices"][0]
    message = choice.get("message", {})
    content = message.get("content") or ""
    tool_calls = []
    for tc in message.get("tool_calls", []) or []:
        func = tc.get("function", {})
        try:
            import json

            arguments = json.loads(func.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        tool_calls.append(ToolCall(id=tc.get("id", ""), name=func.get("name", ""), arguments=arguments))
    return LLMResponse(content=content, tool_calls=tool_calls, raw=data)
