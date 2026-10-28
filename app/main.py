from __future__ import annotations

import errno
import hashlib
import hmac
import os
import secrets
import stat
import tempfile
import threading
import time
import unicodedata
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

import anyio
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.requests import ClientDisconnect

ASSETS = Path(__file__).parent / "static"
COOKIE_NAME = "drop_session"
LOGIN_WINDOW = 300
LOGIN_ATTEMPTS = 5
CHUNK_SIZE = 256 * 1024


@dataclass
class Session:
    expires: float
    csrf: str


class LoginBody(BaseModel):
    password: str = Field(max_length=1024)


def valid_name(name: str) -> str:
    if (
        not name
        or name.startswith(".")
        or name != name.strip()
        or "/" in name
        or "\\" in name
        or len(name.encode("utf-8")) > 240
        or any(unicodedata.category(c).startswith("C") for c in name)
    ):
        raise HTTPException(
            400,
            "파일 이름을 확인해 주세요. 숨김 파일, 경로, 제어 문자 또는 너무 긴 이름은 사용할 수 없습니다.",
        )
    return name


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        password = os.environ.get("APP_PASSWORD", "")
        root = Path(os.environ.get("DATA_DIR", "./data")).resolve()
        root.mkdir(parents=True, exist_ok=True)
        temporary = root / ".drop-tmp"
        if temporary.is_symlink():
            raise RuntimeError("임시 저장 폴더는 심볼릭 링크일 수 없습니다.")
        temporary.mkdir(mode=0o700, exist_ok=True)
        # 단일 worker 전용: 이전 프로세스가 중단되어 남은 임시 파일을 정리합니다.
        for entry in temporary.glob("upload-*.part"):
            if entry.is_file() and not entry.is_symlink():
                entry.unlink()
        try:
            with tempfile.TemporaryFile(dir=temporary):
                pass
        except OSError as exc:
            raise RuntimeError(
                "저장 폴더에 쓰기 권한이 없습니다. UPLOAD_DIR과 APP_UID/APP_GID를 확인하세요."
            ) from exc
        app.state.root = root
        app.state.temporary = temporary
        app.state.max_upload = int(os.environ.get("MAX_UPLOAD_MB", "1024")) * 1024 * 1024
        app.state.session_seconds = int(os.environ.get("SESSION_HOURS", "12")) * 3600
        if app.state.max_upload <= 0 or app.state.session_seconds <= 0:
            raise RuntimeError("MAX_UPLOAD_MB와 SESSION_HOURS는 양수여야 합니다.")
        app.state.secure_cookie = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
        app.state.salt = secrets.token_bytes(32)
        app.state.password_hash = hashlib.scrypt(
            password.encode(), salt=app.state.salt, n=16384, r=8, p=1
        )
        app.state.sessions = {}
        app.state.attempts = defaultdict(deque)
        app.state.auth_lock = threading.Lock()
        app.state.hash_slots = threading.BoundedSemaphore(2)
        yield
        app.state.sessions.clear()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        # 로그인 또한 다른 사이트에서 시작할 수 없도록 Origin을 검사합니다.
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or parsed.netloc != request.headers.get(
                "host"
            ):
                return JSONResponse(
                    {"detail": "다른 사이트에서 보낸 요청은 허용되지 않습니다."}, status_code=403
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' https://cdn.jsdelivr.net; font-src 'self' https://cdn.jsdelivr.net; "
            "img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    def session_for(request: Request) -> Session:
        token = request.cookies.get(COOKIE_NAME, "")
        key = hashlib.sha256(token.encode()).hexdigest()
        session = app.state.sessions.get(key)
        if not session or session.expires <= time.time():
            app.state.sessions.pop(key, None)
            raise HTTPException(401, "로그인이 필요합니다. 비밀번호를 입력해 주세요.")
        return session

    def require_csrf(request: Request, session: Session = Depends(session_for)) -> Session:
        if not hmac.compare_digest(
            request.headers.get("x-csrf-token", "").encode(), session.csrf.encode()
        ):
            raise HTTPException(403, "인증 정보가 올바르지 않습니다. 페이지를 새로고침해 주세요.")
        return session

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/")
    def home(request: Request):
        try:
            session_for(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return FileResponse(ASSETS / "index.html")

    @app.get("/login")
    def login_page():
        return FileResponse(ASSETS / "login.html")

    @app.post("/api/login")
    def login(body: LoginBody, request: Request):
        now = time.time()
        client = request.client.host if request.client else "unknown"
        # 잠금 안에서 예약하므로 동시 요청도 제한을 우회하지 못합니다.
        with app.state.auth_lock:
            attempts = app.state.attempts
            for ip in list(attempts):
                while attempts[ip] and attempts[ip][0] <= now - LOGIN_WINDOW:
                    attempts[ip].popleft()
                if not attempts[ip]:
                    del attempts[ip]
            recent = attempts[client]
            if len(recent) >= LOGIN_ATTEMPTS or sum(map(len, attempts.values())) >= 100:
                raise HTTPException(
                    429,
                    "로그인 시도가 너무 많습니다. 5분 뒤 다시 시도해 주세요.",
                    headers={"Retry-After": "300"},
                )
            recent.append(now)
        with app.state.hash_slots:
            candidate = hashlib.scrypt(
                body.password.encode(), salt=app.state.salt, n=16384, r=8, p=1
            )
        if not hmac.compare_digest(candidate, app.state.password_hash):
            raise HTTPException(401, "비밀번호가 일치하지 않습니다.")
        with app.state.auth_lock:
            for key in list(app.state.sessions):
                if app.state.sessions[key].expires <= now:
                    del app.state.sessions[key]
            if len(app.state.sessions) >= 4096:
                raise HTTPException(503, "현재 접속자가 많습니다. 잠시 뒤 다시 시도해 주세요.")
            old_token = request.cookies.get(COOKIE_NAME, "")
            app.state.sessions.pop(hashlib.sha256(old_token.encode()).hexdigest(), None)
            token = secrets.token_urlsafe(48)
            csrf = secrets.token_urlsafe(32)
            app.state.sessions[hashlib.sha256(token.encode()).hexdigest()] = Session(
                now + app.state.session_seconds, csrf
            )
        response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=app.state.session_seconds,
            httponly=True,
            secure=app.state.secure_cookie,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/api/logout")
    def logout(request: Request, _: Session = Depends(require_csrf)):
        token = request.cookies.get(COOKIE_NAME, "")
        app.state.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(
            COOKIE_NAME, path="/", secure=app.state.secure_cookie, httponly=True, samesite="strict"
        )
        return response

    @app.get("/api/session")
    def session_info(session: Session = Depends(session_for)):
        return {"csrf_token": session.csrf, "max_upload_bytes": app.state.max_upload}

    @app.get("/api/files")
    def list_files(_: Session = Depends(session_for)):
        files = []
        with os.scandir(app.state.root) as entries:
            for entry in entries:
                if entry.name.startswith(".") or not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    valid_name(entry.name)
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError, HTTPException:
                    continue
                files.append({"name": entry.name, "size": info.st_size, "modified": info.st_mtime})
        files.sort(key=lambda f: (-f["modified"], f["name"]))
        return {"files": files, "total_bytes": sum(f["size"] for f in files)}

    @app.put("/api/files/{name:path}", status_code=201)
    async def upload(name: str, request: Request, _: Session = Depends(require_csrf)):
        name = valid_name(name)
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError as exc:
                raise HTTPException(400, "파일 크기 정보가 올바르지 않습니다.") from exc
            if length < 0:
                raise HTTPException(400, "파일 크기 정보가 올바르지 않습니다.")
            if length > app.state.max_upload:
                raise HTTPException(413, "파일이 업로드 최대 크기를 초과합니다.")
        path = None
        try:
            fd, temporary_name = tempfile.mkstemp(
                prefix="upload-", suffix=".part", dir=app.state.temporary
            )
            path = Path(temporary_name)
            size = 0
            with os.fdopen(fd, "wb") as destination:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > app.state.max_upload:
                        raise HTTPException(413, "파일이 업로드 최대 크기를 초과합니다.")
                    await anyio.to_thread.run_sync(destination.write, chunk)
                if declared is not None and size != int(declared):
                    raise HTTPException(400, "파일 전송이 완료되지 않았습니다.")
                await anyio.to_thread.run_sync(destination.flush)
                await anyio.to_thread.run_sync(os.fsync, destination.fileno())

            # 완성된 파일만 원자적으로 공개하며, 기존 파일은 덮어쓰지 않습니다.
            def publish():
                stem, suffix = os.path.splitext(name)
                for number in range(10000):
                    if number == 0:
                        candidate = name
                    else:
                        marker = f" ({number})"
                        base, extension = stem, suffix
                        if len((marker + extension).encode()) > 240:
                            base, extension = name, ""
                        budget = 240 - len((marker + extension).encode())
                        base = base.encode()[:budget].decode("utf-8", errors="ignore")
                        candidate = f"{base}{marker}{extension}"
                    try:
                        os.link(path, app.state.root / candidate, follow_symlinks=False)
                        return candidate
                    except FileExistsError:
                        continue
                raise HTTPException(409, "같은 이름의 파일이 너무 많습니다. 이름을 변경해 주세요.")

            stored_name = await anyio.to_thread.run_sync(publish)
            return {"name": stored_name, "size": size}
        except ClientDisconnect as exc:
            raise HTTPException(400, "업로드 연결이 끊겼습니다.") from exc
        except OSError as exc:
            if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                raise HTTPException(507, "저장 공간이 부족합니다.") from exc
            raise HTTPException(
                500, "파일을 저장하지 못했습니다. 저장 폴더의 권한을 확인해 주세요."
            ) from exc
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    @app.delete("/api/files/{name:path}")
    def delete_file(name: str, _: Session = Depends(require_csrf)):
        name = valid_name(name)
        path = app.state.root / name
        try:
            # 링크를 따라가지 않으며, 목록에서 지원하는 일반 파일만 삭제합니다.
            if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
                raise HTTPException(404, "파일을 찾을 수 없습니다.")
            path.unlink()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            raise HTTPException(404, "파일을 찾을 수 없습니다.") from exc
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
                raise HTTPException(
                    403, "파일을 삭제할 권한이 없습니다. 저장 폴더의 권한을 확인해 주세요."
                ) from exc
            raise HTTPException(
                500, "파일을 삭제하지 못했습니다. 잠시 후 다시 시도해 주세요."
            ) from exc
        return {"ok": True, "name": name}

    @app.get("/api/files/{name:path}")
    def download(name: str, _: Session = Depends(session_for)):
        name = valid_name(name)
        try:
            # 열린 파일을 직접 전송해 심볼릭 링크 교체를 통한 외부 파일 접근도 차단합니다.
            fd = os.open(app.state.root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR}:
                raise HTTPException(404, "파일을 찾을 수 없습니다.") from exc
            raise HTTPException(403, "파일에 접근할 수 없습니다.") from exc
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            raise HTTPException(404, "파일을 찾을 수 없습니다.")

        async def content():
            try:
                while chunk := await anyio.to_thread.run_sync(os.read, fd, CHUNK_SIZE):
                    yield chunk
            finally:
                os.close(fd)

        return StreamingResponse(
            content(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}",
                "Content-Length": str(info.st_size),
            },
        )

    # HTML과 업로드 폴더는 정적 경로로 노출하지 않습니다.
    @app.get("/assets/{name}")
    def asset(name: str):
        if name not in {"app.css", "app.js", "login.js", "theme.js", "mark.svg"}:
            raise HTTPException(404)
        return FileResponse(ASSETS / name)

    return app


app = create_app()
