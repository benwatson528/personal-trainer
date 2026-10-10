"""Condensed session records: written by the LLM, read by Python.

The format is deliberately trivial — exercise name to an ordered list of
`weight x reps` strings. Python's job is to store it, sanity-check the shape and
hand it to the model. It must never need to interpret the app's export.
"""

from __future__ import annotations

import pytest

from personal_trainer.sessions import SessionSummaries

BODY = """
Squat (Barbell): ['60x5', '80x3', '97.5x10', '91x6', '91x6']
Deadlift (Barbell): ['60x3', '92.5x10', '92.5x10', '92.5x10', '92.5x10']
Hammer Curl (Dumbbell): ['20x20', '20x20', '20x20']
"""


@pytest.fixture
def summaries(storage):
    return SessionSummaries(storage, "sessions")


# --- writing ---------------------------------------------------------------

def test_round_trips_a_session(summaries, storage):
    path = summaries.save("2026-06-23", "J&T 2.0 - 4", BODY)
    assert storage.read(path) == (
        "date: 2026-06-23\n"
        # Quoted: '&' is a YAML anchor character, so the workout name is
        # quoted defensively rather than trusted to stay plain.
        "workout: 'J&T 2.0 - 4'\n"
        "Squat (Barbell): ['60x5', '80x3', '97.5x10', '91x6', '91x6']\n"
        "Deadlift (Barbell): ['60x3', '92.5x10', '92.5x10', '92.5x10', '92.5x10']\n"
        "Hammer Curl (Dumbbell): ['20x20', '20x20', '20x20']\n"
    )


def test_a_quoted_workout_name_reads_back_correctly(summaries, storage):
    import yaml

    path = summaries.save("2026-06-23", "J&T 2.0 - 4", BODY)
    assert yaml.safe_load(storage.read(path))["workout"] == "J&T 2.0 - 4"


def test_saves_under_a_date_prefixed_filename(summaries):
    path = summaries.save("2026-06-23", "J&T 2.0 - 4", BODY)
    assert path.startswith("sessions/2026-06-23_")


def test_saving_the_same_date_twice_overwrites(summaries):
    """Re-extracting a session must replace it, not duplicate it."""
    summaries.save("2026-06-23", "Legs", "Squat (Barbell): ['100x5']\n")
    summaries.save("2026-06-23", "Legs", "Squat (Barbell): ['110x5']\n")
    assert summaries.count() == 1
    assert "'110x5'" in summaries.render()


# --- validation ------------------------------------------------------------

def test_rejects_a_malformed_set(summaries):
    with pytest.raises(ValueError, match="malformed set"):
        summaries.save("2026-06-23", "Legs", "Squat (Barbell): ['100x5', 'heavy']\n")


def test_rejects_an_exercise_without_sets(summaries):
    with pytest.raises(ValueError, match="non-empty list"):
        summaries.save("2026-06-23", "Legs", "Squat (Barbell): []\n")


def test_rejects_a_summary_with_no_exercises(summaries):
    with pytest.raises(ValueError, match="no exercises"):
        summaries.save("2026-06-23", "Legs", "date: 2026-06-23\nworkout: Legs\n")


def test_rejects_invalid_yaml(summaries):
    with pytest.raises(ValueError, match="not valid YAML"):
        summaries.save("2026-06-23", "Legs", "Squat (Barbell): [60x5\n  bad: : :\n")


def test_rejects_a_bad_date(summaries):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        summaries.save("23/06/2026", "Legs", "Squat (Barbell): [60x5]\n")


def test_tolerates_decimal_weights(summaries):
    summaries.save("2026-06-23", "Legs", "Squat (Barbell): ['97.5x10', '60x5']\n")
    assert "'97.5x10'" in summaries.render()


# --- reading ---------------------------------------------------------------

def test_renders_newest_session_first(summaries):
    summaries.save("2026-06-01", "Legs", "Squat (Barbell): ['100x5']\n")
    summaries.save("2026-09-01", "Upper", "Bench Press (Barbell): ['90x5']\n")
    summaries.save("2026-07-01", "Push", "Bench Press (Barbell): [80x5]\n")
    rendered = summaries.render()
    assert rendered.index("2026-09-01") < rendered.index("2026-07-01")
    assert rendered.index("2026-07-01") < rendered.index("2026-06-01")


def test_render_respects_the_limit(summaries):
    for day in ("2026-06-01", "2026-07-01", "2026-08-01"):
        summaries.save(day, "Legs", "Squat (Barbell): ['100x5']\n")
    assert summaries.render(limit=2).count("date:") == 2
    assert summaries.render(limit=1).count("date:") == 1


def test_render_is_empty_when_no_sessions_exist(summaries):
    assert summaries.render() == ""
    assert summaries.count() == 0


def test_session_date_is_readable_for_reconciliation(summaries):
    path = summaries.save("2026-06-23", "Legs", "Squat (Barbell): ['100x5']\n")
    assert summaries.session_date(path).isoformat() == "2026-06-23"


# --- history context --------------------------------------------------------

def test_generation_uses_the_condensed_records(strong_service, storage):
    """The training data is the condensed records; no raw export is needed."""
    SessionSummaries(storage, "sessions").save(
        "2026-06-23", "Legs", "Squat (Barbell): ['97.5x10']\n"
    )
    programme = strong_service._programmes.get("jnt_2")
    text = strong_service._sample_history(programme)
    assert "Squat (Barbell): ['97.5x10']" in text
    assert "raw rows" not in text


