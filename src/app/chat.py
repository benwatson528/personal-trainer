"""OpenAI-compatible chat completions endpoint (spec section 14).

Open WebUI talks to the PT through POST /v1/chat/completions. The backend:
receives the conversation, determines intent via the LLM tool loop, loads
relevant state/files, validates structured responses, performs requested
actions, persists state, and returns the conversational response.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

logger = logging.getLogger("personal_trainer.chat")


def _brief(value: Any, limit: int = 220) -> str:
    """Compact one-line rendering of tool args/results for the log.

    Tool payloads include whole CSVs and full history blobs, so truncate —
    otherwise pt.log becomes unreadable.
    """
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
    except Exception:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + f"... (+{len(text) - limit})"

from personal_trainer.config import Settings
from personal_trainer.equipment import load_equipment_from_storage
from personal_trainer.integrations.llm import LLMClient
from personal_trainer.pt.coaching import build_system_prompt
from personal_trainer.pt.memory import MemoryManager
from personal_trainer.pt.tools import ToolExecutor
from personal_trainer.programmes.manager import ProgrammeManager
from personal_trainer.state.manager import StateManager
from personal_trainer.storage.base import StorageBackend
from personal_trainer.strong.generation import StrongGenerationService

MAX_TOOL_ITERATIONS = 6  # hard bound; a normal turn needs 2-4 tool rounds


class PersonalTrainer:
    def __init__(
        self,
        settings: Settings,
        storage: StorageBackend,
        llm: LLMClient,
        programme_manager: ProgrammeManager,
    ):
        self._settings = settings
        self._storage = storage
        self._llm = llm
        self._programmes = programme_manager
        self._state = StateManager(storage, settings.folders.state)
        self._strong = StrongGenerationService(
            llm, storage, self._state, programme_manager, settings
        )
        self._tools = ToolExecutor(
            storage, self._state, programme_manager, self._strong, settings, llm,
        )
        self._memory = MemoryManager(
            self._state, self._programmes, self._storage, self._settings.folders.programmes_config
        )

    # ------------------------------------------------------------------ public

    def handle_completion(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Run the tool-calling loop and return an OpenAI-style response."""
        logger.info("handle_completion: %d messages", len(messages))
        system = self._system_prompt()
        conversation = [{"role": "system", "content": system}, *messages]

        for iteration in range(MAX_TOOL_ITERATIONS):
            logger.info("LLM call iteration %d", iteration + 1)
            response = self._llm.complete(
                messages=conversation,
                tools=self._tools.definitions,
                tool_choice="auto",
            )
            logger.info("LLM response: %d tool_calls", len(response.tool_calls))
            if not response.tool_calls:
                return _chat_response(response.content)

            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in response.tool_calls
                ],
            }
            conversation.append(assistant_message)

            for tool_call in response.tool_calls:
                logger.info("tool_call: %s(%s)", tool_call.name, _brief(tool_call.arguments))
                result = self._tools.execute(tool_call.name, tool_call.arguments)
                logger.info("tool_result: %s -> %s", tool_call.name, _brief(result))
                conversation.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_call.name,
                    "content": json.dumps(result, default=str),
                })

        # Iteration bound hit: close the loop gracefully.
        return _chat_response(
            "I've hit my processing limit for this message. Could you ask again?"
        )

    def stream_completion(self, messages: list[dict[str, Any]]):
        """SSE stream variant of handle_completion (OpenAI-compatible)."""
        system = self._system_prompt()
        conversation = [{"role": "system", "content": system}, *messages]
        response_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
        created = int(time.time())

        def _chunk(content: str | None, finish_reason: str | None = None) -> dict:
            return {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": "personal-trainer",
                "choices": [{
                    "index": 0,
                    "delta": {"content": content} if content else {},
                    "finish_reason": finish_reason,
                }],
            }

        # Yield an initial empty chunk immediately so Open WebUI's stream
        # parser has data to work with while tools execute.
        yield _sse_event(_chunk(None), response_id, created)

        for _ in range(MAX_TOOL_ITERATIONS):
            response = self._llm.complete(
                messages=conversation,
                tools=self._tools.definitions,
                tool_choice="auto",
            )
            if not response.tool_calls:
                yield _sse_event(_chunk(response.content, "stop"), response_id, created)
                yield _sse_event("[DONE]", response_id, created)
                return

            conversation.append({
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in response.tool_calls
                ],
            })
            for tool_call in response.tool_calls:
                logger.info("tool_call: %s(%s)", tool_call.name, _brief(tool_call.arguments))
                result = self._tools.execute(tool_call.name, tool_call.arguments)
                logger.info("tool_result: %s -> %s", tool_call.name, _brief(result))
                conversation.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_call.name,
                    "content": json.dumps(result, default=str),
                })

        yield _sse_event(_chunk(
            "I've hit my processing limit for this message. Could you ask again?", "stop",
        ), response_id, created)
        yield _sse_event("[DONE]", response_id, created)

    # ----------------------------------------------------------------- private

    def _system_prompt(self) -> str:
        state = self._state.load()
        active = self._programmes.resolve_active(state)
        programmes = []
        all_memory = []
        for p in active:
            cached = state.programmes.get(p.id)
            entry = f"{p.name}"
            if cached and cached.latest_confirmed_workout:
                ref = cached.latest_confirmed_workout
                pos = ref.programme_position
                if pos and pos.week is not None:
                    entry += (
                        f" — last confirmed: {ref.label} "
                        f"(Week {pos.week}, {pos.session})"
                    )
                else:
                    entry += f" — last confirmed: {ref.label or ref.id}"
            programmes.append(entry)
            # Collect memory from each programme
            if p.memory.preferences:
                all_memory.extend(p.memory.preferences)
        return build_system_prompt(
            programmes=programmes,
            state_yaml=json.dumps(state.model_dump(exclude_none=True), default=str),
            memory=all_memory,
            equipment=load_equipment_from_storage(
                self._storage, self._settings.folders.equipment_config
            ).describe_brief(),
        )


def _chat_response(content: str, response_id: str | None = None,
                   created: int | None = None) -> dict[str, Any]:
    return {
        "id": response_id or f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": created or int(time.time()),
        "model": "personal-trainer",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _sse_event(data: dict | str, response_id: str, created: int) -> str:
    if isinstance(data, str):
        payload = data
    else:
        payload = json.dumps({**data, "id": response_id, "created": created})
    return f"data: {payload}\n\n"
