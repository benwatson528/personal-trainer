"""LLM context selection (spec section 35).

Do not blindly send every file into every LLM call. The always-included
context is small (state, active programmes, memory); programme references and
history are pulled on demand through tools, scoped to the modality under
discussion.
"""

from __future__ import annotations

import csv
import io
import json

from ..models import TrainingState
from ..programmes.manager import ProgrammeManager
from ..storage.base import StorageBackend


def build_always_context(
    state: TrainingState,
    programme_manager: ProgrammeManager,
    storage: StorageBackend,
) -> dict:
    """Small, always-included context for the system prompt."""
    active = programme_manager.resolve_active(state)
    programmes = []
    for p in active:
        cached = state.programmes.get(p.id)
        entry = f"{p.name} ({p.modality})"
        if cached and cached.latest_confirmed_workout:
            ref = cached.latest_confirmed_workout
            entry += f" — last confirmed: {ref.label or ref.id} ({ref.date or 'date unknown'})"
        programmes.append(entry)
    return {
        "active_programmes": programmes,
        "state": json.dumps(state.model_dump(exclude_none=True), default=str),
    }


def select_history_rows(history_csv: str, max_rows: int = 40) -> str:
    """Most recent history rows — enough to establish format and recent patterns."""
    rows = list(csv.reader(io.StringIO(history_csv)))
    if not rows:
        return ""
    header, data = rows[0], rows[1:]
    recent = data[-max_rows:]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(recent)
    return out.getvalue()
