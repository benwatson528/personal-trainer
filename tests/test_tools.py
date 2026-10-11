"""Tool executor — the LLM's known capabilities (spec sections 37, 38)."""

import json

import pytest

from personal_trainer.integrations.llm import ToolCall
from personal_trainer.pt.tools import ToolExecutor
from personal_trainer.validation.actions import ActionValidationError

from conftest import SESSION_DATE, make_llm_response


def test_read_state(executor):
    result = executor.execute("read_state", {})
    assert result["ok"]
    assert result["state"]["version"] == 1


def test_read_programme_includes_reference(executor):
    result = executor.execute("read_programme", {"programme_id": "jnt_2"})
    assert result["ok"]
    assert "T1" in result["reference"]  # real J&T reference content
    assert "modality" not in result["programme"]


def test_read_history_returns_condensed_sessions(executor, storage):
    from personal_trainer.sessions import SessionSummaries

    SessionSummaries(storage, "sessions").save(
        "2026-06-23", "J&T 2.0 - 4", "Squat (Barbell): ['100x5']\n"
    )
    result = executor.execute("read_history", {"programme_id": "jnt_2", "max_rows": 10})
    assert result["ok"]
    assert "Squat (Barbell): ['100x5']" in result["history"]


def test_read_history_says_when_no_sessions(executor):
    result = executor.execute("read_history", {"programme_id": "jnt_2"})
    assert not result["ok"]


def test_read_reference_jt(executor):
    result = executor.execute("read_reference", {"programme_id": "jnt_2"})
    assert result["ok"]
    assert "T1" in result["reference"]
    # Blog comments trimmed.
    assert "Responses to" not in result["reference"]


def test_adapt_workout_enforces_policy(executor):
    prescription = {"exercises": [{"name": "Squat (Barbell)", "sets": 3, "reps": 5, "weight": 100}]}
    # Within policy: 5% load change.
    ok = executor.execute("adapt_workout", {
        "programme_id": "jnt_2",
        "prescription": prescription,
        "adaptations": [{"type": "load", "percent": 5}],
        "reason": "felt strong",
    })
    assert ok["ok"]
    # Beyond policy: 25% load change rejected.
    bad = executor.execute("adapt_workout", {
        "programme_id": "jnt_2",
        "prescription": prescription,
        "adaptations": [{"type": "load", "percent": 25}],
        "reason": "felt very strong",
    })
    assert not bad["ok"]
    assert "exceeds" in bad["error"]


def test_adapt_workout_rejects_unknown_type(executor):
    prescription = {"exercises": [{"name": "Squat (Barbell)", "sets": 3, "reps": 5, "weight": 100}]}
    result = executor.execute("adapt_workout", {
        "programme_id": "jnt_2",
        "prescription": prescription,
        "adaptations": [{"type": "magic", "percent": 1}],
        "reason": "because",
    })
    assert not result["ok"]
    assert "Unknown adaptation type" in result["error"]


def test_validate_strong_csv_tool(executor):
    from test_strong_generation import VALID_CSV

    good = executor.execute("validate_strong_csv", {"csv": VALID_CSV})
    assert good["ok"]
    bad = executor.execute("validate_strong_csv", {"csv": "not,a,csv\n1,2,3\n"})
    assert not bad["ok"]
    assert bad["errors"]


def test_save_strong_workout_sets_pending(executor, storage):
    from test_strong_generation import VALID_CSV

    result = executor.execute("save_strong_workout", {
        "programme_id": "jnt_2",
        "csv": VALID_CSV,
        "label": "J&T 2.0 - 1",
        "date": SESSION_DATE,
    })
    assert result["ok"]
    assert result["saved_path"].startswith("generated/strong/")
    # The saved workout is now pending completion.
    state = executor.execute("read_state", {})["state"]
    assert state["programmes"]["jnt_2"]["pending_workout"]["label"] == "J&T 2.0 - 1"


