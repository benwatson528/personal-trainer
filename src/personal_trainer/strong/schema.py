"""Derive the Strong CSV schema from the actual Strong history export.

Spec sections 8, 21 and 41: there is no separate Strong template and the
system must not assume the Strong CSV format in advance. The latest real
Strong history is the authoritative format reference, so the expected
headers, column order, data types and required fields are all derived from
it at runtime.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field


@dataclass
class StrongColumn:
    name: str
    dtype: str  # "string" | "integer" | "number" | "date"
    required: bool


@dataclass
class StrongSchema:
    columns: list[StrongColumn] = field(default_factory=list)

    @property
    def headers(self) -> list[str]:
        return [c.name for c in self.columns]

    def column(self, name: str) -> StrongColumn | None:
        for c in self.columns:
            if c.name == name:
                return c
        return None


def _looks_like_int(value: str) -> bool:
    try:
        int(value)
        return True
    except ValueError:
        return False


def _looks_like_number(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def _looks_like_date(value: str) -> bool:
    import re

    return bool(re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}", value.strip()))


# The Strong export's column types. The day templates carry the exact header
# (names and order), so the columns come from the template; the types are fixed
# because a fill-in template leaves weights/reps blank and can't be inferred
# from. Keyed by column name.
_TEMPLATE_TYPES: dict[str, tuple[str, bool]] = {
    "Date": ("date", True),
    "Timezone": ("string", True),
    "Workout": ("string", True),
    "Workout Duration (s)": ("integer", True),
    "Group": ("string", False),
    "Exercise": ("string", True),
    "Set": ("string", True),
    "Weight (kg)": ("number", True),
    "Reps": ("integer", True),
    "RPE": ("number", False),
    "Distance (m)": ("number", False),
    "Duration (s)": ("integer", False),
    "Rest Timer (s)": ("integer", False),
    "Workout Notes": ("string", False),
    "Exercise Notes": ("string", False),
}


def schema_from_template(template_csv: str) -> StrongSchema:
    """Build the Strong schema from a day template's header.

    The template declares the workout format (columns and order); the types are
    fixed in `_TEMPLATE_TYPES` because a fill-in skeleton leaves weights and reps
    blank, so nothing can be inferred from the values.
    """
    rows = list(csv.reader(io.StringIO(template_csv))) if template_csv else []
    if not rows:
        return StrongSchema()
    columns: list[StrongColumn] = []
    for name in (h.strip() for h in rows[0]):
        dtype, required = _TEMPLATE_TYPES.get(name, ("string", False))
        columns.append(StrongColumn(name=name, dtype=dtype, required=required))
    return StrongSchema(columns=columns)


def derive_schema(history_csv: str, sample_rows: int = 10000) -> StrongSchema:
    """Infer the Strong schema from the real history export.

    Type inference: a column is "integer" if every sampled non-empty value
    parses as int, "number" if every value parses as float, "date" if every
    value looks like an ISO date, otherwise "string". A column is required
    when it is populated in every sampled row.

    Sampling scans (nearly) the whole file so type inference is not fooled
    by an unrepresentative prefix — e.g. a history whose early weights are
    all whole numbers but which later uses microplates.
    """
    reader = csv.reader(io.StringIO(history_csv))
    rows = list(reader)
    if not rows:
        return StrongSchema()
    headers = rows[0]
    all_data = [r for r in rows[1:] if any(cell.strip() for cell in r)]
    if len(all_data) > sample_rows:
        # Strided sample spread across the whole file.
        step = len(all_data) / sample_rows
        data_rows = [all_data[int(i * step)] for i in range(sample_rows)]
    else:
        data_rows = all_data

    columns: list[StrongColumn] = []
    for idx, name in enumerate(headers):
        values = []
        for row in data_rows:
            if idx < len(row):
                values.append(row[idx].strip())
        non_empty = [v for v in values if v]
        if not non_empty:
            columns.append(StrongColumn(name=name, dtype="string", required=False))
            continue
        if all(_looks_like_int(v) for v in non_empty):
            dtype = "integer"
        elif all(_looks_like_number(v) for v in non_empty):
            dtype = "number"
        elif all(_looks_like_date(v) for v in non_empty):
            dtype = "date"
        else:
            dtype = "string"
        columns.append(StrongColumn(
            name=name,
            dtype=dtype,
            required=len(non_empty) == len(values),
        ))
    return StrongSchema(columns=columns)
