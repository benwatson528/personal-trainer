"""Drive backend behaviour with a stubbed googleapiclient (no network).

Covers index build, folder creation on write (with nested paths — the
`PurePosixPath(parent) == "."` root case), create-vs-update, and read.
Uses a real OAuth token file parsed locally; the google client is faked.
"""

from __future__ import annotations

import json

from personal_trainer.config import StorageConfig
from personal_trainer.storage import drive as drive_mod

_FOLDER_MIME = "application/vnd.google-apps.folder"


class _Resp:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class _FilesResource:
    def __init__(self, service):
        self._service = service

    def list(self, **kwargs):
        files = [{"id": i, "name": n, "mimeType": m}
                 for i, (n, m) in self._service.files_index.items()]
        return _Resp({"files": files})

    def create(self, body=None, media_body=None, fields=None):
        service = self._service
        service.next_id += 1
        fid = f"id-{service.next_id}"
        service.creates.append(
            {"name": body.get("name"), "mimeType": body.get("mimeType"),
             "parents": body.get("parents"), "media": media_body is not None}
        )
        service.files_index[fid] = (body["name"], body.get("mimeType", _FOLDER_MIME))
        return _Resp({"id": fid})

    def update(self, fileId=None, body=None, media_body=None):
        self._service.updates.append({"name": body.get("name")})
        return _Resp({"id": fileId})

    def get_media(self, fileId=None):
        return _Resp(b"content-of-" + fileId.encode())

    def delete(self, fileId=None):
        self._service.deletes.append(fileId)
        return _Resp(None)


class _FakeDrive:
    def __init__(self):
        self.next_id = 0
        self.files_index = {}  # id -> (name, mimeType)
        self.creates: list[dict] = []
        self.updates: list[dict] = []
        self.deletes: list[str] = []

    def files(self):
        return _FilesResource(self)


def _token_file(tmp_path):
    token = {
        "client_id": "c", "client_secret": "s",
        "refresh_token": "r", "type": "authorized_user",
    }
    path = tmp_path / "token.json"
    path.write_text(json.dumps(token), encoding="utf-8")
    return str(path)


def _backend(tmp_path, monkeypatch):
    monkeypatch.setenv("GDRIVE_ROOT_FOLDER_ID", "root")
    monkeypatch.setattr(
        "googleapiclient.discovery.build", lambda *a, **k: _FakeDrive()
    )
    return drive_mod.GoogleDriveStorageBackend(
        StorageConfig(oauth_token_file=_token_file(tmp_path))
    )


def test_write_creates_nested_folders_then_file(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)

    backend.write("generated/strong/2026_10_x.csv", "a,b\n1,2\n")

    folder_creates = [c for c in backend._service.creates
                      if c["mimeType"] == _FOLDER_MIME]
    assert [c["name"] for c in folder_creates] == ["generated", "strong"]
    # first folder under root, second under the just-created first.
    assert folder_creates[0]["parents"] == ["root"]
    assert folder_creates[1]["parents"] == ["id-1"]

    file_create = [c for c in backend._service.creates
                   if c["mimeType"] != _FOLDER_MIME]
    assert len(file_create) == 1
    assert file_create[0]["name"] == "2026_10_x.csv"
    assert file_create[0]["media"] is True
    # file lives inside the "strong" folder (id-2)
    assert file_create[0]["parents"] == ["id-2"]

    assert backend.exists("generated/strong/2026_10_x.csv")
    assert backend.read("generated/strong/2026_10_x.csv") == "content-of-id-3"


def test_list_files_root_and_subfolder(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)
    backend.write("state/training_state.yaml", "a: 1\n")
    backend.write("history/strong/h.csv", "x\n")

    # root lists everything recursively; folders are not returned
    assert backend.list_files("") == ["history/strong/h.csv", "state/training_state.yaml"]
    assert backend.list_files("state") == ["state/training_state.yaml"]


def test_write_at_root_creates_single_folder(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)
    backend.write("state/training_state.yaml", "pending_workout: null\n")

    folder_creates = [c for c in backend._service.creates
                      if c["mimeType"] == _FOLDER_MIME]
    assert [c["name"] for c in folder_creates] == ["state"]
    assert folder_creates[0]["parents"] == ["root"]


def test_write_existing_file_updates_in_place(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)
    backend.write("sessions/2026-09-01_rowing_a.yaml", "one: 1\n")
    backend.write("sessions/2026-09-01_rowing_a.yaml", "one: 2\n")

    assert backend._service.updates == [
        {"name": "2026-09-01_rowing_a.yaml"}
    ]
    # only one file create, then an update
    file_creates = [c for c in backend._service.creates
                    if c["mimeType"] != _FOLDER_MIME]
    assert len(file_creates) == 1


def test_read_missing_raises_file_not_found(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)
    try:
        backend.read("nope/missing.md")
    except FileNotFoundError:
        return
    raise AssertionError("expected FileNotFoundError")


def test_delete_removes_file_and_drops_from_index(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)
    backend.write("programmes/J&T 2.0/templates/day_1.md", "x\n")
    backend.delete("programmes/J&T 2.0/templates/day_1.md")

    assert backend._service.deletes == ["id-4"]  # after 3 folders (ids 1-3)
    assert not backend.exists("programmes/J&T 2.0/templates/day_1.md")
    assert f"programmes/J&T 2.0/templates/day_1.md" not in backend._index


def test_delete_missing_raises_file_not_found(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)
    try:
        backend.delete("nope/missing.md")
    except FileNotFoundError:
        return
    raise AssertionError("expected FileNotFoundError")


def test_refuses_operations_on_root_and_folders(tmp_path, monkeypatch):
    """The integration is confined to files inside the app's root folder."""
    backend = _backend(tmp_path, monkeypatch)
    backend.write("state/training_state.yaml", "a: 1\n")  # creates a "state" folder

    attempts = [
        (backend.read, ("",), ValueError),
        (backend.write, ("", "x"), ValueError),
        (backend.delete, ("",), ValueError),
        (backend.read, ("state",), IsADirectoryError),
        (backend.write, ("state", "x"), IsADirectoryError),
        (backend.delete, ("state",), IsADirectoryError),
    ]
    for op, args, exc in attempts:
        try:
            op(*args)
        except exc:
            continue
        raise AssertionError(f"expected {exc.__name__} for {op.__name__}{args}")

    # Nothing was touched: no delete calls, file still present.
    assert backend._service.deletes == []
    assert backend.exists("state/training_state.yaml")


def test_refuses_path_traversal_and_absolute_paths(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)
    for bad in ("../escape.csv", "a/../../x.csv", "/abs.csv", "state/"):
        try:
            backend.write(bad, "x")
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")
    assert backend.list_files("") == []