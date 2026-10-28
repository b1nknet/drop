import logging
import os
import threading
import time
from contextlib import nullcontext
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.settings import SETTINGS_FILENAME, AutoDeleteSettings

DAY = 24 * 3600


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setenv("APP_PASSWORD", "retention-test-password")
    monkeypatch.setenv("DATA_DIR", str(root))
    monkeypatch.setenv("COOKIE_SECURE", "false")
    (root / SETTINGS_FILENAME).write_text(
        AutoDeleteSettings(enabled=True, retention_hours=24).model_dump_json()
    )
    return root


def write_at(path, modified):
    path.write_text("contents")
    os.utime(path, (modified, modified))
    return path


def login(client):
    assert (
        client.post("/api/login", json={"password": "retention-test-password"}).status_code == 200
    )
    return client.get("/api/session").json()


@pytest.mark.parametrize("saved", [False, True])
def test_disabled_retention_preserves_old_files(storage, saved):
    settings_path = storage / SETTINGS_FILENAME
    if saved:
        settings_path.write_text(AutoDeleteSettings().model_dump_json())
    else:
        settings_path.unlink()
    old = write_at(storage / "keep.txt", time.time() - 1000 * DAY)

    with TestClient(main.create_app()) as client:
        login(client)
        assert client.get("/api/settings").json() == {"enabled": False, "retention_hours": 24}
        assert client.get("/api/files").json()["files"][0]["expires_at"] is None
        assert client.get("/api/files/keep.txt").text == "contents"
    assert old.exists()


@pytest.mark.parametrize(
    "contents",
    ["not json", '{"enabled":true,"retention_hours":2}', '{"enabled":"true"}', "x" * 4097],
)
def test_invalid_settings_fail_before_deleting_files(storage, contents):
    (storage / SETTINGS_FILENAME).write_text(contents)
    old = write_at(storage / "keep.txt", time.time() - 2 * DAY)
    with pytest.raises(RuntimeError, match="설정 파일을 읽을 수 없습니다"):
        with TestClient(main.create_app()):
            pass
    assert old.exists()


