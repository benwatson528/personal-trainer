"""Schema derivation from a real-looking Strong export (spec sections 8, 21, 41).

The full Strong history is no longer kept; this small sample stands in for it so
the inference is still exercised against real column shapes (microplate weights,
warmup 'W' markers, sparse note columns).
"""

from pathlib import Path

from personal_trainer.strong.schema import derive_schema

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "strong_export_sample.csv"


def load_real_history() -> str:
    return FIXTURE.read_text()


def test_schema_matches_real_strong_format():
    schema = derive_schema(load_real_history())
    assert schema.headers == [
        "Date", "Timezone", "Workout", "Workout Duration (s)", "Group",
        "Exercise", "Set", "Weight (kg)", "Reps", "RPE", "Distance (m)",
        "Duration (s)", "Rest Timer (s)", "Workout Notes", "Exercise Notes",
    ]


def test_weight_column_is_number_not_integer():
    """The real history uses microplates (82.5, 33.5) — must infer number."""
    schema = derive_schema(load_real_history())
    weight = schema.column("Weight (kg)")
    assert weight is not None
    assert weight.dtype == "number"


def test_set_column_is_string_due_to_warmup_markers():
    schema = derive_schema(load_real_history())
    set_col = schema.column("Set")
    assert set_col is not None
    assert set_col.dtype == "string"  # contains 'W', 'D', 'F' markers


def test_required_columns_populated_in_every_row():
    schema = derive_schema(load_real_history())
    required = {c.name for c in schema.columns if c.required}
    assert {"Date", "Timezone", "Workout", "Workout Duration (s)", "Exercise", "Set", "Reps"} <= required


def test_sparse_columns_not_required():
    schema = derive_schema(load_real_history())
    assert not schema.column("Exercise Notes").required
    assert not schema.column("RPE").required
    assert not schema.column("Distance (m)").required
