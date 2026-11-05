import errno
import hashlib
import os
import threading
import time
from urllib.parse import quote

import anyio
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

import app.main as main
from app.main import COOKIE_NAME
from app.uploads import UPLOAD_CHUNK_BYTES, UPLOAD_IDLE_SECONDS

PASSWORD = "chunk-upload-test-password"


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAX_UPLOAD_MB", "16")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    return tmp_path


@pytest.fixture
def client(storage):
    with TestClient(main.create_app()) as browser:
        yield browser


def login(client):
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 200
    return {"X-CSRF-Token": client.get("/api/session").json()["csrf_token"]}


def start(client, headers, name="example.bin", size=4):
    response = client.post("/api/uploads", json={"name": name, "size": size}, headers=headers)
    assert response.status_code == 201
    data = response.json()
    assert data["chunk_size"] == UPLOAD_CHUNK_BYTES
    assert data["offset"] == 0
    return "/api/uploads/" + data["id"]


def test_multiple_requests_publish_only_the_complete_file(client):
    headers = login(client)
    name = "회의 자료 + #1.bin"
    first = bytes(range(256)) * (UPLOAD_CHUNK_BYTES // 256)
    last = b"last chunk\x00\xff"
    url = start(client, headers, name, len(first) + len(last))
    response = client.put(url + "?offset=0", content=first, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"offset": len(first)}
    assert client.get("/api/files").json()["files"] == []
    assert client.get("/api/files/" + quote(name)).status_code == 404
    assert client.post(url + "/complete", headers=headers).status_code == 409
    response = client.put(url + f"?offset={len(first)}", content=last, headers=headers)
    assert response.json() == {"offset": len(first) + len(last)}
    assert client.get("/api/files").json()["files"] == []
    response = client.post(url + "/complete", headers=headers)
    assert response.status_code == 201
    assert response.json() == {"name": name, "size": len(first) + len(last)}
    assert client.get("/api/files/" + quote(name)).content == first + last
    assert not list(client.app.state.temporary.iterdir())
    assert client.post(url + "/complete", headers=headers).status_code == 404


def test_empty_file_and_duplicate_name_preserve_existing_files(client):
    headers = login(client)
    for expected in ("empty.txt", "empty (1).txt"):
        url = start(client, headers, "empty.txt", 0)
        response = client.post(url + "/complete", headers=headers)
        assert response.status_code == 201
        assert response.json() == {"name": expected, "size": 0}
        assert client.get("/api/files/" + quote(expected)).content == b""
    assert len(client.get("/api/files").json()["files"]) == 2


@pytest.mark.parametrize(
    "size, status", [(-1, 422), (1.5, 422), ("4", 422), (True, 422), (16 * 1024 * 1024 + 1, 413)]
)
def test_invalid_or_oversize_total_never_creates_temporary_files(client, size, status):
    headers = login(client)
    response = client.post("/api/uploads", json={"name": "test.bin", "size": size}, headers=headers)
    assert response.status_code == status
    assert not list(client.app.state.temporary.iterdir())


@pytest.mark.parametrize("name", [".secret", "../escape", "folder/file", "a\nb", "한" * 81])
def test_chunk_upload_validates_names(client, name):
    headers = login(client)
    response = client.post("/api/uploads", json={"name": name, "size": 4}, headers=headers)
    assert response.status_code == 400
    assert not list(client.app.state.temporary.iterdir())


def test_new_routes_require_authentication_csrf_and_same_origin(client):
    operations = [
        ("POST", "/api/uploads"),
        ("PUT", "/api/uploads/unknown?offset=0"),
        ("POST", "/api/uploads/unknown/complete"),
        ("DELETE", "/api/uploads/unknown"),
    ]
    for method, path in operations:
        assert client.request(method, path).status_code == 401
    headers = login(client)
    for method, path in operations:
        assert client.request(method, path).status_code == 403
        assert client.request(method, path, headers={"X-CSRF-Token": "bad"}).status_code == 403
        assert (
            client.request(
                method, path, headers={**headers, "Origin": "https://evil.example"}
            ).status_code
            == 403
        )


def test_other_session_cannot_append_complete_or_cancel_upload(client):
    owner = login(client)
    cookie = client.cookies.get(COOKIE_NAME)
    url = start(client, owner)
    client.cookies.clear()
    other = login(client)
    assert client.put(url + "?offset=0", content=b"test", headers=other).status_code == 404
    assert client.post(url + "/complete", headers=other).status_code == 404
    assert client.delete(url, headers=other).status_code == 404
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, cookie)
    assert client.put(url + "?offset=0", content=b"test", headers=owner).status_code == 200
    assert client.post(url + "/complete", headers=owner).status_code == 201


