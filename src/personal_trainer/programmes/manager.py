"""Programme registry (spec sections 11, 12).

Programme definitions are configuration, not history: adding a programme
means adding a reference document in Drive plus an entry in the Drive-hosted
config/programmes.yaml — no core code changes. The file is read through the
storage backend, so it lives beside the programme material it describes.
Whether a programme is currently active is cached state (training_state.yaml),
resolved by `resolve_active` below.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ..models import Programme, TrainingState

if TYPE_CHECKING:
    from ..storage.base import StorageBackend


class ProgrammeManager:
    def __init__(self, programmes: dict[str, Programme]):
        self._programmes = programmes

    @classmethod
    def from_text(cls, text: str) -> "ProgrammeManager":
        data = yaml.safe_load(text) or {}
        programmes = {p["id"]: Programme(**p) for p in data.get("programmes", [])}
        return cls(programmes)

    @classmethod
    def from_file(cls, path: str | Path) -> "ProgrammeManager":
        """Load from a local file (tests, local development, PT_PROGRAMMES_CONFIG)."""
        return cls.from_text(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def from_storage(cls, storage: StorageBackend, path: str) -> "ProgrammeManager":
        """Load the registry through the storage backend (Drive or local)."""
        if not storage.exists(path):
            raise FileNotFoundError(
                f"Programme registry not found at {path!r} in the storage root. "
                "Upload config/programmes.yaml (see scripts/gdrive_upload_config.py) "
                "or set PT_PROGRAMMES_CONFIG to a local file."
            )
        return cls.from_text(storage.read(path))

    def get(self, programme_id: str) -> Programme | None:
        return self._programmes.get(programme_id)

    def all(self) -> list[Programme]:
        return list(self._programmes.values())

    def resolve_active(self, state: TrainingState) -> list[Programme]:
        """Active programmes = registered AND not marked inactive in state."""
        active = []
        for programme in self.all():
            cached = state.programmes.get(programme.id)
            if cached is not None and not cached.active:
                continue
            active.append(programme)
        return active
