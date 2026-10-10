"""LLM context selection (spec section 35).

Do not blindly send every file into every LLM call. The always-included
context is small (state, active programmes, memory); programme references and
history are pulled on demand through tools, scoped to the modality under
discussion.
"""

from __future__ import annotations

import csv
import io


def select_history_rows(history_csv: str, max_rows: int = 40) -> str:
    """Most recent history rows — enough to establish format and recent patterns."""
    rows = list(csv.reader(io.StringIO(history_csv)))
    if not rows:
        return ""
    header, data = rows[0], rows[1:]
    recent = data[-max_rows:]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(recent)
    return out.getvalue()
