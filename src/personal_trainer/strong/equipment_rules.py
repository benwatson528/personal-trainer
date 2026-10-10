"""Equipment enforcement: reject workouts the athlete cannot actually load.

This is deliberately Python, not the LLM. Semantic validation failed earlier
because it is a *judgement* call — opinions that oscillated between attempts.
Equipment feasibility is arithmetic: a dumbbell weight is either in the
inventory or it is not, and a barbell load is either buildable from the owned
plates or it is not. Deterministic checks converge; judgement checks do not.

Two design choices keep this from becoming a new source of false rejections:

1. **Skip when the implement is unknown.** Strong encodes the implement in the
   exercise name (``Hammer Curl (Dumbbell)``) but not reliably (``T Bar Row``
   has no suffix). Where the signal is missing or ambiguous the exercise is
   skipped, never guessed at.
2. **Warmups are exempt.** They are ramp-up weights on the way to the working
   weight, not prescribed loads, and a barbell warmup is frequently a single
   empty bar. Only working sets are checked.

Errors are written to be actionable, naming the available alternative, because
they are fed straight back to the model as the retry prompt.
"""

from __future__ import annotations

import csv
import io
import re
from fractions import Fraction
from math import gcd

from ..equipment import Equipment
from .schema import StrongSchema

# Strong exercise names carry the implement in trailing parentheses, e.g.
# "Hammer Curl (Dumbbell)". Some programme references write it bare instead --
# "Tricep Kickback Dumbbell" -- so both forms are accepted. The bare form is
# only honoured as a whole-word match at the very end of the name, which keeps
# a name like "Barbell Row" from being misread.
_IMPLEMENT_RE = re.compile(r"\((Barbell|Dumbbell|Kettlebell|Band|Machine|Cable|Bodyweight)\)")
_IMPLEMENT_BARE_RE = re.compile(
    r"\s(Barbell|Dumbbell|Kettlebell|Band|Machine|Cable|Bodyweight)$"
)


def implement_of(exercise_name: str) -> str | None:
    """Implement an exercise uses, from its Strong name. None if not stated."""
    name = (exercise_name or "").strip()
    match = _IMPLEMENT_RE.search(name)
    if match:
        return match.group(1)
    bare = _IMPLEMENT_BARE_RE.search(name)
    return bare.group(1) if bare else None


def is_warmup(set_id: str) -> bool:
    return (set_id or "").strip().upper().startswith("W")


def _parse_weight(raw: str) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        # Some rows carry a unit suffix, e.g. "100kg".
        digits = re.match(r"^-?\d+(?:\.\d+)?", raw)
        return float(digits.group(0)) if digits else None


def _nearest_loadable(
    equipment: Equipment, bar_weight: float, target: float, span: int = 60
) -> float | None:
    """Closest loadable barbell weight to `target`, preferring the lighter one.

    Searches outward in units of the inventory's own granularity, so the
    suggestion is always a weight the athlete can actually put on the bar and
    is genuinely the nearest one rather than merely the nearest at some coarser
    step. Every candidate is still verified with `_plate_loads` before being
    returned, so a coarser or wrong step could only cost precision, never
    correctness.
    """
    sizes = [p for p in equipment.plates if p.weight > 0 and p.count >= 2]
    step = float(_gcd([_frac(p.weight) for p in sizes])) if sizes else 0.5
    for offset in range(0, span + 1):
        for candidate in (target - offset * step, target + offset * step):
            if candidate <= bar_weight:
                continue
            if _plate_loads(equipment, bar_weight, candidate):
                return round(candidate, 2)
    return None


def _nearest_dumbbell(equipment: Equipment, target: float) -> float | None:
    if not equipment.dumbbells:
        return None
    return min(equipment.dumbbells, key=lambda d: (abs(d - target), d))


def _plate_loads(equipment: Equipment, bar_weight: float, target: float) -> bool:
    """Can `target` be built as bar + matched plate pairs?

    Exact rational arithmetic throughout. The natural unit is the greatest
    common divisor of the owned plate sizes, NOT the smallest one: with 0.5kg
    and 1.25kg plates the smallest is not a divisor of the other, and using it
    as the unit silently rejects perfectly buildable loads such as 22.5kg.
    """
    sizes = [p for p in equipment.plates if p.weight > 0 and p.count >= 2]
    target = _frac(target)
    bar = _frac(bar_weight)
    per_side = (target - bar) / 2

    if per_side < 0:
        return False  # lighter than the bare bar
    if per_side == 0:
        return True  # bare bar
    if not sizes:
        return False

    weights = [_frac(p.weight) for p in sizes]
    unit = _gcd(weights)

    exact_units = per_side / unit
    if exact_units.denominator != 1:
        return False  # unreachable with the owned plates
    target_units = int(exact_units)
    if target_units <= 0:
        return True

    # Bounded subset-sum: per side, 0..(count // 2) plates of each size.
    reachable = [False] * (target_units + 1)
    reachable[0] = True
    for plate, weight in zip(sizes, weights):
        weight_units = int(weight / unit)
        if weight_units <= 0 or weight_units > target_units:
            continue
        max_count = plate.count // 2
        # Snapshot before adding any of this size. Growing reachable in place
        # lets a plate count against itself and exceed what is owned — with four
        # 1.25kg plates that silently allowed five per side, which made
        # unbuildable weights like 60.5kg look reachable.
        before = reachable[:]
        for total in range(target_units + 1):
            if not before[total]:
                continue
            for extra in range(1, max_count + 1):
                new_total = total + extra * weight_units
                if new_total > target_units:
                    break
                reachable[new_total] = True
    return reachable[target_units]