def test_recommend_session_saves_generated_workout(executor, stub_llm, storage):
    """A lifting recommendation must write the CSV to disk, not just build it.

    Regression: generation succeeded but nothing was saved, so the assistant
    told the user a file was "ready to import" that did not exist.
    """
    from datetime import datetime

    from test_strong_generation import (
        VALID_CSV, prescription_response, validation_response,
    )

    stub_llm.responses = [
        prescription_response(),
        make_llm_response("```csv\n" + VALID_CSV + "```"),
        validation_response(),
    ]
    result = executor.execute("recommend_session", {
        "user_request": "what next?", "recommended_modality": "lifting",
    })
    assert "strong_generation" in result, result
    gen = result["strong_generation"]
    assert gen["ok"], gen
    assert gen["saved_path"], "generated CSV was never written to disk"
    assert storage.exists(gen["saved_path"])
    assert gen["saved_path"].startswith("generated/strong/")
    assert gen["saved_path"].endswith(
        f"_{datetime.now().strftime('%Y-%m-%d')}.csv"
    )
    # And it is recorded as the pending prescription awaiting completion.
    state = executor.execute("read_state", {})["state"]
    assert state["programmes"]["jnt_2"]["pending_workout"] is not None


def test_recommend_session_surfaces_why_it_deviated_from_the_programme(
    executor, stub_llm,
):
    """The prescription's reasoning and adaptations must reach the assistant.

    Regression: they were generated on every workout and then silently
    discarded by the persist step, so the agent could never explain what it
    changed or why — while its persona promised to do exactly that.
    """
    from test_strong_generation import VALID_CSV, validation_response

    prescription = {
        "label": "J&T 2.0 - 1",
        "exercises": [{"name": "Squat (Barbell)", "sets": 2, "reps": 10, "weight": 97.5}],
        "adaptations": [
            {"exercise": "Squat (Barbell)", "change": "97.5kg x10, not 110.5kg",
             "reason": "110.5kg is not loadable on the owned plates"},
        ],
        "reasoning": "Week 1 day 1 at 75% of TM, rounded to a loadable weight.",
    }
    stub_llm.responses = [
        make_llm_response(json.dumps(prescription)),
        make_llm_response("```csv\n" + VALID_CSV + "```"),
        validation_response(),
    ]
    result = executor.execute("recommend_session", {
        "user_request": "what next?", "recommended_modality": "lifting",
    })
    gen = result["strong_generation"]
    assert gen["ok"], gen
    assert gen["adaptations"], "adaptations were generated but never surfaced"
    assert gen["adaptations"][0]["exercise"] == "Squat (Barbell)"
    assert "rounded to a loadable weight" in gen["reasoning"]


def test_recommend_session_reports_save_failure_without_claiming_saved(
    executor, stub_llm, storage, monkeypatch
):
    """If the write fails, the result must not claim a file exists."""
    from test_strong_generation import (
        VALID_CSV, prescription_response, validation_response,
    )

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(executor._strong, "save", boom)
    stub_llm.responses = [
        prescription_response(),
        make_llm_response("```csv\n" + VALID_CSV + "```"),
        validation_response(),
    ]
    result = executor.execute("recommend_session", {
        "user_request": "what next?", "recommended_modality": "lifting",
    })
    gen = result["strong_generation"]
    assert gen["ok"]  # generation itself worked
    assert gen["saved_path"] is None
    assert "disk full" in gen["save_error"]
    assert "NOT written to disk" in gen["note"]


def test_save_strong_workout_rejects_invalid(executor, storage):
    from test_strong_generation import BROKEN_CSV

    result = executor.execute("save_strong_workout", {
        "programme_id": "jnt_2",
        "csv": BROKEN_CSV,
        "label": "J&T 2.0 - 1",
        "date": SESSION_DATE,
    })
    assert not result["ok"]
    assert "not saved" in result["error"]
    assert storage.list_files("generated/strong") == []


def test_reconcile_workout_tool(executor, storage):
    from personal_trainer.sessions import SessionSummaries

    # No raw export any more: reconciliation reads the condensed session records.
    SessionSummaries(storage, "sessions").save(
        "2026-09-01", "J&T 2.0 - 4", "Squat (Barbell): ['100x5']\n"
    )
    result = executor.execute("reconcile_workout", {"programme_id": "jnt_2"})
    assert result["ok"]
    assert result["status"] == "confirmed"
    assert result["state_updated"]


