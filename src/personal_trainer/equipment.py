"""Equipment the user owns, used to pick loadable weights.

Kept as its own content-config file (`config/equipment.yaml` in the storage
root — i.e. Drive) because it is a per-athlete inventory that changes
independently of the app settings: you gain or lose a pair of dumbbells and edit
one list, with no code change.

The inventory is presented to the LLM as context only. Python never decides
what weight to prescribe — that judgement, including how to improvise when an
exact weight is unreachable, stays with the model.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from .storage.base import StorageBackend

logger = logging.getLogger("personal_trainer.equipment")

DEFAULT_PATH = "config/equipment.yaml"


@dataclass
class Plate:
    weight: float
    count: int = 2

    def __str__(self) -> str:
        return f"{_fmt(self.weight)}kg x{self.count}"


@dataclass
class Equipment:
    """What the athlete has available to lift with."""

    bars: dict[str, float] = field(default_factory=dict)
    plates: list[Plate] = field(default_factory=list)
    dumbbells: list[float] = field(default_factory=list)
    other: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Normalise on construction so the model always sees one clean scale,
        # however the inventory was written down.
        self.dumbbells = sorted({float(d) for d in self.dumbbells}, reverse=True)
        self.plates = sorted(self.plates, key=lambda p: p.weight, reverse=True)

    @property
    def is_empty(self) -> bool:
        return not (self.bars or self.plates or self.dumbbells or self.other)

    def describe(self) -> str:
        """Markdown block for the generation prompt."""
        if self.is_empty:
            return "(No equipment inventory configured.)"

        lines = ["## Equipment available"]
        lines.append(
            "These are the only weights and implements available. Never prescribe "
            "a dumbbell weight that is not listed, and never prescribe a barbell "
            "load that cannot be built from the listed bar and plates."
        )

        if self.bars:
            bars = ", ".join(f"{name} {_fmt(w)}kg" for name, w in self.bars.items())
            lines.append(f"\nBars (bare weight): {bars}.")

        if self.plates:
            plates = ", ".join(str(p) for p in self.plates)
            lines.append(
                f"\nPlates (per side, each loaded in matching pairs): {plates}. "
                "A barbell working weight must equal the bar weight plus plates, "
                "where each plate is counted on BOTH sides."
            )

        if self.dumbbells:
            dbs = ", ".join(_fmt(d) for d in sorted(self.dumbbells, reverse=True))
            lines.append(
                f"\nDumbbells (weight of a single dumbbell): {dbs}. "
                "Only these weights are available for dumbbell work."
            )

        if self.other:
            lines.append("\nAlso available: " + "; ".join(self.other) + ".")

        lines.append(
            "\nWhen an exact weight is not reachable, choose the nearest available "
            "weight and say which one you chose and why. Never leave a weight blank."
        )
        return "\n".join(lines)

    def describe_brief(self) -> str:
        """Compact one-liner for the chat system prompt.

        Deliberately excludes the plate inventory: the chat model only needs to
        know what exists to answer questions, while the full list belongs in
        the generation prompt where barbell loads are actually built.
        """
        if self.is_empty:
            return ""
        bits = []
        if self.bars:
            bits.append("bars: " + ", ".join(
                f"{n} {_fmt(w)}kg" for n, w in self.bars.items()))
        if self.dumbbells:
            bits.append("dumbbells (each): " + ", ".join(
                _fmt(d) for d in sorted(self.dumbbells, reverse=True)))
        if self.other:
            bits.append("also: " + "; ".join(self.other))
        return " | ".join(bits)


def _fmt(value: float) -> str:
    """Trim a float to its shortest exact form (2.5 not 2.50, 20 not 20.0)."""
    return f"{value:g}"


def parse_equipment(text: str, source: str = "equipment config") -> Equipment:
    """Parse the inventory from YAML text. Bad values degrade to empty + warning.

    A syntactically valid blob with a bad value (``dumbbells: [ten]``) degrades
    to "no enforcement" with a warning rather than crashing generation: this is
    the one file the athlete edits by hand, so a typo must not take down the
    lifting path.
    """
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        logger.warning("Equipment config %s is not valid YAML (%s); ignoring it.", source, exc)
        return Equipment()

    if not isinstance(data, dict):
        logger.warning("Equipment config %s is not a mapping; ignoring it.", source)
        return Equipment()

    try:
        bars = data.get("bars") or {}
        bars = (
            {str(k): float(v) for k, v in bars.items()}
            if isinstance(bars, dict)
            else {}
        )

        plates = []
        for entry in data.get("plates") or []:
            if isinstance(entry, dict) and "weight" in entry:
                plates.append(
                    Plate(weight=float(entry["weight"]), count=int(entry.get("count", 2)))
                )
            elif isinstance(entry, (int, float)):
                plates.append(Plate(weight=float(entry)))
        plates.sort(key=lambda p: p.weight, reverse=True)

        dumbbells = [float(d) for d in (data.get("dumbbells") or [])]
        other = [str(o) for o in (data.get("other") or [])]
    except (TypeError, ValueError) as exc:
        logger.warning(
            "Equipment config %s has a non-numeric value (%s); ignoring it so "
            "generation falls back to no equipment enforcement.",
            source,
            exc,
        )
        return Equipment()

    return Equipment(bars=bars, plates=plates, dumbbells=dumbbells, other=other)


def load_equipment(path: str | Path | None = None) -> Equipment:
    """Read the inventory from a local file. Missing or unreadable -> empty.

    Kept for direct path use (tests, scripts, local development). The app loads
    through the storage backend with `load_equipment_from_storage`, so the
    inventory can live in Drive.
    """
    if path is None:
        path = os.environ.get("PT_EQUIPMENT_CONFIG", DEFAULT_PATH)
    p = Path(path)
    if not p.exists():
        return Equipment()
    return parse_equipment(p.read_text(encoding="utf-8"), source=str(p))


def load_equipment_from_storage(storage: StorageBackend, path: str) -> Equipment:
    """Read the inventory through the storage backend (Drive or local)."""
    if not storage.exists(path):
        logger.warning(
            "No equipment inventory at %s; generation will not enforce weights. "
            "Upload config/equipment.yaml to the storage root.",
            path,
        )
        return Equipment()
    return parse_equipment(storage.read(path), source=path)