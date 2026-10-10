"""Drive backend credential selection.

OAuth as the user is the route for personal Google accounts; a service
account is the Workspace shared-drive route. These tests cover the selection
logic only — no network calls (credential files are parsed locally).
"""

from __future__ import annotations

import json

import pytest

from personal_trainer.config import StorageConfig
from personal_trainer.storage.drive import _build_credentials, SCOPES


def _write_token(tmp_path) -> str:
    token = {
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "refresh_token": "test-refresh-token",
        "type": "authorized_user",
    }
    path = tmp_path / "token.json"
    path.write_text(json.dumps(token), encoding="utf-8")
    return str(path)


def test_no_credentials_raises():
    with pytest.raises(ValueError, match="Google Drive credentials required"):
        _build_credentials(StorageConfig())


def test_oauth_token_file(tmp_path, monkeypatch):
    monkeypatch.delenv("PT_GOOGLE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("PT_GOOGLE_CREDENTIALS", raising=False)
    creds = _build_credentials(StorageConfig(oauth_token_file=_write_token(tmp_path)))

    from google.oauth2.credentials import Credentials

    assert isinstance(creds, Credentials)
    assert creds.has_scopes(SCOPES)


def test_oauth_token_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv("PT_GOOGLE_CREDENTIALS", raising=False)
    monkeypatch.setenv("PT_GOOGLE_OAUTH_TOKEN", _write_token(tmp_path))
    creds = _build_credentials(StorageConfig())

    from google.oauth2.credentials import Credentials

    assert isinstance(creds, Credentials)


def test_oauth_wins_over_service_account_config(tmp_path, monkeypatch):
    """OAuth is the current setup; if both are configured it takes precedence."""
    monkeypatch.setenv("PT_GOOGLE_OAUTH_TOKEN", _write_token(tmp_path))
    creds = _build_credentials(StorageConfig(credentials_file="never-read.json"))

    from google.oauth2.credentials import Credentials

    assert isinstance(creds, Credentials)