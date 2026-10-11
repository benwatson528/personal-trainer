"""Strong CSV validation (spec sections 21, 22).

Python decides whether LLM output is structurally acceptable. The validator
exists primarily to prevent malformed output, not to be a complete semantic
Strong parser. Checks, at minimum:

* valid CSV syntax
* expected headers (names and order, derived from the real history)
* expected column count per row
* valid data types/representations per column
* required fields populated
* sensible row structure (at least one set row)
"""

from __future__ import annotations

import csv
import io

from pydantic import BaseModel, ConfigDict, create_model

from .schema import StrongSchema


def normalize_csv(csv_text: str, expected_cols: int | None = None) -> str:
    """
    Normalize CSV text by re-parsing and re-writing with proper quoting.

    This handles the case where LLM output contains unquoted commas in note fields
    that would otherwise be split into extra columns. Uses Python's csv module
    to correctly parse quoted fields and re-write with proper quoting.

    If expected_cols is provided, handles unquoted commas in trailing note columns
    by joining excess columns back into the last expected column.
    """
    if not csv_text.strip():
        return csv_text

    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return csv_text

    # If expected_cols is given and we have rows with extra columns, fix them
    if expected_cols is not None:
        fixed_rows = []
        for row in rows:
            if len(row) > expected_cols:
                # Excess columns are from unquoted commas in trailing note columns
                # Join excess columns back into the last expected column
                excess = len(row) - expected_cols
                # The last expected column index is expected_cols - 1
                # Join the excess columns (including the last expected one) with commas
                excess_cols = row[expected_cols - 1:]
                fixed_row = row[:expected_cols - 1]
                fixed_row.append(",".join(excess_cols))
                fixed_rows.append(fixed_row)
            else:
                fixed_rows.append(row)
        rows = fixed_rows

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerows(rows)
    return out.getvalue().rstrip("\n")


_PYDANTIC_TYPES = {
    "string": str,
    "integer": int,
    "number": float,
    "date": str,  # keep dates as strings; format already checked by schema inference
}


def build_pydantic_model(schema: StrongSchema) -> type[BaseModel]:
    """Dynamically build a pydantic model from the derived Strong schema."""
    fields: dict[str, tuple] = {}
    for col in schema.columns:
        py_type = _PYDANTIC_TYPES.get(col.dtype, str)
        if col.required:
            fields[col.name] = (py_type, ...)
        else:
            fields[col.name] = (py_type | None, None)
    model = create_model(
        "StrongRow",
        __config__=ConfigDict(extra="forbid", populate_by_name=True),
        **fields,
    )
    return model


