import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.main import COOKIE_NAME, create_app

PASSWORD = "a-private-test-password-123!"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    monkeypatch.setenv("SESSION_HOURS", "1")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    with TestClient(create_app()) as browser:
        yield browser


def login(client):
    response = client.post("/api/login", json={"password": PASSWORD})
    assert response.status_code == 200
    return {"X-CSRF-Token": client.get("/api/session").json()["csrf_token"]}


def test_private_page_and_all_file_routes_require_login(client):
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.get("/").url.path == "/login"
    for method, path in [
        ("get", "/api/files"),
        ("get", "/api/session"),
        ("get", "/api/files/secret.txt"),
        ("put", "/api/files/new.txt"),
        ("delete", "/api/files/secret.txt"),
        ("post", "/api/logout"),
    ]:
        assert getattr(client, method)(path).status_code == 401
    assert client.get("/assets/index.html").status_code == 404
    assert client.get("/assets/../main.py").status_code == 404
    assert client.get("/data/secret.txt").status_code == 404
    assert client.get("/health").json() == {"status": "ok"}


def test_password_cookie_and_security_headers(client):
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    response = client.post("/api/login", json={"password": PASSWORD})
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    page = client.get("/")
    assert page.status_code == 200 and "내 파일" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    policy = dict(
        directive.strip().split(" ", 1)
        for directive in page.headers["content-security-policy"].split(";")
    )
    assert policy["style-src"] == "'self' https://cdn.jsdelivr.net"
    assert policy["font-src"] == "'self' https://cdn.jsdelivr.net"
    assert policy["script-src"] == "'self'"
    assert (
        client.get("/login").headers["content-security-policy"]
        == page.headers["content-security-policy"]
    )


def test_upload_list_download_unicode_and_empty_file(client):
    headers = login(client)
    name = "회의 자료 + #1.txt"
    contents = "안녕하세요\nfile contents".encode()
    response = client.put("/api/files/" + quote(name, safe=""), content=contents, headers=headers)
    assert response.status_code == 201
    assert response.json() == {"name": name, "size": len(contents)}
    root = client.app.state.root
    assert (root / name).read_bytes() == contents
    listing = client.get("/api/files").json()
    assert listing["total_bytes"] == len(contents)
    assert listing["files"][0]["name"] == name
    downloaded = client.get("/api/files/" + quote(name, safe=""))
    assert downloaded.content == contents
    assert downloaded.headers["content-type"] == "application/octet-stream"
    assert "attachment; filename*=UTF-8''" in downloaded.headers["content-disposition"]
    assert downloaded.headers["content-length"] == str(len(contents))
    assert client.put("/api/files/empty.txt", content=b"", headers=headers).status_code == 201
    assert client.get("/api/files/empty.txt").content == b""


def test_duplicate_files_do_not_overwrite_even_concurrently(client):
    headers = login(client)

    def upload(number):
        response = client.put(
            "/api/files/report.pdf", content=str(number).encode(), headers=headers
        )
        assert response.status_code == 201
        return response.json()["name"]

    with ThreadPoolExecutor(max_workers=6) as workers:
        names = list(workers.map(upload, range(6)))
    assert len(set(names)) == 6
    assert "report.pdf" in names and "report (5).pdf" in names
    assert sorted((client.app.state.root / name).read_text() for name in names) == list("012345")
    assert not list(client.app.state.temporary.iterdir())


def test_long_unicode_duplicate_remains_listable_and_downloadable(client):
    headers = login(client)
    name = "한" * 78 + ".txt"
    for _ in range(2):
        response = client.put("/api/files/" + quote(name), content=b"test", headers=headers)
        assert response.status_code == 201
        stored = response.json()["name"]
        assert len(stored.encode()) <= 240
        assert client.get("/api/files/" + quote(stored)).content == b"test"
    assert len(client.get("/api/files").json()["files"]) == 2


