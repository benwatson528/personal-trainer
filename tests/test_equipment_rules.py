"""Equipment enforcement: reject workouts the athlete cannot physically load."""

from __future__ import annotations

import itertools

import pytest
from conftest import DATA_DIR, SESSION_TIMESTAMP

from personal_trainer.equipment import Equipment, Plate
from personal_trainer.strong.equipment_rules import (
    implement_of,
    is_warmup,
    validate_equipment,
)

HEADER = (
    "Date,Timezone,Workout,Workout Duration (s),Group,Exercise,Set,"
    "Weight (kg),Reps,RPE,Distance (m),Duration (s),Rest Timer (s),"
    "Workout Notes,Exercise Notes"
)


def _row(exercise: str, set_id: str, weight: str, reps: str = "5") -> str:
    return ",".join([
        SESSION_TIMESTAMP, "Europe/London", "J&T 2.0 Day 1", "1", "",
        exercise, set_id, weight, reps, "", "", "", "180", "", "",
    ])


def _csv(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


@pytest.fixture
def kit() -> Equipment:
    """Mirrors the shape of the real inventory: 0.5 and 1.25kg plates."""
    return Equipment(
        bars={"barbell": 20.0, "ez_bar": 8.0},
        plates=[
            Plate(25.0, 2), Plate(20.0, 6), Plate(15.0, 2), Plate(10.0, 4),
            Plate(5.0, 4), Plate(2.5, 4), Plate(1.25, 4), Plate(0.5, 2),
        ],
        dumbbells=[40.0, 24.0, 22.5, 20.0, 17.5, 12.5, 10.0, 5.0, 2.5],
    )


# --- implement detection ----------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("Squat (Barbell)", "Barbell"),
    ("Hammer Curl (Dumbbell)", "Dumbbell"),
    ("Skullcrusher (Barbell)", "Barbell"),
    ("Chest Supported Row (Dumbbell)", "Dumbbell"),
    ("Tricep Kickback Dumbbell", "Dumbbell"),
    ("Leg Press Machine", "Machine"),
    ("T Bar Row", None),
    ("Pull Up", None),
    ("Box Squat (Barbell)", "Barbell"),
])
def test_implement_is_read_from_the_strong_name(name, expected):
    assert implement_of(name) == expected


@pytest.mark.parametrize("name", ["Barbell Row", "Dumbbell Press", "Cable Fly"])
def test_bare_implement_only_matches_a_trailing_whole_word(name):
    """A name that merely starts with an implement word is not an implement."""
    assert implement_of(name) is None


def test_unknown_implement_is_never_guessed(kit):
    """No suffix means no enforcement — a false rejection is worse than a gap."""
    errors = validate_equipment(_csv(_row("T Bar Row", "1", "60")), kit)
    assert errors == []


# --- dumbbells --------------------------------------------------------------

def test_unowned_dumbbell_is_rejected_with_a_suggestion(kit):
    errors = validate_equipment(_csv(_row("Hammer Curl (Dumbbell)", "1", "23")), kit)
    assert len(errors) == 1
    assert "23" in errors[0]
    assert "22.5kg" in errors[0], "must suggest a weight the user owns"
    assert "40" in errors[0], "must list what is available"


def test_owned_dumbbell_is_accepted(kit):
    assert validate_equipment(_csv(_row("Hammer Curl (Dumbbell)", "1", "22.5")), kit) == []


def test_one_error_per_exercise_not_per_set(kit):
    """Four bad sets is one problem, not four — keeps the retry prompt short."""
    rows = [_row("Hammer Curl (Dumbbell)", str(n), "23") for n in range(1, 5)]
    assert len(validate_equipment(_csv(*rows), kit)) == 1


# --- barbell plate maths ----------------------------------------------------

def test_unloadable_barbell_weight_is_rejected(kit):
    """110.5kg needs a 0.25kg increment per side, which is not owned.

    This is the weight the agent actually generated for a week 1 squat.
    """
    errors = validate_equipment(_csv(_row("Squat (Barbell)", "1", "110.5")), kit)
    assert len(errors) == 1
    assert "cannot be loaded" in errors[0]
    assert "110.5kg" in errors[0]


@pytest.mark.parametrize("weight", [20, 21, 22.5, 25, 30, 40, 60, 100, 140, 200])
def test_loadable_barbell_weights_are_accepted(kit, weight):
    assert validate_equipment(_csv(_row("Squat (Barbell)", "1", str(weight))), kit) == []


