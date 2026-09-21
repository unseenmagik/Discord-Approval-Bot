from __future__ import annotations

import os
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.ini"


class ConfigError(ValueError):
    """Raised when config.ini is missing or contains invalid values."""


@dataclass(frozen=True)
class BotSettings:
    token: str
    guild_id: int
    verified_role_ids: frozenset[int]
    approved_role_id: int
    required_role_ids: frozenset[int]
    admin_user_ids: frozenset[int]
    admin_role_ids: frozenset[int]
    welcome_channel_id: int
    admin_channel_id: int
    lockout_mention_user_ids: tuple[int, ...]
    lockout_mention_role_ids: tuple[int, ...]
    approval_mention_user_ids: tuple[int, ...]
    approval_mention_role_ids: tuple[int, ...]
    max_attempts: int
    referrers_file: Path
    database_file: Path
    panel_title: str
    panel_description: str
    embed_color: int

    @property
    def already_verified_role_ids(self) -> frozenset[int]:
        return self.verified_role_ids | {self.approved_role_id}


def _str(config: ConfigParser, section: str, key: str, fallback: str = "") -> str:
    return config.get(section, key, fallback=fallback).strip()


def _int(config: ConfigParser, section: str, key: str) -> int:
    raw = _str(config, section, key)
    if not raw:
        raise ConfigError(f"[{section}] {key} is required")
    try:
        return int(raw, 0)
    except ValueError as exc:
        raise ConfigError(f"[{section}] {key} must be a number, got {raw!r}") from exc


def _int_list(config: ConfigParser, section: str, key: str) -> tuple[int, ...]:
    raw = _str(config, section, key)
    try:
        return tuple(int(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise ConfigError(f"[{section}] {key} must be a comma-separated list of IDs") from exc


def load_settings(config_path: str | Path | None = None) -> BotSettings:
    path = Path(config_path or os.environ.get("APPROVAL_BOT_CONFIG") or DEFAULT_CONFIG_PATH).resolve()
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path} (copy config.ini.example to config.ini)")

    config = ConfigParser(interpolation=None)
    config.read(path, encoding="utf-8")
    base_dir = path.parent

    token = _str(config, "bot", "token")
    if not token:
        raise ConfigError("[bot] token is required")

    verified_role_ids = frozenset(_int_list(config, "roles", "verified_role_ids"))
    if not verified_role_ids:
        raise ConfigError("[roles] verified_role_ids needs at least one role ID")

    max_attempts = int(_str(config, "verification", "max_attempts", "3"))
    if max_attempts < 1:
        raise ConfigError("[verification] max_attempts must be at least 1")

    return BotSettings(
        token=token,
        guild_id=_int(config, "bot", "guild_id"),
        verified_role_ids=verified_role_ids,
        approved_role_id=_int(config, "roles", "approved_role_id"),
        required_role_ids=frozenset(_int_list(config, "roles", "required_role_ids")),
        admin_user_ids=frozenset(_int_list(config, "admin", "admin_user_ids")),
        admin_role_ids=frozenset(_int_list(config, "admin", "admin_role_ids")),
        welcome_channel_id=_int(config, "channels", "welcome_channel_id"),
        admin_channel_id=_int(config, "channels", "admin_channel_id"),
        lockout_mention_user_ids=_int_list(config, "notifications", "lockout_mention_user_ids"),
        lockout_mention_role_ids=_int_list(config, "notifications", "lockout_mention_role_ids"),
        approval_mention_user_ids=_int_list(config, "notifications", "approval_mention_user_ids"),
        approval_mention_role_ids=_int_list(config, "notifications", "approval_mention_role_ids"),
        max_attempts=max_attempts,
        referrers_file=base_dir / _str(config, "verification", "referrers_file", "referrers.json"),
        database_file=base_dir / _str(config, "verification", "database_file", "data/approvals.db"),
        panel_title=_str(config, "panel", "title", "Verify your referral"),
        panel_description=_str(config, "panel", "description", "Press **Verify** to get started.").replace("\\n", "\n"),
        embed_color=int(_str(config, "panel", "embed_color", "0x5865F2"), 0),
    )