def test_csrf_and_foreign_origin_are_blocked(client):
    headers = login(client)
    assert client.put("/api/files/a.txt", content=b"a").status_code == 403
    assert client.post("/api/logout").status_code == 403
    assert (
        client.put("/api/files/a.txt", content=b"a", headers={"X-CSRF-Token": "wrong"}).status_code
        == 403
    )
    foreign = {**headers, "Origin": "https://evil.example"}
    assert client.put("/api/files/a.txt", content=b"a", headers=foreign).status_code == 403
    assert (
        client.post("/api/login", json={"password": PASSWORD}, headers=foreign).status_code == 403
    )
    assert (
        client.put(
            "/api/files/a.txt", content=b"a", headers={**headers, "Origin": "http://testserver"}
        ).status_code
        == 201
    )


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        "..%2Fsecret",
        "folder%2Ftest.txt",
        "%5Csecret",
        "%20leading.txt",
        "trailing.txt%20",
        "bad%0Aname",
        "bad%E2%80%AEname",
        "a" * 241,
    ],
)
def test_unsafe_names_are_rejected(client, name):
    headers = login(client)
    # 줄바꿈이 포함된 경로는 라우터 단계에서 404로 거부될 수도 있습니다.
    assert client.put("/api/files/" + name, content=b"secret", headers=headers).status_code in {
        400,
        404,
    }
    assert client.get("/api/files/" + name).status_code in {400, 404}
    assert client.delete("/api/files/" + name, headers=headers).status_code in {400, 404}


def test_delete_removes_only_selected_file_and_updates_listing(client):
    headers = login(client)
    name = "삭제할 회의 자료 + #1.txt"
    url = "/api/files/" + quote(name, safe="")
    assert client.put(url, content=b"remove this", headers=headers).status_code == 201
    client.put("/api/files/keep.txt", content=b"keep", headers=headers)
    response = client.delete(url, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "name": name}
    assert not (client.app.state.root / name).exists()
    assert client.get(url).status_code == 404
    listing = client.get("/api/files").json()
    assert [file["name"] for file in listing["files"]] == ["keep.txt"]
    assert listing["total_bytes"] == 4
    assert client.get("/api/files/keep.txt").content == b"keep"
    assert client.delete(url, headers=headers).status_code == 404


