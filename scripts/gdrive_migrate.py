#!/usr/bin/env python3
"""Seed Google Drive from the local data/ mirror.

One-time, read-only over the local folder: every file under data/ is written
to the same relative path under the "Personal Trainer" Drive root. Folders are
created on demand by the backend.

Run after gdrive_auth.py has written a token and config/config.yaml has
storage.backend = "drive" with root_folder_id and oauth_token_file set:

    PYTHONPATH=src .venv/bin/python scripts/gdrive_migrate.py

Safe to re-run: existing Drive files are updated in place.
"""

from __future__ import annotations

import sys
from pathlib import Path

from personal_trainer.config import load_settings
from personal_trainer.storage.drive import GoogleDriveStorageBackend

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    settings = load_settings()
    if settings.storage.backend not in ("gdrive", "drive"):
        sys.exit(
            "config/config.yaml must set storage.backend: gdrive before "
            "migrating (nothing would change otherwise)."
        )

    backend = GoogleDriveStorageBackend(settings.storage)
    src = Path(settings.storage.root)
    if not src.is_absolute():
        src = PROJECT_ROOT / src
    if not src.is_dir():
        sys.exit(f"Local data folder not found: {src}")

    files = sorted(p for p in src.rglob("*") if p.is_file())
    print(f"Migrating {len(files)} file(s) from {src} to Drive...")
    for path in files:
        rel = path.relative_to(src).as_posix()
        backend.write(rel, path.read_text(encoding="utf-8"))
        print(f"  wrote {rel}")

    print("\nDone. Confirm in Drive that data/ mirrors the 'Personal Trainer' "
          "root folder, then restart the server.")


if __name__ == "__main__":
    main()