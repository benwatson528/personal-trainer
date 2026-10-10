"""Programme registry (spec sections 11, 12).

Programme definitions are configuration, not history: adding a programme
means adding a reference document in Drive plus an entry in
config/programmes.yaml — no core code changes. Whether a programme is
currently active is cached state (training_state.yaml), resolved by
`resolve_active` below.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..models import Programme, TrainingState


class ProgrammeManager:
    def __init__(self, config_path: str | Path):
        data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        self._programmes: dict[str, Programme] = {
            p["id"]: Programme(**p) for p in data.get("programmes", [])
        }

    def get(self, programme_id: str) -> Programme | None:
        return self._programmes.get(programme_id)

    def all(self) -> list[Programme]:
        return list(self._programmes.values())

    def by_modality(self, modality: str) -> list[Programme]:
        return [p for p in self.all() if p.modality == modality]

    def resolve_active(self, state: TrainingState) -> list[Programme]:
        """Active programmes = registered AND not marked inactive in state."""
        active = []
        for programme in self.all():
            cached = state.programmes.get(programme.id)
            if cached is not None and not cached.active:
                continue
            active.append(programme)
        return active
