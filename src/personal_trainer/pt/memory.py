"""Durable coaching memory (spec section 33).

A small amount of durable training memory lives in the state YAML. Only
information that is useful for future training decisions is preserved —
not every conversation.
"""

from __future__ import annotations

from ..models import TrainingState


class MemoryManager:
    def __init__(self, state_manager):
        self._state = state_manager

    def all(self) -> list[str]:
        return self._state.load().memory.preferences

    def add(self, preference: str) -> list[str]:
        self._state.add_memory_preference(preference)
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
