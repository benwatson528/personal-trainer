"""PT tools — the known capabilities the LLM operates through (spec section 38).

The LLM reasons and proposes; Python executes and validates. Each tool has a
JSON-schema input contract and an executor returning a JSON-serialisable
result that is fed back to the LLM.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime
from typing import Any, Callable

from ..integrations.llm import LLMClient
from ..programmes.manager import ProgrammeManager
from ..config import Settings
from ..sessions import SessionSummaries
from ..state.manager import StateManager
from ..storage.base import StorageBackend
from ..strong.generation import StrongGenerationService
from ..strong.validation import validate_strong_csv
from ..validation.actions import (
    ActionValidationError,
    validate_adaptation,
    validate_prescription,
    validate_programme_id,
)
from . import context as ctx


class ToolError(Exception):
    pass


_SESSION_SUMMARY_SYSTEM = """You condense a completed lifting session into a compact record.

Return a YAML mapping with a `workout` key naming the session as the rows record
it, then one entry per exercise holding that exercise's sets in the order
performed, written weight x reps and each set in single quotes:

    workout: 'J&T 2.0 - 4'
    Squat (Barbell): ['60x5', '80x3', '97.5x10', '91x6', '91x6', '91x6']
    Hammer Curl (Dumbbell): ['20x20', '20x20', '20x20']

Quote every set. Unquoted, YAML reads 0x8 as the number 8, which silently ruins
any bodyweight set. Quote the workout name too if it contains &, : or #.

Notes matter as much as the weights. The export carries a note on the workout and
a note on individual exercises — cues, grip, pain, what to fix. Someone planning
the next session needs those, so carry them across verbatim:

    workout_notes: 'bar speed slow on last set, back felt tight'
    notes:
      Pull Up: 'narrow grip'
      Squat (Barbell): 'knees caved, brace harder'

If the athlete's own words about how the session went are given to you, add them
to `workout_notes` too, verbatim. They are the athlete's feedback and are as
important as anything in the export.

Rules:
- Copy the workout name from the rows exactly. Do not invent or correct it, and
  do not guess it from the session date.
- Include every set that was performed, including warmups. Keep them in the
  order they were done.
- Write weight in kg exactly as recorded, with no unit suffix and no rounding.
- Reps as an integer. If a set's reps are blank, write the weight with a 0 rep
  count rather than inventing a number.
- Use the exercise names exactly as they appear in the rows.
- One entry per exercise. If an exercise appears on non-adjacent rows, merge
  them into one list in the order performed.
- Reproduce every note word for word. Do not summarise, reword or invent one,
  and omit the `notes` block entirely when there are none. Do not judge the
  session, and do not add advice of your own.