def test_generation_says_so_when_no_sessions_exist(strong_service):
    programme = strong_service._programmes.get("jnt_2")
    assert "No condensed session records" in strong_service._sample_history(programme)


def test_generation_shows_the_strong_header_from_the_day_template(
    strong_service, state_manager
):
    """The CSV header the model must copy now comes from the day template,
    which is injected into the generation messages."""
    programme = strong_service._programmes.get("jnt_2")
    text = strong_service._generation_messages(
        programme=programme,
        reference="ref",
        sample="sample",
        state=state_manager.load(),
        user_request="",
        prescription={"label": "J&T 2.0 - 1"},
        retry_errors=None,
    )[0]["content"]
    assert "## Day template" in text
    assert (
        "Date,Timezone,Workout,Workout Duration (s),Group,Exercise,Set,"
        "Weight (kg),Reps" in text
    )


# --- notes ------------------------------------------------------------------

NOTES_BODY = """\
workout_notes: 'bar speed slow on the last set'
Squat (Barbell): ['60x5', '97.5x10', '91x6']
Pull Up: ['0x8', '0x8']
notes:
  Pull Up: 'narrow grip'
  Squat (Barbell): 'knees caved, brace harder'
"""


def test_preserves_workout_and_exercise_notes(summaries, storage):
    """Notes are the athlete's own record of what to fix. Someone planning the
    next session needs them, so they must survive verbatim."""
    path = summaries.save("2026-06-23", "Legs", NOTES_BODY)
    saved = storage.read(path)
    # Plain notes are left unquoted; only text needing it gets quotes.
    assert "workout_notes: bar speed slow on the last set" in saved
    assert "  Pull Up: narrow grip" in saved
    assert "  Squat (Barbell): 'knees caved, brace harder'" in saved


def test_notes_read_back_as_a_mapping(summaries, storage):
    import yaml

    path = summaries.save("2026-06-23", "Legs", NOTES_BODY)
    data = yaml.safe_load(storage.read(path))
    assert data["notes"]["Pull Up"] == "narrow grip"
    assert data["workout_notes"] == "bar speed slow on the last set"


def test_notes_never_break_the_sets(summaries, storage):
    """A note lives in its own mapping, so it cannot be read as a set."""
    import yaml

    path = summaries.save("2026-06-23", "Legs", NOTES_BODY)
    data = yaml.safe_load(storage.read(path))
    assert data["Pull Up"] == ["0x8", "0x8"]
    assert data["Squat (Barbell)"] == ["60x5", "97.5x10", "91x6"]


def test_bodyweight_sets_survive_the_round_trip(summaries, storage):
    """Strong records weight 0 for bodyweight work, so `0x8` is not an edge case
    — it is what every pull-up and push-up looks like."""
    import yaml

    path = summaries.save("2026-06-23", "Legs", "Pull Up: ['0x8', '0x8']\n")
    assert yaml.safe_load(storage.read(path))["Pull Up"] == ["0x8", "0x8"]


def test_unquoted_sets_are_rejected_with_an_actionable_error(summaries):
    """Unquoted 0x8 is the number 8 in YAML, and the reps cannot be recovered.
    Naming that precisely is the difference between a repairable retry and the
    model resubmitting the same broken summary."""
    with pytest.raises(ValueError, match="read as a number"):
        summaries.save("2026-06-23", "Legs", "Pull Up: [0x8, 0x8]\n")


def test_a_summary_without_notes_is_still_valid(summaries, storage):
    path = summaries.save("2026-06-23", "Legs", "Squat (Barbell): ['60x5']\n")
    saved = storage.read(path)
    assert "notes" not in saved
    assert "workout_notes" not in saved


def test_rejects_a_note_that_is_actually_a_list(summaries):
    with pytest.raises(ValueError, match="note as text"):
        summaries.save(
            "2026-06-23", "Legs",
            "Squat (Barbell): ['60x5']\nnotes:\n  Pull Up:\n    - a\n    - b\n",
        )


def test_rejects_notes_that_are_not_a_mapping(summaries):
    with pytest.raises(ValueError, match="must map exercise name to text"):
        summaries.save(
            "2026-06-23", "Legs", "Squat (Barbell): ['60x5']\nnotes: 'narrow grip'\n"
        )


def test_a_note_with_yaml_syntax_is_quoted(summaries, storage):
    """A note is free text and may contain a colon, which would otherwise make
    the file unreadable."""
    import yaml

    path = summaries.save(
        "2026-06-23", "Legs", "Squat (Barbell): ['60x5']\nnotes:\n  Squat: 'bar speed: slow'\n"
    )
    assert "bar speed: slow" in storage.read(path)
    assert yaml.safe_load(storage.read(path))["notes"]["Squat"] == "bar speed: slow"


def test_generation_prompt_tells_the_model_notes_are_kept(summaries, strong_service, storage):
    """A condensed record that silently drops the notes would be worse than the
    raw rows it replaces, so the reader must be told they are there."""
    SessionSummaries(storage, "sessions").save("2026-06-23", "Legs", NOTES_BODY)
    text = strong_service._sample_history(strong_service._programmes.get("jnt_2"))
    assert "narrow grip" in text
    assert "notes:" in text
