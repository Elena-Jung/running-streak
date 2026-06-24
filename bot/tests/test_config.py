"""config 검증·DSN 인코딩·시크릿 노출 방지 테스트."""

import os

import pytest

from app.config import Config, ConfigError, _env_int


def _cfg(**kw):
    base = dict(
        discord_token="tok",
        guild_id=1,
        target_channel_id=2,
        pg_host="db",
        pg_port=5432,
        pg_user="streak",
        pg_password="pw",
        pg_db="streak",
        ocr_enabled=True,
    )
    base.update(kw)
    return Config(**base)


def test_dsn_encodes_special_chars():
    # 특수문자 비번/유저가 있어도 DSN URL 이 깨지지 않게 인코딩되어야 한다.
    dsn = _cfg(pg_user="u@x", pg_password="p@ss:w/rd#1").dsn
    assert "p@ss:w/rd#1" not in dsn  # 원문 그대로 들어가면 안 됨
    assert "p%40ss%3Aw%2Frd%231" in dsn
    assert "u%40x" in dsn
    assert dsn.startswith("postgresql://")


def test_repr_hides_secrets():
    r = repr(_cfg(discord_token="SECRET_TOKEN_123", pg_password="SECRET_PW_456"))
    assert "SECRET_TOKEN_123" not in r
    assert "SECRET_PW_456" not in r


def test_env_int_valid_and_default(monkeypatch):
    monkeypatch.delenv("X_PORT", raising=False)
    assert _env_int("X_PORT", 5432) == 5432
    monkeypatch.setenv("X_PORT", "6000")
    assert _env_int("X_PORT", 5432) == 6000


def test_env_int_bad_raises_configerror(monkeypatch):
    monkeypatch.setenv("X_PORT", "not-a-number")
    with pytest.raises(ConfigError):
        _env_int("X_PORT", 5432)
