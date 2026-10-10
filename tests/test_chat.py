"""End-to-end chat loop with a stub LLM (spec section 14)."""

import json

import pytest
from fastapi.testclient import TestClient

from personal_trainer.config import Settings
from personal_trainer.integrations.llm import LLMResponse, StubLLMClient, ToolCall
from personal_trainer.programmes.manager import ProgrammeManager
from personal_trainer.storage.local import LocalStorageBackend

from app.api import create_app
from app.chat import PersonalTrainer

from conftest import make_llm_response


def build_pt(data_dir, llm, settings=None):
    settings = settings or Settings(storage={"backend": "local", "root": str(data_dir)})
    storage = LocalStorageBackend(data_dir)
    pm = ProgrammeManager("config/programmes.yaml")
    return PersonalTrainer(settings, storage, llm, pm)


def test_health_endpoint():
    app = create_app(Settings(storage={"backend": "local", "root": "./data"}))
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_chat_returns_openai_compatible_response(data_dir, stub_llm):
    stub_llm.responses = [make_llm_response("Hey! Ready when you are.")]
    pt = build_pt(data_dir, stub_llm)
    response = pt.handle_completion([{"role": "user", "content": "Hello"}])
    assert response["object"] == "chat.completion"
    assert response["choices"][0]["message"]["content"] == "Hey! Ready when you are."
    assert response["choices"][0]["finish_reason"] == "stop"


def test_chat_executes_tool_calls(data_dir, stub_llm):
    stub_llm.responses = [
        make_llm_response(tool_calls=[ToolCall(
            id="call_1", name="read_state", arguments={},
        )]),
        make_llm_response("Your state looks good."),
    ]
    pt = build_pt(data_dir, stub_llm)
    response = pt.handle_completion([{"role": "user", "content": "How's my training?"}])
    content = response["choices"][0]["message"]["content"]
    assert content == "Your state looks good."
    # Two LLM calls: one with the tool call, one with the tool result.
    assert len(stub_llm.calls) == 2
    second_messages = stub_llm.calls[1]["messages"]
    assert any(m["role"] == "tool" for m in second_messages)


def test_chat_tool_result_fed_back_to_llm(data_dir, stub_llm):
    (data_dir / "sessions").mkdir(exist_ok=True)
    (data_dir / "sessions" / "2026-06-23_legs.yaml").write_text(
        "date: 2026-06-23\nworkout: Legs\nSquat (Barbell): ['100x5']\n"
    )
    stub_llm.responses = [
        make_llm_response(tool_calls=[ToolCall(
            id="call_1", name="read_history",
            arguments={"programme_id": "jnt_2", "max_rows": 5},
        )]),
        make_llm_response("You have been lifting consistently."),
    ]
    pt = build_pt(data_dir, stub_llm)
    pt.handle_completion([{"role": "user", "content": "What have I done lately?"}])
    tool_messages = [m for m in stub_llm.calls[1]["messages"] if m["role"] == "tool"]
    assert len(tool_messages) == 1
    payload = json.loads(tool_messages[0]["content"])
    assert payload["ok"]
    assert "Squat (Barbell): ['100x5']" in payload["history"]


def test_chat_bounded_loop(data_dir, stub_llm):
    """Infinite tool-calling must be bounded (spec section 37)."""
    stub_llm.responses = [
        make_llm_response(tool_calls=[ToolCall(id="c", name="read_state", arguments={})])
        for _ in range(50)
    ]
    pt = build_pt(data_dir, stub_llm)
    response = pt.handle_completion([{"role": "user", "content": "loop"}])
    assert "processing limit" in response["choices"][0]["message"]["content"]
    assert len(stub_llm.calls) <= 12


def test_streaming_endpoint(data_dir, stub_llm):
    stub_llm.responses = [make_llm_response("streamed reply")]
    pt = build_pt(data_dir, stub_llm)
    events = list(pt.stream_completion([{"role": "user", "content": "hi"}]))
    assert any("streamed reply" in e for e in events)
    assert events[-1].strip() == "data: [DONE]"


def test_post_workout_flow(data_dir, stub_llm):
    """'Finished. It was fine.' triggers reconciliation via tools."""
    stub_llm.responses = [
        make_llm_response(tool_calls=[ToolCall(
            id="call_1", name="reconcile_workout", arguments={"programme_id": "jnt_2"},
        )]),
        make_llm_response("J&T 2.0 - 4 confirmed. Nice work."),
    ]
    pt = build_pt(data_dir, stub_llm)
    response = pt.handle_completion([
        {"role": "user", "content": "Finished. It was fine."}
    ])
    assert "confirmed" in response["choices"][0]["message"]["content"]
