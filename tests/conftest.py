"""Shared test fixtures.

Tests run against copies of the real dev data folder (real Strong/Concept2
exports and the J&T reference) so they exercise actual formats, not invented
ones — while never mutating the dev data itself.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from personal_trainer.config import Settings
from personal_trainer.integrations.llm import LLMResponse, StubLLMClient
from personal_trainer.programmes.manager import ProgrammeManager
from personal_trainer.state.manager import StateManager
from personal_trainer.storage.local import LocalStorageBackend
from personal_trainer.strong.generation import StrongGenerationService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The single session timestamp all fixtures build rows from; see the
# fixed_session_timestamp fixture below.
SESSION_TIMESTAMP = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        storage={"backend": "local", "root": "./data"},
        llm={"model": "stub"},
    )


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Copy of the dev data folder; tests mutate the copy, not the original.

    Any pending prescription is cleared so tests get a deterministic starting
    point. Without this, a real workout generated during development leaves a
    pending_workout in the live YAML, which leaks into the tests and sends
    reconciliation down the "expecting a pending workout" branch.

    latest_confirmed_workout is deliberately left alone: it is committed
    baseline state that tests assert against.

    The Generated/Strong folder is emptied for the same reason: real workouts
    generated during development would otherwise be copied in, and tests that
    assert "nothing was written" would fail on the developer's own output.

    Sessions is emptied too, for the same reason and more sharply: a stray
    summary would make the generation prompt prefer condensed history over raw
    rows, quietly changing what every generation test feeds the model.
    """
    target = tmp_path / "data"
    shutil.copytree(DATA_DIR, target)
    _clear_pending_workouts(target)
    _empty_generated(target)
    _empty_sessions(target)
    _seed_concept2(target)
    return target


def _seed_concept2(data_root: Path) -> None:
    """Supply a small synthetic Concept2 export when the real one is absent.

    The athlete's own Concept2 history is personal data and is not committed, so
    a fresh clone has no concept2 CSV. Tests that exercise rowing reconciliation
    need one; a synthetic sample keeps the suite self-contained without
    publishing the athlete's metrics. A developer who has their own file keeps
    using it.
    """
    target = data_root / "history" / "concept2" / "concept2_history.csv"
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / "concept2_history_sample.csv", target)


def _empty_sessions(data_root: Path) -> None:
    sessions = data_root / "sessions"
    if sessions.exists():
        for stale in sessions.glob("*.yaml"):
            stale.unlink()


def _empty_generated(data_root: Path) -> None:
    generated = data_root / "generated" / "strong"
    if generated.exists():
        for stale in generated.glob("*.csv"):
            stale.unlink()


def _clear_pending_workouts(data_root: Path) -> None:
    state_path = data_root / "state" / "training_state.yaml"
    if not state_path.exists():
        return
    state = yaml.safe_load(state_path.read_text()) or {}
    for programme in (state.get("programmes") or {}).values():
        programme.pop("pending_workout", None)
    state_path.write_text(yaml.safe_dump(state, sort_keys=False, allow_unicode=True))


@pytest.fixture
def storage(data_dir: Path) -> LocalStorageBackend:
    return LocalStorageBackend(data_dir)


@pytest.fixture
def programme_manager() -> ProgrammeManager:
    return ProgrammeManager(PROJECT_ROOT / "config" / "programmes.yaml")


@pytest.fixture
def state_manager(storage: LocalStorageBackend, settings: Settings) -> StateManager:
    return StateManager(storage, settings.folders.state)


@pytest.fixture
def strong_service(storage, state_manager, programme_manager, settings, stub_llm):
    return StrongGenerationService(
        llm=stub_llm,
        storage=storage,
        state_manager=state_manager,
        programme_manager=programme_manager,
        settings=settings,
    )


@pytest.fixture
def executor(storage, state_manager, programme_manager, strong_service, settings, stub_llm):
    from personal_trainer.pt.tools import ToolExecutor

    return ToolExecutor(
        storage, state_manager, programme_manager, strong_service,
        settings, stub_llm,
    )


@pytest.fixture
def stub_llm() -> StubLLMClient:
    return StubLLMClient()


@pytest.fixture(autouse=True)
def fixed_session_timestamp(monkeypatch):
    """Pin the Strong session timestamp for every test.

    `generate()` stamps rows with the wall clock at call time and validates the
    exact string. Without pinning, fixture CSVs built at import time would
    disagree with the validator by however long the test took to run, so every
    generation test would flake. Autouse and global because any module that
    triggers generation needs the same pin.
    """
    from personal_trainer.strong import generation

    monkeypatch.setattr(generation, "_session_timestamp", lambda: SESSION_TIMESTAMP)


def make_llm_response(content: str = "", tool_calls: list | None = None) -> LLMResponse:
    from personal_trainer.integrations.llm import ToolCall

    calls = tool_calls or []
    return LLMResponse(content=content, tool_calls=calls)
