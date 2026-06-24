"""환경변수 로딩 및 검증.

봇 기동 시 필수값이 비어 있으면 즉시 명확한 오류로 죽인다(조용한 오작동 방지).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import quote


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(f"환경변수 {name} 가 설정되지 않았습니다. .env 를 확인하세요.")
    return val


def _require_int(name: str) -> int:
    raw = _require(name)
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"환경변수 {name} 는 정수여야 합니다 (현재값: {raw!r}).") from e


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as e:
        raise ConfigError(f"환경변수 {name} 는 정수여야 합니다 (현재값: {raw!r}).") from e


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    # 비밀값은 repr 에서 제외 → 로그/예외에 토큰·비번이 평문 노출되지 않게.
    discord_token: str = field(repr=False)
    guild_id: int = 0
    target_channel_id: int = 0

    pg_host: str = "db"
    pg_port: int = 5432
    pg_user: str = "streak"
    pg_password: str = field(repr=False, default="")
    pg_db: str = "streak"

    ocr_enabled: bool = True

    @property
    def dsn(self) -> str:
        # 유저/비밀번호를 URL 인코딩 → 특수문자(@ : / # 등)가 있어도 DSN 이 깨지지 않음.
        user = quote(self.pg_user, safe="")
        pw = quote(self.pg_password, safe="")
        return f"postgresql://{user}:{pw}@{self.pg_host}:{self.pg_port}/{self.pg_db}"


def load_config() -> Config:
    return Config(
        discord_token=_require("DISCORD_TOKEN"),
        guild_id=_require_int("DISCORD_GUILD_ID"),
        target_channel_id=_require_int("TARGET_CHANNEL_ID"),
        pg_host=os.environ.get("POSTGRES_HOST", "db").strip() or "db",
        pg_port=_env_int("POSTGRES_PORT", 5432),
        pg_user=_require("POSTGRES_USER"),
        pg_password=_require("POSTGRES_PASSWORD"),
        pg_db=_require("POSTGRES_DB"),
        ocr_enabled=_bool("OCR_ENABLED", True),
    )
