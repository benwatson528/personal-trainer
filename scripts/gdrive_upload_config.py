#!/usr/bin/env python3
"""Copy the local content-config into Google Drive, once.

`programmes.yaml` and `equipment.yaml` are not secrets but they are content, so
they live in the storage root (Drive) beside the programme material and the
training data rather than in the repo's `config/` directory. Only `config.yaml`
stays local.

Run after `gdrive_auth.py` has written a token and `config/config.yaml` has
`storage.backend: gdrive` plus `GDRIVE_ROOT_FOLDER_ID` in the environment:

    PYTHONPATH=src .venv/bin/python scripts/gdrive_upload_config.py

Safe to re-run: existing Drive files are overwritten in place. Once it has run
you can delete the local `config/programmes.yaml` and `config/equipment.yaml`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from personal_trainer.config import load_settings
from personal_trainer.storage.drive import GoogleDriveStorageBackend

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Local source name -> storage path. Kept identical so the mapping is obvious.
FILES = {
    "config/programmes.yaml": "config/programmes.yaml",
    "config/equipment.yaml": "config/equipment.yaml",
}


def main() -> None:
    settings = load_settings()
    if settings.storage.backend not in ("gdrive", "drive"):
        sys.exit(
            "config/config.yaml must set storage.backend: gdrive before uploading "
            "(nothing would change otherwise)."
        )

    backend = GoogleDriveStorageBackend(settings.storage)
    wrote = 0
    for src_rel, dst_rel in FILES.items():
        src = PROJECT_ROOT / src_rel
        if not src.exists():
            print(f"skip {src_rel} (no local copy)")
            continue
        backend.write(dst_rel, src.read_text(encoding="utf-8"))
        print(f"wrote {dst_rel}")
        wrote += 1

    if not wrote:
        sys.exit(
            "Nothing uploaded: no local config/programmes.yaml or "
            "config/equipment.yaml to copy."
        )
    print(
        "\nDone. Programmes and equipment now load from Drive; the local copies "
        "under config/ can be removed."
    )


if __name__ == "__main__":
    main()
