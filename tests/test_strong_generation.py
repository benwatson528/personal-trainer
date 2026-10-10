"""Strong generation + validation retry loop (spec sections 20, 22, 23)."""

import pytest

from personal_trainer.integrations.llm import LLMResponse, ToolCall
from personal_trainer.strong.generation import StrongGenerationService

from conftest import SESSION_TIMESTAMP, make_llm_response

HEADER = (
    "Date,Timezone,Workout,Workout Duration (s),Group,Exercise,Set,"
    "Weight (kg),Reps,RPE,Distance (m),Duration (s),Rest Timer (s),"
    "Workout Notes,Exercise Notes"
)

# Generated workouts must carry the exact session timestamp, so fixtures are
# built from the shared pinned value rather than hardcoded. See the
# fixed_session_timestamp fixture in conftest.py, which pins the clock to this.
SESSION_TS = SESSION_TIMESTAMP


def _row(exercise, set_no, weight, reps, timestamp=None):
    return (
        f"{timestamp or SESSION_TS},Europe/London (+01:00) [device],J&T 2.0 - 1,1,,"
        f"{exercise},{set_no},{weight},{reps},,,,,,"
    )


VALID_CSV = "\n".join([
    HEADER,
    _row("Squat (Barbell)", "W", "60", "5"),
    _row("Squat (Barbell)", "1", "100", "5"),
    _row("Squat (Barbell)", "2", "100", "5"),
    _row("Bench Press (Barbell)", "1", "80", "5"),
]) + "\n"

BROKEN_CSV = "\n".join([
    HEADER,
    _row("Squat (Barbell)", "1", "100kg", "5"),
]) + "\n"


def prescription_response() -> LLMResponse:
    # No per-exercise set count: this stub is paired with several different
    # fixture CSVs, and a fixed count would contradict whichever one it is not
    # written for. Omitting it makes the fidelity gate skip, which is its
    # documented behaviour when the prescription states no set count. Tests that
    # exercise the gate supply a prescription that matches their CSV.
    return make_llm_response(
        '{"label": "J&T 2.0 - 1", "exercises": [{"name": "Squat (Barbell)", "reps": 5, "weight": 100}], "adaptations": [], "reasoning": "next in cycle"}'
    )


def validation_response() -> LLMResponse:
    return make_llm_response('{"errors": []}')


@pytest.fixture(autouse=True)
def fixed_session_timestamp(monkeypatch):
    """Pin the session timestamp so generated rows match the fixtures.

    `generate()` stamps rows with the wall clock at call time. Without this the
    fixture CSV and the validator's expectation differ by however long the test
    took to run, so every test would flake on the exact timestamp comparison.
    """
    from personal_trainer.strong import generation

    monkeypatch.setattr(generation, "_session_timestamp", lambda: SESSION_TS)


def test_generate_valid_csv_first_try(strong_service, stub_llm):
    stub_llm.responses = [
        prescription_response(),
        make_llm_response("```csv\n" + VALID_CSV + "```"),
        validation_response(),
    ]
    result = strong_service.generate("jnt_2", "I want to work out today")
    assert result.success
    assert result.csv is not None
    assert result.csv.strip() == VALID_CSV.strip()
    assert result.attempts == 1
    assert result.errors == []


def test_retry_on_validation_failure_then_succeed(strong_service, stub_llm):
    stub_llm.responses = [
        prescription_response(),
        make_llm_response("```csv\n" + BROKEN_CSV + "```"),  # attempt 1: invalid
        make_llm_response("```csv\n" + VALID_CSV + "```"),   # attempt 2: fixed
        validation_response(),
    ]
    result = strong_service.generate("jnt_2")
    assert result.success
    assert result.attempts == 2
    # The retry prompt must contain the machine-generated validation errors.
    retry_messages = stub_llm.calls[2]["messages"]
    retry_content = retry_messages[-1]["content"]
    assert "Errors" in retry_content
    assert "Weight (kg)" in retry_content
    # And it must show the model what it already produced, so it repairs rather
    # than starting the session again.
    assert "do not start again" in retry_content.lower()
    assert BROKEN_CSV.strip() in retry_content
    assert "leave every other row" in retry_content


