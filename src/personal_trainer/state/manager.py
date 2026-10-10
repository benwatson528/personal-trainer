"""Training state YAML manager (spec sections 9, 10).

The YAML is a cache: the underlying history files win if there is a
discrepancy. All mutations go through this manager so the file is never
edited by hand behind the PT's back.
"""

from __future__ import annotations

from datetime import datetime, timezone

import yaml

from ..models import TrainingState, WorkoutRef
from ..storage.base import StorageBackend

STATE_FILENAME = "training_state.yaml"


class StateManager:
    def __init__(self, storage: StorageBackend, state_folder: str):
        self._storage = storage
        self._path = f"{state_folder}/{STATE_FILENAME}"

    @property
    def path(self) -> str:
        return self._path

    def load(self) -> TrainingState:
        if not self._storage.exists(self._path):
            return TrainingState()
        raw = yaml.safe_load(self._storage.read(self._path)) or {}
        return TrainingState(**raw)

    def save(self, state: TrainingState) -> None:
        self._storage.write(self._path, yaml.safe_dump(
            state.model_dump(exclude_none=True), sort_keys=False, allow_unicode=True
        ))

    def get_programme_state(self, programme_id: str):
        state = self.load()
        return state.programmes.get(programme_id)

    def update_latest_confirmed(self, programme_id: str, ref: WorkoutRef) -> TrainingState:
        state = self.load()
        if programme_id not in state.programmes:
            raise KeyError(f"Unknown programme: {programme_id}")
        state.programmes[programme_id].latest_confirmed_workout = ref
        self.save(state)
        return state

    def set_pending(self, programme_id: str, ref: WorkoutRef) -> TrainingState:
        """Record the prescribed workout that is awaiting completion."""
        state = self.load()
        if programme_id not in state.programmes:
            raise KeyError(f"Unknown programme: {programme_id}")
        state.programmes[programme_id].pending_workout = ref
        self.save(state)
        return state

    def clear_pending(self, programme_id: str) -> TrainingState:
        state = self.load()
        if programme_id not in state.programmes:
            raise KeyError(f"Unknown programme: {programme_id}")
        state.programmes[programme_id].pending_workout = None
        self.save(state)
        return state

    def add_memory_preference(self, preference: str) -> TrainingState:
        state = self.load()
        if preference not in state.memory.preferences:
            state.memory.preferences.append(preference)
            self.save(state)
        return state

    def touch(self) -> None:
        """Ensure the state file exists on first run."""
        if not self._storage.exists(self._path):
            self.save(TrainingState())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
