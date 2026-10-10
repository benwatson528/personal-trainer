"""State manager (spec sections 9, 10)."""

from personal_trainer.models import TrainingState, WorkoutRef


def test_load_existing_state(state_manager):
    state = state_manager.load()
    assert state.version == 1
    assert "jnt_2" in state.programmes
    # jnt_2 confirmed workout cleared — new cycle starting
    assert state.programmes["jnt_2"].latest_confirmed_workout is None
    # pete plan tracks programme position
    pete = state.programmes["pete_plan_beginner"]
    assert pete.latest_confirmed_workout.programme_position.week == 8
    assert pete.latest_confirmed_workout.programme_position.session == "optional_day_4"


def test_load_missing_state_returns_empty(state_manager, tmp_path, settings):
    from personal_trainer.storage.local import LocalStorageBackend
    from personal_trainer.state.manager import StateManager

    empty = StateManager(LocalStorageBackend(tmp_path / "nothing"), settings.folders.state)
    state = empty.load()
    assert state.version == 1
    assert state.programmes == {}


def test_save_roundtrip(state_manager):
    state = state_manager.load()
    state.memory.preferences.append("test preference")
    state_manager.save(state)
    reloaded = state_manager.load()
    assert "test preference" in reloaded.memory.preferences


def test_update_latest_confirmed(state_manager):
    ref = WorkoutRef(id="2026-09-27:J&T 2.0 - 1", date="2026-09-27", label="J&T 2.0 - 1")
    state_manager.update_latest_confirmed("jnt_2", ref)
    cached = state_manager.get_programme_state("jnt_2")
    assert cached.latest_confirmed_workout.id == "2026-09-27:J&T 2.0 - 1"


def test_update_unknown_programme_raises(state_manager):
    import pytest

    with pytest.raises(KeyError):
        state_manager.update_latest_confirmed("unknown", WorkoutRef())


def test_add_memory_preference_dedupes(state_manager):
    state_manager.add_memory_preference("User prefers flexible training days.")
    state_manager.add_memory_preference("User prefers flexible training days.")
    prefs = state_manager.load().memory.preferences
    assert prefs.count("User prefers flexible training days.") == 1
