"""Strong workout generation with validation and the LLM correction loop.

Implements spec sections 20, 22 and 23:

    programme reference + history + state + user request
        -> structured workout prescription
        -> LLM generates Strong CSV
        -> Python validates
        -> invalid output + machine-generated errors go back to the LLM
        -> repeat until valid or retry limit reached
        -> only a valid CSV is saved to Google Drive

Prescription and serialisation are separated: if the CSV is invalid because
of formatting, only the serialisation is retried — the training decision
(prescription) is not re-litigated.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

logger = logging.getLogger("personal_trainer.strong.generation")

from ..config import Settings
from ..equipment import Equipment, load_equipment_from_storage
from ..integrations.llm import LLMClient
from ..pt.decisions import _newest_csv_in_folder
from ..programmes.manager import ProgrammeManager
from ..sessions import SessionSummaries
from ..state.manager import StateManager
from ..storage.base import StorageBackend
from .equipment_rules import validate_equipment
from .fidelity import validate_prescription_fidelity
from .schema import StrongSchema, derive_schema, schema_from_template
from .validation import normalize_csv, validate_strong_csv

_FENCE_RE = re.compile(r"```(?:csv)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _session_timestamp() -> str:
    """Timestamp for the generated workout, in Strong's exact Date format.

    Strong writes `YYYY-MM-DD HH:MM:SS` (e.g. 2023-02-27 06:59:46), so the
    generated rows must match. Local time, consistent with the timezone strings
    in the user's own history export. Evaluated once per generation so the
    prompt and the validator agree even if a retry crosses midnight.
    """
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class GenerationResult:
    success: bool
    csv: str | None = None
    prescription: dict | None = None
    errors: list[str] = field(default_factory=list)
    attempts: int = 0
    message: str = ""


class StrongGenerationService:
    def __init__(
        self,
        llm: LLMClient,
        storage: StorageBackend,
        state_manager: StateManager,
        programme_manager: ProgrammeManager,
        settings: Settings,
    ):
        self._llm = llm
        self._storage = storage
        self._state = state_manager
        self._programmes = programme_manager
        self._settings = settings
        self._sessions = SessionSummaries(storage, settings.folders.sessions)

    # ------------------------------------------------------------------ public

    def generate(self, programme_id: str, user_request: str = "") -> GenerationResult:
        logger.info("generate: programme=%s", programme_id)
        programme = self._programmes.get(programme_id)
        if programme is None:
            return GenerationResult(success=False, errors=[f"Unknown programme: {programme_id}"])

        equipment = load_equipment_from_storage(
            self._storage, self._settings.folders.equipment_config
        )
        reference = self._load_reference(programme)
        # No history CSV is needed: the Strong format comes from the day
        # templates and the training history from the condensed session records.
        schema = self._schema(programme)
        sample = self._sample_history(programme)
        state = self._state.load()
        policy = programme.adaptation_policy

        system_prompt = self._system_prompt()
        prescription = self._determine_prescription(
            programme, reference, sample, state, user_request, equipment
        )
        if not prescription or not prescription.get("exercises"):
            # The prescription is the training decision the CSV is a copy of. If
            # it is missing, the LLM never made a decision, so there is nothing to
            # serialise faithfully. Fail rather than emit an unconstrained CSV
            # that would be presented to the athlete as a programmed workout.
            logger.info("generate: no usable prescription; aborting before serialisation")
            return GenerationResult(
                success=False,
                errors=[
                    "The prescription step returned no workout decision (empty or "
                    "unparseable JSON). No CSV was generated.",
                ],
                message="Generation failed: the training decision could not be "
                        "determined. No file was saved; programme state is unchanged.",
            )

        max_retries = self._settings.strong_generation.max_validation_retries
        errors: list[str] = []
        candidate: str | None = None
        session_date = _session_timestamp()

        for attempt in range(1, max_retries + 2):  # initial attempt + retries
            logger.info("generate: attempt %d", attempt)
            messages = self._generation_messages(
                programme, reference, sample, state, user_request,
                prescription, errors if attempt > 1 else None, session_date,
                equipment, candidate,
            )
            response = self._llm.complete(
                messages=[{"role": "system", "content": system_prompt}, *messages],
            )
            candidate = self._extract_csv(response.content)
            if candidate is None:
                errors = ["No fenced CSV code block found in the LLM response."]
                continue

            # Normalize CSV: re-parse and re-write with proper quoting to handle
            # unquoted commas in note fields that the LLM may have produced.
            candidate = normalize_csv(candidate)

            errors = validate_strong_csv(candidate, schema, expected_timestamp=session_date)
            if errors:
                logger.info("generate: structural validation failed: %s", errors)
                continue

            # Stage 2: equipment feasibility — deterministic, not a judgement
            # call. A weight the athlete cannot load fails here with a specific
            # suggestion, so the model repairs it rather than re-deciding the
            # session. Runs before semantic validation because it is cheap and
            # its errors are unambiguous.
            equipment_errors = validate_equipment(candidate, equipment, schema=schema)
            if equipment_errors:
                logger.info("generate: equipment validation failed: %s", equipment_errors)
                errors = equipment_errors
                continue

            # Stage 3: prescription fidelity — deterministic. The CSV is a copy
            # of the prescription, and a copy can silently drop a set. Volume is
            # not something a well-formed CSV or a loadable weight will catch.
            fidelity_errors = validate_prescription_fidelity(candidate, prescription, schema)
            if fidelity_errors:
                logger.info("generate: fidelity validation failed: %s", fidelity_errors)
                errors = fidelity_errors
                continue

            # Stage 4: LLM semantic validation — check weights, reps, and total
            # volume are reasonable against history and programme.
            logger.info("generate: running semantic validation")
            semantic_errors = self._validate_workout_content(
                programme, reference, sample, candidate, state
            )
            if not semantic_errors:
                logger.info("generate: success on attempt %d", attempt)
                return GenerationResult(
                    success=True,
                    csv=candidate,
                    prescription=prescription,
                    attempts=attempt,
                    message="Strong CSV generated and validated.",
                )
            logger.info("generate: semantic validation failed: %s", semantic_errors)
            errors = semantic_errors

        return GenerationResult(
            success=False,
            prescription=prescription,
            errors=errors or ["Generation failed."],
            attempts=max_retries + 1,
            message="Generation failed after exhausting validation retries. "
                    "No file was saved; programme state is unchanged.",
        )

    def save(self, programme_id: str, csv_content: str, label: str, date: str) -> str:
        """Save a validated CSV to the programme's generated folder.

        Final output control (spec sections 22, 37): re-validates against the
        derived Strong schema and refuses to write a malformed file.
        """
        programme = self._programmes.get(programme_id)
        if programme is None or not programme.generated_folder:
            raise ValueError(f"No generated folder configured for programme {programme_id}")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
            raise ValueError(
                f"Refusing to save with a malformed date {date!r}: expected YYYY-MM-DD."
            )
        errors = validate_strong_csv(
            csv_content, self._schema(programme), expected_date=date
        )
        if errors:
            raise ValueError(
                "Refusing to save an invalid Strong CSV: " + "; ".join(errors)
            )
        safe_label = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_")
        path = f"{programme.generated_folder}/{safe_label}_{date}.csv"
        self._storage.write(path, csv_content)
        return path

    def target_path(self, programme_id: str, label: str, date: str) -> str:
        """Where save() would write this workout. Shared so callers can check
        for an existing file without duplicating the naming convention."""
        programme = self._programmes.get(programme_id)
        if programme is None or not programme.generated_folder:
            raise ValueError(f"No generated folder configured for programme {programme_id}")
        safe_label = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_")
        return f"{programme.generated_folder}/{safe_label}_{date}.csv"

    # ----------------------------------------------------------------- private

    def _load_reference(self, programme) -> str:
        if programme.reference and self._storage.exists(programme.reference):
            return self._storage.read(programme.reference)
        return "(Programme reference document not found.)"

    def schema(self, programme_id: str) -> StrongSchema:
        """The Strong schema for a programme, from its day templates."""
        programme = self._programmes.get(programme_id)
        if programme is None:
            return StrongSchema()
        return self._schema(programme)

    def _any_template(self, programme) -> str:
        """Any one of the programme's day templates (they share one header)."""
        if not programme.templates_folder:
            return ""
        try:
            found = sorted(
                p for p in self._storage.list_files(programme.templates_folder)
                if PurePosixPath(p).name.startswith("day_") and p.endswith(".csv")
            )
        except (FileNotFoundError, NotADirectoryError):
            return ""
        return self._storage.read(found[0]) if found else ""

    def _schema(self, programme) -> StrongSchema:
        """The Strong format, taken from a day template.

        Falls back to a real exported CSV if one is present (e.g. a fresh
        upload), then to an empty schema so validation fails loudly rather than
        silently passing malformed output.
        """
        template = self._any_template(programme)
        if template:
            return schema_from_template(template)
        if programme.completed_workouts_folder:
            csv_content = _newest_csv_in_folder(self._storage, programme.completed_workouts_folder)
            if csv_content:
                return derive_schema(csv_content)
        return StrongSchema()

    def _day_from_state(self, programme_id: str, state) -> int | None:
        """The day the programme state expects next, as 1..4 (or None).

        Prefers the pending prescription label ("… Day 2"), then the last
        confirmed position ("day_1".."day_4" — the session after it), then
        Day 1 for a brand-new cycle. Only used to pick a template file, never
        to schedule: the prescription LLM still decides the session.
        """
        prog = state.programmes.get(programme_id)
        if prog is not None and prog.pending_workout and prog.pending_workout.label:
            match = re.search(r"\bday\s*(\d+)", prog.pending_workout.label, re.IGNORECASE)
            if match:
                return int(match.group(1))
        if prog is not None and prog.latest_confirmed_workout:
            pos = prog.latest_confirmed_workout.programme_position
            if pos and pos.session:
                match = re.match(r"day_(\d+)", pos.session)
                if match:
                    day = int(match.group(1))
                    return 1 if day >= 4 else day + 1  # day 4 wraps to day 1
        return 1

    def _day_template(self, programme, state) -> str:
        """The fill-in template for the expected day, or '' if unconfigured.

        The template is the day's exercise/set structure as a pure Strong CSV
        skeleton with weights and reps left blank. It gives the model the exact
        header and row layout to reproduce; the reference supplies the loads.
        It is context — the prescription decides.
        """
        if not programme.templates_folder:
            return ""
        day = self._day_from_state(programme.id, state)
        if day is None:
            return ""
        path = f"{programme.templates_folder}/day_{day}.csv"
        if self._storage.exists(path):
            return self._storage.read(path)
        return ""

    def _sample_history(self, programme) -> str:
        """The athlete's condensed session records, used to pick today's load.

        The Strong format itself comes from the day template (see `## Day
        template`), so nothing here needs the app's export. The records are
        written by the LLM after each workout (see `summarise_session`).
        """
        summaries = self._sessions.render(
            self._settings.strong_generation.history_sample_sessions
        )
        if not summaries.strip():
            return "(No condensed session records yet.)"
        return (
            "## Training history (condensed session records, newest first)\n"
            "These are the athlete's own condensed records. Each set is written "
            "weight x reps, in the order performed. A weight of 0 means "
            "bodyweight. Where a session carries notes, treat them as the "
            "athlete's own record of what to fix and carry them into the session "
            "you prescribe — they are why these records exist.\n\n"
            f"{summaries}"
        )

    def _system_prompt(self) -> str:
        return (
            "You are the lifting workout generator for an AI personal trainer.\n"
            "You produce the next programmed lifting workout as a Strong-app-compatible CSV.\n"
            "\n"
            "## Strong CSV format rules (always apply)\n"
            "1. Match the Strong CSV format of the day template EXACTLY: same headers, "
            "same column order, same data representations (including warmup 'W' set markers, "
            "timezone strings and note columns). The generated CSV must be importable into "
            "the Strong app without modification.\n"
            "2. Use the EXACT exercise names from the day template — do not abbreviate "
            "or rename them. The Strong app matches on exact exercise names.\n"
            "3. Return the CSV inside a fenced ```csv code block and nothing else.\n"
            "4. Calculate the EXACT weight for every exercise. Use the programme reference "
            "and the user's recent workout history to determine what weight should be used. "
            "Do NOT leave weights blank or use placeholder values — every set must have a "
            "concrete weight in kg.\n"
            "5. Populate EVERY column the day template uses. Never leave a "
            "required cell empty. 'Workout Duration (s)' is a post-hoc measurement "
            "Strong fills in after the session — always write the placeholder 1 in "
            "that column on every row. Do not estimate a real duration.\n"
            "6. Write the exact 'Session timestamp' given below into the Date column, "
            "verbatim and on every row. Never copy a date or time from the training "
            "history — those are past sessions. Do not invent or adjust a time of day.\n"
            "7. Every row must have exactly the same number of columns as the header. "
            "Write notes WITHOUT any commas — use dashes or semicolons instead "
            "(e.g. 'drop sets - 3 sets of 6'). A comma in a note field breaks the row "
            "into extra columns. Only if a note genuinely needs a comma, wrap that "
            "one field in double quotes.\n"
            "7b. Mark a warmup set with the letter W in the Set column itself — never "
            "number it as a working set, and never put the W in a notes column. "
            "Working sets are numbered from 1 in the Set column. This is how the "
            "Set column tells the two apart.\n"
            "8. Weigh what was actually lifted most recently. The condensed session "
            "records below are newest first, so use them and the programme reference "
            "to justify today's weights and progression.\n"
            "\n"
            "## Equipment constraints\n"
            "Every weight must be loadable with the equipment listed in the message "
            "below. Do not invent a dumbbell weight the user does not own, and do not "
            "write a barbell load that cannot be built from their bar and plates. If "
            "an exact weight is unreachable, use the nearest loadable one and note it "
            "in the workout notes — never leave a weight blank.\n"
            "\n"
            "## Programme-specific rules\n"
            "All programme-specific rules (exercise selection, sets, reps, progression, "
            "warmup sets, rest periods, intensity) come from the programme reference "
            "document provided below. Follow it exactly unless a permitted adaptation "
            "is justified. Never silently rewrite the programme reference."
        )

    def _determine_prescription(self, programme, reference, sample, state,
                                user_request, equipment=None) -> dict:
        """Ask the LLM for the structured workout prescription (the training decision)."""
        messages = [
            {"role": "user", "content": self._prescription_prompt(
                programme, reference, sample, state, user_request, equipment
            )},
        ]
        response = self._llm.complete(
            messages=[{"role": "system", "content": self._system_prompt()}, *messages],
            response_format={"type": "json_object"},
        )
        return self._parse_json(response.content)

    def _generation_messages(self, programme, reference, sample, state,
                             user_request, prescription, retry_errors,
                             session_timestamp=None, equipment=None,
                             previous_csv=None) -> list[dict]:
        session_timestamp = session_timestamp or _session_timestamp()
        day_template = self._day_template(programme, state)
        content = (
            f"Programme: {programme.name} ({programme.id})\n"
            f"User request: {user_request or '(none)'}\n"
            f"Session timestamp: {session_timestamp}\n\n"
            f"## Programme reference\n{reference}\n\n"
        )
        if day_template:
            content += (
                f"## Day template\n{day_template}\n\n"
            )
        content += (
            f"## Recent training (condensed sessions)\n{sample}\n\n"
            f"{equipment.describe() if equipment else ''}\n\n"
            f"## Current state\n{self._format_state(state)}\n\n"
            f"## Adaptation policy\n{programme.adaptation_policy.model_dump_json()}\n\n"
            f"## Workout prescription (training decision — do not change it)\n"
            f"{json.dumps(prescription, indent=2)}\n\n"
        )
        if retry_errors:
            content += (
                "## Repair this CSV — do not start again\n"
                "Your previous attempt failed validation. It is reproduced below.\n"
                "Fix ONLY what the errors describe and leave every other row "
                "exactly as it is. In particular:\n"
                "- Keep the exercises, sets and reps that are not named in the "
                "errors. The training decision above still stands.\n"
                "- Change only the specific values you are told to change.\n"
                "- If an error gives you a suggested weight, use exactly that one.\n"
                "Return the complete corrected CSV.\n\n"
                "### Your previous attempt\n"
                f"{previous_csv}\n\n"
                "### Errors\n"
                + "\n".join(f"- {e}" for e in retry_errors) + "\n\n"
            )
            content += "Return the complete corrected Strong CSV now."
        else:
            content += "Return the complete Strong CSV now."
        return [{"role": "user", "content": content}]

    def _prescription_prompt(self, programme, reference, sample, state,
                            user_request, equipment=None) -> str:
        day_template = self._day_template(programme, state)
        parts = [
            f"Determine the next {programme.name} workout.\n"
            f"User request: {user_request or '(none)'}\n\n",
            f"## Programme reference\n{reference}\n\n",
        ]
        if day_template:
            parts.append(
                f"## Day template\n{day_template}\n\n"
                "The day template above is this day's fill-in CSV skeleton. "
                "Keep the exercises, tier order and rest periods. T1 and T2a "
                "set counts vary by week — take them from the reference's "
                "week table, adding or removing those rows. Fill in Date, "
                "Timezone, Weight (kg) and Reps, and put the week number in "
                "the Workout name.\n\n"
            )
        parts.append(
            f"## Recent training (condensed sessions)\n"
            f"{sample}\n\n"
        )
        parts.append(f"{equipment.describe() if equipment else ''}\n\n")
        parts.append(f"## Current state\n{self._format_state(state)}\n\n")
        parts.append(
            f"## Adaptation policy\n{programme.adaptation_policy.model_dump_json()}\n\n"
        )
        parts.append(
            "Analyse the previous relevant sessions, exercise progression, recent "
            "fatigue/performance and any notes on those sessions. Decide the next workout: "
            "exercises, order, sets, reps, loads, rest. Every prescribed weight must "
            "be loadable with the equipment listed above. Apply permitted adaptations "
            "only when justified, and say so. Return JSON with keys: 'label' "
            "(workout name), 'exercises' (list of {name, sets, reps, weight, rest, "
            "notes}), 'adaptations' (list of changes vs programme prescription, "
            "empty if none), 'reasoning' (short explanation)."
        )
        return "".join(parts)

    def _validate_workout_content(
        self,
        programme,
        reference: str,
        sample: str,
        candidate: str,
        state,
    ) -> list[str]:
        """Stage 2: LLM semantic validation.

        Asks the LLM to check that the generated workout's weights, reps and
        total volume are reasonable against the programme reference and the
        user's recent workout history. Returns a list of errors (empty = valid).

        The prompt below is deliberately biased toward approval. An earlier
        version asked only for "specific error strings" with no tolerance, and
        the validator had no stable verdict: it rejected the same 107.5kg squat
        as "too low" and then "too high" on consecutive attempts, computed
        50% of 185kg deadlift as 92.5kg and rejected its own answer, and flagged
        warmup weights for being lighter than working weights. That burned every
        retry and left the athlete with no workout at all, so the generator and
        this validator disagreed on an underdefined quantity with no fixed point
        to converge on.

        The rules below give it a fixed point: rounding is not an error, correct
        sets are not errors, and an empty result is an expected outcome.
        """
        prompt = (
            f"## Programme reference\n{reference}\n\n"
            f"## Recent workout history\n{sample}\n\n"
            f"## Generated workout\n{candidate}\n\n"
            "Validate the generated workout against the programme reference and "
            "recent history. Your job is to catch real errors, not to optimise "
            "the workout.\n"
            "\n"
            "## What counts as an error\n"
            "- A set count that contradicts the programme reference. Check every tier "
            "before deciding: count the working sets for each exercise (exclude warmup "
            "rows marked W) and compare against the reference's set tables. T2a has an "
            "explicit sets column per week and is the most commonly wrong — a tier "
            "prescribed 4 working sets but given 1 is an error. Report the tier, the "
            "count you found, and the count prescribed.\n"
            "- Reps or set count that contradict the programme reference.\n"
            "- Exercise selection or order that contradicts the programme reference.\n"
            "- A weight that deviates materially from the prescription: by more "
            "than one plate increment (2.5kg), or an unsafe/implausible load.\n"
            "- Total volume wildly out of line with recent sessions.\n"
            "\n"
            "## What is NOT an error\n"
            "- Rounding. A weight rounded to the nearest 2.5kg, or to the nearest "
            "weight the athlete's equipment allows, is correct. Never flag a "
            "rounding difference.\n"
            "- Warmup sets that ramp upward and stay below the working weight. A "
            "warmup lighter than a working set is correct by definition. Never "
            "flag a warmup for being too light.\n"
            "- A set that matches the prescription. Say nothing about it. Do not "
            "praise it and then hedge ('this is correct, however...') — a correct "
            "set is not an error.\n"
            "- Notes wording, rest timer choices, or anything the reference does "
            "not specify.\n"
            "- The same concern twice. Report each issue at most once.\n"
            "\n"
            "Only report a problem if you can name a specific deviation from the "
            "programme reference and it is material. If nothing is materially "
            "wrong, return an empty list. Returning no errors is a valid and "
            "expected outcome — do not invent a problem to justify the review.\n"
            "\n"
            "Return a JSON object with key 'errors' containing a list of "
            "specific error strings. If everything is reasonable, return "
            '{"errors": []}.'
        )
        response = self._llm.complete(
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        try:
            data = json.loads(response.content)
        except json.JSONDecodeError:
            # An unparseable verdict is not approval. Treating it as "no errors"
            # would let a broken validator silently bless any CSV, the opposite
            # of a gate.
            return [
                "Semantic validation returned unparseable JSON, so the workout "
                "could not be approved. Return a JSON object with an 'errors' list.",
            ]
        return data.get("errors", [])

    def _extract_csv(self, content: str) -> str | None:
        match = _FENCE_RE.search(content)
        if match:
            return match.group(1).strip()
        # Tolerate a raw CSV without a fence.
        if content.strip() and "," in content.strip():
            return content.strip()
        return None

    def _parse_json(self, content: str) -> dict:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
        return {}

    def _format_state(self, state) -> str:
        return json.dumps(state.model_dump(exclude_none=True), indent=2, default=str)