def test_a_dropped_sets_is_caught_and_repaired(strong_service, stub_llm):
    """A well-formed CSV that silently lost three quarters of its volume.

    Regression: a live Week 1 Day 1 prescribed 4 working sets of T2a deadlift
    and emitted 1. The structural gate passed it (valid CSV), the equipment gate
    passed it (loadable weight) and the LLM validator approved it. This gate
    compares the CSV against the prescription it is a copy of.
    """
    dropped = "\n".join([
        HEADER,
        _row("Deadlift (Barbell)", "W", "60", "10"),
        _row("Deadlift (Barbell)", "1", "92.5", "10"),
    ]) + "\n"
    fixed = "\n".join([
        HEADER,
        _row("Deadlift (Barbell)", "W", "60", "10"),
        _row("Deadlift (Barbell)", "1", "92.5", "10"),
        _row("Deadlift (Barbell)", "2", "92.5", "10"),
        _row("Deadlift (Barbell)", "3", "92.5", "10"),
        _row("Deadlift (Barbell)", "4", "92.5", "10"),
    ]) + "\n"
    prescription = make_llm_response(
        '{"label": "J&T 2.0 - 1", "exercises": '
        '[{"name": "Deadlift (Barbell)", "sets": 4, "reps": 10, "weight": 92.5}], '
        '"adaptations": [], "reasoning": "T2a"}'
    )
    stub_llm.responses = [
        prescription,
        make_llm_response("```csv\n" + dropped + "```"),  # attempt 1: 1 set
        make_llm_response("```csv\n" + fixed + "```"),    # attempt 2: 4 sets
        validation_response(),
    ]
    result = strong_service.generate("jnt_2")
    assert result.success, result.errors
    assert result.attempts == 2
    retry_content = stub_llm.calls[2]["messages"][-1]["content"]
    assert "4 working sets but the CSV has 1" in retry_content


def test_past_timestamp_from_history_sample_is_rejected(strong_service, stub_llm):
    """A workout stamped with a past timestamp must not be accepted.

    Regression: the LLM used to copy the newest timestamp out of the history
    sample, which stamped planned workouts weeks in the past.
    """
    stale = VALID_CSV.replace(SESSION_TS, "2026-09-01 16:39:41")
    assert stale != VALID_CSV  # guard: the fixture timestamp must actually differ
    stub_llm.responses = [
        prescription_response(),
        make_llm_response("```csv\n" + stale + "```"),   # attempt 1: past date
        make_llm_response("```csv\n" + VALID_CSV + "```"),  # attempt 2: current
        validation_response(),
    ]
    result = strong_service.generate("jnt_2")
    assert result.success
    assert result.attempts == 2
    retry_content = stub_llm.calls[2]["messages"][-1]["content"]
    assert "Date must be exactly" in retry_content
    assert SESSION_TS in result.csv


def test_invented_time_of_day_is_rejected(strong_service, stub_llm):
    """Right date but a made-up time must still fail.

    Python supplies the timestamp precisely so the LLM cannot pick its own
    time of day, which would otherwise differ row to row.
    """
    wrong_time = VALID_CSV.replace(SESSION_TS, SESSION_TS[:10] + " 03:17:00")
    assert wrong_time != VALID_CSV
    stub_llm.responses = [
        prescription_response(),
        make_llm_response("```csv\n" + wrong_time + "```"),
        make_llm_response("```csv\n" + VALID_CSV + "```"),
        validation_response(),
    ]
    result = strong_service.generate("jnt_2")
    assert result.success
    assert result.attempts == 2
    retry_content = stub_llm.calls[2]["messages"][-1]["content"]
    assert "Date must be exactly" in retry_content
    assert "03:17:00" in retry_content  # names the wrong value it rejected


def test_generation_prompt_states_the_session_timestamp(strong_service, stub_llm):
    """The prompt must name the exact timestamp so the LLM doesn't infer it."""
    stub_llm.responses = [
        prescription_response(),
        make_llm_response("```csv\n" + VALID_CSV + "```"),
        validation_response(),
    ]
    strong_service.generate("jnt_2")
    prompt = stub_llm.calls[1]["messages"][-1]["content"]
    assert f"Session timestamp: {SESSION_TS}" in prompt


def test_gives_up_after_retry_limit(strong_service, stub_llm):
    stub_llm.responses = [
        prescription_response(),
        *[make_llm_response("```csv\n" + BROKEN_CSV + "```") for _ in range(4)],
        *[validation_response() for _ in range(4)],
    ]
    result = strong_service.generate("jnt_2")
    assert not result.success
    assert result.attempts == 4  # initial + 3 retries
    assert result.errors
    assert "failed" in result.message.lower()


