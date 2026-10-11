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
    def __init__(self, programmes: dict[str, Programme], source_path: str | None = None, storage: StorageBackend | None = None):
        self._programmes = programmes
        self._source_path = source_path
        self._storage = storage

    @classmethod
    def from_text(cls, text: str) -> "ProgrammeManager":
        data = yaml.safe_load(text) or {}
        programmes = {p["id"]: Programme(**p) for p in data.get("programmes", [])}
        return cls(programmes)

    @classmethod
    def from_file(cls, path: str | Path) -> "ProgrammeManager":
        """Load from a local file (tests, local development, PT_PROGRAMMES_CONFIG)."""
        obj = cls.from_text(Path(path).read_text(encoding="utf-8"))
        obj._source_path = "config/programmes.yaml"
        from ..storage.local import LocalStorageBackend
        obj._storage = LocalStorageBackend(Path(path).parent.parent)
        return obj

    @classmethod
    def from_storage(cls, storage: StorageBackend, path: str) -> "ProgrammeManager":
        """Load the registry through the storage backend (Drive or local)."""
        if not storage.exists(path):
            raise FileNotFoundError(
                f"Programme registry not found at {path!r} in the storage root. "
                "Upload config/programmes.yaml (see scripts/gdrive_upload_config.py) "
                "or set PT_PROGRAMMES_CONFIG to a local file."
            )
        obj = cls.from_text(storage.read(path))
        obj._source_path = path
        obj._storage = storage
        return obj

    def get(self, programme_id: str) -> Programme | None:
        return self._programmes.get(programme_id)

    def reload(self) -> None:
        """Reload programmes from the source file."""
        if self._source_path and self._storage:
            self._programmes = {}
            text = self._storage.read(self._source_path)
            data = yaml.safe_load(text) or {}
            for p in data.get("programmes", []):
                self._programmes[p["id"]] = Programme(**p)
                self._programmes[p["id"]] = Programme(**p)

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

    def update_last_workout_hash(self, storage: StorageBackend, path: str, programme_id: str, hash_val: str) -> None:
        """Update the last_workout_hash for a programme in the storage YAML."""
        import yaml
        text = storage.read(path)
        data = yaml.safe_load(text) or {}
        for prog in data.get("programmes", []):
            if prog.get("id") == programme_id:
                prog["last_workout_hash"] = hash_val
                break
        storage.write(path, yaml.safe_dump(data, sort_keys=False))

    def add_memory_preference(self, storage: StorageBackend, path: str, programme_id: str, preference: str) -> None:
        """Add a durable memory preference for a programme."""
        import yaml
        text = storage.read(path)
        data = yaml.safe_load(text) or {}
        programmes = data.get("programmes", [])
        if isinstance(programmes, list):
            # programmes.yaml format: list of dicts with 'id' field
            for prog in programmes:
                if prog.get("id") == programme_id:
                    memory = prog.setdefault("memory", {"preferences": []})
                    prefs = memory.setdefault("preferences", [])
                    if preference not in prefs:
                        prefs.append(preference)
                    break
        elif isinstance(programmes, dict):
            # training_state.yaml format: dict keyed by programme_id
            prog = programmes.get(programme_id)
            if prog:
                memory = prog.setdefault("memory", {"preferences": []})
                prefs = memory.setdefault("preferences", [])
                if preference not in prefs:
                    prefs.append(preference)
        storage.write(path, yaml.safe_dump(data, sort_keys=False))
