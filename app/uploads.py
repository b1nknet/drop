from __future__ import annotations

import errno
import logging
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import anyio
from fastapi import HTTPException, Request
from starlette.requests import ClientDisconnect

UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
UPLOAD_IDLE_SECONDS = 3600
MAX_PENDING_UPLOADS = 128
logger = logging.getLogger(__name__)


@contextmanager
def storage_errors():
    try:
        yield
    except ClientDisconnect as exc:
        raise HTTPException(400, "업로드 연결이 끊겼습니다.") from exc
    except OSError as exc:
        if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
            raise HTTPException(507, "저장 공간이 부족합니다.") from exc
        raise HTTPException(
            500, "파일을 저장하지 못했습니다. 저장 폴더의 권한을 확인해 주세요."
        ) from exc


def publish_file(path: Path, root: Path, name: str, file_lock: threading.Lock) -> str:
    # 완성된 파일만 공개하며, 중복 이름과 보관 기간은 기존 업로드와 동일하게 처리합니다.
    with file_lock:
        os.utime(path, None, follow_symlinks=False)
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
                os.link(path, root / candidate, follow_symlinks=False)
                return candidate
            except FileExistsError:
                continue
    raise HTTPException(409, "같은 이름의 파일이 너무 많습니다. 이름을 변경해 주세요.")


@dataclass
class PendingUpload:
    name: str
    size: int
    owner: str
    expires: float
    path: Path
    offset: int = 0
    touched: float = field(default_factory=time.monotonic)
    lock: anyio.Lock = field(default_factory=anyio.Lock)

    def expired(self) -> bool:
        return self.expires <= time.time() or self.touched + UPLOAD_IDLE_SECONDS <= time.monotonic()


class UploadStore:
    # 단일 이벤트 루프에서 레지스트리를 관리하고 파일별 잠금으로 변경을 직렬화합니다.
    def __init__(self, root: Path, temporary: Path, max_size: int, file_lock: threading.Lock):
        self.root = root
        self.temporary = temporary
        self.max_size = max_size
        self.file_lock = file_lock
        self.pending: dict[str, PendingUpload] = {}

    def discard(self, upload_id: str):
        upload = self.pending[upload_id]
        upload.path.unlink(missing_ok=True)
        del self.pending[upload_id]

    def cleanup(self, active_owners: set[str] | None = None):
        for upload_id, upload in list(self.pending.items()):
            revoked = active_owners is not None and upload.owner not in active_owners
            if (upload.expired() or revoked) and not upload.lock.locked():
                try:
                    self.discard(upload_id)
                except OSError:
                    logger.warning(
                        "미완료 업로드를 삭제하지 못했습니다: %s", upload_id, exc_info=True
                    )

    def start(self, name: str, size: int, owner: str, expires: float) -> dict:
        if size > self.max_size:
            raise HTTPException(413, "파일이 업로드 최대 크기를 초과합니다.")
        self.cleanup()
        if len(self.pending) >= MAX_PENDING_UPLOADS:
            raise HTTPException(503, "진행 중인 업로드가 많습니다. 잠시 후 다시 시도해 주세요.")
        with storage_errors():
            fd, temporary_name = tempfile.mkstemp(
                prefix="upload-", suffix=".part", dir=self.temporary
            )
            os.close(fd)
        upload_id = secrets.token_urlsafe(32)
        self.pending[upload_id] = PendingUpload(name, size, owner, expires, Path(temporary_name))
        return {"id": upload_id, "chunk_size": UPLOAD_CHUNK_BYTES, "offset": 0}

    @contextmanager
    def acquire(self, upload_id: str, owner: str):
        upload = self.pending.get(upload_id)
        if upload is None or upload.owner != owner or upload.expired():
            raise HTTPException(404, "업로드를 찾을 수 없습니다. 파일을 다시 선택해 주세요.")
        try:
            upload.lock.acquire_nowait()
        except anyio.WouldBlock as exc:
            raise HTTPException(409, "이 파일의 이전 업로드 요청을 처리 중입니다.") from exc
        try:
            yield upload
        finally:
            upload.lock.release()

    async def append(self, upload_id: str, owner: str, offset: int, request: Request) -> dict:
        with self.acquire(upload_id, owner) as upload, storage_errors():
            if offset != upload.offset:
                raise HTTPException(
                    409, "업로드 위치가 일치하지 않습니다. 파일을 다시 선택해 주세요."
                )
            limit = min(UPLOAD_CHUNK_BYTES, upload.size - offset)
            declared = request.headers.get("content-length")
            if declared is not None:
                try:
                    length = int(declared)
                except ValueError as exc:
                    raise HTTPException(400, "파일 크기 정보가 올바르지 않습니다.") from exc
                if length < 0:
                    raise HTTPException(400, "파일 크기 정보가 올바르지 않습니다.")
                if length > limit:
                    raise HTTPException(413, "업로드 조각의 최대 크기를 초과합니다.")
            received = 0
            accepted = False
            # 실패하거나 끊긴 요청은 마지막으로 확인한 위치로 되돌립니다.
            with upload.path.open("r+b") as destination:
                destination.seek(offset)
                try:
                    async for chunk in request.stream():
                        received += len(chunk)
                        if received > limit:
                            raise HTTPException(413, "업로드 조각의 최대 크기를 초과합니다.")
                        await anyio.to_thread.run_sync(destination.write, chunk)
                    if received == 0 or (declared is not None and received != int(declared)):
                        raise HTTPException(400, "파일 전송이 완료되지 않았습니다.")
                    await anyio.to_thread.run_sync(destination.flush)
                    accepted = True
                finally:
                    if not accepted:
                        destination.truncate(offset)
            upload.offset += received
            upload.touched = time.monotonic()
            return {"offset": upload.offset}

    async def complete(self, upload_id: str, owner: str) -> dict:
        with self.acquire(upload_id, owner) as upload, storage_errors():
            if upload.offset != upload.size:
                raise HTTPException(409, "파일 전송이 완료되지 않았습니다.")
            try:
                with upload.path.open("rb") as source:
                    await anyio.to_thread.run_sync(os.fsync, source.fileno())
                stored_name = await anyio.to_thread.run_sync(
                    publish_file, upload.path, self.root, upload.name, self.file_lock
                )
                return {"name": stored_name, "size": upload.size}
            finally:
                self.discard(upload_id)

    def cancel(self, upload_id: str, owner: str) -> dict:
        with self.acquire(upload_id, owner), storage_errors():
            self.discard(upload_id)
        return {"ok": True}