def test_wrong_offsets_and_replayed_chunks_do_not_corrupt_file(client):
    headers = login(client)
    url = start(client, headers, size=8)
    assert client.put(url, content=b"test", headers=headers).status_code == 422
    assert client.put(url + "?offset=-1", content=b"test", headers=headers).status_code == 422
    assert client.put(url + "?offset=4", content=b"test", headers=headers).status_code == 409
    assert client.put(url + "?offset=0", content=b"test", headers=headers).status_code == 200
    assert client.put(url + "?offset=0", content=b"bad!", headers=headers).status_code == 409
    assert client.put(url + "?offset=4", content=b"file", headers=headers).status_code == 200
    assert client.post(url + "/complete", headers=headers).status_code == 201
    assert client.get("/api/files/example.bin").content == b"testfile"


@pytest.mark.parametrize("streamed", [False, True])
def test_chunk_cannot_exceed_request_limit_or_declared_file_size(client, streamed):
    headers = login(client)
    for total, sent in [(16 * 1024 * 1024, UPLOAD_CHUNK_BYTES + 1), (4, 5)]:
        url = start(client, headers, size=total)
        content = (part for part in (b"x", b"y" * (sent - 1))) if streamed else b"x" * sent
        response = client.put(url + "?offset=0", content=content, headers=headers)
        assert response.status_code == 413
        upload = client.app.state.uploads.pending[url.rsplit("/", 1)[1]]
        assert upload.offset == 0
        assert upload.path.stat().st_size == 0
        assert client.delete(url, headers=headers).status_code == 200
    assert not list(client.app.state.temporary.iterdir())


@pytest.mark.parametrize("declared", ["wrong", "-1", "3", "5"])
def test_invalid_or_truncated_chunk_preserves_previous_data(client, declared):
    headers = login(client)
    url = start(client, headers, size=8)
    client.put(url + "?offset=0", content=b"keep", headers=headers)
    response = client.put(
        url + "?offset=4", content=b"xx", headers={**headers, "Content-Length": declared}
    )
    assert response.status_code in {400, 413}
    assert client.put(url + "?offset=4", content=b"good", headers=headers).status_code == 200
    assert client.post(url + "/complete", headers=headers).status_code == 201
    assert client.get("/api/files/example.bin").content == b"keepgood"


