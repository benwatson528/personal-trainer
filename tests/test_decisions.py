"""Decisions, overrides and post-workout reconciliation (spec sections 17, 28, 29)."""

from conftest import DATA_DIR

from personal_trainer.models import Programme, WorkoutRef
from personal_trainer.programmes.manager import ProgrammeManager
from personal_trainer.pt.decisions import reconcile_completion

# Reconciliation only reads the Date and Workout columns.
HISTORY = (
    "Date,Workout\n"
    "2026-06-22 18:00:00,J&T 2.0 - 3\n"
    "2026-06-23 18:00:00,J&T 2.0 - 4\n"
)


def lifting_programme():
    return Programme(
        id="jnt_2", name="J&T 2.0",
        history_file="history/strong/strong_history.csv",
    )


def test_reconcile_confirms_pending_prescription(state_manager):
    """Spec section 28: expected workout exists in history -> confirmed."""
    # Last confirmed was the previous workout; the pending one is newer.
    state_manager.update_latest_confirmed(
        "jnt_2", WorkoutRef(id="2026-06-22:J&T 2.0 - 3", date="2026-06-22", label="J&T 2.0 - 3")
    )
    state_manager.set_pending(
        "jnt_2", WorkoutRef(id="2026-06-23:J&T 2.0 - 4", date="2026-06-23", label="J&T 2.0 - 4")
    )
    result = reconcile_completion(
        lifting_programme(), state_manager.load(), HISTORY
    )
    assert result.status == "confirmed"
    assert result.found_label == "J&T 2.0 - 4"
    assert result.found_date == "2026-06-23"


def test_reconcile_does_not_advance_without_expected_workout(state_manager):
    """Spec section 29: expected workout missing -> do NOT advance."""
    # Set last confirmed to the newest J&T workout so pending must be newer.
    state_manager.update_latest_confirmed(
        "jnt_2", WorkoutRef(id="2026-06-23:J&T 2.0 - 4", date="2026-06-23", label="J&T 2.0 - 4")
    )
    state_manager.set_pending(
        "jnt_2", WorkoutRef(id="x", date="2026-09-27", label="J&T 2.0 - 1")
    )
    result = reconcile_completion(
        lifting_programme(), state_manager.load(), HISTORY
    )
    assert result.status == "mismatch"
    # The newest workout in the history is J&T 2.0 - 4, not the pending one.
    assert result.found_label == "J&T 2.0 - 4"
    assert "haven't advanced" in result.message
    # State must be unchanged.
    assert state_manager.get_programme_state("jnt_2").latest_confirmed_workout.label == "J&T 2.0 - 4"


def test_reconcile_confirms_newer_workout_without_pending(state_manager):
    """No pending prescription: a newest workout newer than last confirmed is new."""
    state_manager.update_latest_confirmed(
        "jnt_2", WorkoutRef(id="old", date="2026-06-19", label="J&T 2.0 - 3")
    )
    result = reconcile_completion(
        lifting_programme(), state_manager.load(), HISTORY
    )
    assert result.status == "confirmed"
    # Newest workout in the history is 2026-06-23 J&T 2.0 - 4.
    assert result.found_label == "J&T 2.0 - 4"
    assert result.found_date == "2026-06-23"


def test_reconcile_nothing_new_when_history_unchanged(state_manager):
    # Confirm the actual newest workout, so a second pass finds nothing new.
    state_manager.update_latest_confirmed(
        "jnt_2", WorkoutRef(id="2026-06-23:J&T 2.0 - 4", date="2026-06-23", label="J&T 2.0 - 4")
    )
    result = reconcile_completion(
        lifting_programme(), state_manager.load(), HISTORY
    )
    assert result.status == "not_found"
    assert "Nothing new to reconcile" in result.message


def test_reconcile_handles_missing_history(state_manager):
    programme = Programme(
        id="jnt_2", name="J&T 2.0",
        history_file="history/strong/missing.csv",
    )
    result = reconcile_completion(programme, state_manager.load(), None)
    assert result.status == "no_history"
    assert "export" in result.message.lower()


def test_programme_manager_resolves_active(state_manager):
    pm = ProgrammeManager.from_file(DATA_DIR / "config" / "programmes.yaml")
    active = pm.resolve_active(state_manager.load())
    assert {p.id for p in active} == {"jnt_2"}


def test_programme_manager_marks_inactive(state_manager, programme_manager):
    state = state_manager.load()
    state.programmes["jnt_2"].active = False
    state_manager.save(state)
    active = programme_manager.resolve_active(state_manager.load())
    assert [p.id for p in active] == []