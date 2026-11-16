"""Configuration from the process environment; never loads or prints secrets."""

from dataclasses import dataclass, field
import os
import re
from typing import Mapping, Optional, FrozenSet


class ConfigError(ValueError):
    """A configuration error with a safe, value-free explanation."""


def validate_token(token: str) -> None:
    # Grammar validation is local only. It cannot establish token validity in Telegram.
    if not isinstance(token, str) or not re.fullmatch(
        r"[1-9][0-9]{4,19}:[A-Za-z0-9_-]{20,200}", token
    ):
        raise ConfigError("BOT_TOKEN отсутствует или имеет неверный формат. Значение скрыто.")


@dataclass(frozen=True)
class Config:
    bot_token: str = field(repr=False)
    staff_ids: FrozenSet[int]
    db_path: str
    poll_timeout: int = 25

    @classmethod
    def from_env(
        cls, require_token: bool = False, environ: Optional[Mapping[str, str]] = None
    ) -> "Config":
        return load_config(require_token=require_token, environ=environ)


def load_config(
    require_token: bool = False, environ: Optional[Mapping[str, str]] = None
) -> Config:
    env = os.environ if environ is None else environ
    token = env.get("BOT_TOKEN", "")
    if require_token:
        validate_token(token)

    raw_staff = env.get("STAFF_IDS", "").strip()
    staff = set()
    if raw_staff:
        for item in raw_staff.split(","):
            item = item.strip()
            if not re.fullmatch(r"[1-9][0-9]{0,18}", item):
                raise ConfigError("STAFF_IDS: нужны положительные Telegram ID через запятую.")
            value = int(item)
            if value > 2**63 - 1:
                raise ConfigError("STAFF_IDS: один из Telegram ID вне допустимого диапазона.")
            staff.add(value)
    if require_token and not staff:
        raise ConfigError("Для Telegram-режима укажите подтверждённые ID сотрудников в STAFF_IDS.")

    db_path = env.get("DB_PATH", "data/repairbot.sqlite3").strip()
    if not db_path or "\x00" in db_path:
        raise ConfigError("DB_PATH: укажите путь к файлу SQLite.")

    raw_timeout = env.get("POLL_TIMEOUT", "25").strip()
    if not re.fullmatch(r"[0-9]{1,2}", raw_timeout):
        raise ConfigError("POLL_TIMEOUT: нужно целое число от 1 до 50 секунд.")
    timeout = int(raw_timeout)
    if not 1 <= timeout <= 50:
        raise ConfigError("POLL_TIMEOUT: нужно целое число от 1 до 50 секунд.")

    return Config(token, frozenset(staff), db_path, timeout)