def test_no_retry_without_fenced_csv(strong_service, stub_llm):
    stub_llm.responses = [
        prescription_response(),
        make_llm_response("here is your workout"),  # no fenced CSV
        make_llm_response("```csv\n" + VALID_CSV + "```"),
        validation_response(),
    ]
    result = strong_service.generate("jnt_2")
    assert result.success
    assert result.attempts == 2


def test_save_only_valid_csv(strong_service, stub_llm, storage):
    stub_llm.responses = [
        prescription_response(),
        make_llm_response("```csv\n" + VALID_CSV + "```"),
        validation_response(),
    ]
    result = strong_service.generate("jnt_2")
    assert result.success
    path = strong_service.save("jnt_2", result.csv, "J&T 2.0 - 1", "2026-09-27")
    assert path == "generated/strong/J_T_2_0_1_2026-09-27.csv"
    assert storage.exists(path)


def test_save_rejects_invalid_csv(strong_service, stub_llm, storage):
    with pytest.raises(Exception):
        strong_service.save("jnt_2", BROKEN_CSV, "J&T 2.0 - 1", "2026-09-27")
    assert not storage.list_files("generated/strong")


def test_rowing_programme_needs_no_strong_csv(strong_service, stub_llm):
    result = strong_service.generate("pete_plan_beginner")
    assert not result.success
    assert any("not a lifting programme" in e for e in result.errors)
    assert stub_llm.calls == []  # no LLM calls made


# --- Stage 2 validation must be able to reach a stable verdict -------------
#
# Without an approval standard the validator had no fixed point: it rejected the
# same 107.5kg squat as "too low" and then "too high" on consecutive attempts,
# computed 50% of 185kg deadlift as 92.5kg and rejected its own answer, and
# flagged warmup weights for being lighter than working weights. That exhausted
# every retry and left the athlete with no workout at all.


def test_validation_prompt_states_an_approval_standard(strong_service, stub_llm):
    stub_llm.responses = [
        prescription_response(),
        make_llm_response("```csv\n" + VALID_CSV + "```"),
        validation_response(),
    ]
    strong_service.generate("jnt_2")
    prompt = stub_llm.calls[-1]["messages"][-1]["content"]

    # Rounding, warmups and correct sets are explicitly not errors.
    assert "Rounding" in prompt
    assert "Never flag a rounding difference" in prompt
    assert "warmup" in prompt.lower()
    assert "lighter than a working set is correct by definition" in prompt
    # And an empty result is an expected outcome, not a failure to fill in.
    assert "Returning no errors is a valid" in prompt
    assert "expected outcome" in prompt
    # Plus a hard materiality threshold so it cannot nitpick.
    assert "2.5kg" in prompt


def test_reference_defines_the_rm_attempt_weight():
    """The J&T reference must state what an RM attempt weighs.

    It previously gave only a rep target ("10RM"), so the generator and the
    validator each invented a conversion and disagreed: the generator wrote
    110.5kg for a week 1 10RM while the validator insisted on ~97.5kg, then
    rejected 97.5, 105, 107.5, 110.5 and 117.5 across attempts.
    """
    from pathlib import Path

    reference = (
        Path(__file__).resolve().parents[1]
        / "data" / "programmes" / "J&T 2.0" / "reference.md"
    ).read_text(encoding="utf-8")

    # A formula, not just a rep target.
    assert "RM attempt weight = TM x 30 / (30 + reps)" in reference
    # The concrete number the validator kept asking for.
    assert "97.5kg" in reference
    # TM itself is defined.
    assert '### What "TM" means' in reference
    assert "estimated one-rep max" in reference
    # And the TM-vs-RM ambiguity that let drop sets drift is called out.
    assert "**% of TM**" in reference
    assert "**% of RM**" in reference
    # A 1RM is the TM itself; the inverted formula does not apply at one rep.
    assert "for a 1RM (the max IS the TM)" in reference


def test_validation_prompt_is_not_asked_to_optimise(strong_service, stub_llm):
    """Guards against the old open-ended 'are the weights reasonable' framing."""
    stub_llm.responses = [
        prescription_response(),
        make_llm_response("```csv\n" + VALID_CSV + "```"),
        validation_response(),
    ]
    strong_service.generate("jnt_2")
    prompt = stub_llm.calls[-1]["messages"][-1]["content"]
    assert "catch real errors, not to optimise" in prompt


