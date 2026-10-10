"""Google Drive storage backend.

Maps logical paths (e.g. "history/strong/strong_history.csv") to files under
a root "Personal Trainer" folder in Drive (spec section 6). Authenticates as
the user via OAuth (see scripts/gdrive_auth.py) — the only route for personal
accounts, since a service account cannot own files. A service account is
still supported for Workspace shared drives.
"""

from __future__ import annotations

import io
import os
from pathlib import PurePosixPath

from ..config import StorageConfig

_MIME_TYPES = {
    ".csv": "text/csv",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".md": "text/markdown",
    ".txt": "text/plain",
}
_FOLDER_MIME = "application/vnd.google-apps.folder"
SCOPES = ["https://www.googleapis.com/auth/drive"]


def _build_credentials(config: StorageConfig):
    """Credentials for the Drive backend.

    OAuth first — a token file written once by ``scripts/gdrive_auth.py`` (or
    ``PT_GOOGLE_OAUTH_TOKEN``). That is the only route that works on a personal
    Google account: service accounts have no storage quota and cannot own
    files, so uploads to a personal My Drive fail with ``storageQuotaExceeded``
    regardless of size. A service-account key is still supported for Workspace
    shared drives, where the drive owns the file rather than the account.
    """
    token_file = config.oauth_token_file or os.environ.get("PT_GOOGLE_OAUTH_TOKEN")
    if token_file:
        from google.oauth2.credentials import Credentials

        return Credentials.from_authorized_user_file(token_file, scopes=SCOPES)

    credentials_file = config.credentials_file or os.environ.get("PT_GOOGLE_CREDENTIALS")
    if credentials_file:
        from google.oauth2.service_account import Credentials

        return Credentials.from_service_account_file(credentials_file, scopes=SCOPES)

    raise ValueError(
        "Google Drive credentials required: set storage.oauth_token_file "
        "(recommended — personal accounts) or storage.credentials_file "
        "(service account — Workspace shared drives)."
    )


class GoogleDriveStorageBackend:
    def __init__(self, config: StorageConfig):
        # The folder id comes from the environment so the committed config.yaml
        # never carries a real Drive id. config.yaml is only a local-dev fallback.
        root_folder_id = os.environ.get("GDRIVE_ROOT_FOLDER_ID") or config.root_folder_id
        if not root_folder_id:
            raise ValueError(
                "storage.root_folder_id is required for the gdrive backend: set "
                "the GDRIVE_ROOT_FOLDER_ID environment variable (or "
                "storage.root_folder_id in config.yaml for local development)."
            )

        from googleapiclient.discovery import build

        creds = _build_credentials(config)
        self._service = build("drive", "v3", credentials=creds)
        self._root_folder_id = root_folder_id
        self._index: dict[str, str] = {}  # logical path -> file id
        self._folders: set[str] = set()  # logical paths that are folders
        self._build_index()

    # ------------------------------------------------------------------ index

    def _build_index(self) -> None:
        """Walk the folder tree once and cache logical path -> file id."""
        self._index[""] = self._root_folder_id  # root path
        folder_ids: dict[str, str] = {"": self._root_folder_id}
        # BFS over subfolders
        pending = [""]
        while pending:
            parent_path = pending.pop(0)
            parent_id = folder_ids[parent_path]
            response = (
                self._service.files()
                .list(
                    q=f"'{parent_id}' in parents and trashed = false",
                    fields="files(id, name, mimeType)",
                    pageSize=1000,
                )
                .execute()
            )
            for item in response.get("files", []):
                logical = f"{parent_path}/{item['name']}" if parent_path else item["name"]
                self._index[logical] = item["id"]
                if item["mimeType"] == _FOLDER_MIME:
                    self._folders.add(logical)
                    folder_ids[logical] = item["id"]
                    pending.append(logical)

    def _folder_id_for(self, path: str, create: bool = False) -> str:
        parent = str(PurePosixPath(path).parent)
        parent_id = self._index.get(parent)
        if parent_id is None and create:
            parent_id = self._ensure_folder(parent)
        if parent_id is None:
            raise FileNotFoundError(f"Folder not found in Drive: {path}")
        return parent_id

    def _ensure_folder(self, folder_path: str) -> str:
        parts = PurePosixPath(folder_path).parts
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else part
            if current not in self._index:
                parent_path = str(PurePosixPath(current).parent)
                parent_id = self._index["" if parent_path == "." else parent_path]
                metadata = {
                    "name": part,
                    "mimeType": _FOLDER_MIME,
                    "parents": [parent_id],
                }
                created = self._service.files().create(body=metadata, fields="id").execute()
                self._index[current] = created["id"]
                self._folders.add(current)
        return self._index[folder_path]

    def _file_id(self, path: str) -> str | None:
        return self._index.get(path)

    # ----------------------------------------------------------------- guardrail

    def _assert_file_path(self, path: str) -> None:
        """Refuse anything that is not a file inside the app's Drive folder.

        Every public action (read/write/delete) must target a *file* resolved
        through the index built under the root folder. The root itself, any
        subfolder, empty paths and relative/traversal segments never address a
        file, so they are rejected before any API call is made. This is the
        boundary that keeps the integration from ever acting outside the app's
        directory.
        """
        if not path:
            raise ValueError("refusing to address the Drive root: path is empty")
        if any(segment in ("", ".", "..") for segment in path.split("/")):
            raise ValueError(
                f"refusing non-file path {path!r}: empty or traversal segments"
            )

    def _require_file(self, path: str) -> None:
        self._assert_file_path(path)
        if path in self._folders:
            raise IsADirectoryError(path)

    # ----------------------------------------------------------------- public

    def read(self, path: str) -> str:
        self._require_file(path)
        file_id = self._file_id(path)
        if file_id is None:
            raise FileNotFoundError(path)
        request = self._service.files().get_media(fileId=file_id)
        return request.execute().decode("utf-8")

    def write(self, path: str, content: str) -> None:
        from googleapiclient.http import MediaIoBaseUpload

        self._require_file(path)
        file_id = self._file_id(path)
        body = {"name": PurePosixPath(path).name}
        media = MediaIoBaseUpload(io.BytesIO(content.encode("utf-8")), mimetype=_MIME_TYPES.get(
            PurePosixPath(path).suffix, "text/plain"
        ), resumable=False)
        if file_id is not None:
            self._service.files().update(fileId=file_id, body=body, media_body=media).execute()
        else:
            body["parents"] = [self._folder_id_for(path, create=True)]
            created = self._service.files().create(body=body, media_body=media, fields="id").execute()
            self._index[path] = created["id"]

    def exists(self, path: str) -> bool:
        return path in self._index

    def delete(self, path: str) -> None:
        self._require_file(path)
        file_id = self._file_id(path)
        if file_id is None:
            raise FileNotFoundError(path)
        self._service.files().delete(fileId=file_id).execute()
        self._index.pop(path, None)

    def list_files(self, folder: str) -> list[str]:
        prefix = f"{folder}/" if folder else ""
        return sorted(
            p for p in self._index
            if p and p.startswith(prefix) and p not in self._folders
        )
