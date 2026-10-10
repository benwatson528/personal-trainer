"""Shared test fixtures.

Tests run against a copy of the committed fixture tree (``tests/fixtures/data``)
so they exercise realistic formats while never touching the repo-root ``data/``
mirror or the live data in Google Drive.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pytest

from personal_trainer.config import Settings
from personal_trainer.integrations.llm import LLMResponse, StubLLMClient
from personal_trainer.programmes.manager import ProgrammeManager
from personal_trainer.state.manager import StateManager
from personal_trainer.storage.local import LocalStorageBackend
from personal_trainer.strong.generation import StrongGenerationService

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# Self-contained example tree the suite copies per test. Deliberately under
# tests/ so no test depends on the repo-root data/ mirror, which is only a
# local scratch copy of the live Google Drive contents.
DATA_DIR = FIXTURES / "data"

# The single session timestamp all fixtures build rows from; see the
# fixed_session_timestamp fixture below.
SESSION_TIMESTAMP = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
SESSION_DATE = SESSION_TIMESTAMP[:10]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        storage={"backend": "local", "root": "./data"},
        llm={"model": "stub"},
    )


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Copy of the committed fixture tree; tests mutate the copy, not the source.

    The tree carries the deterministic baseline state, the J&T reference and day
    templates, and a synthetic Concept2 export — so the suite is self-contained
    and never reads the repo-root data/ mirror or Google Drive.

    It contains no pending prescription and no generated/session output, so
    tests start from a fixed point. Empty folders are not committed (git cannot
    track them); storage creates them on write and reports no files when absent.
    """
    target = tmp_path / "data"
    shutil.copytree(DATA_DIR, target)
    return target


@pytest.fixture
def storage(data_dir: Path) -> LocalStorageBackend:
    return LocalStorageBackend(data_dir)


@pytest.fixture
def programme_manager() -> ProgrammeManager:
    return ProgrammeManager.from_file(DATA_DIR / "config" / "programmes.yaml")


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
