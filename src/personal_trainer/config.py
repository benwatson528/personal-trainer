"""Application settings loaded from config/config.yaml plus environment secrets."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Load .env from the project root (or cwd) so secrets don't need to be
# exported into the shell every time.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")
load_dotenv()  # also pick up a .env in the current working directory


class StorageConfig(BaseModel):
    backend: str = "local"  # "local" | "drive"
    root: str = "./data"
    # Drive root folder id (the "Personal Trainer" folder shared with the app).
    root_folder_id: str | None = None
    # Service-account key file. Only usable on Workspace shared drives, because
    # service accounts have no storage quota of their own and cannot own files.
    credentials_file: str | None = None
    # User OAuth token file, written once by scripts/gdrive_auth.py. This is the
    # route for a personal Google account: the app acts as you, so files are
    # yours and count against your own quota.
    oauth_token_file: str | None = None


class LLMConfig(BaseModel):
    provider: str = "openrouter"
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "anthropic/claude-haiku-4.5"
    api_key_env: str = "OPENROUTER_API_KEY"
    temperature: float = 0.4
    max_tokens: int = 4096
    # USD per million tokens. Used only for cost logging in pt.log, never for
    # routing — there is one model for the whole agent.
    price_in: float = 0.0
    price_out: float = 0.0

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env)


class StrongGenerationConfig(BaseModel):
    max_validation_retries: int = 3
    # Condensed session records shown to the LLM as training history.
    history_sample_sessions: int = 20


class SessionSummaryConfig(BaseModel):
    # Export rows sent to the LLM when condensing one session. A session is
    # ~20 rows, so this covers several recent ones at a fraction of the cost of
    # the whole file (400 rows is ~11k tokens; 120 is ~3k).
    summary_rows: int = 120


class FolderConfig(BaseModel):
    programmes: str = "programmes"
    history_strong: str = "history/strong"
    history_concept2: str = "history/concept2"
    sessions: str = "sessions"
    state: str = "state"
    generated_strong: str = "generated/strong"


class Settings(BaseModel):
    storage: StorageConfig = Field(default_factory=StorageConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    strong_generation: StrongGenerationConfig = Field(default_factory=StrongGenerationConfig)
    session_summary: SessionSummaryConfig = Field(default_factory=SessionSummaryConfig)
    folders: FolderConfig = Field(default_factory=FolderConfig)


def load_settings(path: str | Path | None = None) -> Settings:
    if path is None:
        path = os.environ.get("PT_CONFIG", "config/config.yaml")
    p = Path(path)
    if not p.exists():
        return Settings()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return Settings(**data)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
