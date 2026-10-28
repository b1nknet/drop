import os
import stat
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

SETTINGS_FILENAME = ".drop-settings.json"
RETENTION_HOURS = (1, 3, 6, 12, 24, 72, 120, 168)


class AutoDeleteSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, strict=True)
    retention_hours: int = Field(default=24, strict=True)

    @field_validator("retention_hours")
    @classmethod
    def supported_retention(cls, value: int) -> int:
        if value not in RETENTION_HOURS:
            raise ValueError("보관 기간은 1·3·6·12시간 또는 1·3·5·7일 중에서 선택해 주세요.")
        return value


def load_settings(root: Path) -> AutoDeleteSettings:
    try:
        fd = os.open(root / SETTINGS_FILENAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("일반 설정 파일이 아닙니다.")
            contents = source.read(4097)
            if len(contents) > 4096:
                raise ValueError("설정 파일이 너무 큽니다.")
        return AutoDeleteSettings.model_validate_json(contents)
    except FileNotFoundError:
        return AutoDeleteSettings()
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{SETTINGS_FILENAME} 설정 파일을 읽을 수 없습니다.") from exc


def save_settings(root: Path, temporary: Path, settings: AutoDeleteSettings) -> None:
    fd, name = tempfile.mkstemp(prefix="settings-", suffix=".part", dir=temporary)
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as destination:
            destination.write(settings.model_dump_json() + "\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(path, root / SETTINGS_FILENAME)
    finally:
        path.unlink(missing_ok=True)