def _frac(value: float) -> Fraction:
    """Exact Fraction from a decimal weight (0.1 + 0.2 problems avoided)."""
    return Fraction(str(value))


def _gcd(values: list[Fraction]) -> Fraction:
    """Greatest common divisor of a list of positive fractions."""
    denominator = 1
    for v in values:
        denominator = denominator * v.denominator // gcd(denominator, v.denominator)
    result = 0
    for v in values:
        result = gcd(result, int(v * denominator))
    return Fraction(result, denominator) or Fraction(1)


def resolve_column(header: list[str], schema: StrongSchema | None, name: str) -> int | None:
    """Index of a column in this file, preferring the derived schema's position.

    The name is only used to match against columns the schema already found in
    the athlete's own export, so a renamed column resolves to whatever that
    export actually calls it. Falls back to the header when no schema is
    supplied.
    """
    if schema is not None:
        column = schema.column(name)
        if column is None:
            return None
        try:
            resolved = header.index(column.name)
        except ValueError:
            return None
        return resolved
    try:
        return header.index(name)
    except ValueError:
        return None


def validate_equipment(
    csv_text: str,
    equipment: Equipment,
    bar: str = "barbell",
    schema: StrongSchema | None = None,
) -> list[str]:
    """Errors for weights the athlete cannot load with their own equipment.

    Column positions come from the schema derived from the athlete's real
    export, never from hardcoded names. If the columns this check needs are not
    present the check is skipped rather than guessed at — a missed check is
    recoverable, a wrong one is not.

    Returns an empty list when the workout is buildable, when the inventory is
    unknown, or when the implement cannot be determined from the exercise name.
    """
    if equipment.is_empty:
        return []

    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        return []

    header = [h.strip() for h in rows[0]]
    exercise_col = resolve_column(header, schema, "Exercise")
    set_col = resolve_column(header, schema, "Set")
    weight_col = resolve_column(header, schema, "Weight (kg)")
    if exercise_col is None or set_col is None or weight_col is None:
        return []

    bar_weight = equipment.bars.get(bar)
    errors: list[str] = []
    checked: set[str] = set()

    for row in rows[1:]:
        if len(row) < len(header):
            continue
        if max(exercise_col, set_col, weight_col) >= len(row):
            continue
        exercise = row[exercise_col].strip()
        set_id = row[set_col].strip()
        weight = _parse_weight(row[weight_col])
        if weight is None or weight <= 0 or is_warmup(set_id):
            continue

        # Collapse to one error per exercise, not one per set.
        key = exercise
        if key in checked:
            continue

        kind = implement_of(exercise)
        if kind == "Dumbbell":
            if not equipment.dumbbells:
                continue
            if not any(abs(weight - d) < 1e-6 for d in equipment.dumbbells):
                checked.add(key)
                nearest = _nearest_dumbbell(equipment, weight)
                suggestion = (
                    f" Use {nearest:g}kg instead." if nearest is not None else ""
                )
                have = ", ".join(f"{d:g}" for d in equipment.dumbbells)
                errors.append(
                    f"{exercise}: {weight:g}kg is not a dumbbell the user owns. "
                    f"Available dumbbells: {have}.{suggestion}"
                )
        elif kind == "Barbell":
            if bar_weight is None:
                continue
            checked.add(key)
            if not _plate_loads(equipment, bar_weight, weight):
                nearest = _nearest_loadable(equipment, bar_weight, weight)
                available = ", ".join(f"{p}" for p in equipment.plates)
                suggestion = (
                    f" Use {nearest:g}kg instead." if nearest is not None else
                    " Round to a weight that can be built from those plates."
                )
                errors.append(
                    f"{exercise}: {weight:g}kg cannot be loaded on a "
                    f"{bar_weight:g}kg bar from the plates the user owns. "
                    f"Plates available per side: {available}.{suggestion}"
                )
        elif kind == "Kettlebell":
            # Free-form "other" list; too unstructured to enforce safely.
            continue

    return errors