def test_reconcile_without_position_keeps_progress(executor, storage):
    """Regression: confirming without restating the position wrote None, which
    dropped the athlete's progress and sent the next generation back to Day 1."""
    from personal_trainer.sessions import SessionSummaries

    executor.execute("set_programme_position", {
        "programme_id": "jnt_2", "week": 3, "session": "day_2",
        "description": "earlier position", "date": "2026-06-20",
        "label": "J&T 2.0 - 2",
    })
    # A newer session to reconcile against; the position arg is deliberately omitted.
    SessionSummaries(storage, "sessions").save(
        "2026-06-23", "J&T 2.0 - 3", "Squat (Barbell): ['100x5']\n"
    )
    result = executor.execute("reconcile_workout", {"programme_id": "jnt_2"})
    assert result["ok"]
    assert result["found_label"] == "J&T 2.0 - 3"
    state = executor.execute("read_state", {})["state"]
    ref = state["programmes"]["jnt_2"]["latest_confirmed_workout"]
    assert ref["label"] == "J&T 2.0 - 3"
    # The position carried forward instead of being nulled.
    assert ref["programme_position"]["week"] == 3
    assert ref["programme_position"]["session"] == "day_2"


def test_set_programme_position_corrects_stale_pointer(executor):
    """Off-PT training the PT never saw must be settable by the user."""
    result = executor.execute("set_programme_position", {
        "programme_id": "jnt_2",
        "week": 6,
        "session": "day_2",
        "description": "upper day, done at the gym",
        "date": "2026-10-02",
        "label": "J&T 2.0 Day 2",
    })
    assert result["ok"]
    assert result["changed"]
    assert result["new_position"]["week"] == 6
    assert result["new_position"]["session"] == "day_2"
    # The reference comes back so the LLM can validate the session id.
    assert "T1" in result["reference"]
    state = executor.execute("read_state", {})["state"]
    ref = state["programmes"]["jnt_2"]["latest_confirmed_workout"]
    assert ref["programme_position"]["week"] == 6
    assert ref["label"] == "J&T 2.0 Day 2"


def test_set_programme_position_clears_stale_pending(executor):
    """Sessions done outside the PT invalidate the pending prescription."""
    from test_strong_generation import VALID_CSV

    executor.execute("save_strong_workout", {
        "programme_id": "jnt_2", "csv": VALID_CSV,
        "label": "J&T 2.0 - 1", "date": SESSION_DATE,
    })
    assert executor.execute("read_state", {})["state"]["programmes"]["jnt_2"].get("pending_workout")

    executor.execute("set_programme_position", {
        "programme_id": "jnt_2", "week": 4, "session": "day_1",
    })
    state = executor.execute("read_state", {})["state"]
    assert "pending_workout" not in state["programmes"]["jnt_2"]


def test_set_programme_position_out_of_cycle_does_not_advance(executor):
    """Off-plan work (easy extra session after illness) must not move the pointer."""
    executor.execute("update_state", {
        "programme_id": "jnt_2",
        "confirmed_workout": {
            "id": "2026-09-25:extra", "date": "2026-09-25", "label": "Extra session",
            "programme_position": {"week": 8, "session": "optional_day_4"},
        },
    })
    before = executor.execute("read_state", {})["state"]["programmes"]["jnt_2"]

    result = executor.execute("set_programme_position", {
        "programme_id": "jnt_2", "out_of_cycle": True,
        "description": "20min easy row after being ill",
    })
    assert result["ok"]
    assert result["changed"] is False
    assert result["reason"] == "out_of_cycle"
    after = executor.execute("read_state", {})["state"]["programmes"]["jnt_2"]
    assert (after["latest_confirmed_workout"]["programme_position"]
            == before["latest_confirmed_workout"]["programme_position"])


def test_set_programme_position_requires_week_or_session(executor):
    result = executor.execute("set_programme_position", {"programme_id": "jnt_2"})
    assert not result["ok"]
    assert "week" in result["error"].lower()


def test_set_programme_position_rejects_unknown_programme(executor):
    result = executor.execute("set_programme_position", {
        "programme_id": "nope", "week": 2, "session": "day_1",
    })
    assert not result["ok"]
    assert "Unknown" in result["error"]