def test_cleanup_obeys_boundary_and_excludes_unsupported_files(storage, tmp_path, monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(main.time, "time", lambda: now)
    expired = write_at(storage / "만료.txt", now - DAY - 1)
    boundary = write_at(storage / "boundary.txt", now - DAY)
    kept = [
        write_at(storage / "recent.txt", now - DAY + 1),
        write_at(storage / "future.txt", now + DAY),
        write_at(storage / ".hidden", now - 2 * DAY),
        write_at(storage / " leading.txt", now - 2 * DAY),
        write_at(storage / "bad\nname.txt", now - 2 * DAY),
        write_at(tmp_path / "outside.txt", now - 2 * DAY),
    ]
    directory = storage / "directory"
    directory.mkdir()
    kept.append(write_at(directory / "nested.txt", now - 2 * DAY))
    temporary = storage / ".drop-tmp"
    temporary.mkdir()
    kept.append(write_at(temporary / "upload-active.part", now - 2 * DAY))
    link = storage / "link.txt"
    link.symlink_to(tmp_path / "outside.txt")
    broken = storage / "broken.txt"
    broken.symlink_to(tmp_path / "missing.txt")
    os.mkfifo(storage / "pipe")

    settings = AutoDeleteSettings(enabled=True, retention_hours=24)
    assert main.cleanup_expired_files(storage, settings, threading.Lock()) == 2
    assert not expired.exists() and not boundary.exists()
    assert all(path.read_text() == "contents" for path in kept)
    assert link.is_symlink() and broken.is_symlink()
    assert (storage / "pipe").exists()


def test_startup_removes_expired_files_and_reports_expiry(storage):
    expired = write_at(storage / "expired.txt", time.time() - 2 * DAY)
    recent = write_at(storage / "recent.txt", time.time() - 3600)
    with TestClient(main.create_app()) as client:
        assert not expired.exists()
        login(client)
        assert client.get("/api/settings").json() == {"enabled": True, "retention_hours": 24}
        listing = client.get("/api/files").json()
        assert listing["total_bytes"] == len("contents")
        assert listing["files"] == [
            {
                "name": "recent.txt",
                "size": len("contents"),
                "modified": recent.stat().st_mtime,
                "expires_at": recent.stat().st_mtime + DAY,
            }
        ]
        assert client.get("/api/files/expired.txt").status_code == 404


def test_background_cleanup_runs_without_requests_and_stops_at_shutdown(storage, monkeypatch):
    monkeypatch.setattr(main, "CLEANUP_INTERVAL_SECONDS", 0.01)
    expired = storage / "expired.txt"
    removed = threading.Event()
    unlink = Path.unlink

    def observe_unlink(path, *args, **kwargs):
        result = unlink(path, *args, **kwargs)
        if path == expired:
            removed.set()
        return result

    monkeypatch.setattr(Path, "unlink", observe_unlink)
    with TestClient(main.create_app()) as client:
        write_at(expired, time.time() - 2 * DAY)
        assert removed.wait(timeout=3)
        assert not expired.exists()
        assert client.get("/health").json() == {"status": "ok"}
    removed.clear()
    write_at(expired, time.time() - 2 * DAY)
    assert not removed.wait(timeout=0.05)
    assert expired.exists()


def test_cleanup_continues_after_permission_errors_and_retries(storage, monkeypatch, caplog):
    blocked = write_at(storage / "blocked.txt", time.time() - 2 * DAY)
    removable = write_at(storage / "removable.txt", time.time() - 2 * DAY)
    unlink = Path.unlink
    fail = True

    def fail_unlink(path, *args, **kwargs):
        if path == blocked and fail:
            raise PermissionError("read-only file")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        assert (
            main.cleanup_expired_files(storage, AutoDeleteSettings(enabled=True), threading.Lock())
            == 1
        )
    assert blocked.exists() and not removable.exists()
    assert "blocked.txt" in caplog.text
    fail = False
    assert (
        main.cleanup_expired_files(storage, AutoDeleteSettings(enabled=True), threading.Lock()) == 1
    )
    assert not blocked.exists()


def test_background_cleanup_recovers_after_scan_error(storage, monkeypatch, caplog):
    monkeypatch.setattr(main, "CLEANUP_INTERVAL_SECONDS", 0.01)
    expired = write_at(storage / "expired.txt", time.time() - 2 * DAY)
    scandir = os.scandir
    removed = threading.Event()
    unlink = Path.unlink
    fail = True

    def fail_first_scan(path):
        nonlocal fail
        if path == storage and fail:
            fail = False
            raise PermissionError("temporarily inaccessible")
        return scandir(path)

    def observe_unlink(path, *args, **kwargs):
        result = unlink(path, *args, **kwargs)
        if path == expired:
            removed.set()
        return result

    monkeypatch.setattr(os, "scandir", fail_first_scan)
    monkeypatch.setattr(Path, "unlink", observe_unlink)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        with TestClient(main.create_app()):
            assert removed.wait(timeout=3)
    assert "저장 폴더를 읽지 못했습니다" in caplog.text
    assert not expired.exists()


def test_cleanup_preserves_new_file_replacing_an_expired_entry(storage, monkeypatch):
    target = write_at(storage / "replaced.txt", time.time() - 2 * DAY)
    with os.scandir(storage) as scanner:
        entries = list(scanner)
    assert entries[0].stat().st_mtime < time.time() - DAY
    target.unlink()
    target.write_text("new upload")
    monkeypatch.setattr(os, "scandir", lambda _: nullcontext(iter(entries)))
    assert (
        main.cleanup_expired_files(storage, AutoDeleteSettings(enabled=True), threading.Lock()) == 0
    )
    assert target.read_text() == "new upload"


@pytest.mark.parametrize("contents", [b"", b"uploaded data"])
def test_upload_retention_starts_at_publication_and_keeps_duplicate_names(
    storage, monkeypatch, contents
):
    fsync = os.fsync

    def age_temporary_file(fd):
        fsync(fd)
        old = time.time() - 2 * DAY
        os.utime(fd, (old, old))

    monkeypatch.setattr(os, "fsync", age_temporary_file)
    with TestClient(main.create_app()) as client:
        session = login(client)
        headers = {"X-CSRF-Token": session["csrf_token"]}
        started = time.time()
        for _ in range(2):
            assert (
                client.put("/api/files/file.txt", content=contents, headers=headers).status_code
                == 201
            )
        files = client.get("/api/files").json()["files"]
        assert {file["name"] for file in files} == {"file.txt", "file (1).txt"}
        for file in files:
            assert file["expires_at"] >= started + DAY
            assert (storage / file["name"]).read_bytes() == contents


def test_settings_require_login_csrf_and_same_origin(storage):
    update = {"enabled": False, "retention_hours": 3}
    with TestClient(main.create_app()) as client:
        assert client.get("/api/settings").status_code == 401
        assert client.put("/api/settings", json=update).status_code == 401
        session = login(client)
        headers = {"X-CSRF-Token": session["csrf_token"]}
        assert client.put("/api/settings", json=update).status_code == 403
        assert (
            client.put("/api/settings", json=update, headers={"X-CSRF-Token": "wrong"}).status_code
            == 403
        )
        assert (
            client.put(
                "/api/settings", json=update, headers={**headers, "Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert client.get("/api/settings").json()["enabled"] is True
        assert (
            client.put(
                "/api/settings", json=update, headers={**headers, "Origin": "http://testserver"}
            ).status_code
            == 200
        )
        assert client.get("/api/settings").json() == update


@pytest.mark.parametrize("hours", [1, 3, 6, 12, 24, 72, 120, 168])
def test_all_supported_periods_persist_and_survive_restart(storage, hours):
    update = {"enabled": True, "retention_hours": hours}
    with TestClient(main.create_app()) as client:
        headers = {"X-CSRF-Token": login(client)["csrf_token"]}
        recent = write_at(storage / "recent.txt", time.time())
        assert client.put("/api/settings", json=update, headers=headers).json() == update
        listing = client.get("/api/files").json()
        assert listing["settings"] == update
        assert [file["name"] for file in listing["files"]] == ["recent.txt"]
        assert listing["files"][0]["expires_at"] == recent.stat().st_mtime + hours * 3600
        assert client.get("/api/files/" + SETTINGS_FILENAME).status_code == 400
    with TestClient(main.create_app()) as restarted:
        login(restarted)
        assert restarted.get("/api/settings").json() == update
        assert restarted.get("/api/files/recent.txt").text == "contents"


@pytest.mark.parametrize(
    "update",
    [
        {"enabled": True, "retention_hours": hours}
        for hours in [0, -1, 2, 48, 169, 1.5, True, "1", None]
    ]
    + [{"enabled": "true", "retention_hours": 1}, {"enabled": True, "extra": 1}],
)
def test_invalid_settings_updates_leave_saved_policy_unchanged(storage, update):
    original = (storage / SETTINGS_FILENAME).read_bytes()
    with TestClient(main.create_app()) as client:
        headers = {"X-CSRF-Token": login(client)["csrf_token"]}
        assert client.put("/api/settings", json=update, headers=headers).status_code == 422
        assert client.get("/api/settings").json() == {"enabled": True, "retention_hours": 24}
    assert (storage / SETTINGS_FILENAME).read_bytes() == original


@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_settings_write_errors_preserve_previous_settings(storage, monkeypatch, operation):
    original = (storage / SETTINGS_FILENAME).read_bytes()
    with TestClient(main.create_app()) as client:
        headers = {"X-CSRF-Token": login(client)["csrf_token"]}

        def fail(*args):
            raise OSError("disk error")

        monkeypatch.setattr(os, operation, fail)
        response = client.put(
            "/api/settings", json={"enabled": False, "retention_hours": 1}, headers=headers
        )
        assert response.status_code == 500
        assert "설정을 저장하지 못했습니다" in response.json()["detail"]
        assert client.get("/api/settings").json() == {"enabled": True, "retention_hours": 24}
        assert (storage / SETTINGS_FILENAME).read_bytes() == original
        assert not list(client.app.state.temporary.iterdir())


def test_toggle_controls_background_cleanup_without_restart(storage, monkeypatch):
    (storage / SETTINGS_FILENAME).write_text(AutoDeleteSettings().model_dump_json())
    monkeypatch.setattr(main, "CLEANUP_INTERVAL_SECONDS", 0.01)
    expired = write_at(storage / "expired.txt", time.time() - 2 * DAY)
    removed = threading.Event()
    unlink = Path.unlink

    def observe_unlink(path, *args, **kwargs):
        result = unlink(path, *args, **kwargs)
        if path == expired:
            removed.set()
        return result

    monkeypatch.setattr(Path, "unlink", observe_unlink)
    with TestClient(main.create_app()) as client:
        headers = {"X-CSRF-Token": login(client)["csrf_token"]}
        assert not removed.wait(timeout=0.03)
        assert (
            client.put(
                "/api/settings", json={"enabled": True, "retention_hours": 1}, headers=headers
            ).status_code
            == 200
        )
        assert removed.wait(timeout=3)
        assert not expired.exists()
        disabled = {"enabled": False, "retention_hours": 1}
        assert client.put("/api/settings", json=disabled, headers=headers).json() == disabled
        removed.clear()
        write_at(expired, time.time() - 2 * DAY)
        assert not removed.wait(timeout=0.05)
        assert client.get("/api/files").json()["files"][0]["expires_at"] is None
    with TestClient(main.create_app()) as restarted:
        login(restarted)
        assert restarted.get("/api/settings").json() == disabled
        assert expired.exists()


def test_changing_period_and_disabling_interrupt_an_active_sweep(storage, monkeypatch):
    settings = AutoDeleteSettings(enabled=True, retention_hours=1)
    lock = threading.Lock()
    now = time.time()
    first = write_at(storage / "first.txt", now - 2 * 3600)
    second = write_at(storage / "second.txt", now - 2 * 3600)
    third = write_at(storage / "third.txt", now - 4 * 3600)
    with os.scandir(storage) as scanner:
        entries = {entry.name: entry for entry in scanner}

    def changed_entries():
        yield entries[first.name]
        with lock:
            settings.retention_hours = 3
        yield entries[second.name]
        with lock:
            settings.enabled = False
        yield entries[third.name]

    monkeypatch.setattr(os, "scandir", lambda _: nullcontext(changed_entries()))
    assert main.cleanup_expired_files(storage, settings, lock) == 1
    assert not first.exists()
    assert second.exists() and third.exists()


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_settings_file_does_not_follow_links_or_read_special_files(storage, tmp_path, kind):
    path = storage / SETTINGS_FILENAME
    path.unlink()
    if kind == "symlink":
        outside = tmp_path / "outside.json"
        outside.write_text(AutoDeleteSettings(enabled=True).model_dump_json())
        path.symlink_to(outside)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises(RuntimeError, match="설정 파일을 읽을 수 없습니다"):
        with TestClient(main.create_app()):
            pass
