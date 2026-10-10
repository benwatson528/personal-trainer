"""Does the generated CSV actually contain the prescription it was given?

The LLM makes the training decision once, as a structured prescription, and then
serialises that decision as a Strong CSV. Everything up to the serialisation is
one decision; the serialisation is transcription. Transcription can drop a set.

That is not a hypothetical. A live Week 1 Day 1 generated a T2a deadlift block
with 1 working set where the programme prescribes 4 — the structural and
equipment gates both passed it, because the CSV was well-formed and every weight
was loadable, and the LLM semantic validator approved it. A file that is missing
three quarters of its volume is still a valid-looking CSV.

So this is a separate, deterministic gate. It compares the CSV's working-set
count per exercise against the prescription, which is the decision the CSV is
supposed to be a faithful copy of. It does not know or care what the programme
prescribes — no programme logic is encoded here. It only checks the copy against
the thing it is a copy of.

Like the equipment gate, column positions come from the schema derived from the
athlete's real export, and the check is skipped when those columns cannot be
found rather than guessed at.
"""

from __future__ import annotations

import csv
import io

from .equipment_rules import resolve_column, is_warmup
from .schema import StrongSchema


def _as_int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def validate_prescription_fidelity(
    csv_text: str,
    prescription: dict | None,
    schema: StrongSchema | None = None,
) -> list[str]:
    """Errors where the CSV's working sets do not match the prescription.

    Returns an empty list when the workout is faithful, when there is no
    prescription to check against, or when the columns this needs are absent.
    """
    if not prescription or not isinstance(prescription.get("exercises"), list):
        return []

    rows = list(csv.reader(io.StringIO(csv_text)))
    if len(rows) < 2:
        return []

    header = [h.strip() for h in rows[0]]
    exercise_col = resolve_column(header, schema, "Exercise")
    set_col = resolve_column(header, schema, "Set")
    if exercise_col is None or set_col is None:
        return []

    # Count working sets per exercise, collapsing duplicate names the same way
    # the prescription is authored (one entry per exercise).
    found: dict[str, int] = {}
    for row in rows[1:]:
        if len(row) <= max(exercise_col, set_col):
            continue
        exercise = row[exercise_col].strip()
        if not exercise:
            continue
        if is_warmup(row[set_col]):
            continue
        found[exercise] = found.get(exercise, 0) + 1

    errors: list[str] = []
    for exercise in prescription["exercises"]:
        if not isinstance(exercise, dict):
            continue
        name = str(exercise.get("name") or "").strip()
        expected = _as_int(exercise.get("sets"))
        if not name or expected is None:
            continue
        actual = found.get(name)
        if actual is None:
            errors.append(
                f"{name}: the prescription has {expected} working sets but the CSV "
                f"has no rows for it. Add them."
            )
        elif actual != expected:
            errors.append(
                f"{name}: the prescription has {expected} working sets but the CSV "
                f"has {actual}. Make the CSV match the prescription — do not change "
                f"the prescription."
            )
    return errors