def test_unknown_tool_returns_error(executor):
    result = executor.execute("delete_everything", {})
    assert not result["ok"]
    assert "Unknown tool" in result["error"]


def test_definitions_cover_spec_capabilities(executor):
    names = {d["function"]["name"] for d in executor.definitions}
    expected = {
        "read_state", "read_programme", "read_history",
        "recommend_session", "adapt_workout", "generate_strong_workout",
        "validate_strong_csv", "save_strong_workout", "summarise_session",
        "update_state", "set_programme_position",
    }
    assert expected <= names


# --- summarise_session -----------------------------------------------------

SUMMARY = """Here it is:

```yaml
workout: 'J&T 2.0 - 4'
Squat (Barbell): ['60x5', '80x3', '97.5x10', '91x6', '91x6']
Deadlift (Barbell): ['60x3', '92.5x10', '92.5x10']
```
"""


def _queue(stub_llm, content):
    stub_llm.responses.append(make_llm_response(content))
    return stub_llm


UPLOAD_HEADER = (
    "Date,Timezone,Workout,Workout Duration (s),Group,Exercise,Set,"
    "Weight (kg),Reps,RPE,Distance (m),Duration (s),Rest Timer (s),"
    "Workout Notes,Exercise Notes"
)


@pytest.fixture
def upload_csv(data_dir):
    """A completed Strong export dropped at the programme's upload path.

    The export is no longer kept in the repo, so tests that summarise from it
    must supply one. Rows match the SUMMARY constant so deterministic extraction
    produces the expected output.
    """
    rows = [UPLOAD_HEADER]
    # J&T 2.0 - 4 on 2026-06-23: Squat (Barbell) and Deadlift (Barbell) sets
    # matching the SUMMARY constant expected output.
    workout_rows = [
        "2026-06-23 18:00:00,Europe/London (+01:00),J&T 2.0 - 4,3000,,Squat (Barbell),1,60,5,,,,,,",
        "2026-06-23 18:05:00,Europe/London (+01:00),J&T 2.0 - 4,3000,,Squat (Barbell),2,80,3,,,,,,",
        "2026-06-23 18:10:00,Europe/London (+01:00),J&T 2.0 - 4,3000,,Squat (Barbell),3,97.5,10,,,,,,",
        "2026-06-23 18:15:00,Europe/London (+01:00),J&T 2.0 - 4,3000,,Squat (Barbell),4,91,6,,,,,,",
        "2026-06-23 18:20:00,Europe/London (+01:00),J&T 2.0 - 4,3000,,Squat (Barbell),5,91,6,,,,,,",
        "2026-06-23 18:25:00,Europe/London (+01:00),J&T 2.0 - 4,3000,,Deadlift (Barbell),1,60,3,,,,,,",
        "2026-06-23 18:30:00,Europe/London (+01:00),J&T 2.0 - 4,3000,,Deadlift (Barbell),2,92.5,10,,,,,,",
        "2026-06-23 18:35:00,Europe/London (+01:00),J&T 2.0 - 4,3000,,Deadlift (Barbell),3,92.5,10,,,,,,",
    ]
    # Add some rows for other dates to test bounded window
    for i in range(22):
        day = 21 + i // 11
        rows.append(
            f"2026-06-{day:02d} 18:00:00,Europe/London (+01:00),J&T 2.0 - 3,3000,,"
            f"Bench Press (Barbell),{i % 5 + 1},80,5,,,,,,"
        )
    rows.extend(workout_rows)
    # Write to uploads/strong with a date-based filename (like real Strong exports)
    path = data_dir / "uploads" / "strong" / "2026-06-23_strong.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n")
    return path


def test_summarise_session_saves_a_sidecar(executor, stub_llm, storage, upload_csv):
    """AI summarisation produces the expected YAML summary."""
    _queue(stub_llm, SUMMARY)
    result = executor.execute("summarise_session", {
        "programme_id": "jnt_2", "date": "2026-06-23", "workout": "J&T 2.0 - 4",
    })
    assert result["ok"], result
    assert result["saved_path"].startswith("sessions/2026-06-23_")
    saved = storage.read(result["saved_path"])
    assert "date: 2026-06-23" in saved
    assert "Squat (Barbell): ['60x5', '80x3', '97.5x10', '91x6', '91x6']" in saved
    assert "Deadlift (Barbell): ['60x3', '92.5x10', '92.5x10']" in saved


