"""Load and validate the account configuration."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

import platformdirs

APP_NAME = "gmail-mcp"
VALID_SEND_POLICIES = frozenset({"send", "confirm", "draft_only"})


class ConfigError(Exception):
    """Configuration is missing, malformed, or invalid."""


class UnknownAliasError(ConfigError):
    """An account alias was requested that is not configured."""


@dataclass(frozen=True)
class Account:
    alias: str
    email: str
    send_policy: str


@dataclass(frozen=True)
class Config:
    accounts: tuple[Account, ...]

    @property
    def aliases(self) -> list[str]:
        return [a.alias for a in self.accounts]

    def get(self, alias: str) -> Account:
        for account in self.accounts:
            if account.alias == alias:
                return account
        raise UnknownAliasError(
            f"Unknown account alias {alias!r}. "
            f"Configured aliases: {', '.join(self.aliases)}."
        )


def config_dir() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME))


def config_path() -> Path:
    return config_dir() / "config.toml"


def _parse_account(index: int, raw: object) -> Account:
    where = f"accounts[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a table.")

    for field in ("alias", "email", "send_policy"):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{where} is missing a non-empty {field!r}.")

    policy = raw["send_policy"]
    if policy not in VALID_SEND_POLICIES:
        allowed = ", ".join(sorted(VALID_SEND_POLICIES))
        raise ConfigError(
            f"{where} has send_policy {policy!r}. Must be one of: {allowed}."
        )

    return Account(
        alias=raw["alias"], email=raw["email"], send_policy=policy
    )


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        raise ConfigError(
            f"No config file at {path}. "
            "Create a config.toml with at least one [[accounts]] entry."
        )

    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    raw_accounts = data.get("accounts") or []
    if not raw_accounts:
        raise ConfigError(f"{path} defines no accounts.")

    accounts = tuple(
        _parse_account(i, raw) for i, raw in enumerate(raw_accounts)
    )

    seen: set[str] = set()
    for account in accounts:
        if account.alias in seen:
            raise ConfigError(f"duplicate alias {account.alias!r} in {path}.")
        seen.add(account.alias)

    return Config(accounts=accounts)
