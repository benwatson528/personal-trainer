"""Strong CSV validation (spec section 21).

Validation uses the schema built from a day template — the format the generator
is told to reproduce.
"""

from pathlib import Path

from personal_trainer.strong.schema import schema_from_template
from personal_trainer.strong.validation import validate_strong_csv

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

HEADER = (
    "Date,Timezone,Workout,Workout Duration (s),Group,Exercise,Set,"
    "Weight (kg),Reps,RPE,Distance (m),Duration (s),Rest Timer (s),"
    "Workout Notes,Exercise Notes"
)


def real_schema():
    template = (DATA_DIR / "programmes" / "J&T 2.0" / "templates" / "day_1.csv").read_text()
    return schema_from_template(template)


def test_valid_csv_passes():
    rows = [
        HEADER,
        '2026-09-27 18:00:00,Europe/London (+01:00) [device],J&T 2.0 - 1,3000,,Squat (Barbell),W,60,5,,,,,,',
        '2026-09-27 18:00:00,Europe/London (+01:00) [device],J&T 2.0 - 1,3000,,Squat (Barbell),1,100,5,,,,,,',
        '2026-09-27 18:00:00,Europe/London (+01:00) [device],J&T 2.0 - 1,3000,,Squat (Barbell),2,100,5,,,,,,',
    ]
    errors = validate_strong_csv("\n".join(rows) + "\n", real_schema())
    assert errors == []


def test_wrong_column_count_detected():
    rows = [
        HEADER,
        '2026-09-27 18:00:00,Europe/London (+01:00) [device],J&T 2.0 - 1,3000,,Squat (Barbell),1,100,5',
    ]
    errors = validate_strong_csv("\n".join(rows) + "\n", real_schema())
    assert any("9 columns; expected 15" in e for e in errors)


def test_wrong_column_order_detected():
    header = HEADER.replace("Weight (kg),Reps", "Reps,Weight (kg)")
    rows = [header, "2026-09-27 18:00:00,x,J&T,1,,Squat,1,100,5,,,,,,"]
    errors = validate_strong_csv("\n".join(rows) + "\n", real_schema())
    assert any("Column order" in e for e in errors)


def test_missing_column_detected():
    header = ",Workout,Exercise,Set,Weight (kg),Reps"
    rows = [header, "2026-09-27,J&T,Squat,1,100,5"]
    errors = validate_strong_csv("\n".join(rows) + "\n", real_schema())
    assert any("Missing expected columns" in e for e in errors)


def test_non_numeric_weight_detected():
    rows = [
        HEADER,
        '2026-09-27 18:00:00,Europe/London (+01:00) [device],J&T 2.0 - 1,3000,,Squat (Barbell),1,100kg,5,,,,,,',
    ]
    errors = validate_strong_csv("\n".join(rows) + "\n", real_schema())
    assert any("Weight (kg)" in e for e in errors)


def test_empty_workout_name_detected():
    rows = [
        HEADER,
        "2026-09-27 18:00:00,Europe/London (+01:00) [device],,3000,,Squat (Barbell),1,100,5,,,,,,",
    ]
    errors = validate_strong_csv("\n".join(rows) + "\n", real_schema())
    assert any("Workout" in e for e in errors)


def test_header_only_rejected():
    errors = validate_strong_csv(HEADER + "\n", real_schema())
    assert any("no workout rows" in e for e in errors)


def test_empty_csv_rejected():
    errors = validate_strong_csv("", real_schema())
    assert errors


def test_quoted_notes_with_commas_pass():
    rows = [
        HEADER,
        '2026-09-27 18:00:00,Europe/London (+01:00) [device],J&T 2.0 - 1,3000,,Squat (Barbell),1,100,5,,,,,3 sets,"felt good, strong lockout"',
    ]
    errors = validate_strong_csv("\n".join(rows) + "\n", real_schema())
    assert errors == []