def test_disconnected_chunk_rolls_back_and_can_be_sent_again(client):
    headers = login(client)
    url = start(client, headers, size=8)
    client.put(url + "?offset=0", content=b"keep", headers=headers)
    messages = iter(
        [
            {"type": "http.request", "body": b"bad", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(messages)

    async def disconnect():
        request = Request({"type": "http", "headers": []}, receive)
        await client.app.state.uploads.append(
            url.rsplit("/", 1)[1], headers["X-CSRF-Token"], 4, request
        )

    with pytest.raises(HTTPException) as failure:
        client.portal.call(disconnect)
    assert failure.value.status_code == 400
    assert client.put(url + "?offset=4", content=b"good", headers=headers).status_code == 200
    assert client.post(url + "/complete", headers=headers).status_code == 201
    assert client.get("/api/files/example.bin").content == b"keepgood"


def test_concurrent_requests_cannot_modify_or_remove_active_upload(client):
    headers = login(client)
    url = start(client, headers)
    started = threading.Event()
    release = threading.Event()

    async def receive():
        started.set()
        await anyio.to_thread.run_sync(release.wait)
        return {"type": "http.request", "body": b"test", "more_body": False}

    async def append():
        request = Request({"type": "http", "headers": []}, receive)
        return await client.app.state.uploads.append(
            url.rsplit("/", 1)[1], headers["X-CSRF-Token"], 0, request
        )

    future = client.portal.start_task_soon(append)
    try:
        assert started.wait(5)
        assert client.put(url + "?offset=0", content=b"bad!", headers=headers).status_code == 409
        assert client.post(url + "/complete", headers=headers).status_code == 409
        assert client.delete(url, headers=headers).status_code == 409
    finally:
        release.set()
    assert future.result(timeout=5) == {"offset": 4}
    assert client.post(url + "/complete", headers=headers).status_code == 201
    assert client.get("/api/files/example.bin").content == b"test"


def test_cancel_and_idle_expiry_remove_partial_files(client):
    headers = login(client)
    url = start(client, headers, size=8)
    client.put(url + "?offset=0", content=b"half", headers=headers)
    assert client.delete(url, headers=headers).status_code == 200
    assert not list(client.app.state.temporary.iterdir())
    assert client.post(url + "/complete", headers=headers).status_code == 404
    url = start(client, headers)
    upload = client.app.state.uploads.pending[url.rsplit("/", 1)[1]]
    upload.touched -= UPLOAD_IDLE_SECONDS + 1
    assert client.put(url + "?offset=0", content=b"test", headers=headers).status_code == 404
    next_url = start(client, headers)
    assert not upload.path.exists()
    assert client.delete(next_url, headers=headers).status_code == 200
    assert not list(client.app.state.temporary.iterdir())


def test_background_cleanup_removes_uploads_after_logout(storage, monkeypatch):
    monkeypatch.setattr(main, "CLEANUP_INTERVAL_SECONDS", 0.01)
    with TestClient(main.create_app()) as client:
        headers = login(client)
        start(client, headers)
        assert client.post("/api/logout", headers=headers).status_code == 200
        deadline = time.monotonic() + 2
        while list(client.app.state.temporary.iterdir()) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not list(client.app.state.temporary.iterdir())


def test_expired_login_cannot_complete_upload(client):
    headers = login(client)
    url = start(client, headers)
    cookie = client.cookies.get(COOKIE_NAME)
    client.app.state.sessions[hashlib.sha256(cookie.encode()).hexdigest()].expires = time.time() - 1
    assert client.post(url + "/complete", headers=headers).status_code == 401


def test_disk_full_at_completion_cleans_up_without_publishing(client, monkeypatch):
    headers = login(client)
    url = start(client, headers)
    client.put(url + "?offset=0", content=b"test", headers=headers)

    def full(*args):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", full)
    assert client.post(url + "/complete", headers=headers).status_code == 507
    assert client.get("/api/files").json()["files"] == []
    assert not list(client.app.state.temporary.iterdir())


def test_retention_starts_when_complete_file_is_published(client):
    headers = login(client)
    url = start(client, headers)
    client.put(url + "?offset=0", content=b"test", headers=headers)
    temporary = next(client.app.state.temporary.iterdir())
    old = time.time() - 2 * 86400
    os.utime(temporary, (old, old))
    client.put("/api/settings", json={"enabled": True, "retention_hours": 1}, headers=headers)
    before = time.time()
    assert client.post(url + "/complete", headers=headers).status_code == 201
    file = client.get("/api/files").json()["files"][0]
    assert before <= file["modified"] <= time.time()
    assert file["expires_at"] == file["modified"] + 3600


def test_unfinished_uploads_are_removed_on_shutdown(storage):
    with TestClient(main.create_app()) as client:
        headers = login(client)
        start(client, headers)
        temporary = next(client.app.state.temporary.iterdir())
    assert not temporary.exists()