# ------------------------------------------------------------------ day templates


def _jnt_state(**kwargs):
    from personal_trainer.models import ProgrammeState, TrainingState

    defaults = {"modality": "lifting"}
    defaults.update(kwargs)
    return TrainingState(programmes={"jnt_2": ProgrammeState(**defaults)})


def test_day_from_state_prefers_pending_label(strong_service):
    from personal_trainer.models import WorkoutRef

    state = _jnt_state(
        pending_workout=WorkoutRef(label="J&T 2.0 Block 1 Week 1 Day 3"),
        latest_confirmed_workout=WorkoutRef(
            programme_position={"week": 1, "session": "day_1"}
        ),
    )
    assert strong_service._day_from_state("jnt_2", state) == 3


def test_day_from_state_next_after_confirmed(strong_service):
    from personal_trainer.models import WorkoutRef

    state = _jnt_state(
        latest_confirmed_workout=WorkoutRef(
            programme_position={"week": 2, "session": "day_3"}
        )
    )
    assert strong_service._day_from_state("jnt_2", state) == 4

    # day 4 wraps back to day 1
    state = _jnt_state(
        latest_confirmed_workout=WorkoutRef(
            programme_position={"week": 2, "session": "day_4"}
        )
    )
    assert strong_service._day_from_state("jnt_2", state) == 1


def test_day_from_state_new_cycle_is_day_one(strong_service):
    state = _jnt_state()  # no pending, no confirmed
    assert strong_service._day_from_state("jnt_2", state) == 1


def test_generation_attaches_the_expected_day_template(strong_service, storage, stub_llm):
    """The day's template (day 1 for a fresh cycle) reaches the prescription prompt."""
    assert storage.exists("programmes/J&T 2.0/templates/day_1.csv")
    stub_llm.responses = [
        prescription_response(),
        make_llm_response("```csv\n" + VALID_CSV + "```"),
        validation_response(),
    ]
    assert strong_service.generate("jnt_2", "I want to work out today").success

    prescription_prompt = stub_llm.calls[0]["messages"][-1]["content"]
    assert "## Day template" in prescription_prompt
    assert "J&T 2.0 - Week {x} Day 1" in prescription_prompt

    generation_prompt = stub_llm.calls[1]["messages"][-1]["content"]
    assert "## Day template" in generation_prompt


def test_day_templates_are_compliant_fill_in_skeletons(storage):
    """Templates are pure Strong-CSV skeletons: fixed exercises/sets, NO weights.

    Guards the intent: the agent fills in weights/reps from the reference; the
    template never bakes them in. Each file is literally CSV (header first,
    no markdown), every row is 15 columns with blank Weight and Reps, the
    Workout column is templated "J&T 2.0 - Week {x} Day N" (day fixed, week the
    only variable), and warmup rows rest 60s.
    """
    import csv as _csv

    for day in range(1, 5):
        text = storage.read(f"programmes/J&T 2.0/templates/day_{day}.csv")
        lines = text.splitlines()
        assert lines[0].startswith("Date,"), f"day {day}: not pure CSV"
        rows = list(_csv.reader(lines[1:]))
        assert len(rows) == 24, f"day {day} expected 24 fixed rows"
        exercises = []
        for vals in rows:
            assert len(vals) == 15, vals
            assert vals[7] == "", f"day {day}: template contains a weight"
            assert vals[8] == "", f"day {day}: template contains reps"
            assert vals[2] == f"J&T 2.0 - Week {{x}} Day {day}", (
                f"day {day}: Workout not a week-only fill-in template"
            )
            if vals[6] == "W":
                assert vals[12] == "60", f"day {day}: warmup rest is not 60s"
            exercises.append(vals[5])
        # T1 (7 rows incl 3 warmups), T2a (5 rows incl 1 warmup), then 4 x 3-set MRS
        assert exercises[0] == exercises[1] == exercises[2]  # 3 T1 warmups
        assert "W" in [r[6] for r in rows[:7]]               # T1 warmups present
        assert exercises[7] == exercises[8]                  # T2a warmup + first set
        assert len({exercises[i] for i in range(12, 24)}) == 4  # four 3-set MRS runs