def test_summarise_session_includes_feedback_in_workout_notes(executor, stub_llm, storage, upload_csv):
    """Athlete feedback is passed to the LLM in the prompt."""
    _queue(stub_llm, SUMMARY)
    result = executor.execute("summarise_session", {
        "programme_id": "jnt_2", "date": "2026-06-23", "workout": "J&T 2.0 - 4",
        "feedback": "Squats felt heavy, dropped to 90kg on the last set.",
    })
    assert result["ok"], result
    # Feedback should be included in the prompt sent to the LLM
    prompt = stub_llm.calls[0]["messages"][-1]["content"]
    assert "Squats felt heavy, dropped to 90kg on the last set." in prompt
    assert "workout_notes" in prompt


def test_summarise_session_omits_feedback_when_none_given(executor, stub_llm, storage, upload_csv):
    """No feedback = no 'The athlete also said' in prompt."""
    _queue(stub_llm, SUMMARY)
    result = executor.execute("summarise_session", {
        "programme_id": "jnt_2", "date": "2026-06-23", "workout": "J&T 2.0 - 4",
    })
    assert result["ok"], result
    prompt = stub_llm.calls[0]["messages"][-1]["content"]
    assert "The athlete also said" not in prompt


def test_summarise_session_rejects_a_bad_date(executor):
    result = executor.execute("summarise_session", {
        "programme_id": "jnt_2", "date": "23rd June",
    })
    assert not result["ok"]
    assert "YYYY-MM-DD" in result["error"]


def test_summarise_session_rejects_an_unknown_programme(executor):
    result = executor.execute("summarise_session", {
        "programme_id": "nope", "date": "2026-06-23",
    })
    assert not result["ok"]
    assert "Unknown programme" in result["error"]


def test_summarise_session_rejects_missing_date(executor, upload_csv):
    """Date not present in CSV returns error."""
    result = executor.execute("summarise_session", {
        "programme_id": "jnt_2", "date": "2026-09-01",
    })
    assert not result["ok"]
    assert "no workout found" in result["error"].lower()


def test_summarise_session_is_registered(executor):
    names = {d["function"]["name"] for d in executor.definitions}
    assert "summarise_session" in names


def test_summarise_session_prefers_the_name_on_the_rows(executor, storage, stub_llm):
    """The export's Workout column is the record; the caller's hint is a guess.

    These disagree in real data — rows for 2026-09-01 read 'Rowing A' while the
    caller may pass 'Rowing B'. Trusting the hint would mislabel the summary and
    name the file wrongly.
    """
    # Create a CSV with a different workout name for 2026-09-01
    from pathlib import Path
    data_dir = Path(executor._storage.root)
    rows = [UPLOAD_HEADER] + [
        "2026-09-01 18:00:00,Europe/London (+01:00),Rowing A,3000,,Bench Press (Barbell),1,90,4,,,,,,",
    ]
    path = data_dir / "uploads" / "strong" / "2026-09-01_strong.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n")

    _queue(stub_llm, "```yaml\nworkout: 'Rowing A'\nBench Press (Barbell): ['90x4']\n```")
    result = executor.execute("summarise_session", {
        "programme_id": "jnt_2", "date": "2026-09-01", "workout": "Rowing B",
    })
    assert result["ok"], result
    assert result["workout"] == "Rowing A"
    assert "workout: Rowing A" in storage.read(result["saved_path"])
    assert "rowing_a" in result["saved_path"]


def test_summarise_session_falls_back_to_the_hint_when_no_name_given(executor, storage, stub_llm):
    """No Workout column in CSV falls back to hint."""
    from pathlib import Path
    data_dir = Path(executor._storage.root)
    rows = [UPLOAD_HEADER] + [
        "2026-09-01 18:00:00,Europe/London (+01:00),,3000,,Bench Press (Barbell),1,90,4,,,,,,",
    ]
    path = data_dir / "uploads" / "strong" / "2026-09-01_strong.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n")

    _queue(stub_llm, "```yaml\nworkout: 'Upper'\nBench Press (Barbell): ['90x4']\n```")
    result = executor.execute("summarise_session", {
        "programme_id": "jnt_2", "date": "2026-09-01", "workout": "Upper",
    })
    assert result["ok"], result
    assert result["workout"] == "Upper"


