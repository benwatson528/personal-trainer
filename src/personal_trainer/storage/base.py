"""Storage backend protocol.

The application is a thin intelligent layer over files that already exist
(spec section 3). All file access (programme references, Strong/Concept2
history, session records, state) goes through this interface so the storage
medium can be swapped without touching PT logic.
"""

from __future__ import annotations

from typing import Protocol


class StorageBackend(Protocol):
    def read(self, path: str) -> str:
        """Return file content as text. Raise FileNotFoundError if missing."""
        ...

    def write(self, path: str, content: str) -> None:
        """Create or replace a file, creating parent folders as needed."""
        ...

    def exists(self, path: str) -> bool:
        ...

    def delete(self, path: str) -> None:
        """Remove a file. Raise FileNotFoundError if missing."""
        ...

    def list_files(self, folder: str) -> list[str]:
        """List files recursively under folder, paths relative to the root."""
        ...