- Return only the YAML inside a fenced ```yaml code block."""


def _tail_rows(csv_text: str, n: int) -> str:
    """The last `n` data rows plus the header, as text.

    Reads rows only to re-emit them verbatim for the model. The columns are
    never interpreted here — a missing or renamed column changes nothing about
    what the model receives.
    """
    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        return ""
    header, data = rows[0], rows[1:]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(data[-n:] if n > 0 else data)
    return out.getvalue().rstrip("\n")


def _extract_yaml(text: str) -> str:
    """Pull the contents of a fenced ```yaml block out of a model response."""
    match = re.search(r"```(?:yaml|yml)?\s*\n(.*?)```", text, re.DOTALL)
    return match.group(1).strip() if match else ""


def _extract_latest_workout(csv_text: str) -> dict:
    """
    Extract the most recent workout from a Strong CSV export.

    Strong appends workouts to the bottom, so the last workout name in the file
    is the most recent one. We find that workout name, then return all rows
    matching it (plus the header).
    """
    import csv as csv_module
    import io

    reader = csv_module.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return {"workout_name": "", "rows": [], "header": []}

    # The last row's Workout column is the most recent workout
    last_workout = rows[-1].get("Workout", "").strip()
    if not last_workout:
        return {"workout_name": "", "rows": [], "header": []}

    # Get all rows for that workout (in case it spans multiple rows)
    workout_rows = [r for r in rows if r.get("Workout", "").strip() == last_workout]
    header = list(rows[0].keys()) if rows else []

    return {
        "workout_name": last_workout,
        "rows": workout_rows,
        "header": header,
    }


def _workout_rows_to_text(extracted: dict, max_rows: int = 40) -> str:
    """Convert extracted workout rows to CSV text for the LLM prompt."""
    if not extracted["rows"]:
        return ""
    import csv as csv_module
    import io

    out = io.StringIO()
    writer = csv_module.writer(out, lineterminator="\n")
    writer.writerow(extracted["header"])
    for row in extracted["rows"][-max_rows:]:
        writer.writerow([row.get(h, "") for h in extracted["header"]])
    return out.getvalue().rstrip("\n")


def _recommend_note(has_history: bool) -> str:
    """Guidance for reading the recommendation payload.

    Split on whether history was sent, because instructing the model to cite
    `recent_history_md` when the key is absent just produces an instruction it
    cannot follow.
    """
    parts = [
        "programme_positions shows the LAST COMPLETED session for each programme. "
        "If last_completed is null, the user is starting a new cycle — recommend "
        "Week 1 Day 1 from the programme reference. "
        "CRITICAL: null last_completed means START FROM WEEK 1 DAY 1, A NEW CYCLE. "
        "Do NOT ask the user to confirm anything — just recommend and generate. "
        "Use programme_positions to determine where the user is in the programme. "
        "Read the programme reference document (via read_reference tool) to find "
        "the exact next session based on programme position. "
        "If last_completed has a position, the next session is the one AFTER it.",
    ]
    if has_history:
        parts.append(
            "Do NOT use recent_history_md for programme position — it shows PREVIOUS "
            "cycles. recent_history_md is only for: (1) building advice, (2) setting "
            "specific weights. When recommending, cite actual recent sessions — e.g. "
            "'your pace has been improving so hold the same pace for longer'. "
            "Do NOT pattern-match on recent history distances."
        )
    else:
        parts.append(
            "No history was sent with this call, because the modality decides it and "
            "lifting history is not what you are planning. Base the recommendation on "
            "the programme reference and programme position alone. If you later need "
            "lifting history to set weights, the read_history tool will fetch it."
        )
    parts.append(
        "When recommending, say where the next session sits in the programme "
        "(e.g. 'Week 1 Day 1'). "
        "If strong_generation.adaptations is non-empty, tell the user what you changed "
        "and why, distinguishing it from what the programme prescribed. If it is "
        "empty, the workout is programmed verbatim."
    )
    return " ".join(parts)


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_state",
            "description": "Read the current training state YAML (cached pointers, preferences, adaptation policy).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_programme",
            "description": "Read a programme's reference document and cached state.",
            "parameters": {
                "type": "object",
                "properties": {"programme_id": {"type": "string"}},
                "required": ["programme_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_history",
            "description": "Read recent workout history for a lifting programme. Optionally filter by exercise name to find specific lift progression.",
            "parameters": {
                "type": "object",
                "properties": {
                    "programme_id": {"type": "string"},
                    "max_rows": {"type": "integer", "default": 40},
                    "exercise": {"type": "string", "description": "Filter to only this exercise (e.g. 'Squat (Barbell)')"}
                },
                "required": ["programme_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_reference",
            "description": "Read the raw programme reference document (for LLM reasoning).",
            "parameters": {
                "type": "object",
                "properties": {"programme_id": {"type": "string"}},
                "required": ["programme_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_session",
            "description": (
                "Recommend today's session. You MUST call this for any 'what next' / "
                "'what should I do today' question rather than answering from history. "
                "Considers active programmes, programme position, recent training balance, "
                "recovery/gaps and preferences. Generates a Strong CSV, validates it, "
                "saves it to generated/strong/ and returns saved_path — so the user gets "
                "an importable file, not prose. Returns the recommendation, the reasoning, "
                "and alternatives."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_request": {"type": "string", "default": ""},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "adapt_workout",
            "description": (
                "Propose a small, explicit adaptation to a programmed workout (load, reps, "
                "volume, rest, permitted substitution). Python validates "
                "the adaptation against the adaptation policy before it is accepted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "programme_id": {"type": "string"},
                    "prescription": {"type": "object"},
                    "adaptations": {"type": "array", "items": {"type": "object"}},
                    "reason": {"type": "string"},
                },
                "required": ["programme_id", "prescription", "adaptations", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_strong_workout",
            "description": (
                "Generate the next fully populated Strong-compatible CSV for a lifting "
                "programme. Python validates the CSV and sends validation errors back to "
                "the LLM for correction, retrying up to the configured limit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "programme_id": {"type": "string"},
                    "user_request": {"type": "string", "default": ""},
                },
                "required": ["programme_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_strong_csv",
            "description": "Validate a candidate Strong CSV against the schema derived from the real Strong history.",
            "parameters": {
                "type": "object",
                "properties": {"csv": {"type": "string"}},
                "required": ["csv"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_strong_workout",
            "description": "Save an already-validated Strong CSV to the generated/strong folder in storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "programme_id": {"type": "string"},
                    "csv": {"type": "string"},
                    "label": {"type": "string"},
                    "date": {"type": "string"},
                },
                "required": ["programme_id", "csv", "label", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reconcile_workout",
            "description": (
                "Reconcile a completed workout after the user says they finished. Reads the "
                "current history file, identifies the newest workout, compares it against the "
                "expected/prescribed workout and reports whether programme position can advance. "
                "Never advances state on unsupported evidence."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "programme_id": {"type": "string"},
                    "programme_position": {
                        "type": "object",
                        "description": "Where in the programme this workout sits (e.g. week 8, optional_day_4).",
                        "properties": {
                            "week": {"type": "integer"},
                            "session": {"type": "string"},
                            "description": {"type": "string"},
                        },
                    },
                },
                "required": ["programme_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "summarise_session",
            "description": (
                "Record a completed lifting session: condense the uploaded Strong CSV "
                "for that date into a compact session YAML that becomes the training "
                "history. Pass the athlete's own words about how it went as `feedback` "
                "and they are saved with the session. Safe to repeat for the same date."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "programme_id": {"type": "string"},
                    "date": {
                        "type": "string",
                        "description": "Date the session was trained, YYYY-MM-DD.",
                    },
                    "workout": {
                        "type": "string",
                        "description": "Workout name as the app recorded it.",
                    },
                    "feedback": {
                        "type": "string",
                        "description": (
                            "The athlete's own words about how the session went — "
                            "difficulty, form, niggles, what to change next time. "
                            "Stored verbatim as notes on the session."
                        ),
                    },
                },
                "required": ["programme_id", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_programme_position",
            "description": (
                "Set where the user is in a programme when the cached pointer is wrong — "
                "typically because they trained without using this PT. Use this whenever "
                "the user tells you they have done sessions you have not seen ('I did last "
                "week's sessions at the gym', 'I finished week 6', 'I skipped ahead'). "
                "Read the programme reference returned by this tool to map what the user "
                "described onto the correct week and session id, then call it. Also clears "
                "any stale pending prescription, since sessions done outside the PT "
                "invalidate the workout this PT was waiting for."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "programme_id": {
                        "type": "string",
                        "description": "Programme whose position is being corrected.",
                    },
                    "week": {
                        "type": "integer",
                        "description": "Week number the user has just completed.",
                    },
                    "session": {
                        "type": "string",
                        "description": (
                            "Session id for that week, taken from the programme reference. "
                            "J&T 2.0 uses 'day_1'..'day_4'. Pete Plan uses 'day_1'..'day_3' "
                            "and 'optional_day_1'..'optional_day_4'."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "What was actually done, e.g. 'lower day, squatted heavy'.",
                    },
                    "date": {
                        "type": "string",
                        "description": "Date of the last session (YYYY-MM-DD). Defaults to today.",
                    },
                    "label": {
                        "type": "string",
                        "description": "Workout name for the pointer, e.g. 'J&T 2.0 Day 1'.",
                    },
                    "out_of_cycle": {
                        "type": "boolean",
                        "description": (
                            "Set true when the sessions done were NOT part of the programme "
                            "(e.g. an off-plan easy row after illness). Leaves the programme "
                            "position untouched instead of advancing it."
                        ),
                    },
                },
                "required": ["programme_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_state",
            "description": (
                "Update cached training state (e.g. confirm a completed workout, add a durable "
                "memory preference). To correct WHERE the user is in a programme, use "
                "set_programme_position instead — it also clears stale pending prescriptions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "programme_id": {"type": "string"},
                    "confirmed_workout": {"type": "object"},
                    "add_memory": {"type": "string"},
                },
                "required": ["programme_id"],
            },
        },
    },
]


class ToolExecutor:
    """Dispatches tool calls to the PT services, validating inputs and outputs."""

    def __init__(
        self,
        storage: StorageBackend,
        state_manager: StateManager,
        programme_manager: ProgrammeManager,
        strong_generation: StrongGenerationService,
        settings: Settings,
        llm: LLMClient,
    ):
        self._storage = storage
        self._state = state_manager
        self._programmes = programme_manager
        self._strong = strong_generation
        self._settings = settings
        self._llm = llm
        self._sessions = SessionSummaries(storage, settings.folders.sessions)
        self._handlers: dict[str, Callable] = {
            "read_state": self._read_state,
            "read_programme": self._read_programme,
            "read_history": self._read_history,
            "read_reference": self._read_reference,
            "recommend_session": self._recommend_session,
            "adapt_workout": self._adapt_workout,
            "generate_strong_workout": self._generate_strong_workout,
            "validate_strong_csv": self._validate_strong_csv,
            "save_strong_workout": self._save_strong_workout,
            "reconcile_workout": self._reconcile_workout,
            "summarise_session": self._summarise_session,
            "update_state": self._update_state,
            "set_programme_position": self._set_programme_position,
        }

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return TOOL_DEFINITIONS

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            handler = self._handlers.get(name)
            if handler is None:
                raise ToolError(f"Unknown tool: {name}")
            return handler(arguments or {})
        except ActionValidationError as exc:
            return {"ok": False, "error": str(exc)}
        except ToolError as exc:
            return {"ok": False, "error": str(exc)}
        except FileNotFoundError as exc:
            return {"ok": False, "error": f"File not found: {exc}"}
        except Exception as exc:  # never let a tool crash the conversation
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # ----------------------------------------------------------------- handlers

    def _active_ids(self) -> list[str]:
        state = self._state.load()
        return [p.id for p in self._programmes.resolve_active(state)]

    def _read_state(self, args: dict) -> dict:
        state = self._state.load()
        return {"ok": True, "state": json.loads(state.model_dump_json(exclude_none=True))}

    def _read_programme(self, args: dict) -> dict:
        programme = self._programmes.get(args["programme_id"])
        if programme is None:
            raise ToolError(f"Unknown programme: {args['programme_id']}")
        reference = ""
        if programme.reference and self._storage.exists(programme.reference):
            reference = self._storage.read(programme.reference)
        cached = self._state.get_programme_state(programme.id)
        return {
            "ok": True,
            "programme": programme.model_dump(),
            "reference": reference,
            "cached_state": cached.model_dump() if cached else None,
        }

    def _read_history(self, args: dict) -> dict:
        programme = self._programmes.get(args["programme_id"])
        if programme is None:
            raise ToolError(f"Unknown programme: {args['programme_id']}")
        exercise_filter = args.get("exercise", "").strip()
        # All programmes are lifting; history is condensed session records.
        history = self._sessions.render(args.get("max_rows", 40))
        if not history.strip():
            return {"ok": False,
                    "error": f"No condensed sessions recorded yet for {programme.id}"}
        if exercise_filter:
            # Filter to only sessions mentioning the exercise.
            filtered = []
            for block in history.split("\n\n"):
                if exercise_filter.lower() in block.lower():
                    filtered.append(block)
            if not filtered:
                return {"ok": True, "programme_id": programme.id,
                        "history": f"(no sessions found matching '{exercise_filter}')"}
            history = "\n\n".join(filtered)
        return {
            "ok": True,
            "programme_id": programme.id,
            "history": history,
        }

    def _read_reference(self, args: dict) -> dict:
        """Read the raw programme reference document (for LLM reasoning)."""
        programme = self._programmes.get(args["programme_id"])
        if programme is None:
            raise ToolError(f"Unknown programme: {args['programme_id']}")
        if not programme.reference or not self._storage.exists(programme.reference):
            return {"ok": False, "error": f"No reference document for {programme.id}"}
        reference = self._storage.read(programme.reference)
        # Trim blog comments/footers from the reference document.
        marker = "Responses to"
        if marker in reference:
            reference = reference[: reference.index(marker)].rstrip() + "\n"
        return {
            "ok": True,
            "programme_id": programme.id,
            "reference": reference,
        }

    def _recommend_session(self, args: dict) -> dict:
        from .decisions import DecisionService

        state = self._state.load()
        service = DecisionService(self._programmes, self._storage)
        context = service.build_context(state, user_request=args.get("user_request", ""))

        # Give the LLM the programme position and let it read the reference document
        # to figure out the next session. No programmatic next-workout identification.
        programme_positions = {}
        for programme in context.active_programmes:
            cached = state.programmes.get(programme.id)
            if cached and cached.latest_confirmed_workout:
                pos = cached.latest_confirmed_workout.programme_position
                if pos and pos.week is not None:
                    programme_positions[programme.id] = {
                        "last_completed": cached.latest_confirmed_workout.label,
                        "last_completed_week": pos.week,
                        "last_completed_session": pos.session,
                    }
                else:
                    programme_positions[programme.id] = {
                        "last_completed": cached.latest_confirmed_workout.label,
                        "last_completed_week": None,
                        "last_completed_session": None,
                    }
            else:
                programme_positions[programme.id] = {
                    "last_completed": None,
                    "last_completed_week": None,
                    "last_completed_session": None,
                }

        # All programmes are lifting; render recent session history for the prompt.
        recent_history_md = {}
        active_programmes = context.active_programmes
        if active_programmes:
            lifting_id = active_programmes[0].id
            sessions_history = self._sessions.render(
                self._settings.strong_generation.history_sample_sessions
            )
            if sessions_history.strip():
                recent_history_md[lifting_id] = sessions_history
            elif lifting_id in context.recent_history:
                # A freshly uploaded raw export has not been condensed yet.
                recent_history_md[lifting_id] = self._history_to_markdown(
                    context.recent_history[lifting_id]
                )

        result = {
            "ok": True,
            "active_programmes": [p.id for p in active_programmes],
            "recent_history_md": recent_history_md or None,
            "state": json.loads(state.model_dump_json(exclude_none=True)),
            "programme_positions": programme_positions,
            "note": _recommend_note(bool(recent_history_md)),
        }

        # Always generate Strong CSV for the first active lifting programme.
        if active_programmes:
            programme = active_programmes[0]
            gen_result = self._strong.generate(programme.id, args.get("user_request", ""))
            payload = {
                "ok": gen_result.success,
                "csv": gen_result.csv,
                "message": gen_result.message,
                "errors": gen_result.errors,
                "saved_path": None,
                "adaptations": (gen_result.prescription or {}).get("adaptations") or [],
                "reasoning": (gen_result.prescription or {}).get("reasoning"),
            }
            if gen_result.success and gen_result.csv:
                payload.update(self._persist_generated(programme, gen_result))
            result["strong_generation"] = payload

        return result

    def _persist_generated(self, programme, gen_result) -> dict:
        """Write a validated CSV to disk and mark it pending.

        Returns saved_path / pending info, or a save_error explaining why the
        file could not be written so the LLM never claims a file exists when
        it does not.
        """
        from ..models import WorkoutRef

        date = datetime.now().strftime("%Y-%m-%d")
        label = (gen_result.prescription or {}).get("label") or programme.name
        try:
            path = self._strong.save(programme.id, gen_result.csv, label, date)
        except Exception as exc:  # validation refused it, or storage failed
            return {
                "saved_path": None,
                "save_error": str(exc),
                "note": "The workout was generated but NOT written to disk. "
                        "Do not tell the user it is saved or ready to import.",
            }
        self._state.set_pending(
            programme.id,
            WorkoutRef(id=f"{date}:{label}", date=date, label=label),
        )
        return {
            "saved_path": path,
            "pending_workout": {"date": date, "label": label},
            "note": f"Saved to {path}. Quote this exact path when telling the "
                    f"user where the file is. Do not invent a filename.",
        }

    def _history_to_markdown(self, csv_content: str) -> str:
        """Convert history CSV to a markdown table using pandas (no column selection)."""
        import io

        import pandas as pd

        df = pd.read_csv(io.StringIO(csv_content))
        return df.tail(20).to_markdown(index=False)

    def _adapt_workout(self, args: dict) -> dict:
        validate_programme_id(args["programme_id"], self._active_ids())
        validate_prescription(args.get("prescription"))
        state = self._state.load()
        validate_adaptation(args.get("adaptations"), state.adaptation_policy)
        return {
            "ok": True,
            "adapted_prescription": args["prescription"],
            "adaptations": args["adaptations"],
            "reason": args.get("reason", ""),
            "note": "Adaptation accepted within policy. Prescription vs adaptation "
                    "must be reported separately to the user.",
        }

    def _generate_strong_workout(self, args: dict) -> dict:
        validate_programme_id(args["programme_id"], self._active_ids())
        result = self._strong.generate(args["programme_id"], args.get("user_request", ""))
        payload = {
            "ok": result.success,
            "attempts": result.attempts,
            "message": result.message,
            "errors": result.errors,
        }
        if result.success:
            payload["csv"] = result.csv
            payload["prescription"] = result.prescription
        return payload

    def _validate_strong_csv(self, args: dict) -> dict:
        active = self._programmes.all()
        if not active:
            return {"ok": False, "error": "No active lifting programme."}
        schema = self._strong.schema(active[0].id)
        errors = validate_strong_csv(args["csv"], schema, expected_date=args.get("date"))
        return {"ok": not errors, "errors": errors}

    def _save_strong_workout(self, args: dict) -> dict:
        validate_programme_id(args["programme_id"], self._active_ids())
        # Re-validate before saving: only a validated CSV may be written, and it
        # must belong to the date it is being filed under.
        check = self._validate_strong_csv({"csv": args["csv"], "date": args["date"]})
        if not check["ok"]:
            return {"ok": False, "error": "CSV failed validation; not saved.",
                    "validation_errors": check["errors"]}
        path = self._strong.save(
            args["programme_id"], args["csv"], args["label"], args["date"]
        )
        # The saved workout is now the pending prescription awaiting completion.
        from ..models import WorkoutRef

        self._state.set_pending(
            args["programme_id"],
            WorkoutRef(id=f"{args['date']}:{args['label']}", date=args["date"], label=args["label"]),
        )
        return {"ok": True, "saved_path": path}

    def _reconcile_workout(self, args: dict) -> dict:
        from .decisions import reconcile_completion, _newest_csv_in_folder

        programme = self._programmes.get(args["programme_id"])
        if programme is None:
            raise ToolError(f"Unknown programme: {args['programme_id']}")
        history_csv = None
        if programme.completed_workouts_folder:
            history_csv = _newest_csv_in_folder(self._storage, programme.completed_workouts_folder)
        if history_csv is None:
            # No raw export: reconcile against the condensed session records.
            history_csv = self._sessions.reconcile_csv()
        result = reconcile_completion(programme, self._state.load(), history_csv)
        payload = result.model_dump()
        # On confirmation, repair the YAML cache to match the history (spec section 10).
        if result.status == "confirmed" and result.found_id:
            from ..models import ProgrammePosition, WorkoutRef

            position = None
            if args.get("programme_position"):
                position = ProgrammePosition(**args["programme_position"])
            else:
                # The model may confirm without restating the position. Writing
                # None here would drop the athlete's progress and silently send
                # the next generation back to Day 1, so carry the existing
                # position forward instead.
                cached = self._state.load().programmes.get(programme.id)
                if cached and cached.latest_confirmed_workout:
                    position = cached.latest_confirmed_workout.programme_position
            self._state.update_latest_confirmed(
                programme.id,
                WorkoutRef(
                    id=result.found_id,
                    date=result.found_date,
                    label=result.found_label,
                    programme_position=position,
                ),
            )
            self._state.clear_pending(programme.id)
            payload["state_updated"] = True
        else:
            payload["state_updated"] = False
        return {"ok": result.status == "confirmed", **payload}

    def _summarise_session(self, args: dict) -> dict:
        """Record a completed session: condense the raw export into a session record.

        Python hands the LLM the recent export rows as text and asks it to find
        the session and rewrite it compactly. Nothing here parses the export: the
        rows go out verbatim and the summary comes back as text, so the app's
        column layout is never something we depend on.

        Any `feedback` the athlete gave about the session is passed through too,
        and the summariser stores it as a note on the session — the session YAML
        is the only place feedback lives.
        """
        from .decisions import _newest_csv_in_folder

        programme = self._programmes.get(args["programme_id"])
        if programme is None:
            raise ToolError(f"Unknown programme: {args['programme_id']}")
        date = str(args.get("date") or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            raise ToolError(f"date must be YYYY-MM-DD, got {date!r}")
        if not programme.completed_workouts_folder:
            raise ToolError(
                f"No completed_workouts_folder configured for {programme.id}. Set "
                "completed_workouts_folder in config/programmes.yaml."
            )

        history_csv = _newest_csv_in_folder(self._storage, programme.completed_workouts_folder)
        if not history_csv:
            raise ToolError(
                f"No CSV files found in {programme.completed_workouts_folder}. Upload the "
                "completed workout export to Drive first."
            )
        # Guardrail: hash the newest workout in the CSV and compare with stored hash.
        from .decisions import _hash_newest_workout
        workout_hash = _hash_newest_workout(history_csv)
        if workout_hash and programme.last_workout_hash == workout_hash:
            return {"ok": False,
                    "error": f"Duplicate workout detected (hash {workout_hash}). "
                             f"This CSV has already been processed. Upload a new workout."}

        # Extract the most recent workout from the CSV (last workout name + its rows)
        extracted = _extract_latest_workout(history_csv)
        if not extracted["rows"]:
            raise ToolError("History file is empty or no workout found")
        workout_name_from_csv = extracted["workout_name"]

        # After successful summarisation, update the stored hash so future
        # duplicate uploads are rejected.
        if workout_hash:
            self._programmes.update_last_workout_hash(
                self._storage,
                self._settings.folders.programmes_config,
                programme.id,
                workout_hash,
            )
            # Update in-memory copy for this request cycle.
            programme.last_workout_hash = workout_hash

        hint = args.get("workout") or ""
        feedback = str(args.get("feedback") or "").strip()

        # Convert extracted rows to CSV text for the prompt (bounded)
        rows = _workout_rows_to_text(extracted, self._settings.session_summary.summary_rows)
        if not rows:
            raise ToolError("Extracted workout has no rows")

        prompt = (
            f"Condense the session trained on {date}"
            + (f" ({hint})" if hint else "")
            + ".\n\nThe rows below are from the training app's export for the workout "
            f"'{workout_name_from_csv}'. Strong appends workouts to the bottom, "
            "so these rows are the complete session. "
            f"Find every row whose Date begins with {date} — the Date column "
            "carries a time as well as the day, so match on the date part only. "
            "A workout name repeats across separate sessions, so the date, not "
            "the name, is what identifies a session.\n\n"
            f"{rows}\n"
        )
        if feedback:
            prompt += (
                "\nThe athlete also said about this session (store it in "
                f"`workout_notes` verbatim): {feedback}\n"
            )
        response = self._llm.complete(
            messages=[
                {"role": "system", "content": _SESSION_SUMMARY_SYSTEM},
                {"role": "user", "content": prompt},
            ],
        )
        body = _extract_yaml(response.content or "")
        if not body:
            return {"ok": False, "error": "No YAML block in the model response"}

        # Prefer the name the model read off the rows over the caller's hint. The
        # hint is a guess — the export's own Workout column is the record, and a
        # mismatch would put the wrong name in both the header and the filename.
        workout = self._sessions.workout_name(body) or hint or date
        try:
            path = self._sessions.save(date, workout, body)
        except ValueError as exc:
            return {"ok": False, "error": f"Session summary rejected: {exc}"}
        return {"ok": True, "saved_path": path, "date": date, "workout": workout}

    def _set_programme_position(self, args: dict) -> dict:
        """Correct the cached programme pointer when the user trained off-PT.

        Returns the programme reference so the LLM can map the user's words
        onto a real week/session. Python does not decide what position the user
        is in — the LLM reads the reference and passes the result back.
        """
        from ..models import ProgrammePosition, WorkoutRef

        programme_id = args["programme_id"]
        if programme_id not in self._active_ids():
            raise ToolError(f"Unknown or inactive programme: {programme_id}")
        programme = self._programmes.get(programme_id)
        if programme is None:
            raise ToolError(f"Unknown programme: {programme_id}")

        state = self._state.load()
        cached = state.programmes.get(programme_id)
        previous = None
        if cached and cached.latest_confirmed_workout:
            previous = cached.latest_confirmed_workout.model_dump(exclude_none=True)

        # Off-plan work (easy row after illness, etc.) must not advance position.
        if args.get("out_of_cycle"):
            return {
                "ok": True,
                "changed": False,
                "reason": "out_of_cycle",
                "previous_position": previous,
                "reference": self._programme_reference(programme),
                "note": (
                    "Recorded as done but NOT counted as programme progress, so the "
                    "programme position is unchanged. Tell the user their programme "
                    "position still stands."
                ),
            }

        week = args.get("week")
        session = args.get("session")
        if week is None and session is None:
            raise ToolError(
                "Provide week and/or session, or set out_of_cycle=true if the "
                "sessions done were not part of the programme."
            )
        if week is not None and int(week) < 1:
            raise ToolError(f"week must be 1 or greater, got {week}")

        date = args.get("date") or datetime.now().strftime("%Y-%m-%d")
        label = args.get("label") or (
            f"{programme.name} {('week ' + str(week)) if week else session or 'workout'}"
        )
        position = ProgrammePosition(
            week=int(week) if week is not None else None,
            session=session,
            description=args.get("description"),
        )
        self._state.update_latest_confirmed(
            programme_id,
            WorkoutRef(
                id=f"{date}:{label}",
                date=date,
                label=label,
                programme_position=position,
            ),
        )
        # Sessions done outside the PT invalidate anything we were waiting on.
        self._state.clear_pending(programme_id)

        return {
            "ok": True,
            "changed": True,
            "previous_position": previous,
            "new_position": position.model_dump(exclude_none=True),
            "pending_cleared": True,
            "reference": self._programme_reference(programme),
            "note": (
                f"Programme position moved to week {position.week} "
                f"session {position.session}. Pending prescription cleared. "
                "Confirm this back to the user and do not re-run reconciliation "
                "for it — this was set directly."
            ),
        }

    def _programme_reference(self, programme) -> str:
        if programme.reference and self._storage.exists(programme.reference):
            return self._storage.read(programme.reference)
        return "(No reference document configured for this programme.)"

    def _update_state(self, args: dict) -> dict:
        programme_id = args["programme_id"]
        if programme_id not in self._active_ids():
            raise ToolError(f"Unknown or inactive programme: {programme_id}")
        if args.get("confirmed_workout"):
            from ..models import ProgrammePosition, WorkoutRef

            ref_data = dict(args["confirmed_workout"])
            position = None
            if "programme_position" in ref_data:
                position = ProgrammePosition(**ref_data.pop("programme_position"))
            ref = WorkoutRef(**ref_data, programme_position=position)
            self._state.update_latest_confirmed(programme_id, ref)
        if args.get("add_memory"):
            self._state.add_memory_preference(args["add_memory"])
        return {"ok": True, "state": json.loads(self._state.load().model_dump_json(exclude_none=True))}
