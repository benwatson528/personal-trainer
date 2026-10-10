"""Filesystem-backed storage for development and testing.

Root directory mirrors the Drive folder structure from spec section 6.
"""

from __future__ import annotations

from pathlib import Path


class LocalStorageBackend:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _resolve(self, path: str) -> Path:
        return self.root / path

    def _require_file(self, path: str) -> None:
        """Refuse the root, folders, empty paths and traversal segments.

        Mirrors the Drive guardrail: the backend only ever touches files
        inside its root directory.
        """
        if not path:
            raise ValueError("refusing to address the storage root: path is empty")
        if any(segment in ("", ".", "..") for segment in path.split("/")):
            raise ValueError(
                f"refusing non-file path {path!r}: empty or traversal segments"
            )
        if self._resolve(path).is_dir():
            raise IsADirectoryError(path)

    def read(self, path: str) -> str:
        self._require_file(path)
        return self._resolve(path).read_text(encoding="utf-8")

    def write(self, path: str, content: str) -> None:
        self._require_file(path)
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def exists(self, path: str) -> bool:
        return self._resolve(path).exists()

    def delete(self, path: str) -> None:
        self._require_file(path)
        target = self._resolve(path)
        if not target.exists():
            raise FileNotFoundError(path)
        target.unlink()

    def list_files(self, folder: str) -> list[str]:
        directory = self._resolve(folder)
        if not directory.is_dir():
            return []
        return [
            str(p.relative_to(self.root)).replace("\\", "/")
            for p in sorted(directory.rglob("*"))
            if p.is_file()
        ]