@pytest.mark.parametrize("weight", [19, 60.5, 7.5])
def test_impossible_barbell_weights_are_rejected(kit, weight):
    assert validate_equipment(_csv(_row("Squat (Barbell)", "1", str(weight))), kit)


def test_barbell_math_matches_brute_force(kit):
    """Cross-check the bounded knapsack against exhaustive enumeration.

    An in-place knapsack silently over-uses a plate size — four 1.25kg plates
    became five per side — which made unbuildable weights look reachable.
    Exhaustively enumerating every valid plate combination is the ground truth.
    """
    sizes = [(p.weight, p.count // 2) for p in kit.plates if p.count >= 2]

    def brute(per_side: float) -> bool:
        for combo in itertools.product(*[range(c + 1) for _, c in sizes]):
            if abs(sum(n * w for (w, _), n in zip(sizes, combo)) - per_side) < 1e-9:
                return True
        return False

    mismatches = []
    for step in range(0, 200):
        target = 20 + step * 0.25
        errors = validate_equipment(
            _csv(_row("Squat (Barbell)", "1", f"{target:g}")), kit)
        expected_ok = brute(step * 0.25 / 2)
        if (errors == []) != expected_ok:
            mismatches.append((target, errors == [], expected_ok))
    assert mismatches == [], f"validator disagrees with brute force: {mismatches[:5]}"


# --- exemptions ------------------------------------------------------------

def test_warmups_are_never_rejected(kit):
    """A barbell warmup is often a single 20kg bar, which is loadable anyway,
    but a light dumbbell warmup must not be judged against working weights."""
    rows = [
        _row("Squat (Barbell)", "W", "20"),
        _row("Squat (Barbell)", "W", "60"),
        _row("Hammer Curl (Dumbbell)", "W", "7.5"),  # not an owned dumbbell
    ]
    assert validate_equipment(_csv(*rows), kit) == []


def test_is_warmup_recognises_strong_markers():
    assert is_warmup("W")
    assert is_warmup("w")
    assert is_warmup("W1")
    assert not is_warmup("1")
    assert not is_warmup("")


def test_empty_inventory_disables_the_check():
    """A missing config must not block workouts."""
    csv_text = _csv(_row("Hammer Curl (Dumbbell)", "1", "23"))
    assert validate_equipment(csv_text, Equipment()) == []


def test_ez_bar_weight_is_not_judged_against_the_barbell(kit):
    """An 8kg EZ curl bar is a different implement; only 'Barbell' is checked."""
    errors = validate_equipment(_csv(_row("Skullcrusher (Barbell)", "1", "20")), kit)
    assert errors == []


def test_malformed_csv_does_not_crash():
    assert validate_equipment("not,a,csv\n1,2,3\n", Equipment(
        bars={"barbell": 20.0}, dumbbells=[10.0])) == []


def test_real_inventory_loads_itself():
    """Sanity check on the file the athlete actually edits."""
    from personal_trainer.equipment import load_equipment

    equipment = load_equipment(DATA_DIR / "config" / "equipment.yaml")
    assert not equipment.is_empty
    # Every dumbbell the model could pick must be in the file.
    for weight in (40, 22.5, 10):
        assert validate_equipment(
            _csv(_row("Hammer Curl (Dumbbell)", "1", str(weight))), equipment) == []


def test_equipment_errors_reach_the_generation_loop(strong_service, stub_llm):
    """The gate must run inside generate(), not just exist as a function."""
    from test_strong_generation import prescription_response, validation_response
    from conftest import make_llm_response

    good = "\n".join([
        HEADER,
        _row("Squat (Barbell)", "1", "100", "5"),
    ]) + "\n"
    bad = "\n".join([
        HEADER,
        _row("Squat (Barbell)", "1", "110.5", "5"),
    ]) + "\n"
    stub_llm.responses = [
        prescription_response(),
        make_llm_response("```csv\n" + bad + "```"),
        make_llm_response("```csv\n" + good + "```"),
        validation_response(),
    ]
    result = strong_service.generate("jnt_2")
    assert result.success, result.errors
    # It had to retry, so the equipment gate rejected the first attempt.
    assert result.attempts == 2
    retry_prompt = stub_llm.calls[2]["messages"][-1]["content"]
    assert "cannot be loaded" in retry_prompt