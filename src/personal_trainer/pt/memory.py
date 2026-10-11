"""Durable coaching memory (spec section 33).

A small amount of durable training memory lives in the programme config YAML.
Only information that is useful for future training decisions is preserved —
not every conversation.
"""

from __future__ import annotations

from ..programmes.manager import ProgrammeManager
from ..state.manager import StateManager
from ..storage.base import StorageBackend


class MemoryManager:
    def __init__(self, state_manager: StateManager, programme_manager: ProgrammeManager, storage: StorageBackend, programmes_config_path: str):
        self._state = state_manager
        self._programmes = programme_manager
        self._storage = storage
        self._path = programmes_config_path

    def all(self) -> list[str]:
        state = self._state.load()
        all_prefs = []
        for p_id, prog_state in state.programmes.items():
            programme = self._programmes.get(p_id)
            if programme and programme.memory.preferences:
                all_prefs.extend(programme.memory.preferences)
        return all_prefs

    def add(self, programme_id: str, preference: str) -> list[str]:
        self._programmes.add_memory_preference(self._storage, self._path, programme_id, preference)
        return self.all()

    def extract_preferences(self, conversation: list[dict]) -> list[str]:
        """Filter a conversation for statements worth remembering.

        Keeps explicit, durable preferences; drops trivia and small talk.
        """
        durable = []
        for message in conversation:
            if message.get("role") != "user":
                continue
            text = (message.get("content") or "").strip()
            lowered = text.lower()
            if len(text) < 12 or len(text) > 300:
                continue
            if any(k in lowered for k in ("i prefer", "i'd rather", "i would rather",
                                          "always ", "never ", "instead of", "from now on")):
                if text not in durable:
                    durable.append(text)
        return durable
