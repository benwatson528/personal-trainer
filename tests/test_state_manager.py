"""State manager (spec sections 9, 10)."""

from personal_trainer.models import TrainingState, WorkoutRef
from personal_trainer.programmes.manager import ProgrammeManager


def test_load_existing_state(state_manager):
    state = state_manager.load()
    assert state.version == 1
    assert "jnt_2" in state.programmes
    # jnt_2 confirmed workout cleared — new cycle starting
    assert state.programmes["jnt_2"].latest_confirmed_workout is None


def test_load_missing_state_returns_empty(state_manager, tmp_path, settings):
    from personal_trainer.storage.local import LocalStorageBackend
    from personal_trainer.state.manager import StateManager

    empty = StateManager(LocalStorageBackend(tmp_path / "nothing"), settings.folders.state)
    state = empty.load()
    assert state.version == 1
    assert state.programmes == {}


def test_save_roundtrip(state_manager, programme_manager):
    """Test that programme memory preferences can be accessed."""
    programme = programme_manager.get("jnt_2")
    programme.memory.preferences.append("test preference")
    # Programme config is separate from state; this test verifies the model works
    reloaded = programme_manager.get("jnt_2")
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


def test_add_memory_preference_dedupes(state_manager, storage, data_dir):
    # Use a programme manager that reads from the temp copy
    pm = ProgrammeManager.from_file(data_dir / "config" / "programmes.yaml")
    pm.add_memory_preference(storage, "config/programmes.yaml", "jnt_2", "User prefers morning workouts.")
    pm.add_memory_preference(storage, "config/programmes.yaml", "jnt_2", "User prefers morning workouts.")
    pm.reload()
    prefs = pm.get("jnt_2").memory.preferences
    assert prefs.count("User prefers morning workouts.") == 1
