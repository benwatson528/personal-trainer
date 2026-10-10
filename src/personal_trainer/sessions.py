"""Condensed per-session lifting history.

The raw app export is expensive to send: one session is ~350 tokens of CSV, and
a 15-column row is mostly empty cells. Twenty sessions of raw CSV is ~7,000
tokens, and `to_markdown()` costs nearly three times that again in padding.

So after each workout the LLM condenses that session into a few lines, and the
generation prompt reads these instead:

    date: 2026-06-23
    workout: J&T 2.0 - 4
    Overhead Press (Barbell): [40x5, 50x2, 60x1]
    Shrug (Dumbbell): [60x10, 60x10, 60x10]

~55 tokens per session, and the LLM writes it — so nothing here needs to
understand the app's export format. This module deliberately does almost no
interpretation: it validates that a file is well-formed enough to be trusted and
otherwise passes the text through untouched. Re-serialising would mean
re-quoting keys like `J&T 2.0 - 4`, which is more risk than it saves.

These are the lifting history, written by the LLM after each workout. The raw
app export is no longer kept: generation reads these records and takes the
Strong CSV format from the day templates.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date as date_cls
from pathlib import PurePosixPath
from typing import Any

import yaml

from .storage.base import StorageBackend

# A set is written as `40x5` — weight by reps. Kept deliberately dumb: the
# reader infers ramp, top set and backoff from the order, so the extractor never
# has to guess which of those a set was, and cannot label it wrongly.
_SET_RE = re.compile(r"^\d+(?:\.\d+)?\s*[x×]\s*\d+$")


def _slug(text: str | None) -> str:
    if not text:
        return "session"
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "session"


def _note_text(value: Any, where: str) -> str:
    """Coerce a note to text, refusing to invent one.

    A note is free text the athlete wrote, so it can never be a list. Rejecting
    the wrong shape is better than stringifying something meaningless.
    """
    if isinstance(value, (list, dict)):
        raise ValueError(f"{where} must be a note as text, got {value!r}")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{where} is empty")
    return text


class SessionSummaries:
    """Reads and writes the condensed session sidecars."""

    def __init__(self, storage: StorageBackend, sessions_folder: str):
        self._storage = storage
        self._folder = sessions_folder

    # --- writing ------------------------------------------------------------

    def save(self, date: str, workout: str, body: str) -> str:
        """Validate and store one condensed session. Returns its path."""
        parsed = self._validate(date, workout, body)
        path = f"{self._folder}/{date}_{_slug(workout)}.yaml"
        header = f"date: {date}\nworkout: {_yaml_scalar(workout)}\n"
        self._storage.write(path, header + parsed)
        return path

    def _validate(self, date: str, workout: str, body: str) -> str:
        """Return the exercise lines, or raise if the summary can't be trusted.

        Shallow on purpose. The LLM wrote this, so the risk is a plausible but
        wrong summary rather than a crash, and the cheapest guard is to insist
        the shape is right and that every set is a `weight x reps` pair.
        """
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(date or "")):
            raise ValueError(f"session date must be YYYY-MM-DD, got {date!r}")
        try:
            data = yaml.safe_load(body)
        except yaml.YAMLError as exc:
            raise ValueError(f"session summary is not valid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("session summary must be a YAML mapping")

        lines = []
        notes: dict[str, str] = {}
        workout_notes = ""
        for key, value in data.items():
            lowered = str(key).strip().lower()
            if lowered == "workout_notes":
                workout_notes = _note_text(value, key)
            elif lowered == "notes":
                # Notes live in their own mapping so the exercise -> sets shape
                # stays pure, and so a note can never be mistaken for a set.
                if not isinstance(value, dict):
                    raise ValueError(f"'notes' must map exercise name to text, got {value!r}")
                for ex, text in value.items():
                    notes[str(ex)] = _note_text(text, f"notes.{ex}")
            elif lowered in {"date", "workout", "programme"}:
                continue
            else:
                if not isinstance(value, list) or not value:
                    raise ValueError(
                        f"{key!r} must map to a non-empty list of sets, got {value!r}"
                    )
                for item in value:
                    if isinstance(item, int) and not isinstance(item, bool):
                        # YAML already read `0x8` as the hex integer 8, so the
                        # reps are gone and the set cannot be recovered. Say so
                        # precisely — a vague error just gets retried unchanged.
                        raise ValueError(
                            f"{key!r} has set {item!r}, which YAML read as a number. "
                            "Sets must be quoted strings, e.g. ['0x8'] — unquoted "
                            "0x8 is the number 8 in YAML."
                        )
                    if not isinstance(item, str) or not _SET_RE.match(item.strip()):
                        raise ValueError(
                            f"{key!r} has a malformed set {item!r}; expected 'weight x reps'"
                        )
                # Sets are re-emitted quoted. Unquoted, YAML reads `0x8` as the
                # hex integer 8, so every bodyweight set would come back as a
                # number instead of a set — and bodyweight is exactly where
                # Strong records weight 0.
                lines.append(
                    f"{key}: ["
                    + ", ".join("'" + str(i).strip().replace("'", "''") + "'" for i in value)
                    + "]"
                )
        if not lines:
            raise ValueError("session summary contains no exercises")

        out = []
        if workout_notes:
            out.append(f"workout_notes: {_yaml_scalar(workout_notes)}")
        out.extend(lines)
        if notes:
            out.append("notes:")
            out.extend(f"  {k}: {_yaml_scalar(v)}" for k, v in notes.items())
        return "\n".join(out) + "\n"

    # --- reading ------------------------------------------------------------

    def paths(self) -> list[str]:
        """Existing summary paths, newest session first (filenames start with the date)."""
        try:
            found = self._storage.list_files(self._folder)
        except (FileNotFoundError, NotADirectoryError):
            return []
        return sorted(
            (p for p in found if p.endswith(".yaml")),
            key=lambda p: PurePosixPath(p).name,
            reverse=True,
        )

    def count(self) -> int:
        return len(self.paths())

    def render(self, limit: int = 20) -> str:
        """The most recent summaries as one block of text for the prompt.

        Files are passed through verbatim rather than re-serialised: they are
        small, already in the right shape, and re-emitting them would only risk
        mangling keys that need quoting.
        """
        paths = self.paths()
        if limit > 0:
            paths = paths[:limit]
        if not paths:
            return ""
        blocks = []
        for path in paths:
            try:
                blocks.append(self._storage.read(path).strip())
            except FileNotFoundError:
                continue
        return "\n\n".join(b for b in blocks if b)

    def workout_name(self, body: str) -> str | None:
        """The workout name in a summary body, if the extractor supplied one.

        Optional, so an older or terser summary still loads. Used to keep the
        stored name honest: the name on the export's own rows beats the caller's
        guess.
        """
        try:
            data = yaml.safe_load(body)
        except yaml.YAMLError:
            return None
        if not isinstance(data, dict):
            return None
        value = data.get("workout")
        return value.strip() if isinstance(value, str) and value.strip() else None

    def reconcile_csv(self) -> str:
        """A minimal `Date,Workout` CSV built from the session sidecars.

        Reconciliation only ever needs the workout label and its date, so this
        keeps `reconcile_completion` working now that the raw export is gone.
        Rows run oldest first, matching the app's export ordering.
        """
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["Date", "Workout"])
        for path in reversed(self.paths()):
            try:
                data = yaml.safe_load(self._storage.read(path)) or {}
            except (FileNotFoundError, yaml.YAMLError):
                continue
            if not isinstance(data, dict):
                continue
            day = str(data.get("date", ""))[:10]
            workout = data.get("workout")
            if day and isinstance(workout, str) and workout.strip():
                writer.writerow([day, workout.strip()])
        return out.getvalue()

    def session_date(self, path: str) -> date_cls | None:
        """The date a stored summary describes, for reconciling against state."""
        try:
            data = yaml.safe_load(self._storage.read(path)) or {}
        except (FileNotFoundError, yaml.YAMLError):
            return None
        value = data.get("date") if isinstance(data, dict) else None
        if isinstance(value, date_cls):
            return value
        if isinstance(value, str):
            try:
                return date_cls.fromisoformat(value[:10])
            except ValueError:
                return None
        return None


def _yaml_scalar(value: str) -> str:
    """Quote a scalar only when YAML would otherwise misread it."""
    text = str(value)
    if re.match(r"^[A-Za-z0-9][\w .()/-]*$", text) and not text.strip().isdigit():
        return text
    return "'" + text.replace("'", "''") + "'"
