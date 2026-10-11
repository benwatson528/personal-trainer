"""Domain models for PT state, programmes, sessions and decisions.

The training_state.yaml is derived/cache state, not the historical source of
truth (spec section 3). These models mirror the YAML shape from spec section 9
but allow extra fields so the cache can evolve without migration pain.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ProgrammePosition(BaseModel):
    """Where in a programme a confirmed workout sits.

    For Pete Plan: week=8, session="optional_day_4".
    For J&T 2.0: week=3, session="day_1" (T1 lower A).
    """

    week: int | None = None
    session: str | None = None
    description: str | None = None


class WorkoutRef(BaseModel):
    """Pointer to a specific workout in a history file."""

    id: str | None = None
    date: str | None = None
    label: str | None = None
    programme_position: ProgrammePosition | None = None


class ProgrammeState(BaseModel):
    """Per-programme cached state inside training_state.yaml.

    Only state that changes per workout. Programme config (reference,
    completed_workouts_folder, generated_folder, templates_folder) lives
    in the Programme model from programmes.yaml.
    """

    active: bool = True
    latest_confirmed_workout: WorkoutRef | None = None
    pending_workout: WorkoutRef | None = None  # prescribed, awaiting completion
    last_workout_hash: str | None = None  # for duplicate detection


class TrainingPreferences(BaseModel):
    desired_sessions_per_week: dict[str, int] = Field(default_factory=dict)
    fixed_weekdays: bool = False
    double_sessions_preferred: bool = False


class AdaptationPolicy(BaseModel):
    """Configurable boundaries for controlled adaptation (spec section 19).

    Fixed percentage limits removed — the agent determines appropriate
    adaptation magnitudes based on the athlete's history and context.
    """

    allow_load_adjustment: bool = True
    allow_rep_adjustment: bool = True
    allow_volume_adjustment: bool = True
    allow_rest_adjustment: bool = True
    allow_exercise_substitution: bool = True


class CoachingMemory(BaseModel):
    """Durable training memory: only what is useful for future decisions."""

    preferences: list[str] = Field(default_factory=list)


class TrainingState(BaseModel):
    version: int = 1
    training_preferences: TrainingPreferences = Field(default_factory=TrainingPreferences)
    programmes: dict[str, ProgrammeState] = Field(default_factory=dict)
    adaptation_policy: AdaptationPolicy = Field(default_factory=AdaptationPolicy)
    memory: CoachingMemory = Field(default_factory=CoachingMemory)

    model_config = {"extra": "allow"}


class Programme(BaseModel):
    """Static programme definition from config/programmes.yaml."""

    id: str
    name: str
    reference: str | None = None
    # Folder where completed Strong CSVs are uploaded (one file per workout).
    completed_workouts_folder: str | None = None
    generated_folder: str | None = None
    # Folder holding one fill-in template per session day (e.g.
    # "programmes/J&T 2.0/templates/day_1.md"). Optional.
    templates_folder: str | None = None
    # SHA256 hash of the most recent workout's exercise data (set by summarise_session).
    # Used to detect duplicate CSV uploads (Strong exports cumulative history).
    last_workout_hash: str | None = None


class ReconciliationResult(BaseModel):
    """Outcome of post-workout state reconciliation (spec section 28)."""

    programme_id: str
    status: Literal["confirmed", "not_found", "mismatch", "no_history"]
    expected_label: str | None = None
    found_label: str | None = None
    found_date: str | None = None
    found_id: str | None = None
    message: str