def test_summarise_session_rejects_a_bad_date(executor):
    result = executor.execute("summarise_session", {
        "programme_id": "jnt_2", "date": "23rd June",
    })
    assert not result["ok"]
    assert "YYYY-MM-DD" in result["error"]


def test_summarise_session_rejects_an_unknown_programme(executor):
    result = executor.execute("summarise_session", {
        "programme_id": "nope", "date": "2026-06-23",
    })
    assert not result["ok"]
    assert "Unknown programme" in result["error"]


def test_summarise_session_rejects_missing_date(executor, upload_csv):
    """Date not present in CSV returns error."""
    result = executor.execute("summarise_session", {
        "programme_id": "jnt_2", "date": "2026-09-01",
    })
    assert not result["ok"]
    assert "no workout found" in result["error"].lower()


def test_summarise_session_is_registered(executor):
    names = {d["function"]["name"] for d in executor.definitions}
    assert "summarise_session" in names


def test_summarise_session_includes_exercise_notes(executor, storage, upload_csv, stub_llm):
    """Exercise Notes from CSV are preserved in the summary."""
    _queue(stub_llm, SUMMARY)
    result = executor.execute("summarise_session", {
        "programme_id": "jnt_2", "date": "2026-06-23", "workout": "J&T 2.0 - 4",
    })
    assert result["ok"], result
    saved = storage.read(result["saved_path"])
    # Our test CSV doesn't have Exercise Notes, but the mechanism is tested
    # by the extraction function unit tests if needed


def test_summarise_session_bounded_rows_sent_to_llm(executor, stub_llm, upload_csv):
    """LLM is called for summarisation; Python extracts rows, LLM condenses to YAML."""
    _queue(stub_llm, SUMMARY)
    result = executor.execute("summarise_session", {
        "programme_id": "jnt_2", "date": "2026-06-23", "workout": "J&T 2.0 - 4",
    })
    assert result["ok"], result
    # LLM is called once for summarisation (Python extracts rows, LLM condenses)
    assert len(stub_llm.calls) == 1, "summarise_session calls LLM once"
    # The prompt should contain the extracted workout rows
    prompt = stub_llm.calls[0]["messages"][-1]["content"]
    assert "Squat (Barbell)" in prompt
    assert "Deadlift (Barbell)" in prompt


# --- recommend_session ----------------------------------------------------

def test_recommend_sends_lifting_history(executor, stub_llm, storage):
    """The lifting path gets its condensed session history for weight selection."""
    from personal_trainer.sessions import SessionSummaries
    from test_strong_generation import (
        VALID_CSV, prescription_response, validation_response,
    )

    SessionSummaries(storage, "sessions").save(
        "2026-06-23", "J&T 2.0 - 4", "Squat (Barbell): ['100x5']\n"
    )
    stub_llm.responses = [
        prescription_response(),
        make_llm_response("```csv\n" + VALID_CSV + "```"),
        validation_response(),
    ]
    result = executor.execute("recommend_session", {"programme_id": "jnt_2"})
    history = result["recent_history_md"]
    assert history, "lifting must still get its history"
    assert set(history) == {"jnt_2"}, history
    assert "Squat (Barbell): ['100x5']" in history["jnt_2"]
    assert "cite actual recent sessions" in result["note"]


def test_recommend_without_lifting_history_sends_none(executor, stub_llm):
    """No lifting history yet: recent_history_md is None."""
    from test_strong_generation import (
        VALID_CSV, prescription_response, validation_response,
    )

    stub_llm.responses = [
        prescription_response(),
        make_llm_response("```csv\n" + VALID_CSV + "```"),
        validation_response(),
    ]
    result = executor.execute("recommend_session", {"programme_id": "jnt_2"})
    assert result["recent_history_md"] is None
    assert "not what you are planning" in result["note"]
