"""Action validation — LLM guardrails (spec sections 19, 37).

The LLM may reason and propose actions; Python validates important outputs
before they are executed: valid programme/session, permitted adaptation,
required workout information, allowed file operations.
"""

from __future__ import annotations

from ..models import AdaptationPolicy


class ActionValidationError(Exception):
    pass


def validate_programme_id(programme_id: str, active_ids: list[str]) -> None:
    if programme_id not in active_ids:
        raise ActionValidationError(
            f"Programme '{programme_id}' is not active. Active: {active_ids}"
        )


def validate_adaptation(
    adaptations: list[dict],
    policy: AdaptationPolicy,
) -> None:
    """Check proposed adaptations are permitted by the adaptation policy.

    Magnitudes are not capped by fixed percentages — the agent determines
    appropriate adaptation sizes based on the athlete's history and context.
    """
    for adaptation in adaptations or []:
        kind = (adaptation.get("type") or "").lower()
        if kind in ("load", "load_adjustment"):
            if not policy.allow_load_adjustment:
                raise ActionValidationError("Load adjustment is not permitted by policy.")
        elif kind in ("rep", "rep_adjustment"):
            if not policy.allow_rep_adjustment:
                raise ActionValidationError("Rep adjustment is not permitted by policy.")
        elif kind in ("volume", "volume_adjustment"):
            if not policy.allow_volume_adjustment:
                raise ActionValidationError("Volume adjustment is not permitted by policy.")
        elif kind in ("rest", "rest_adjustment"):
            if not policy.allow_rest_adjustment:
                raise ActionValidationError("Rest adjustment is not permitted by policy.")
        elif kind in ("substitution", "exercise_substitution"):
            if not policy.allow_exercise_substitution:
                raise ActionValidationError("Exercise substitution is not permitted by policy.")
        else:
            raise ActionValidationError(f"Unknown adaptation type: {kind!r}")


def validate_prescription(prescription: dict) -> None:
    """A workout prescription must carry the required workout information."""
    if not prescription:
        raise ActionValidationError("Prescription is empty.")
    exercises = prescription.get("exercises")
    if not exercises or not isinstance(exercises, list):
        raise ActionValidationError("Prescription must include a non-empty 'exercises' list.")
    for exercise in exercises:
        if not exercise.get("name"):
            raise ActionValidationError("Every prescription exercise needs a name.")
        if exercise.get("sets") is None or exercise.get("reps") is None:
            raise ActionValidationError(
                f"Exercise '{exercise.get('name')}' is missing sets or reps."
            )