def validate_strong_csv(
    candidate: str,
    schema: StrongSchema,
    expected_timestamp: str | None = None,
    expected_date: str | None = None,
) -> list[str]:
    """Validate a candidate Strong CSV against the derived schema.

    Returns a list of concise, machine-generated error strings.
    An empty list means the CSV is structurally acceptable.

    When expected_timestamp is given (Strong's 'YYYY-MM-DD HH:MM:SS'), every
    row's date field must equal it exactly. Without this the LLM copies a date
    out of the history sample, stamping planned workouts in the past, and
    invents its own time of day.

    expected_date (YYYY-MM-DD) is the weaker check used by save(), which knows
    the day a workout belongs to but not the exact time the generator stamped.
    Every row's date must start with it, so a saved file can never carry a
    different day from its own filename and pending record.
    """
    errors: list[str] = []
    if not candidate.strip():
        return ["CSV is empty."]

    # --- CSV syntax -----------------------------------------------------
    try:
        rows = list(csv.reader(io.StringIO(candidate)))
    except csv.Error as exc:
        return [f"Invalid CSV syntax: {exc}"]

    if not rows:
        return ["CSV has no header row."]

    # --- headers ----------------------------------------------------------
    headers = [h.strip() for h in rows[0]]
    expected = schema.headers
    if headers != expected:
        if sorted(headers) == sorted(expected):
            errors.append(
                f"Column order differs from the Strong history format. "
                f"Expected order: {expected}. Got: {headers}."
            )
        else:
            missing = [h for h in expected if h not in headers]
            extra = [h for h in headers if h not in expected]
            if missing:
                errors.append(f"Missing expected columns: {missing}.")
            if extra:
                errors.append(f"Unexpected columns: {extra}.")
            if not missing and not extra:
                errors.append(f"Expected headers {expected}, got {headers}.")

    # --- row structure ----------------------------------------------------
    data_rows = rows[1:]
    if not data_rows or not any(any(cell.strip() for cell in row) for row in data_rows):
        errors.append("CSV contains a header but no workout rows.")
        return errors

    n_cols = len(headers)
    model = build_pydantic_model(schema)

    seen_exercises: set[str] = set()
    for line_offset, row in enumerate(data_rows, start=2):
        if not any(cell.strip() for cell in row):
            continue  # blank line
        if len(row) != n_cols:
            extra = len(row) - n_cols
            hint = (
                " This is almost always an unquoted comma inside a note field — "
                "rewrite notes using dashes or semicolons instead of commas."
                if extra > 0 else ""
            )
            errors.append(
                f"Row {line_offset} has {len(row)} columns; expected {n_cols}.{hint}"
            )
            continue
        record = {headers[i]: (row[i].strip() if i < len(row) else "") for i in range(n_cols)}
        if expected_timestamp:
            date_col = _date_column(schema, headers)
            actual = record.get(date_col, "") if date_col else ""
            if actual != expected_timestamp:
                errors.append(
                    f"Row {line_offset}: Date must be exactly '{expected_timestamp}', "
                    f"got '{actual}'. Use the session timestamp verbatim, not a date "
                    f"from the history sample and not an invented time."
                )
        elif expected_date:
            date_col = _date_column(schema, headers)
            actual = record.get(date_col, "") if date_col else ""
            if not actual.startswith(expected_date):
                errors.append(
                    f"Row {line_offset}: Date must be on '{expected_date}', got "
                    f"'{actual}'. The file's date must match the workout it records."
                )
        # Empty cells mean "not populated" — treat as None for optional columns.
        for col in schema.columns:
            if not col.required and record.get(col.name) == "":
                record[col.name] = None
        # Required fields must be populated (pydantic can't see empty strings).
        # Track which fields are missing so pydantic's duplicate type errors for the
        # same fields can be filtered out — otherwise one empty cell yields two error
        # lines and the retry prompt gets needlessly long.
        missing: set[str] = set()
        for col in schema.columns:
            if col.required and not record.get(col.name):
                missing.add(col.name)
                errors.append(f"Row {line_offset}: required field '{col.name}' is empty.")
        try:
            parsed = model(**record)
        except Exception as exc:  # pydantic ValidationError
            detail = _format_validation_error(exc, skip_fields=missing)
            if detail:
                errors.append(f"Row {line_offset}: {detail}")
            continue
        # Track workout/exercise structure for the sanity checks below.
        for key in ("Exercise Name", "Exercise"):
            if key in record and getattr(parsed, key, None):
                seen_exercises.add(getattr(parsed, key))

    if not seen_exercises and not errors:
        errors.append("No exercise names found; expected at least one exercise row.")

    return errors


def _date_column(schema: StrongSchema, headers: list[str]) -> str | None:
    """The column holding the session date, derived from the schema dtype."""
    for col in schema.columns:
        if col.dtype == "date" and col.name in headers:
            return col.name
    return None


def _format_validation_error(exc: Exception, skip_fields: set[str] | None = None) -> str:
    """Condense a pydantic ValidationError into one concise line.

    Errors for fields already reported as empty are dropped, so a missing value
    produces a single actionable message instead of two overlapping ones.
    """
    skip = skip_fields or set()
    try:
        parts = []
        for err in exc.errors():
            if err["loc"] and str(err["loc"][0]) in skip:
                continue
            loc = ".".join(str(x) for x in err["loc"])
            parts.append(f"{loc}: {err['msg']}")
        return "; ".join(parts)[:300]
    except Exception:
        return str(exc)[:300]
