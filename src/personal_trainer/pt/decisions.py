"""Session decisions and user overrides (spec sections 16, 17, 28, 29).

Python assembles the decision context and validates the outcome; the LLM
does the reasoning. Guardrails: the PT never advances programme position on
unsupported evidence, and every decision must reference an active programme.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ..models import Programme, ReconciliationResult, TrainingState
from ..programmes.manager import ProgrammeManager
from ..storage.base import StorageBackend


def _newest_csv_in_folder(storage: StorageBackend, folder: str) -> str | None:
    """Return the content of the newest CSV file in a folder, or None."""
    try:
        files = storage.list_files(folder)
    except (FileNotFoundError, NotADirectoryError):
        return None
    csv_files = [f for f in files if f.endswith(".csv")]
    if not csv_files:
        return None
    csv_files.sort(key=lambda p: PurePosixPath(p).name, reverse=True)
    return storage.read(csv_files[0])


def _hash_newest_workout(csv_text: str) -> str | None:
    """
    Compute SHA256 hash of the newest workout's exercise data.

    Strong exports cumulative history with the newest workout at the bottom.
    This extracts the last workout block (by Date+Workout) and hashes
    the Exercise/Set/Weight/Reps columns so duplicate uploads are detected
    even if the export file name changes.
    """
    if not csv_text.strip():
        return None
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return None

    # Find the last workout: last row's Date+Workout combo, then collect all
    # rows with that same Date+Workout (a workout can span multiple rows).
    last = rows[-1]
    last_date = last.get("Date", "").strip()
    last_workout = last.get("Workout", "").strip()
    if not last_date or not last_workout:
        return None

    workout_rows = [
        r for r in rows
        if r.get("Date", "").strip() == last_date and r.get("Workout", "").strip() == last_workout
    ]
    if not workout_rows:
        return None

    # Hash the exercise data only (Exercise, Set Order, Weight, Reps)
    # This is what actually matters for detecting "same workout".
    parts = []
    for r in workout_rows:
        ex = r.get("Exercise", "").strip()
        if not ex:
            continue
        set_order = r.get("Set Order", "").strip()
        weight = r.get("Weight", "").strip()
        reps = r.get("Reps", "").strip()
        parts.append(f"{ex}|{set_order}|{weight}|{reps}")

    if not parts:
        return None
    payload = "\n".join(parts)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]  # truncate for readability


@dataclass
class DecisionContext:
    """Everything the LLM needs to recommend today's session."""

    state: TrainingState
    active_programmes: list[Programme]
    recent_history: dict[str, str] = field(default_factory=dict)  # programme_id -> csv sample
    user_request: str = ""


class DecisionService:
    def __init__(self, programme_manager: ProgrammeManager, storage: StorageBackend):
        self._programmes = programme_manager
        self._storage = storage

    def build_context(self, state: TrainingState, user_request: str = "") -> DecisionContext:
        active = self._programmes.resolve_active(state)
        recent_history: dict[str, str] = {}
        for programme in active:
            if programme.completed_workouts_folder:
                csv_content = _newest_csv_in_folder(self._storage, programme.completed_workouts_folder)
                if csv_content:
                    recent_history[programme.id] = _recent_rows(csv_content)
        return DecisionContext(
            state=state,
            active_programmes=active,
            recent_history=recent_history,
            user_request=user_request,
        )


