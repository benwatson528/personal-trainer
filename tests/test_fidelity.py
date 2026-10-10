"""Prescription fidelity: the CSV must actually contain the decision it copies.

A live Week 1 Day 1 passed the structural gate, the equipment gate and the LLM
validator while containing 1 working set of T2a deadlift where the programme
prescribes 4. Every check that looked at the file said it was fine; nobody
checked that the file still contained the workout. These tests pin that down.
"""

from __future__ import annotations

from personal_trainer.strong.equipment_rules import is_warmup
from personal_trainer.strong.fidelity import validate_prescription_fidelity
from personal_trainer.strong.schema import derive_schema

HEADER = (
    "Date,Timezone,Workout,Workout Duration (s),Group,Exercise,Set,"
    "Weight (kg),Reps,RPE,Distance (m),Duration (s),Rest Timer (s),"
    "Workout Notes,Exercise Notes"
)


def row(exercise: str, set_id: str, weight: float, reps: int) -> str:
    return (
        f"2026-10-10 15:00:00,UTC,Day 1,1,,{exercise},{set_id},{weight},{reps},"
        f"0,,0,180,,"
    )


def csv_of(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


def prescription(*pairs) -> dict:
    return {"exercises": [{"name": n, "sets": s, "reps": 10} for n, s in pairs]}


SCHEMA = derive_schema(csv_of(row("Squat (Barbell)", "1", 100, 5)))


# --- the failure that motivated this ---------------------------------------

def test_catches_a_tier_that_lost_its_sets():
    """The real regression: 4 prescribed working sets, 1 in the file."""
    text = csv_of(
        row("Deadlift (Barbell)", "W", 60, 5),
        row("Deadlift (Barbell)", "1", 92.5, 10),
    )
    errors = validate_prescription_fidelity(
        text, prescription(("Deadlift (Barbell)", 4)), SCHEMA
    )
    assert len(errors) == 1
    assert "has 4 working sets but the CSV has 1" in errors[0]


def test_warmups_do_not_count_towards_the_total():
    text = csv_of(
        row("Squat (Barbell)", "W", 60, 5),
        row("Squat (Barbell)", "1", 97.5, 10),
        row("Squat (Barbell)", "2", 91, 6),
        row("Squat (Barbell)", "3", 91, 6),
        row("Squat (Barbell)", "4", 91, 6),
    )
    assert validate_prescription_fidelity(
        text, prescription(("Squat (Barbell)", 4)), SCHEMA
    ) == []


def test_normalises_warmup_case_and_whitespace():
    text = csv_of(
        row("Squat (Barbell)", "w ", 60, 5),
        row("Squat (Barbell)", "1", 100, 5),
    )
    assert validate_prescription_fidelity(
        text, prescription(("Squat (Barbell)", 1)), SCHEMA
    ) == []
    assert is_warmup("w ")


# --- correct workouts pass -------------------------------------------------

def test_a_faithful_workout_passes():
    text = csv_of(
        row("Squat (Barbell)", "W", 60, 5),
        row("Squat (Barbell)", "1", 97.5, 10),
        row("Squat (Barbell)", "2", 91, 6),
        row("Deadlift (Barbell)", "W", 60, 5),
        row("Deadlift (Barbell)", "1", 92.5, 10),
        row("Deadlift (Barbell)", "2", 92.5, 10),
        row("Deadlift (Barbell)", "3", 92.5, 10),
        row("Deadlift (Barbell)", "4", 92.5, 10),
    )
    assert validate_prescription_fidelity(
        text,
        prescription(("Squat (Barbell)", 2), ("Deadlift (Barbell)", 4)),
        SCHEMA,
    ) == []


def test_flags_an_entirely_missing_exercise():
    text = csv_of(row("Squat (Barbell)", "1", 100, 5))
    errors = validate_prescription_fidelity(
        text, prescription(("Squat (Barbell)", 1), ("Hammer Curl (Dumbbell)", 3)), SCHEMA
    )
    assert len(errors) == 1
    assert "Hammer Curl (Dumbbell)" in errors[0]
    assert "no rows for it" in errors[0]


def test_flags_an_over_prescription_too():
    text = csv_of(
        row("Squat (Barbell)", "1", 100, 5),
        row("Squat (Barbell)", "2", 100, 5),
        row("Squat (Barbell)", "3", 100, 5),
    )
    errors = validate_prescription_fidelity(
        text, prescription(("Squat (Barbell)", 1)), SCHEMA
    )
    assert "has 1 " in errors[0] and "has 3" in errors[0]


# --- skip rather than guess -------------------------------------------------

def test_no_prescription_means_no_check():
    text = csv_of(row("Squat (Barbell)", "1", 100, 5))
    assert validate_prescription_fidelity(text, None, SCHEMA) == []
    assert validate_prescription_fidelity(text, {}, SCHEMA) == []
    assert validate_prescription_fidelity(text, {"exercises": []}, SCHEMA) == []


def test_a_prescription_without_set_counts_is_skipped():
    """Never invent an expectation the prescription did not state."""
    text = csv_of(row("Squat (Barbell)", "1", 100, 5))
    assert validate_prescription_fidelity(
        text, {"exercises": [{"name": "Squat (Barbell)", "reps": 5}]}, SCHEMA
    ) == []


def test_coerces_a_numeric_string_set_count():
    """The LLM sometimes writes sets as a string; that is still a prescription."""
    text = csv_of(
        row("Squat (Barbell)", "1", 100, 5),
        row("Squat (Barbell)", "2", 100, 5),
    )
    assert validate_prescription_fidelity(
        text, {"exercises": [{"name": "Squat (Barbell)", "sets": "2", "reps": 5}]},
        SCHEMA,
    ) == []


def test_missing_schema_columns_skip_the_check():
    """Same contract as the equipment gate: a missed check is recoverable, a
    wrong one is not. With no Set column the two are indistinguishable, so the
    check declines to guess."""
    text = csv_of(row("Squat (Barbell)", "1", 100, 5))
    assert validate_prescription_fidelity(
        "Exercise,Weight (kg)\nSquat (Barbell),100\n",
        prescription(("Squat (Barbell)", 4)),
        SCHEMA,
    ) == []


def test_falls_back_to_header_names_without_a_schema():
    """resolve_column prefers the derived schema but still works from a plain header."""
    text = csv_of(row("Squat (Barbell)", "1", 100, 5))
    errors = validate_prescription_fidelity(
        text, prescription(("Squat (Barbell)", 4)), None
    )
    assert len(errors) == 1


def test_a_header_only_file_is_skipped():
    assert validate_prescription_fidelity(
        HEADER + "\n", prescription(("Squat (Barbell)", 4)), SCHEMA
    ) == []