def test_delete_requires_csrf_and_same_origin(client):
    headers = login(client)
    url = "/api/files/protected.txt"
    client.put(url, content=b"protected", headers=headers)
    assert client.delete(url).status_code == 403
    assert client.delete(url, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert (
        client.delete(url, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    )
    assert client.get(url).content == b"protected"
    assert client.delete(url, headers={**headers, "Origin": "http://testserver"}).status_code == 200


def test_delete_does_not_remove_directories_symlinks_or_special_files(client):
    headers = login(client)
    root = client.app.state.root
    (root / "keep.txt").write_text("keep")
    (root / "link.txt").symlink_to(root / "keep.txt")
    (root / "directory").mkdir()
    (root / "directory" / "keep.txt").write_text("nested")
    os.mkfifo(root / "pipe")
    for name in ["link.txt", "directory", "pipe", "missing.txt"]:
        assert client.delete("/api/files/" + name, headers=headers).status_code == 404
    assert (root / "keep.txt").read_text() == "keep"
    assert (root / "link.txt").is_symlink()
    assert (root / "directory" / "keep.txt").read_text() == "nested"
    assert (root / "pipe").exists()


@pytest.mark.parametrize("error_number, status", [(13, 403), (30, 403), (5, 500)])
def test_delete_reports_filesystem_errors_without_removing_file(
    client, monkeypatch, error_number, status
):
    from pathlib import Path

    headers = login(client)
    target = client.app.state.root / "keep.txt"
    target.write_text("keep")
    original_unlink = Path.unlink

    def fail_unlink(path, *args, **kwargs):
        if path == target:
            raise OSError(error_number, "test error")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    response = client.delete("/api/files/keep.txt", headers=headers)
    assert response.status_code == status
    assert target.read_text() == "keep"


def test_oversize_content_length_is_rejected(client):
    headers = login(client)
    response = client.put("/api/files/large.bin", content=b"x" * (1024 * 1024 + 1), headers=headers)
    assert response.status_code == 413
    assert not (client.app.state.root / "large.bin").exists()
    assert not list(client.app.state.temporary.iterdir())


def test_chunked_oversize_is_rejected_and_partial_removed(client):
    headers = login(client)

    def body():
        for _ in range(5):
            yield b"x" * (256 * 1024)

    response = client.put("/api/files/large.bin", content=body(), headers=headers)
    assert response.status_code == 413
    assert not (client.app.state.root / "large.bin").exists()
    assert not list(client.app.state.temporary.iterdir())


def test_disk_full_returns_clear_error_and_cleans_up(client, monkeypatch):
    import errno

    headers = login(client)

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", full)
    response = client.put("/api/files/test.bin", content=b"test", headers=headers)
    assert response.status_code == 507
    assert not list(client.app.state.temporary.iterdir())
    assert not (client.app.state.root / "test.bin").exists()


def test_external_files_hidden_files_and_symlinks(client, tmp_path):
    login(client)
    root = client.app.state.root
    (root / "existing.txt").write_text("existing")
    (root / ".secret").write_text("secret")
    (root / "directory").mkdir()
    (root / "link.txt").symlink_to("/etc/passwd")
    os.mkfifo(root / "pipe")
    assert [f["name"] for f in client.get("/api/files").json()["files"]] == ["existing.txt"]
    assert client.get("/api/files/existing.txt").text == "existing"
    assert client.get("/api/files/link.txt").status_code == 404
    assert client.get("/api/files/directory").status_code == 404
    assert client.get("/api/files/pipe").status_code == 404


def test_logout_revokes_copied_cookie(client):
    headers = login(client)
    token = client.cookies.get(COOKIE_NAME)
    assert client.post("/api/logout", headers=headers).status_code == 200
    client.cookies.set(COOKIE_NAME, token)
    assert client.get("/api/files").status_code == 401


def test_expired_sessions_are_rejected(client):
    login(client)
    token = client.cookies.get(COOKIE_NAME)
    session = client.app.state.sessions[hashlib.sha256(token.encode()).hexdigest()]
    session.expires = time.time() - 1
    assert client.get("/api/files").status_code == 401


def test_login_attempts_are_limited(client):
    for _ in range(5):
        assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    response = client.post("/api/login", json={"password": PASSWORD})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "300"


def test_secure_cookie_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COOKIE_SECURE", "true")
    with TestClient(create_app(), base_url="https://testserver") as client:
        response = client.post("/api/login", json={"password": PASSWORD})
        assert "Secure" in response.headers["set-cookie"]
        assert client.get("/api/files").status_code == 200


def test_files_survive_restart_but_sessions_do_not(client):
    headers = login(client)
    client.put("/api/files/persistent.txt", content=b"persist", headers=headers)
    token = client.cookies.get(COOKIE_NAME)
    with TestClient(create_app()) as second:
        second.cookies.set(COOKIE_NAME, token)
        assert second.get("/api/files").status_code == 401
        login(second)
        assert second.get("/api/files/persistent.txt").content == b"persist"


@pytest.mark.parametrize("password", ["a", "short", "a" * 11])
def test_short_password_allows_startup_and_login(tmp_path, monkeypatch, password):
    monkeypatch.setenv("APP_PASSWORD", password)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COOKIE_SECURE", "false")
    with TestClient(create_app()) as client:
        assert client.get("/api/files").status_code == 401
        assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
        assert client.post("/api/login", json={"password": password}).status_code == 200
        assert client.get("/api/files").status_code == 200