def reconcile_completion(
    programme: Programme,
    state: TrainingState,
    history_csv: str | None,
) -> ReconciliationResult:
    """Post-workout reconciliation (spec sections 28, 29).

    The history file is the source of truth; the YAML is repaired to match.
    Never advance programme position on unsupported evidence.

    Expected workout = the pending prescription when one exists (the PT
    generated/prescribed it and is waiting for it); otherwise any workout
    newer than the latest confirmed one counts as newly completed.
    """
    cached = state.programmes.get(programme.id)
    pending = cached.pending_workout if cached else None
    latest = cached.latest_confirmed_workout if cached else None

    if not history_csv:
        return ReconciliationResult(
            programme_id=programme.id,
            status="no_history",
            expected_label=_next_label(pending) or _next_label(latest),
            message=f"No history file for {programme.name}. Ask the user to export "
                    "the latest history to Drive, then tell me it's finished.",
        )

    rows = [r for r in csv.reader(io.StringIO(history_csv)) if any(c.strip() for c in r)]
    if len(rows) <= 1:
        return ReconciliationResult(
            programme_id=programme.id,
            status="no_history",
            expected_label=_next_label(pending) or _next_label(latest),
            message=f"The {programme.name} history file contains no workouts yet.",
        )

    header = [h.strip() for h in rows[0]]
    data = rows[1:]
    blocks = _workout_blocks(header, data)
    if not blocks:
        return ReconciliationResult(
            programme_id=programme.id,
            status="no_history",
            expected_label=_next_label(pending) or _next_label(latest),
            message=f"Could not identify any workout in the {programme.name} history.",
        )

    # Newest block overall (for mismatch reporting).
    newest = max(blocks, key=lambda b: b[1] or "")

    if pending is not None:
        expected_label = _next_label(pending)
        # Find programme-specific blocks matching the expected workout that are
        # newer than the last confirmed one (avoids double-confirming).
        latest_date = (latest.date or "") if latest else ""
        matches = [
            b for b in blocks
            if expected_label and b[0] and expected_label.lower() in b[0].lower()
            and (not latest_date or (b[1] or "") > latest_date)
        ]
        if matches:
            label, date_str, row_id = matches[-1]
            return ReconciliationResult(
                programme_id=programme.id,
                status="confirmed",
                expected_label=expected_label,
                found_label=label,
                found_date=date_str,
                found_id=row_id,
                message=f"{label} confirmed against the {programme.name} history.",
            )
        label, date_str = newest[0], newest[1]
        return ReconciliationResult(
            programme_id=programme.id,
            status="mismatch",
            expected_label=expected_label,
            found_label=label,
            found_date=date_str,
            message=f"I can't see the expected workout '{expected_label}' in the current "
                    f"{programme.name} history — the newest is '{label}' ({date_str}). "
                    "I haven't advanced the programme position yet. Check that the "
                    "latest export is in Drive.",
        )

    # No pending prescription: a newest workout newer than the last confirmed
    # one is newly completed; otherwise there is nothing to reconcile.
    # "Same workout" means same label AND same date (e.g. two different
    # "15:00 row" sessions on different days are different workouts).
    label, date_str, row_id = newest
    same_as_confirmed = (
        latest is not None
        and _next_label(latest) and label
        and _next_label(latest).lower() == label.lower()
        and (latest.date or "") == date_str
    )
    if same_as_confirmed:
        return ReconciliationResult(
            programme_id=programme.id,
            status="not_found",
            expected_label=None,
            found_label=label,
            found_date=date_str,
            message=f"The newest {programme.name} workout is still '{label}' ({date_str}) — "
                    "the one already confirmed. Nothing new to reconcile.",
        )

    return ReconciliationResult(
        programme_id=programme.id,
        status="confirmed",
        expected_label=None,
        found_label=label,
        found_date=date_str,
        found_id=row_id,
        message=f"{label} confirmed against the {programme.name} history.",
    )


def _next_label(expected) -> str | None:
    if expected is None:
        return None
    return expected.label or expected.id


def _workout_blocks(header: list[str], data: list[list[str]]) -> list[tuple[str, str, str]]:
    """All distinct (label, date, id) workout blocks in the export.

    A block = a run of rows sharing the same (date, workout name). The
    history mixes programmes (J&T, PHUL, Rowing A/B...), so reconciliation
    must be able to search within programme-specific blocks rather than
    assuming the newest row belongs to the programme being reconciled.
    """
    def col(*names):
        for name in names:
            if name in header:
                return header.index(name)
        return None

    date_i = col("Date")
    # Strong uses "Workout"/"Workout Name"; Concept2 Logbook uses "Description".
    name_i = col("Workout Name", "Workout", "Description")
    if date_i is None or name_i is None:
        return []
    blocks: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in data:
        label = row[name_i].strip() if name_i < len(row) else ""
        date_str = row[date_i].strip()[:10] if date_i < len(row) else ""
        if not label or not date_str:
            continue
        key = (label, date_str)
        if key not in seen:
            seen.add(key)
            blocks.append((label, date_str, f"{date_str}:{label}"))
    return blocks


def _recent_rows(history_csv: str, max_rows: int = 30) -> str:
    rows = list(csv.reader(io.StringIO(history_csv)))
    if not rows:
        return ""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(rows[0])
    writer.writerows(rows[-max_rows:] if max_rows else rows[1:])
    return out.getvalue()
