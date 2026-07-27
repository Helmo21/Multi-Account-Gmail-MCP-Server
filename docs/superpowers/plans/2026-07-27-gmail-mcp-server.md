# Local Gmail MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local stdio MCP server that gives Claude Desktop search, read, draft, send, label, and scheduled-summary access to five Gmail mailboxes on one Google Workspace domain.

**Architecture:** A thin `server.py` exposes ten FastMCP tools that validate arguments and delegate to focused modules. Every mail-touching tool takes an `account` alias, resolved through `config.py` to an `Account`, whose credentials come from `storage.py` via `auth.py`. All Gmail calls take an injected `service` object, so the entire suite tests offline against a fake. A `cli.py` handles one-time OAuth setup and a `doctor` health check.

**Tech Stack:** Python 3.12, `uv`, `mcp` (FastMCP), `google-api-python-client`, `google-auth-oauthlib`, `keyring`, `platformdirs`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-07-27-gmail-mcp-server-design.md`

## Global Constraints

These apply to every task. Each task's requirements implicitly include this section.

- **Python `>=3.12`.** Use modern typing (`str | None`, not `Optional[str]`).
- **OAuth scope is exactly one:** `https://www.googleapis.com/auth/gmail.modify`. Never add a second scope, and never use `https://mail.google.com/`.
- **No network in unit tests.** Every Gmail-touching function takes a `service` argument supplied by the caller. Tests pass fakes from `tests/fakes.py`. The only exception is the opt-in integration test in Task 12.
- **One account's failure is never all accounts' failure.** Any function operating over multiple accounts returns per-account errors inline rather than raising.
- **No stack traces reach the model.** Every error surfaced from a tool is a structured message with actionable text.
- **Send policy is enforced in code**, never in prompt text. Valid values are exactly `send`, `confirm`, `draft_only`. There is no default; config validation fails on a missing or unrecognised value.
- **Message IDs are account-scoped.** A 404 on a message or thread lookup must produce an error saying the ID may belong to a different mailbox.
- **Attachments are metadata-only.** Nothing in this project downloads attachment contents or writes outside its own config directory.
- **Commit after every task.** Conventional commit prefixes (`feat:`, `test:`, `chore:`, `docs:`).

## File Structure

| File | Responsibility |
| --- | --- |
| `pyproject.toml` | Package metadata, dependencies, `gmail-mcp` console script |
| `gmail_mcp/config.py` | Load and validate `config.toml`; `Account`/`Config` types; alias resolution |
| `gmail_mcp/storage.py` | `TokenStore` (keyring with `0600` file fallback) and `WatermarkStore` |
| `gmail_mcp/auth.py` | OAuth installed-app flow, credential load/refresh/persist, email-mismatch guard |
| `gmail_mcp/gmail/client.py` | Service factory with cache, retry/backoff, Gmail error mapping |
| `gmail_mcp/gmail/search.py` | Query pass-through and result normalisation |
| `gmail_mcp/gmail/messages.py` | Message/thread reads, MIME body extraction, quoted-text collapsing |
| `gmail_mcp/gmail/compose.py` | MIME construction, reply headers, send-policy gate, drafts and sending |
| `gmail_mcp/gmail/labels.py` | Label listing, name-to-ID resolution, label modification |
| `gmail_mcp/check.py` | Watermarked multi-account inbox sweep |
| `gmail_mcp/server.py` | The ten FastMCP tool definitions |
| `gmail_mcp/cli.py` | `auth add/list/remove`, `doctor`, `serve` |
| `tests/fakes.py` | Fake Gmail service and HTTP error helpers shared by all tests |

Files that change together live together: everything that speaks to the Gmail API sits under `gmail/`, split by the operation it performs rather than by layer.

---

### Task 1: Project scaffolding and configuration

**Files:**
- Create: `pyproject.toml`
- Create: `gmail_mcp/__init__.py`
- Create: `gmail_mcp/config.py`
- Create: `tests/__init__.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Account` frozen dataclass with fields `alias: str`, `email: str`, `send_policy: str`
  - `Config` frozen dataclass with field `accounts: tuple[Account, ...]`, method `get(alias: str) -> Account`, property `aliases -> list[str]`
  - `ConfigError(Exception)`, `UnknownAliasError(ConfigError)`
  - `VALID_SEND_POLICIES: frozenset[str]`
  - `config_dir() -> Path`, `config_path() -> Path`
  - `load_config(path: Path | None = None) -> Config`

- [ ] **Step 1: Create the package skeleton and dependency manifest**

Create `pyproject.toml`:

```toml
[project]
name = "gmail-mcp"
version = "0.1.0"
description = "Local MCP server for multiple Gmail accounts"
requires-python = ">=3.12"
dependencies = [
    "mcp>=1.2.0",
    "google-api-python-client>=2.120.0",
    "google-auth>=2.28.0",
    "google-auth-oauthlib>=1.2.0",
    "keyring>=25.0.0",
    "platformdirs>=4.2.0",
]

[project.scripts]
gmail-mcp = "gmail_mcp.cli:main"

[dependency-groups]
dev = ["pytest>=8.0.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["gmail_mcp"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

Create empty `gmail_mcp/__init__.py` and `tests/__init__.py`. Then run:

```bash
uv sync
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_config.py`:

```python
import pytest

from gmail_mcp.config import (
    Config,
    ConfigError,
    UnknownAliasError,
    load_config,
)

VALID = """
[[accounts]]
alias = "personal"
email = "me@example.com"
send_policy = "confirm"

[[accounts]]
alias = "work-sales"
email = "sales@example.com"
send_policy = "draft_only"
"""


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_loads_accounts_in_order(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert cfg.aliases == ["personal", "work-sales"]
    assert cfg.accounts[1].email == "sales@example.com"
    assert cfg.accounts[1].send_policy == "draft_only"


def test_get_resolves_alias(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert cfg.get("personal").email == "me@example.com"


def test_unknown_alias_lists_valid_aliases(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    with pytest.raises(UnknownAliasError) as exc:
        cfg.get("nope")
    message = str(exc.value)
    assert "nope" in message
    assert "personal" in message
    assert "work-sales" in message


def test_missing_send_policy_is_rejected(tmp_path):
    text = '[[accounts]]\nalias = "a"\nemail = "a@example.com"\n'
    with pytest.raises(ConfigError, match="send_policy"):
        load_config(write(tmp_path, text))


def test_unrecognised_send_policy_is_rejected(tmp_path):
    text = (
        '[[accounts]]\nalias = "a"\nemail = "a@example.com"\n'
        'send_policy = "yolo"\n'
    )
    with pytest.raises(ConfigError, match="yolo"):
        load_config(write(tmp_path, text))


def test_duplicate_alias_is_rejected(tmp_path):
    text = (
        '[[accounts]]\nalias = "a"\nemail = "a@example.com"\n'
        'send_policy = "send"\n'
        '[[accounts]]\nalias = "a"\nemail = "b@example.com"\n'
        'send_policy = "send"\n'
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(write(tmp_path, text))


def test_empty_config_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="no accounts"):
        load_config(write(tmp_path, ""))


def test_missing_file_names_the_path(tmp_path):
    with pytest.raises(ConfigError, match="config.toml"):
        load_config(tmp_path / "config.toml")


def test_config_is_immutable(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert isinstance(cfg, Config)
    with pytest.raises(Exception):
        cfg.accounts[0].alias = "changed"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gmail_mcp.config'`

- [ ] **Step 4: Write the implementation**

Create `gmail_mcp/config.py`:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS — 9 tests

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock gmail_mcp/ tests/
git commit -m "feat: add package scaffolding and account configuration"
```

---

### Task 2: Token and watermark storage

**Files:**
- Create: `gmail_mcp/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `TokenStore(config_dir: Path, keyring_module=keyring)` with `get(alias) -> str | None`, `set(alias, token_json: str) -> None`, `delete(alias) -> None`, property `backend_name -> str`
  - `WatermarkStore(config_dir: Path)` with `get(alias) -> int | None`, `set(alias, epoch_seconds: int) -> None`
  - `SERVICE_NAME: str` (`"gmail-mcp"`)

Tokens are opaque JSON strings here. This module never parses them; that is `auth.py`'s job.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_storage.py`:

```python
import json
import stat
import sys

import pytest

from gmail_mcp.storage import SERVICE_NAME, TokenStore, WatermarkStore


class FakeKeyring:
    """Stands in for the keyring module with a working backend."""

    def __init__(self):
        self.data: dict[tuple[str, str], str] = {}
        self.name = "FakeBackend"

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def get_password(self, service, username):
        return self.data.get((service, username))

    def delete_password(self, service, username):
        self.data.pop((service, username), None)

    def get_keyring(self):
        return self

    def __str__(self):
        return self.name


class BrokenKeyring(FakeKeyring):
    """Stands in for a machine with no usable keyring backend."""

    def set_password(self, service, username, password):
        raise RuntimeError("no backend")

    def get_password(self, service, username):
        raise RuntimeError("no backend")

    def delete_password(self, service, username):
        raise RuntimeError("no backend")


def test_roundtrips_through_keyring(tmp_path):
    fake = FakeKeyring()
    store = TokenStore(tmp_path, keyring_module=fake)
    store.set("personal", '{"token": "abc"}')

    assert store.get("personal") == '{"token": "abc"}'
    assert fake.data[(SERVICE_NAME, "personal")] == '{"token": "abc"}'
    assert not (tmp_path / "tokens.json").exists()


def test_keyring_backend_is_named(tmp_path):
    store = TokenStore(tmp_path, keyring_module=FakeKeyring())
    assert store.backend_name.startswith("keyring:")


def test_missing_token_returns_none(tmp_path):
    store = TokenStore(tmp_path, keyring_module=FakeKeyring())
    assert store.get("personal") is None


def test_delete_removes_token(tmp_path):
    store = TokenStore(tmp_path, keyring_module=FakeKeyring())
    store.set("personal", "x")
    store.delete("personal")
    assert store.get("personal") is None


def test_falls_back_to_file_when_keyring_unusable(tmp_path):
    store = TokenStore(tmp_path, keyring_module=BrokenKeyring())
    store.set("personal", '{"token": "abc"}')

    assert store.get("personal") == '{"token": "abc"}'
    assert store.backend_name.startswith("file:")
    assert json.loads((tmp_path / "tokens.json").read_text()) == {
        "personal": '{"token": "abc"}'
    }


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_fallback_file_is_owner_only(tmp_path):
    store = TokenStore(tmp_path, keyring_module=BrokenKeyring())
    store.set("personal", "x")

    mode = stat.S_IMODE((tmp_path / "tokens.json").stat().st_mode)
    assert mode == 0o600


def test_fallback_delete_removes_entry(tmp_path):
    store = TokenStore(tmp_path, keyring_module=BrokenKeyring())
    store.set("personal", "x")
    store.set("work", "y")
    store.delete("personal")

    assert store.get("personal") is None
    assert store.get("work") == "y"


def test_watermark_roundtrips(tmp_path):
    marks = WatermarkStore(tmp_path)
    assert marks.get("personal") is None

    marks.set("personal", 1700000000)
    assert marks.get("personal") == 1700000000
    assert WatermarkStore(tmp_path).get("personal") == 1700000000


def test_watermark_is_per_account(tmp_path):
    marks = WatermarkStore(tmp_path)
    marks.set("personal", 100)
    marks.set("work", 200)

    assert marks.get("personal") == 100
    assert marks.get("work") == 200


def test_watermark_survives_corrupt_file(tmp_path):
    (tmp_path / "watermarks.json").write_text("not json{{")
    marks = WatermarkStore(tmp_path)

    assert marks.get("personal") is None
    marks.set("personal", 5)
    assert marks.get("personal") == 5
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_storage.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gmail_mcp.storage'`

- [ ] **Step 3: Write the implementation**

Create `gmail_mcp/storage.py`:

```python
"""Persistence for OAuth tokens and per-account check watermarks."""

from __future__ import annotations

import json
import os
from pathlib import Path

import keyring as _keyring

SERVICE_NAME = "gmail-mcp"
_TOKEN_FILE = "tokens.json"
_WATERMARK_FILE = "watermarks.json"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


class TokenStore:
    """Stores opaque token JSON in the OS keyring, or a 0600 file.

    The fallback is deliberately not encrypted: the server starts
    unattended and can never prompt for a passphrase, so any key would
    have to sit beside the ciphertext.
    """

    def __init__(self, config_dir: Path, keyring_module=_keyring):
        self._dir = Path(config_dir)
        self._keyring = keyring_module
        self._usable = self._probe()

    def _probe(self) -> bool:
        try:
            self._keyring.get_password(SERVICE_NAME, "__probe__")
        except Exception:
            return False
        return True

    @property
    def _file(self) -> Path:
        return self._dir / _TOKEN_FILE

    @property
    def backend_name(self) -> str:
        if self._usable:
            return f"keyring:{self._keyring.get_keyring()}"
        return f"file:{self._file}"

    def get(self, alias: str) -> str | None:
        if self._usable:
            try:
                return self._keyring.get_password(SERVICE_NAME, alias)
            except Exception:
                self._usable = False
        return _read_json(self._file).get(alias)

    def set(self, alias: str, token_json: str) -> None:
        if self._usable:
            try:
                self._keyring.set_password(SERVICE_NAME, alias, token_json)
                return
            except Exception:
                self._usable = False
        data = _read_json(self._file)
        data[alias] = token_json
        _write_json_private(self._file, data)

    def delete(self, alias: str) -> None:
        if self._usable:
            try:
                self._keyring.delete_password(SERVICE_NAME, alias)
                return
            except Exception:
                self._usable = False
        data = _read_json(self._file)
        if data.pop(alias, None) is not None:
            _write_json_private(self._file, data)


class WatermarkStore:
    """Remembers the newest message time reported per account."""

    def __init__(self, config_dir: Path):
        self._path = Path(config_dir) / _WATERMARK_FILE

    def get(self, alias: str) -> int | None:
        value = _read_json(self._path).get(alias)
        return value if isinstance(value, int) else None

    def set(self, alias: str, epoch_seconds: int) -> None:
        data = _read_json(self._path)
        data[alias] = int(epoch_seconds)
        _write_json_private(self._path, data)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_storage.py -v`
Expected: PASS — 10 tests

- [ ] **Step 5: Commit**

```bash
git add gmail_mcp/storage.py tests/test_storage.py
git commit -m "feat: add keyring-backed token store and check watermarks"
```

---

### Task 3: OAuth flow, credential refresh, and the alias guard

**Files:**
- Create: `gmail_mcp/auth.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `Account` from `gmail_mcp.config`; `TokenStore` from `gmail_mcp.storage`.
- Produces:
  - `SCOPES: list[str]`
  - `AuthError(Exception)`, `CredentialMissingError(AuthError)`, `CredentialRevokedError(AuthError)`, `AccountMismatchError(AuthError)`
  - `load_credentials(alias: str, store: TokenStore, request_factory=Request) -> Credentials`
  - `run_oauth_flow(client_secret_path: Path, flow_factory=InstalledAppFlow.from_client_secrets_file) -> Credentials`
  - `authenticate_account(account, client_secret_path, store, profile_lookup, flow_factory=...) -> str`, returning the verified email address

`profile_lookup` is a `Callable[[Credentials], str]` returning the authenticated address. Injecting it keeps this module free of Gmail API imports and testable offline; `cli.py` supplies the real one in Task 12.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_auth.py`:

```python
import json

import pytest
from google.auth.exceptions import RefreshError

from gmail_mcp.auth import (
    SCOPES,
    AccountMismatchError,
    CredentialMissingError,
    CredentialRevokedError,
    authenticate_account,
    load_credentials,
)
from gmail_mcp.config import Account


class FakeCreds:
    def __init__(self, valid=True, expired=False, refresh_token="rt"):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.refreshed = False

    def refresh(self, request):
        self.refreshed = True
        self.valid = True
        self.expired = False

    def to_json(self):
        return json.dumps({"token": "access", "refresh_token": self.refresh_token})


class DeadCreds(FakeCreds):
    def refresh(self, request):
        raise RefreshError("Token has been expired or revoked.")


class MemoryStore:
    def __init__(self, initial=None):
        self.data = dict(initial or {})

    def get(self, alias):
        return self.data.get(alias)

    def set(self, alias, token_json):
        self.data[alias] = token_json

    def delete(self, alias):
        self.data.pop(alias, None)


def test_scope_is_exactly_gmail_modify():
    assert SCOPES == ["https://www.googleapis.com/auth/gmail.modify"]


def test_missing_token_names_the_setup_command():
    store = MemoryStore()
    with pytest.raises(CredentialMissingError) as exc:
        load_credentials("personal", store, loader=lambda info, scopes: None)
    assert "gmail-mcp auth add personal" in str(exc.value)


def test_valid_credential_is_returned_without_refresh():
    creds = FakeCreds(valid=True, expired=False)
    store = MemoryStore({"personal": "{}"})

    result = load_credentials("personal", store, loader=lambda i, s: creds)

    assert result is creds
    assert creds.refreshed is False


def test_expired_credential_is_refreshed_and_persisted():
    creds = FakeCreds(valid=False, expired=True)
    store = MemoryStore({"personal": "{}"})

    load_credentials(
        "personal", store, loader=lambda i, s: creds, request_factory=lambda: None
    )

    assert creds.refreshed is True
    assert json.loads(store.data["personal"])["refresh_token"] == "rt"


def test_revoked_credential_names_the_alias_and_the_fix():
    store = MemoryStore({"work-sales": "{}"})
    with pytest.raises(CredentialRevokedError) as exc:
        load_credentials(
            "work-sales",
            store,
            loader=lambda i, s: DeadCreds(valid=False, expired=True),
            request_factory=lambda: None,
        )
    message = str(exc.value)
    assert "work-sales" in message
    assert "gmail-mcp auth add work-sales" in message


def test_authenticate_saves_token_when_email_matches(tmp_path):
    account = Account(alias="personal", email="me@example.com", send_policy="send")
    store = MemoryStore()
    creds = FakeCreds()

    email = authenticate_account(
        account,
        tmp_path / "client_secret.json",
        store,
        profile_lookup=lambda c: "me@example.com",
        flow_factory=lambda path, scopes: _FakeFlow(creds),
    )

    assert email == "me@example.com"
    assert json.loads(store.data["personal"])["refresh_token"] == "rt"


def test_authenticate_rejects_wrong_mailbox_and_saves_nothing(tmp_path):
    account = Account(alias="personal", email="me@example.com", send_policy="send")
    store = MemoryStore()

    with pytest.raises(AccountMismatchError) as exc:
        authenticate_account(
            account,
            tmp_path / "client_secret.json",
            store,
            profile_lookup=lambda c: "someone.else@example.com",
            flow_factory=lambda path, scopes: _FakeFlow(FakeCreds()),
        )

    message = str(exc.value)
    assert "me@example.com" in message
    assert "someone.else@example.com" in message
    assert "personal" in message
    assert store.data == {}


def test_authenticate_ignores_address_case(tmp_path):
    account = Account(alias="personal", email="Me@Example.com", send_policy="send")
    store = MemoryStore()

    authenticate_account(
        account,
        tmp_path / "client_secret.json",
        store,
        profile_lookup=lambda c: "me@example.com",
        flow_factory=lambda path, scopes: _FakeFlow(FakeCreds()),
    )

    assert "personal" in store.data


def test_oauth_flow_requests_offline_access_and_forces_consent(tmp_path):
    flow = _FakeFlow(FakeCreds())
    account = Account(alias="personal", email="me@example.com", send_policy="send")

    authenticate_account(
        account,
        tmp_path / "client_secret.json",
        MemoryStore(),
        profile_lookup=lambda c: "me@example.com",
        flow_factory=lambda path, scopes: flow,
    )

    assert flow.kwargs["access_type"] == "offline"
    assert flow.kwargs["prompt"] == "consent"
    assert flow.kwargs["port"] == 0


class _FakeFlow:
    def __init__(self, creds):
        self._creds = creds
        self.kwargs = {}

    def run_local_server(self, **kwargs):
        self.kwargs = kwargs
        return self._creds
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gmail_mcp.auth'`

- [ ] **Step 3: Write the implementation**

Create `gmail_mcp/auth.py`:

```python
"""OAuth installed-app flow and credential lifecycle."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from gmail_mcp.config import Account

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class AuthError(Exception):
    """Base class for credential problems."""


class CredentialMissingError(AuthError):
    """No token has ever been stored for this account."""


class CredentialRevokedError(AuthError):
    """The stored refresh token no longer works."""


class AccountMismatchError(AuthError):
    """The browser authenticated a different mailbox than configured."""


def _default_loader(info: dict, scopes: list[str]) -> Credentials:
    return Credentials.from_authorized_user_info(info, scopes)


def _default_flow_factory(path: Path, scopes: list[str]):
    return InstalledAppFlow.from_client_secrets_file(str(path), scopes=scopes)


def load_credentials(
    alias: str,
    store,
    *,
    loader: Callable = _default_loader,
    request_factory: Callable = Request,
) -> Credentials:
    """Return usable credentials for ``alias``, refreshing if needed."""
    raw = store.get(alias)
    if raw is None:
        raise CredentialMissingError(
            f"Account {alias!r} has not been authenticated. "
            f"Run: gmail-mcp auth add {alias}"
        )

    creds = loader(json.loads(raw), SCOPES)

    if creds.valid:
        return creds

    if not creds.refresh_token:
        raise CredentialRevokedError(
            f"Account {alias!r} has no refresh token. "
            f"Run: gmail-mcp auth add {alias}"
        )

    try:
        creds.refresh(request_factory())
    except RefreshError as exc:
        raise CredentialRevokedError(
            f"Access for account {alias!r} was revoked or expired "
            f"({exc}). Run: gmail-mcp auth add {alias}"
        ) from exc

    store.set(alias, creds.to_json())
    return creds


def run_oauth_flow(
    client_secret_path: Path,
    *,
    flow_factory: Callable = _default_flow_factory,
) -> Credentials:
    """Open a browser, consent once, return credentials."""
    flow = flow_factory(client_secret_path, SCOPES)
    return flow.run_local_server(
        port=0, access_type="offline", prompt="consent"
    )


def authenticate_account(
    account: Account,
    client_secret_path: Path,
    store,
    profile_lookup: Callable[[Credentials], str],
    *,
    flow_factory: Callable = _default_flow_factory,
) -> str:
    """Authenticate one account, verifying it is the intended mailbox.

    Google reuses whichever browser session is already signed in, so
    authenticating several accounts in a row will silently map two
    aliases onto one mailbox unless the address is verified afterwards.
    """
    creds = run_oauth_flow(client_secret_path, flow_factory=flow_factory)
    actual = profile_lookup(creds)

    if actual.strip().lower() != account.email.strip().lower():
        raise AccountMismatchError(
            f"Alias {account.alias!r} is configured for {account.email}, "
            f"but the browser authenticated {actual}. Nothing was saved. "
            "Sign out of that Google account, or use a private window, "
            "and try again."
        )

    store.set(account.alias, creds.to_json())
    return actual
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_auth.py -v`
Expected: PASS — 9 tests

- [ ] **Step 5: Commit**

```bash
git add gmail_mcp/auth.py tests/test_auth.py
git commit -m "feat: add OAuth flow with refresh and alias-to-mailbox guard"
```

---

### Task 4: Gmail service factory, retries, and error mapping

**Files:**
- Create: `gmail_mcp/gmail/__init__.py`
- Create: `gmail_mcp/gmail/client.py`
- Create: `tests/fakes.py`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: `Config`, `Account` from `gmail_mcp.config`; `load_credentials` from `gmail_mcp.auth`.
- Produces:
  - `GmailError(Exception)`, `NotFoundError(GmailError)`, `RateLimitedError(GmailError)`
  - `execute(request, *, sleep=time.sleep, attempts=3) -> dict`
  - `ServiceCache(config, store)` with `get(alias) -> Resource` and `clear() -> None`
  - `tests/fakes.py`: `make_http_error(status, reason="error")`, `FakeRequest`, `FakeMessages`, `FakeThreads`, `FakeDrafts`, `FakeLabels`, `FakeUsers`, `FakeGmail`

`execute` is the single choke point through which every Gmail call in later tasks passes.

- [ ] **Step 1: Write the shared fakes**

Create `tests/fakes.py`:

```python
"""Offline stand-ins for the Gmail API client."""

from __future__ import annotations

import json

import httplib2
from googleapiclient.errors import HttpError


def make_http_error(status: int, reason: str = "error") -> HttpError:
    response = httplib2.Response({"status": status, "reason": reason})
    response.status = status
    content = json.dumps(
        {"error": {"code": status, "message": reason}}
    ).encode()
    return HttpError(response, content, uri="https://gmail.example/test")


class FakeRequest:
    """A request whose execute() returns a result or raises."""

    def __init__(self, result=None, error=None, errors_then=None):
        self.result = result
        self.error = error
        self.errors_then = list(errors_then or [])
        self.execute_count = 0

    def execute(self):
        self.execute_count += 1
        if self.errors_then:
            raise self.errors_then.pop(0)
        if self.error:
            raise self.error
        return self.result


class FakeMessages:
    def __init__(self, listing=None, messages=None):
        self.listing = listing if listing is not None else {"messages": []}
        self.messages = messages or {}
        self.modified: list[tuple[str, dict]] = []
        self.sent: list[dict] = []
        self.list_calls: list[dict] = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return FakeRequest(self.listing)

    def get(self, *, userId, id, **kwargs):
        if id not in self.messages:
            return FakeRequest(error=make_http_error(404, "Not Found"))
        return FakeRequest(self.messages[id])

    def modify(self, *, userId, id, body):
        self.modified.append((id, body))
        return FakeRequest({"id": id, **body})

    def send(self, *, userId, body):
        self.sent.append(body)
        return FakeRequest({"id": "sent-1", "threadId": body.get("threadId", "t-1")})


class FakeThreads:
    def __init__(self, threads=None):
        self.threads = threads or {}
        self.modified: list[tuple[str, dict]] = []

    def get(self, *, userId, id, **kwargs):
        if id not in self.threads:
            return FakeRequest(error=make_http_error(404, "Not Found"))
        return FakeRequest(self.threads[id])

    def modify(self, *, userId, id, body):
        self.modified.append((id, body))
        return FakeRequest({"id": id, **body})


class FakeDrafts:
    def __init__(self, drafts=None):
        self.drafts = drafts or {}
        self.created: list[dict] = []
        self.sent: list[dict] = []

    def create(self, *, userId, body):
        self.created.append(body)
        draft_id = f"draft-{len(self.created)}"
        return FakeRequest(
            {"id": draft_id, "message": {"id": f"msg-{len(self.created)}"}}
        )

    def send(self, *, userId, body):
        self.sent.append(body)
        if body["id"] not in self.drafts:
            return FakeRequest(error=make_http_error(404, "Not Found"))
        return FakeRequest({"id": "sent-1", "threadId": "t-1"})


class FakeLabels:
    def __init__(self, labels=None):
        self.labels = labels or []

    def list(self, *, userId):
        return FakeRequest({"labels": self.labels})


class FakeUsers:
    def __init__(self, messages=None, threads=None, drafts=None, labels=None,
                 profile="me@example.com"):
        self._messages = messages or FakeMessages()
        self._threads = threads or FakeThreads()
        self._drafts = drafts or FakeDrafts()
        self._labels = labels or FakeLabels()
        self._profile = profile

    def messages(self):
        return self._messages

    def threads(self):
        return self._threads

    def drafts(self):
        return self._drafts

    def labels(self):
        return self._labels

    def getProfile(self, *, userId):
        return FakeRequest({"emailAddress": self._profile})


class FakeGmail:
    def __init__(self, **kwargs):
        self._users = FakeUsers(**kwargs)

    def users(self):
        return self._users
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_client.py`:

```python
import pytest

from gmail_mcp.gmail.client import (
    NotFoundError,
    RateLimitedError,
    ServiceCache,
    execute,
)
from tests.fakes import FakeRequest, make_http_error


def test_returns_result_on_success():
    assert execute(FakeRequest({"ok": True})) == {"ok": True}


def test_retries_on_rate_limit_then_succeeds():
    request = FakeRequest({"ok": True}, errors_then=[make_http_error(429)])
    slept = []

    assert execute(request, sleep=slept.append) == {"ok": True}
    assert request.execute_count == 2
    assert len(slept) == 1


def test_retries_on_server_error_then_succeeds():
    request = FakeRequest({"ok": True}, errors_then=[make_http_error(503)])
    assert execute(request, sleep=lambda s: None) == {"ok": True}
    assert request.execute_count == 2


def test_gives_up_after_three_attempts():
    request = FakeRequest(error=make_http_error(429))
    with pytest.raises(RateLimitedError):
        execute(request, sleep=lambda s: None)
    assert request.execute_count == 3


def test_backoff_grows_between_attempts():
    request = FakeRequest(error=make_http_error(500))
    slept = []
    with pytest.raises(RateLimitedError):
        execute(request, sleep=slept.append)
    assert len(slept) == 2
    assert slept[1] > slept[0]


def test_404_says_the_id_may_belong_to_another_mailbox():
    request = FakeRequest(error=make_http_error(404, "Not Found"))
    with pytest.raises(NotFoundError) as exc:
        execute(request, sleep=lambda s: None)
    assert "different" in str(exc.value).lower()
    assert request.execute_count == 1


def test_403_is_not_retried():
    request = FakeRequest(error=make_http_error(403, "Forbidden"))
    with pytest.raises(Exception):
        execute(request, sleep=lambda s: None)
    assert request.execute_count == 1


def test_service_cache_builds_once_per_alias(tmp_path):
    from gmail_mcp.config import Account, Config

    config = Config(
        accounts=(
            Account("personal", "me@example.com", "send"),
            Account("work", "w@example.com", "send"),
        )
    )
    built = []

    cache = ServiceCache(
        config,
        store=None,
        credential_loader=lambda alias, store: f"creds-{alias}",
        service_builder=lambda creds: built.append(creds) or f"svc-{creds}",
    )

    assert cache.get("personal") == "svc-creds-personal"
    assert cache.get("personal") == "svc-creds-personal"
    assert cache.get("work") == "svc-creds-work"
    assert built == ["creds-personal", "creds-work"]


def test_service_cache_rejects_unknown_alias(tmp_path):
    from gmail_mcp.config import Account, Config
    from gmail_mcp.config import UnknownAliasError

    config = Config(accounts=(Account("personal", "me@example.com", "send"),))
    cache = ServiceCache(
        config,
        store=None,
        credential_loader=lambda alias, store: "creds",
        service_builder=lambda creds: "svc",
    )

    with pytest.raises(UnknownAliasError):
        cache.get("nope")


def test_clear_forces_rebuild():
    from gmail_mcp.config import Account, Config

    config = Config(accounts=(Account("personal", "me@example.com", "send"),))
    built = []
    cache = ServiceCache(
        config,
        store=None,
        credential_loader=lambda alias, store: "c",
        service_builder=lambda creds: built.append(creds) or "svc",
    )

    cache.get("personal")
    cache.clear()
    cache.get("personal")
    assert len(built) == 2
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gmail_mcp.gmail'`

- [ ] **Step 4: Write the implementation**

Create empty `gmail_mcp/gmail/__init__.py`, then `gmail_mcp/gmail/client.py`:

```python
"""Gmail service construction and the single API call choke point."""

from __future__ import annotations

import random
import time
from collections.abc import Callable

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from gmail_mcp.auth import load_credentials
from gmail_mcp.config import Config

_RETRYABLE = {429, 500, 502, 503, 504}


class GmailError(Exception):
    """A Gmail API call failed in a way worth reporting to the model."""


class NotFoundError(GmailError):
    """The requested message, thread, or draft does not exist here."""


class RateLimitedError(GmailError):
    """Gmail kept refusing the request after retries."""


def execute(request, *, sleep: Callable[[float], None] = time.sleep,
            attempts: int = 3) -> dict:
    """Execute a Gmail request, retrying transient failures."""
    last: HttpError | None = None

    for attempt in range(attempts):
        try:
            return request.execute()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)

            if status == 404:
                raise NotFoundError(
                    "Not found in this mailbox. Gmail IDs are specific to the "
                    "account they came from, so this ID may belong to a "
                    "different account."
                ) from exc

            if status not in _RETRYABLE:
                raise GmailError(f"Gmail rejected the request: {exc}") from exc

            last = exc
            if attempt < attempts - 1:
                delay = (2**attempt) + random.uniform(0, 0.25 * (attempt + 1))
                sleep(delay)

    raise RateLimitedError(
        f"Gmail is rate limiting or unavailable after {attempts} attempts: {last}"
    ) from last


def _default_service_builder(creds):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


class ServiceCache:
    """One Gmail service per alias, built lazily and kept for the process.

    Each alias gets its own service object with its own HTTP transport,
    which is what makes the per-account parallelism in check.py safe.
    """

    def __init__(
        self,
        config: Config,
        store,
        *,
        credential_loader: Callable = load_credentials,
        service_builder: Callable = _default_service_builder,
    ):
        self._config = config
        self._store = store
        self._load = credential_loader
        self._build = service_builder
        self._services: dict[str, object] = {}

    def get(self, alias: str):
        self._config.get(alias)  # raises UnknownAliasError with valid names
        if alias not in self._services:
            creds = self._load(alias, self._store)
            self._services[alias] = self._build(creds)
        return self._services[alias]

    def clear(self) -> None:
        self._services.clear()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_client.py -v`
Expected: PASS — 10 tests

- [ ] **Step 6: Commit**

```bash
git add gmail_mcp/gmail/ tests/fakes.py tests/test_client.py
git commit -m "feat: add Gmail service cache with retry and error mapping"
```

---

### Task 5: Search

**Files:**
- Create: `gmail_mcp/gmail/search.py`
- Test: `tests/test_search.py`

**Interfaces:**
- Consumes: `execute`, `NotFoundError` from `gmail_mcp.gmail.client`.
- Produces:
  - `DEFAULT_MAX_RESULTS = 20`, `MAX_RESULTS_CAP = 100`
  - `header(payload: dict, name: str) -> str`
  - `summarise(message: dict) -> dict` returning keys `message_id`, `thread_id`, `from`, `to`, `subject`, `date`, `snippet`, `unread`, `labels`
  - `METADATA_HEADERS: list[str]`
  - `fetch_metadata(service, message_ids: list[str]) -> list[dict]`
  - `search_messages(service, query, *, max_results=DEFAULT_MAX_RESULTS, include_spam_trash=False) -> list[dict]`

`header` and `summarise` are reused by Task 6, and `fetch_metadata` and `summarise` by Task 10, so they live here and are imported rather than duplicated.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_search.py`:

```python
import pytest

from gmail_mcp.gmail.search import (
    MAX_RESULTS_CAP,
    header,
    search_messages,
    summarise,
)
from tests.fakes import FakeGmail, FakeMessages


def metadata(mid, subject="Hi", sender="a@example.com", labels=("INBOX", "UNREAD")):
    return {
        "id": mid,
        "threadId": f"t-{mid}",
        "snippet": "hello there",
        "labelIds": list(labels),
        "internalDate": "1700000000000",
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": "me@example.com"},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
            ]
        },
    }


def test_header_is_case_insensitive():
    payload = {"headers": [{"name": "Subject", "value": "Hi"}]}
    assert header(payload, "subject") == "Hi"


def test_header_missing_returns_empty_string():
    assert header({"headers": []}, "Subject") == ""


def test_summarise_extracts_the_compact_shape():
    result = summarise(metadata("m1", subject="Invoice"))

    assert result == {
        "message_id": "m1",
        "thread_id": "t-m1",
        "from": "a@example.com",
        "to": "me@example.com",
        "subject": "Invoice",
        "date": "Mon, 1 Jan 2024 00:00:00 +0000",
        "snippet": "hello there",
        "unread": True,
        "labels": ["INBOX", "UNREAD"],
    }


def test_summarise_marks_read_messages():
    assert summarise(metadata("m1", labels=("INBOX",)))["unread"] is False


def test_search_returns_summaries_in_listing_order():
    messages = FakeMessages(
        listing={"messages": [{"id": "m2"}, {"id": "m1"}]},
        messages={"m1": metadata("m1", subject="One"),
                  "m2": metadata("m2", subject="Two")},
    )
    service = FakeGmail(messages=messages)

    results = search_messages(service, "is:unread")

    assert [r["subject"] for r in results] == ["Two", "One"]


def test_search_passes_query_through_untouched():
    messages = FakeMessages(listing={"messages": []})
    service = FakeGmail(messages=messages)

    search_messages(service, "from:boss@example.com newer_than:2d")

    assert messages.list_calls[0]["q"] == "from:boss@example.com newer_than:2d"


def test_search_defaults_to_twenty_and_excludes_spam_and_trash():
    messages = FakeMessages(listing={"messages": []})
    search_messages(FakeGmail(messages=messages), "x")

    call = messages.list_calls[0]
    assert call["maxResults"] == 20
    assert call["includeSpamTrash"] is False


def test_search_can_include_spam_and_trash():
    messages = FakeMessages(listing={"messages": []})
    search_messages(FakeGmail(messages=messages), "x", include_spam_trash=True)

    assert messages.list_calls[0]["includeSpamTrash"] is True


def test_max_results_is_capped():
    messages = FakeMessages(listing={"messages": []})
    search_messages(FakeGmail(messages=messages), "x", max_results=5000)

    assert messages.list_calls[0]["maxResults"] == MAX_RESULTS_CAP


def test_max_results_below_one_is_rejected():
    with pytest.raises(ValueError):
        search_messages(FakeGmail(), "x", max_results=0)


def test_empty_listing_returns_empty_list():
    service = FakeGmail(messages=FakeMessages(listing={}))
    assert search_messages(service, "x") == []


def test_message_vanishing_between_list_and_get_is_skipped():
    messages = FakeMessages(
        listing={"messages": [{"id": "gone"}, {"id": "m1"}]},
        messages={"m1": metadata("m1")},
    )
    results = search_messages(FakeGmail(messages=messages), "x")

    assert [r["message_id"] for r in results] == ["m1"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_search.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gmail_mcp.gmail.search'`

- [ ] **Step 3: Write the implementation**

Create `gmail_mcp/gmail/search.py`:

```python
"""Search and the compact message summary shape."""

from __future__ import annotations

from gmail_mcp.gmail.client import NotFoundError, execute

DEFAULT_MAX_RESULTS = 20
MAX_RESULTS_CAP = 100
METADATA_HEADERS = ["From", "To", "Subject", "Date"]


def header(payload: dict, name: str) -> str:
    """Return a header value, matching the name case-insensitively."""
    wanted = name.lower()
    for entry in payload.get("headers", []):
        if entry.get("name", "").lower() == wanted:
            return entry.get("value", "")
    return ""


def summarise(message: dict) -> dict:
    """Reduce a Gmail message resource to the compact shape tools return."""
    payload = message.get("payload", {})
    labels = message.get("labelIds", [])
    return {
        "message_id": message.get("id", ""),
        "thread_id": message.get("threadId", ""),
        "from": header(payload, "From"),
        "to": header(payload, "To"),
        "subject": header(payload, "Subject"),
        "date": header(payload, "Date"),
        "snippet": message.get("snippet", ""),
        "unread": "UNREAD" in labels,
        "labels": list(labels),
    }


def fetch_metadata(service, message_ids: list[str]) -> list[dict]:
    """Fetch metadata for each ID, skipping any that have vanished.

    Fetches are sequential on purpose. googleapiclient service objects
    are not thread-safe, so parallelism happens one level up, across
    accounts, where each account has its own service.
    """
    out = []
    for message_id in message_ids:
        request = service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=METADATA_HEADERS,
        )
        try:
            out.append(execute(request))
        except NotFoundError:
            continue
    return out


def search_messages(
    service,
    query: str,
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    include_spam_trash: bool = False,
) -> list[dict]:
    """Run a Gmail query and return compact summaries."""
    if max_results < 1:
        raise ValueError("max_results must be at least 1.")

    capped = min(max_results, MAX_RESULTS_CAP)
    listing = execute(
        service.users().messages().list(
            userId="me",
            q=query,
            maxResults=capped,
            includeSpamTrash=include_spam_trash,
        )
    )

    ids = [m["id"] for m in listing.get("messages", [])]
    return [summarise(m) for m in fetch_metadata(service, ids)]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_search.py -v`
Expected: PASS — 12 tests

- [ ] **Step 5: Commit**

```bash
git add gmail_mcp/gmail/search.py tests/test_search.py
git commit -m "feat: add mail search with compact result summaries"
```

---

### Task 6: Reading messages and threads

**Files:**
- Create: `gmail_mcp/gmail/messages.py`
- Test: `tests/test_messages.py`

**Interfaces:**
- Consumes: `execute` from `gmail_mcp.gmail.client`; `header`, `summarise` from `gmail_mcp.gmail.search`.
- Produces:
  - `BODY_CHAR_LIMIT = 50_000`
  - `html_to_text(html: str) -> str`
  - `collapse_quoted(text: str) -> str`
  - `extract_body(payload: dict) -> str`
  - `list_attachments(payload: dict) -> list[dict]` with keys `filename`, `mime_type`, `size`
  - `read_message(service, message_id: str) -> dict`
  - `read_thread(service, thread_id: str) -> dict`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_messages.py`:

```python
import base64

import pytest

from gmail_mcp.gmail.client import NotFoundError
from gmail_mcp.gmail.messages import (
    BODY_CHAR_LIMIT,
    collapse_quoted,
    extract_body,
    html_to_text,
    list_attachments,
    read_message,
    read_thread,
)
from tests.fakes import FakeGmail, FakeMessages, FakeThreads


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


def part(mime, text=None, filename=None, size=0, parts=None):
    node = {"mimeType": mime, "headers": [], "body": {"size": size}}
    if text is not None:
        node["body"]["data"] = b64(text)
        node["body"]["size"] = len(text)
    if filename:
        node["filename"] = filename
        node["body"]["attachmentId"] = "att-1"
    if parts:
        node["parts"] = parts
    return node


def message(mid="m1", payload=None, labels=("INBOX",)):
    return {
        "id": mid,
        "threadId": "t-1",
        "snippet": "snip",
        "labelIds": list(labels),
        "internalDate": "1700000000000",
        "payload": payload
        or {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "a@example.com"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Subject", "value": "Hi"},
                {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
                {"name": "Message-ID", "value": "<abc@example.com>"},
            ],
            "body": {"data": b64("plain body"), "size": 10},
        },
    }


def test_extracts_simple_plain_body():
    assert extract_body(message()["payload"]) == "plain body"


def test_prefers_plain_text_over_html_in_multipart():
    payload = part(
        "multipart/alternative",
        parts=[part("text/html", "<p>html body</p>"),
               part("text/plain", "plain body")],
    )
    assert extract_body(payload) == "plain body"


def test_falls_back_to_html_when_no_plain_part():
    payload = part(
        "multipart/alternative",
        parts=[part("text/html", "<p>Hello <b>world</b></p>")],
    )
    assert extract_body(payload) == "Hello world"


def test_finds_body_in_nested_multipart():
    payload = part(
        "multipart/mixed",
        parts=[
            part("multipart/alternative", parts=[part("text/plain", "deep body")]),
            part("application/pdf", filename="doc.pdf", size=99),
        ],
    )
    assert extract_body(payload) == "deep body"


def test_missing_body_returns_empty_string():
    assert extract_body({"mimeType": "text/plain", "body": {}}) == ""


def test_long_body_is_truncated_with_a_notice():
    payload = part("text/plain", "x" * (BODY_CHAR_LIMIT + 500))
    body = extract_body(payload)

    assert len(body) < BODY_CHAR_LIMIT + 200
    assert "truncated" in body.lower()


def test_html_to_text_drops_tags_and_scripts():
    html = "<style>a{}</style><script>x()</script><p>Hi</p><br><p>There</p>"
    assert html_to_text(html) == "Hi\nThere"


def test_html_to_text_unescapes_entities():
    assert html_to_text("<p>A &amp; B &lt;yes&gt;</p>") == "A & B <yes>"


def test_collapse_quoted_removes_quoted_lines():
    text = "My reply\n\nOn Mon someone wrote:\n> old thing\n> more old"
    assert collapse_quoted(text) == "My reply"


def test_collapse_quoted_keeps_text_without_quotes():
    assert collapse_quoted("Just a note") == "Just a note"


def test_collapse_quoted_handles_forwarded_marker():
    text = "See below\n\n---------- Forwarded message ---------\nold stuff"
    assert collapse_quoted(text) == "See below"


def test_lists_attachment_metadata_only():
    payload = part(
        "multipart/mixed",
        parts=[part("text/plain", "body"),
               part("application/pdf", filename="doc.pdf", size=1234)],
    )
    assert list_attachments(payload) == [
        {"filename": "doc.pdf", "mime_type": "application/pdf", "size": 1234}
    ]


def test_read_message_returns_headers_body_and_attachments():
    service = FakeGmail(messages=FakeMessages(messages={"m1": message()}))
    result = read_message(service, "m1")

    assert result["subject"] == "Hi"
    assert result["body"] == "plain body"
    assert result["message_id_header"] == "<abc@example.com>"
    assert result["attachments"] == []
    assert result["unread"] is False


def test_read_message_missing_id_mentions_other_mailboxes():
    service = FakeGmail(messages=FakeMessages(messages={}))
    with pytest.raises(NotFoundError) as exc:
        read_message(service, "nope")
    assert "different" in str(exc.value).lower()


def test_read_thread_returns_messages_in_order_with_quotes_collapsed():
    first = message("m1")
    second = message("m2")
    second["payload"]["body"]["data"] = b64(
        "Reply text\n\nOn Mon someone wrote:\n> quoted"
    )
    service = FakeGmail(
        threads=FakeThreads(threads={"t-1": {"id": "t-1",
                                             "messages": [first, second]}})
    )

    result = read_thread(service, "t-1")

    assert result["thread_id"] == "t-1"
    assert [m["message_id"] for m in result["messages"]] == ["m1", "m2"]
    assert result["messages"][1]["body"] == "Reply text"


def test_read_thread_missing_id_raises_not_found():
    service = FakeGmail(threads=FakeThreads(threads={}))
    with pytest.raises(NotFoundError):
        read_thread(service, "nope")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_messages.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gmail_mcp.gmail.messages'`

- [ ] **Step 3: Write the implementation**

Create `gmail_mcp/gmail/messages.py`:

```python
"""Reading messages and threads, and turning MIME into readable text."""

from __future__ import annotations

import base64
import html
import re

from gmail_mcp.gmail.client import execute
from gmail_mcp.gmail.search import header, summarise

BODY_CHAR_LIMIT = 50_000

_QUOTE_MARKERS = (
    re.compile(r"^On .*wrote:\s*$", re.IGNORECASE),
    re.compile(r"^-+\s*Forwarded message\s*-+", re.IGNORECASE),
    re.compile(r"^-+\s*Original Message\s*-+", re.IGNORECASE),
)


def _decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode()).decode(
        "utf-8", errors="replace"
    )


def html_to_text(source: str) -> str:
    """Crude but predictable HTML flattening for display."""
    text = re.sub(
        r"<(script|style)[^>]*>.*?</\1>", "", source,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def collapse_quoted(text: str) -> str:
    """Drop quoted replies and forwarded tails."""
    kept: list[str] = []
    for line in text.splitlines():
        if any(marker.match(line.strip()) for marker in _QUOTE_MARKERS):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _walk(payload: dict):
    yield payload
    for child in payload.get("parts", []) or []:
        yield from _walk(child)


def _part_text(node: dict) -> str:
    data = node.get("body", {}).get("data")
    return _decode(data) if data else ""


def extract_body(payload: dict) -> str:
    """Return the readable body, preferring text/plain over HTML."""
    plain, rich = "", ""
    for node in _walk(payload):
        if node.get("filename"):
            continue
        mime = node.get("mimeType", "")
        if mime == "text/plain" and not plain:
            plain = _part_text(node)
        elif mime == "text/html" and not rich:
            rich = _part_text(node)

    body = plain or (html_to_text(rich) if rich else "")

    if len(body) > BODY_CHAR_LIMIT:
        body = body[:BODY_CHAR_LIMIT] + "\n\n[body truncated]"
    return body


def list_attachments(payload: dict) -> list[dict]:
    """Report attachment names and sizes. Contents are never fetched."""
    return [
        {
            "filename": node["filename"],
            "mime_type": node.get("mimeType", ""),
            "size": node.get("body", {}).get("size", 0),
        }
        for node in _walk(payload)
        if node.get("filename")
    ]


def _render(message: dict, *, collapse: bool) -> dict:
    payload = message.get("payload", {})
    body = extract_body(payload)
    record = summarise(message)
    record["body"] = collapse_quoted(body) if collapse else body
    record["message_id_header"] = header(payload, "Message-ID")
    record["attachments"] = list_attachments(payload)
    return record


def read_message(service, message_id: str) -> dict:
    """Fetch one message in full."""
    raw = execute(
        service.users().messages().get(
            userId="me", id=message_id, format="full"
        )
    )
    return _render(raw, collapse=False)


def read_thread(service, thread_id: str) -> dict:
    """Fetch a whole thread, oldest first, with quoted text collapsed."""
    raw = execute(
        service.users().threads().get(
            userId="me", id=thread_id, format="full"
        )
    )
    return {
        "thread_id": raw.get("id", thread_id),
        "messages": [
            _render(m, collapse=True) for m in raw.get("messages", [])
        ],
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_messages.py -v`
Expected: PASS — 16 tests

- [ ] **Step 5: Commit**

```bash
git add gmail_mcp/gmail/messages.py tests/test_messages.py
git commit -m "feat: add message and thread reading with MIME body extraction"
```

---

### Task 7: MIME construction and reply threading

**Files:**
- Create: `gmail_mcp/gmail/compose.py`
- Test: `tests/test_compose_mime.py`

**Interfaces:**
- Consumes: `execute` from `gmail_mcp.gmail.client`; `header` from `gmail_mcp.gmail.search`.
- Produces:
  - `build_mime(*, to, subject, body, cc=None, bcc=None, in_reply_to=None, references=None) -> EmailMessage`
  - `encode(message: EmailMessage) -> str` (base64url of the serialised message)
  - `ReplyContext` frozen dataclass with fields `thread_id: str`, `message_id_header: str`, `references: str`, `subject: str`
  - `fetch_reply_context(service, message_id: str) -> ReplyContext`
  - `reply_subject(subject: str) -> str`

Task 8 adds the sending functions to this same file.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_compose_mime.py`:

```python
import base64
from email import message_from_bytes

import pytest

from gmail_mcp.gmail.compose import (
    build_mime,
    encode,
    fetch_reply_context,
    reply_subject,
)
from tests.fakes import FakeGmail, FakeMessages


def decode(raw):
    return message_from_bytes(base64.urlsafe_b64decode(raw.encode()))


def test_sets_the_basic_headers():
    msg = build_mime(to="a@example.com", subject="Hi", body="Hello")

    assert msg["To"] == "a@example.com"
    assert msg["Subject"] == "Hi"
    assert msg.get_content().strip() == "Hello"


def test_accepts_a_list_of_recipients():
    msg = build_mime(
        to=["a@example.com", "b@example.com"], subject="Hi", body="x"
    )
    assert msg["To"] == "a@example.com, b@example.com"


def test_sets_cc_and_bcc():
    msg = build_mime(
        to="a@example.com", subject="Hi", body="x",
        cc=["c@example.com"], bcc=["d@example.com"],
    )
    assert msg["Cc"] == "c@example.com"
    assert msg["Bcc"] == "d@example.com"


def test_omits_cc_and_bcc_when_absent():
    msg = build_mime(to="a@example.com", subject="Hi", body="x")
    assert msg["Cc"] is None
    assert msg["Bcc"] is None


def test_empty_recipient_list_is_rejected():
    with pytest.raises(ValueError, match="recipient"):
        build_mime(to=[], subject="Hi", body="x")


def test_unicode_subject_and_body_survive_a_roundtrip():
    msg = build_mime(to="a@example.com", subject="Réunion 会議", body="Café ☕")
    restored = decode(encode(msg))

    assert restored["Subject"] == "Réunion 会議"
    assert "Café ☕" in restored.get_content()


def test_reply_headers_are_set_when_supplied():
    msg = build_mime(
        to="a@example.com", subject="Re: Hi", body="x",
        in_reply_to="<abc@example.com>",
        references="<root@example.com> <abc@example.com>",
    )
    assert msg["In-Reply-To"] == "<abc@example.com>"
    assert msg["References"] == "<root@example.com> <abc@example.com>"


def test_encode_produces_url_safe_base64():
    raw = encode(build_mime(to="a@example.com", subject="Hi", body="x"))
    assert "+" not in raw and "/" not in raw
    assert decode(raw)["Subject"] == "Hi"


def test_reply_subject_adds_one_prefix():
    assert reply_subject("Hi") == "Re: Hi"


def test_reply_subject_does_not_stack_prefixes():
    assert reply_subject("Re: Hi") == "Re: Hi"
    assert reply_subject("RE: Hi") == "RE: Hi"


def test_fetch_reply_context_reads_threading_headers():
    original = {
        "id": "m1",
        "threadId": "t-9",
        "payload": {
            "headers": [
                {"name": "Message-ID", "value": "<abc@example.com>"},
                {"name": "References", "value": "<root@example.com>"},
                {"name": "Subject", "value": "Quarterly numbers"},
            ]
        },
    }
    service = FakeGmail(messages=FakeMessages(messages={"m1": original}))

    context = fetch_reply_context(service, "m1")

    assert context.thread_id == "t-9"
    assert context.message_id_header == "<abc@example.com>"
    assert context.references == "<root@example.com> <abc@example.com>"
    assert context.subject == "Quarterly numbers"


def test_fetch_reply_context_starts_references_when_absent():
    original = {
        "id": "m1",
        "threadId": "t-9",
        "payload": {"headers": [{"name": "Message-ID", "value": "<abc@x>"}]},
    }
    service = FakeGmail(messages=FakeMessages(messages={"m1": original}))

    assert fetch_reply_context(service, "m1").references == "<abc@x>"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_compose_mime.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gmail_mcp.gmail.compose'`

- [ ] **Step 3: Write the implementation**

Create `gmail_mcp/gmail/compose.py`:

```python
"""Building outgoing mail, including correct reply threading."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from email.message import EmailMessage

from gmail_mcp.gmail.client import execute
from gmail_mcp.gmail.search import header

Recipients = str | list[str]


@dataclass(frozen=True)
class ReplyContext:
    thread_id: str
    message_id_header: str
    references: str
    subject: str


def _join(value: Recipients | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    joined = ", ".join(v.strip() for v in value if v and v.strip())
    return joined or None


def build_mime(
    *,
    to: Recipients,
    subject: str,
    body: str,
    cc: Recipients | None = None,
    bcc: Recipients | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> EmailMessage:
    """Assemble an outgoing message."""
    recipients = _join(to)
    if not recipients:
        raise ValueError("At least one recipient is required.")

    message = EmailMessage()
    message["To"] = recipients
    message["Subject"] = subject

    if cc_value := _join(cc):
        message["Cc"] = cc_value
    if bcc_value := _join(bcc):
        message["Bcc"] = bcc_value

    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references

    message.set_content(body)
    return message


def encode(message: EmailMessage) -> str:
    """Serialise to the base64url form the Gmail API expects."""
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def reply_subject(subject: str) -> str:
    """Prefix with Re: unless it already is one."""
    if subject.strip().lower().startswith("re:"):
        return subject
    return f"Re: {subject}"


def fetch_reply_context(service, message_id: str) -> ReplyContext:
    """Read the headers needed to make a reply thread correctly.

    Without In-Reply-To and References, replies arrive as orphan
    messages in the recipient's client even though Gmail groups them
    correctly on the sender's side.
    """
    raw = execute(
        service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["Message-ID", "References", "Subject"],
        )
    )
    payload = raw.get("payload", {})
    parent = header(payload, "Message-ID")
    existing = header(payload, "References")

    return ReplyContext(
        thread_id=raw.get("threadId", ""),
        message_id_header=parent,
        references=f"{existing} {parent}".strip(),
        subject=header(payload, "Subject"),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_compose_mime.py -v`
Expected: PASS — 12 tests

- [ ] **Step 5: Commit**

```bash
git add gmail_mcp/gmail/compose.py tests/test_compose_mime.py
git commit -m "feat: add MIME construction with reply threading headers"
```

---

### Task 8: The send-policy gate, drafts, and sending

**Files:**
- Modify: `gmail_mcp/gmail/compose.py` (append; do not alter Task 7's functions)
- Test: `tests/test_send_policy.py`

**Interfaces:**
- Consumes: `build_mime`, `encode`, `reply_subject`, `fetch_reply_context` from Task 7; `execute`, `NotFoundError` from `gmail_mcp.gmail.client`; `Account` from `gmail_mcp.config`.
- Produces:
  - `SendAction` str-enum with members `SEND = "send"` and `DRAFT = "draft"`
  - `resolve_send_action(policy: str, confirm: bool) -> SendAction`
  - `compose_and_deliver(service, account, *, to, subject, body, cc=None, bcc=None, reply_to_message_id=None, confirm=False, force_draft=False) -> dict`
  - `send_existing_draft(service, account, draft_id: str, *, confirm=False) -> dict`

Returned dicts always carry `action` (`"sent"` or `"drafted"`), `policy`, and `note`. Sent results add `message_id` and `thread_id`; drafted results add `draft_id`.

This is the code most capable of causing real-world damage, so the gate is a pure function tested exhaustively before anything calls it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_send_policy.py`:

```python
import pytest

from gmail_mcp.config import Account
from gmail_mcp.gmail.compose import (
    SendAction,
    compose_and_deliver,
    resolve_send_action,
    send_existing_draft,
)
from tests.fakes import FakeDrafts, FakeGmail, FakeMessages


def account(policy):
    return Account(alias="a", email="a@example.com", send_policy=policy)


@pytest.mark.parametrize(
    "policy,confirm,expected",
    [
        ("send", False, SendAction.SEND),
        ("send", True, SendAction.SEND),
        ("confirm", False, SendAction.DRAFT),
        ("confirm", True, SendAction.SEND),
        ("draft_only", False, SendAction.DRAFT),
        ("draft_only", True, SendAction.DRAFT),
    ],
)
def test_policy_gate_truth_table(policy, confirm, expected):
    assert resolve_send_action(policy, confirm) is expected


def test_unknown_policy_is_rejected_rather_than_defaulted():
    with pytest.raises(ValueError, match="anything-goes"):
        resolve_send_action("anything-goes", True)


def test_send_policy_transmits_immediately():
    messages = FakeMessages()
    service = FakeGmail(messages=messages)

    result = compose_and_deliver(
        service, account("send"), to="b@example.com", subject="Hi", body="x"
    )

    assert result["action"] == "sent"
    assert result["message_id"] == "sent-1"
    assert len(messages.sent) == 1


def test_confirm_policy_without_confirmation_creates_a_draft():
    drafts = FakeDrafts()
    messages = FakeMessages()
    service = FakeGmail(messages=messages, drafts=drafts)

    result = compose_and_deliver(
        service, account("confirm"), to="b@example.com", subject="Hi", body="x"
    )

    assert result["action"] == "drafted"
    assert result["draft_id"] == "draft-1"
    assert messages.sent == []
    assert "confirm" in result["note"].lower()


def test_confirm_policy_with_confirmation_sends():
    messages = FakeMessages()
    service = FakeGmail(messages=messages)

    result = compose_and_deliver(
        service, account("confirm"), to="b@example.com", subject="Hi",
        body="x", confirm=True,
    )

    assert result["action"] == "sent"
    assert len(messages.sent) == 1


def test_draft_only_policy_never_sends_even_when_confirmed():
    messages = FakeMessages()
    drafts = FakeDrafts()
    service = FakeGmail(messages=messages, drafts=drafts)

    result = compose_and_deliver(
        service, account("draft_only"), to="b@example.com", subject="Hi",
        body="x", confirm=True,
    )

    assert result["action"] == "drafted"
    assert messages.sent == []
    assert "draft_only" in result["note"]


def test_force_draft_overrides_a_send_policy():
    messages = FakeMessages()
    service = FakeGmail(messages=messages, drafts=FakeDrafts())

    result = compose_and_deliver(
        service, account("send"), to="b@example.com", subject="Hi",
        body="x", force_draft=True,
    )

    assert result["action"] == "drafted"
    assert messages.sent == []


def test_reply_sets_threading_headers_and_thread_id():
    original = {
        "id": "m1",
        "threadId": "t-9",
        "payload": {
            "headers": [
                {"name": "Message-ID", "value": "<abc@example.com>"},
                {"name": "Subject", "value": "Numbers"},
            ]
        },
    }
    messages = FakeMessages(messages={"m1": original})
    service = FakeGmail(messages=messages)

    compose_and_deliver(
        service, account("send"), to="b@example.com", subject=None,
        body="x", reply_to_message_id="m1",
    )

    body = messages.sent[0]
    assert body["threadId"] == "t-9"

    import base64
    from email import message_from_bytes

    mime = message_from_bytes(base64.urlsafe_b64decode(body["raw"].encode()))
    assert mime["In-Reply-To"] == "<abc@example.com>"
    assert mime["Subject"] == "Re: Numbers"


def test_explicit_subject_wins_over_derived_reply_subject():
    original = {
        "id": "m1",
        "threadId": "t-9",
        "payload": {"headers": [{"name": "Subject", "value": "Numbers"}]},
    }
    messages = FakeMessages(messages={"m1": original})
    service = FakeGmail(messages=messages)

    compose_and_deliver(
        service, account("send"), to="b@example.com", subject="My subject",
        body="x", reply_to_message_id="m1",
    )

    import base64
    from email import message_from_bytes

    mime = message_from_bytes(
        base64.urlsafe_b64decode(messages.sent[0]["raw"].encode())
    )
    assert mime["Subject"] == "My subject"


def test_new_message_without_subject_is_rejected():
    with pytest.raises(ValueError, match="subject"):
        compose_and_deliver(
            FakeGmail(), account("send"), to="b@example.com",
            subject=None, body="x",
        )


def test_send_existing_draft_respects_confirm_policy():
    drafts = FakeDrafts(drafts={"draft-1": {}})
    service = FakeGmail(drafts=drafts)

    result = send_existing_draft(service, account("confirm"), "draft-1")

    assert result["action"] == "drafted"
    assert drafts.sent == []
    assert result["draft_id"] == "draft-1"


def test_send_existing_draft_sends_when_confirmed():
    drafts = FakeDrafts(drafts={"draft-1": {}})
    service = FakeGmail(drafts=drafts)

    result = send_existing_draft(
        service, account("confirm"), "draft-1", confirm=True
    )

    assert result["action"] == "sent"
    assert drafts.sent == [{"id": "draft-1"}]


def test_send_existing_draft_blocked_by_draft_only():
    drafts = FakeDrafts(drafts={"draft-1": {}})
    service = FakeGmail(drafts=drafts)

    result = send_existing_draft(
        service, account("draft_only"), "draft-1", confirm=True
    )

    assert result["action"] == "drafted"
    assert drafts.sent == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_send_policy.py -v`
Expected: FAIL — `ImportError: cannot import name 'SendAction'`

- [ ] **Step 3: Append the implementation to `gmail_mcp/gmail/compose.py`**

Add these imports at the top of the file, alongside the existing ones:

```python
from enum import StrEnum

from gmail_mcp.config import Account
```

Then append to the end of the file:

```python
class SendAction(StrEnum):
    SEND = "send"
    DRAFT = "draft"


_GATE = {
    ("send", False): SendAction.SEND,
    ("send", True): SendAction.SEND,
    ("confirm", False): SendAction.DRAFT,
    ("confirm", True): SendAction.SEND,
    ("draft_only", False): SendAction.DRAFT,
    ("draft_only", True): SendAction.DRAFT,
}

_NOTES = {
    "confirm": (
        "Account policy is 'confirm', so this was saved as a draft. "
        "Call again with confirm=true to send it."
    ),
    "draft_only": (
        "Account policy is 'draft_only', so this was saved as a draft "
        "and will never be sent by this server."
    ),
    "send": "Saved as a draft because a draft was explicitly requested.",
}


def resolve_send_action(policy: str, confirm: bool) -> SendAction:
    """Decide whether to transmit, from configuration alone.

    The decision lives here rather than in prompt text so it holds
    regardless of what the model has been told in conversation.
    """
    try:
        return _GATE[(policy, bool(confirm))]
    except KeyError:
        raise ValueError(
            f"Unknown send policy {policy!r}. "
            "Must be one of: send, confirm, draft_only."
        ) from None


def _create_draft(service, raw: str, thread_id: str | None) -> dict:
    message: dict = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    return execute(
        service.users().drafts().create(
            userId="me", body={"message": message}
        )
    )


def _send_raw(service, raw: str, thread_id: str | None) -> dict:
    body: dict = {"raw": raw}
    if thread_id:
        body["threadId"] = thread_id
    return execute(service.users().messages().send(userId="me", body=body))


def compose_and_deliver(
    service,
    account: Account,
    *,
    to,
    subject: str | None,
    body: str,
    cc=None,
    bcc=None,
    reply_to_message_id: str | None = None,
    confirm: bool = False,
    force_draft: bool = False,
) -> dict:
    """Compose a message and either send it or save it as a draft."""
    context = (
        fetch_reply_context(service, reply_to_message_id)
        if reply_to_message_id
        else None
    )

    if subject is None:
        if context is None:
            raise ValueError(
                "A subject is required unless replying to a message."
            )
        subject = reply_subject(context.subject)

    mime = build_mime(
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        in_reply_to=context.message_id_header if context else None,
        references=context.references if context else None,
    )
    raw = encode(mime)
    thread_id = context.thread_id if context else None

    action = resolve_send_action(account.send_policy, confirm)
    if force_draft:
        action = SendAction.DRAFT

    if action is SendAction.SEND:
        sent = _send_raw(service, raw, thread_id)
        return {
            "action": "sent",
            "policy": account.send_policy,
            "account": account.alias,
            "message_id": sent.get("id", ""),
            "thread_id": sent.get("threadId", ""),
            "note": "Message sent.",
        }

    draft = _create_draft(service, raw, thread_id)
    return {
        "action": "drafted",
        "policy": account.send_policy,
        "account": account.alias,
        "draft_id": draft.get("id", ""),
        "note": _NOTES[account.send_policy],
    }


def send_existing_draft(
    service, account: Account, draft_id: str, *, confirm: bool = False
) -> dict:
    """Send a previously created draft, subject to the same gate."""
    action = resolve_send_action(account.send_policy, confirm)

    if action is SendAction.DRAFT:
        return {
            "action": "drafted",
            "policy": account.send_policy,
            "account": account.alias,
            "draft_id": draft_id,
            "note": _NOTES[account.send_policy],
        }

    sent = execute(
        service.users().drafts().send(userId="me", body={"id": draft_id})
    )
    return {
        "action": "sent",
        "policy": account.send_policy,
        "account": account.alias,
        "message_id": sent.get("id", ""),
        "thread_id": sent.get("threadId", ""),
        "note": "Draft sent.",
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_send_policy.py -v`
Expected: PASS — 18 tests (6 parametrised cases plus 12)

- [ ] **Step 5: Run the whole suite to confirm nothing regressed**

Run: `uv run pytest -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add gmail_mcp/gmail/compose.py tests/test_send_policy.py
git commit -m "feat: enforce per-account send policy for drafts and sending"
```

---

### Task 9: Labels

**Files:**
- Create: `gmail_mcp/gmail/labels.py`
- Test: `tests/test_labels.py`

**Interfaces:**
- Consumes: `execute`, `GmailError` from `gmail_mcp.gmail.client`.
- Produces:
  - `list_labels(service) -> list[dict]` with keys `id`, `name`, `type`
  - `resolve_label_ids(service, names: list[str]) -> list[str]`
  - `modify_labels(service, *, message_id=None, thread_id=None, add=(), remove=()) -> dict`

`modify_labels` accepts label *names*, not IDs, because that is what the model has. Marking read and unread and archiving all go through it via the `UNREAD` and `INBOX` system labels.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_labels.py`:

```python
import pytest

from gmail_mcp.gmail.client import GmailError
from gmail_mcp.gmail.labels import list_labels, modify_labels, resolve_label_ids
from tests.fakes import FakeGmail, FakeLabels, FakeMessages, FakeThreads

LABELS = [
    {"id": "INBOX", "name": "INBOX", "type": "system"},
    {"id": "UNREAD", "name": "UNREAD", "type": "system"},
    {"id": "Label_7", "name": "Clients/Acme", "type": "user"},
]


def service_with_labels(**kwargs):
    return FakeGmail(labels=FakeLabels(LABELS), **kwargs)


def test_lists_labels_in_a_compact_shape():
    assert list_labels(service_with_labels()) == [
        {"id": "INBOX", "name": "INBOX", "type": "system"},
        {"id": "UNREAD", "name": "UNREAD", "type": "system"},
        {"id": "Label_7", "name": "Clients/Acme", "type": "user"},
    ]


def test_resolves_user_label_names_to_ids():
    assert resolve_label_ids(service_with_labels(), ["Clients/Acme"]) == ["Label_7"]


def test_resolves_system_labels():
    assert resolve_label_ids(service_with_labels(), ["INBOX", "UNREAD"]) == [
        "INBOX",
        "UNREAD",
    ]


def test_label_name_matching_ignores_case():
    assert resolve_label_ids(service_with_labels(), ["clients/acme"]) == ["Label_7"]


def test_unknown_label_lists_the_available_ones():
    with pytest.raises(GmailError) as exc:
        resolve_label_ids(service_with_labels(), ["Nope"])
    message = str(exc.value)
    assert "Nope" in message
    assert "Clients/Acme" in message


def test_empty_name_list_resolves_to_empty():
    assert resolve_label_ids(service_with_labels(), []) == []


def test_modifies_a_message():
    messages = FakeMessages(messages={"m1": {}})
    service = service_with_labels(messages=messages)

    modify_labels(service, message_id="m1", add=["Clients/Acme"], remove=["UNREAD"])

    assert messages.modified == [
        ("m1", {"addLabelIds": ["Label_7"], "removeLabelIds": ["UNREAD"]})
    ]


def test_modifies_a_thread():
    threads = FakeThreads(threads={"t1": {}})
    service = service_with_labels(threads=threads)

    modify_labels(service, thread_id="t1", remove=["INBOX"])

    assert threads.modified == [
        ("t1", {"addLabelIds": [], "removeLabelIds": ["INBOX"]})
    ]


def test_requires_exactly_one_target():
    with pytest.raises(ValueError, match="exactly one"):
        modify_labels(service_with_labels(), add=["INBOX"])

    with pytest.raises(ValueError, match="exactly one"):
        modify_labels(
            service_with_labels(), message_id="m1", thread_id="t1", add=["INBOX"]
        )


def test_requires_at_least_one_change():
    with pytest.raises(ValueError, match="add or remove"):
        modify_labels(service_with_labels(), message_id="m1")


def test_returns_what_changed():
    messages = FakeMessages(messages={"m1": {}})
    result = modify_labels(
        service_with_labels(messages=messages), message_id="m1", remove=["UNREAD"]
    )

    assert result == {
        "target": "message",
        "id": "m1",
        "added": [],
        "removed": ["UNREAD"],
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_labels.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gmail_mcp.gmail.labels'`

- [ ] **Step 3: Write the implementation**

Create `gmail_mcp/gmail/labels.py`:

```python
"""Label listing and modification.

Marking read or unread and archiving are label operations: they add or
remove the UNREAD and INBOX system labels. One tool covers all of it.
"""

from __future__ import annotations

from collections.abc import Sequence

from gmail_mcp.gmail.client import GmailError, execute


def list_labels(service) -> list[dict]:
    """Return every label on the account."""
    raw = execute(service.users().labels().list(userId="me"))
    return [
        {
            "id": label.get("id", ""),
            "name": label.get("name", ""),
            "type": label.get("type", ""),
        }
        for label in raw.get("labels", [])
    ]


def resolve_label_ids(service, names: Sequence[str]) -> list[str]:
    """Map label names to Gmail label IDs, case-insensitively."""
    if not names:
        return []

    labels = list_labels(service)
    by_name = {label["name"].lower(): label["id"] for label in labels}

    resolved = []
    for name in names:
        label_id = by_name.get(name.strip().lower())
        if label_id is None:
            available = ", ".join(sorted(label["name"] for label in labels))
            raise GmailError(
                f"No label named {name!r} on this account. Available: {available}."
            )
        resolved.append(label_id)
    return resolved


def modify_labels(
    service,
    *,
    message_id: str | None = None,
    thread_id: str | None = None,
    add: Sequence[str] = (),
    remove: Sequence[str] = (),
) -> dict:
    """Add and remove labels on exactly one message or thread."""
    if bool(message_id) == bool(thread_id):
        raise ValueError(
            "Pass exactly one of message_id or thread_id."
        )
    if not add and not remove:
        raise ValueError("Pass at least one label to add or remove.")

    body = {
        "addLabelIds": resolve_label_ids(service, list(add)),
        "removeLabelIds": resolve_label_ids(service, list(remove)),
    }

    if message_id:
        execute(
            service.users().messages().modify(
                userId="me", id=message_id, body=body
            )
        )
        target, target_id = "message", message_id
    else:
        execute(
            service.users().threads().modify(
                userId="me", id=thread_id, body=body
            )
        )
        target, target_id = "thread", thread_id

    return {
        "target": target,
        "id": target_id,
        "added": body["addLabelIds"],
        "removed": body["removeLabelIds"],
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_labels.py -v`
Expected: PASS — 11 tests

- [ ] **Step 5: Commit**

```bash
git add gmail_mcp/gmail/labels.py tests/test_labels.py
git commit -m "feat: add label listing and name-based label modification"
```

---

### Task 10: The watermarked inbox check

**Files:**
- Create: `gmail_mcp/check.py`
- Test: `tests/test_check.py`

**Interfaces:**
- Consumes: `execute` from `gmail_mcp.gmail.client`; `fetch_metadata`, `summarise` from `gmail_mcp.gmail.search`; `WatermarkStore` from `gmail_mcp.storage`; `Config` from `gmail_mcp.config`.
- Produces:
  - `DEFAULT_MAX_PER_ACCOUNT = 10`, `FIRST_RUN_LOOKBACK_SECONDS = 86_400`, `SNIPPET_LIMIT = 140`
  - `check_account(service, alias, watermark: int | None, *, max_items, now: int) -> dict`
  - `check_inboxes(config, service_cache, watermarks, *, aliases=None, max_per_account=DEFAULT_MAX_PER_ACCOUNT, now=None) -> dict`

`check_account` returns `alias`, `new_count`, `total_unread`, `items`, `truncated`, `newest` (epoch seconds, or `None`). `check_inboxes` returns `{"accounts": [...], "checked_at": <epoch>}` where failed accounts carry an `error` key instead of counts.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_check.py`:

```python
import pytest

from gmail_mcp.check import (
    FIRST_RUN_LOOKBACK_SECONDS,
    SNIPPET_LIMIT,
    check_account,
    check_inboxes,
)
from gmail_mcp.config import Account, Config
from tests.fakes import FakeGmail, FakeMessages, make_http_error

NOW = 1_700_000_000


def msg(mid, epoch, subject="Hi", sender="a@example.com", snippet="hello"):
    return {
        "id": mid,
        "threadId": f"t-{mid}",
        "snippet": snippet,
        "labelIds": ["INBOX", "UNREAD"],
        "internalDate": str(epoch * 1000),
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": "me@example.com"},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
            ]
        },
    }


def inbox(*messages, total=None):
    listing = {
        "messages": [{"id": m["id"]} for m in messages],
        "resultSizeEstimate": total if total is not None else len(messages),
    }
    return FakeMessages(
        listing=listing, messages={m["id"]: m for m in messages}
    )


def test_first_run_looks_back_one_day():
    messages = inbox(msg("m1", NOW - 60))
    check_account(FakeGmail(messages=messages), "personal", None,
                  max_items=10, now=NOW)

    query = messages.list_calls[0]["q"]
    assert f"after:{NOW - FIRST_RUN_LOOKBACK_SECONDS}" in query
    assert "in:inbox" in query
    assert "is:unread" in query


def test_subsequent_run_uses_the_watermark():
    messages = inbox(msg("m1", NOW - 60))
    check_account(FakeGmail(messages=messages), "personal", NOW - 600,
                  max_items=10, now=NOW)

    assert f"after:{NOW - 600}" in messages.list_calls[0]["q"]


def test_returns_compact_items():
    messages = inbox(msg("m1", NOW - 60, subject="Invoice"))
    result = check_account(FakeGmail(messages=messages), "personal", None,
                           max_items=10, now=NOW)

    assert result["alias"] == "personal"
    assert result["new_count"] == 1
    assert result["items"][0]["subject"] == "Invoice"
    assert result["items"][0]["message_id"] == "m1"
    assert "body" not in result["items"][0]
    assert "labels" not in result["items"][0]


def test_snippets_are_capped():
    messages = inbox(msg("m1", NOW - 60, snippet="x" * 500))
    result = check_account(FakeGmail(messages=messages), "personal", None,
                           max_items=10, now=NOW)

    assert len(result["items"][0]["snippet"]) <= SNIPPET_LIMIT


def test_items_are_capped_and_remainder_reported():
    messages = inbox(*[msg(f"m{i}", NOW - i) for i in range(1, 6)])
    result = check_account(FakeGmail(messages=messages), "personal", None,
                           max_items=2, now=NOW)

    assert len(result["items"]) == 2
    assert result["new_count"] == 5
    assert result["truncated"] == 3


def test_newest_is_the_latest_message_time():
    messages = inbox(msg("m1", NOW - 500), msg("m2", NOW - 100))
    result = check_account(FakeGmail(messages=messages), "personal", None,
                           max_items=10, now=NOW)

    assert result["newest"] == NOW - 100


def test_empty_inbox_reports_no_newest():
    result = check_account(FakeGmail(messages=inbox()), "personal", None,
                           max_items=10, now=NOW)

    assert result["new_count"] == 0
    assert result["items"] == []
    assert result["newest"] is None


def test_sweep_advances_watermark_only_for_successful_accounts():
    config = Config(
        accounts=(
            Account("good", "g@example.com", "send"),
            Account("bad", "b@example.com", "send"),
        )
    )
    good = FakeGmail(messages=inbox(msg("m1", NOW - 100)))

    class Cache:
        def get(self, alias):
            if alias == "bad":
                raise RuntimeError("token revoked, run: gmail-mcp auth add bad")
            return good

    marks = {}

    class Marks:
        def get(self, alias):
            return marks.get(alias)

        def set(self, alias, value):
            marks[alias] = value

    result = check_inboxes(config, Cache(), Marks(), now=NOW)

    by_alias = {a["alias"]: a for a in result["accounts"]}
    assert by_alias["good"]["new_count"] == 1
    assert "revoked" in by_alias["bad"]["error"]
    assert marks == {"good": NOW - 100}


def test_sweep_returns_every_account_even_when_one_fails():
    config = Config(
        accounts=(
            Account("a", "a@example.com", "send"),
            Account("b", "b@example.com", "send"),
            Account("c", "c@example.com", "send"),
        )
    )

    class Cache:
        def get(self, alias):
            if alias == "b":
                raise RuntimeError("boom")
            return FakeGmail(messages=inbox())

    class Marks:
        def get(self, alias):
            return None

        def set(self, alias, value):
            pass

    result = check_inboxes(config, Cache(), Marks(), now=NOW)

    assert [a["alias"] for a in result["accounts"]] == ["a", "b", "c"]


def test_sweep_can_be_limited_to_named_accounts():
    config = Config(
        accounts=(
            Account("a", "a@example.com", "send"),
            Account("b", "b@example.com", "send"),
        )
    )

    class Cache:
        def get(self, alias):
            return FakeGmail(messages=inbox())

    class Marks:
        def get(self, alias):
            return None

        def set(self, alias, value):
            pass

    result = check_inboxes(config, Cache(), Marks(), aliases=["b"], now=NOW)

    assert [a["alias"] for a in result["accounts"]] == ["b"]


def test_sweep_rejects_an_unknown_alias():
    from gmail_mcp.config import UnknownAliasError

    config = Config(accounts=(Account("a", "a@example.com", "send"),))

    class Cache:
        def get(self, alias):
            return FakeGmail()

    class Marks:
        def get(self, alias):
            return None

        def set(self, alias, value):
            pass

    with pytest.raises(UnknownAliasError):
        check_inboxes(config, Cache(), Marks(), aliases=["zzz"], now=NOW)


def test_api_failure_is_reported_as_an_account_error():
    config = Config(accounts=(Account("a", "a@example.com", "send"),))
    broken = FakeMessages(listing={"messages": []})
    broken.list = lambda **kw: (_ for _ in ()).throw(make_http_error(500))

    class Cache:
        def get(self, alias):
            return FakeGmail(messages=broken)

    class Marks:
        def get(self, alias):
            return None

        def set(self, alias, value):
            pass

    result = check_inboxes(config, Cache(), Marks(), now=NOW)

    assert "error" in result["accounts"][0]
    assert "new_count" not in result["accounts"][0]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_check.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gmail_mcp.check'`

- [ ] **Step 3: Write the implementation**

Create `gmail_mcp/check.py`:

```python
"""The scheduled multi-account inbox sweep.

Returns facts, never judgments. Keyword heuristics for "urgent" go stale
and misfire; the model judges from sender and subject with the user's
context in hand.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from gmail_mcp.config import Config
from gmail_mcp.gmail.client import execute
from gmail_mcp.gmail.search import fetch_metadata, summarise

DEFAULT_MAX_PER_ACCOUNT = 10
FIRST_RUN_LOOKBACK_SECONDS = 86_400
SNIPPET_LIMIT = 140
_LIST_CEILING = 100


def _compact(message: dict) -> dict:
    full = summarise(message)
    return {
        "message_id": full["message_id"],
        "thread_id": full["thread_id"],
        "from": full["from"],
        "subject": full["subject"],
        "snippet": full["snippet"][:SNIPPET_LIMIT],
        "received": full["date"],
    }


def _internal_seconds(message: dict) -> int:
    return int(message.get("internalDate", "0")) // 1000


def check_account(
    service,
    alias: str,
    watermark: int | None,
    *,
    max_items: int = DEFAULT_MAX_PER_ACCOUNT,
    now: int,
) -> dict:
    """Summarise one account's new unread inbox mail."""
    since = watermark if watermark is not None else now - FIRST_RUN_LOOKBACK_SECONDS
    query = f"in:inbox is:unread after:{since}"

    listing = execute(
        service.users().messages().list(
            userId="me", q=query, maxResults=_LIST_CEILING,
            includeSpamTrash=False,
        )
    )
    ids = [m["id"] for m in listing.get("messages", [])]

    fetched = fetch_metadata(service, ids[:max_items])
    newest = max((_internal_seconds(m) for m in fetched), default=None)

    return {
        "alias": alias,
        "new_count": len(ids),
        "total_unread": listing.get("resultSizeEstimate", len(ids)),
        "items": [_compact(m) for m in fetched],
        "truncated": max(0, len(ids) - max_items),
        "newest": newest,
    }


def check_inboxes(
    config: Config,
    service_cache,
    watermarks,
    *,
    aliases: list[str] | None = None,
    max_per_account: int = DEFAULT_MAX_PER_ACCOUNT,
    now: int | None = None,
) -> dict:
    """Sweep every configured account, or a named subset.

    Accounts run in parallel; each has its own Gmail service object, so
    no transport is shared across threads. One account's failure is
    reported inline and never fails the sweep.
    """
    now = now if now is not None else int(time.time())

    if aliases is None:
        targets = list(config.accounts)
    else:
        targets = [config.get(alias) for alias in aliases]

    def one(account) -> dict:
        try:
            service = service_cache.get(account.alias)
            result = check_account(
                service,
                account.alias,
                watermarks.get(account.alias),
                max_items=max_per_account,
                now=now,
            )
        except Exception as exc:
            return {"alias": account.alias, "error": str(exc)}

        if result["newest"] is not None:
            watermarks.set(account.alias, result["newest"])
        return result

    if not targets:
        return {"accounts": [], "checked_at": now}

    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        results = list(pool.map(one, targets))

    return {"accounts": results, "checked_at": now}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_check.py -v`
Expected: PASS — 12 tests

- [ ] **Step 5: Commit**

```bash
git add gmail_mcp/check.py tests/test_check.py
git commit -m "feat: add watermarked multi-account inbox check"
```

---

### Task 11: The MCP server and its ten tools

**Files:**
- Create: `gmail_mcp/server.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: everything from Tasks 1 and 3 through 10.
- Produces:
  - `Runtime` class holding `config`, `token_store`, `watermarks`, `services`, with `account(alias) -> Account` and `service(alias)`
  - `build_runtime(config_dir: Path | None = None) -> Runtime`
  - `create_server(runtime: Runtime) -> FastMCP`
  - `main() -> None` starting stdio transport

Tool functions are defined inside `create_server` and closed over `runtime`, so tests construct a server against a fake runtime with no MCP client involved. Each tool is also exposed as an attribute on the returned object under `server.tool_functions[name]` for direct testing.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_server.py`:

```python
import pytest

from gmail_mcp.config import Account, Config
from gmail_mcp.server import Runtime, create_server
from tests.fakes import FakeGmail, FakeLabels, FakeMessages, FakeThreads

LABELS = [
    {"id": "INBOX", "name": "INBOX", "type": "system"},
    {"id": "UNREAD", "name": "UNREAD", "type": "system"},
]


def message(mid="m1", subject="Hi"):
    return {
        "id": mid,
        "threadId": "t-1",
        "snippet": "snip",
        "labelIds": ["INBOX", "UNREAD"],
        "internalDate": "1700000000000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "a@example.com"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
            ],
            "body": {"data": "cGxhaW4gYm9keQ==", "size": 10},
        },
    }


class StubCache:
    def __init__(self, service):
        self.service = service

    def get(self, alias):
        return self.service


class StubMarks:
    def __init__(self):
        self.data = {}

    def get(self, alias):
        return self.data.get(alias)

    def set(self, alias, value):
        self.data[alias] = value


def runtime(service):
    config = Config(
        accounts=(
            Account("personal", "me@example.com", "confirm"),
            Account("work", "w@example.com", "send"),
        )
    )
    return Runtime(
        config=config,
        token_store=None,
        watermarks=StubMarks(),
        services=StubCache(service),
    )


def tools(service):
    return create_server(runtime(service)).tool_functions


def test_list_accounts_exposes_aliases_and_policies():
    result = tools(FakeGmail())["list_accounts"]()

    assert result == [
        {"alias": "personal", "email": "me@example.com", "send_policy": "confirm"},
        {"alias": "work", "email": "w@example.com", "send_policy": "send"},
    ]


def test_search_mail_returns_summaries():
    messages = FakeMessages(
        listing={"messages": [{"id": "m1"}]}, messages={"m1": message()}
    )
    result = tools(FakeGmail(messages=messages))["search_mail"](
        account="personal", query="is:unread"
    )

    assert result[0]["subject"] == "Hi"


def test_unknown_alias_is_a_clear_error_naming_valid_aliases():
    config = Config(accounts=(Account("personal", "me@example.com", "send"),))
    server = create_server(
        Runtime(config=config, token_store=None, watermarks=StubMarks(),
                services=StubCache(FakeGmail()))
    )

    with pytest.raises(Exception) as exc:
        server.tool_functions["search_mail"](account="nope", query="x")

    assert "personal" in str(exc.value)


def test_read_message_returns_the_body():
    messages = FakeMessages(messages={"m1": message()})
    result = tools(FakeGmail(messages=messages))["read_message"](
        account="personal", message_id="m1"
    )

    assert result["body"] == "plain body"


def test_read_thread_returns_ordered_messages():
    threads = FakeThreads(threads={"t-1": {"id": "t-1", "messages": [message()]}})
    result = tools(FakeGmail(threads=threads))["read_thread"](
        account="personal", thread_id="t-1"
    )

    assert len(result["messages"]) == 1


def test_send_message_honours_the_confirm_policy():
    messages = FakeMessages()
    service = FakeGmail(messages=messages)

    result = tools(service)["send_message"](
        account="personal", to="b@example.com", subject="Hi", body="x"
    )

    assert result["action"] == "drafted"
    assert messages.sent == []


def test_send_message_sends_for_a_send_policy_account():
    messages = FakeMessages()
    service = FakeGmail(messages=messages)

    result = tools(service)["send_message"](
        account="work", to="b@example.com", subject="Hi", body="x"
    )

    assert result["action"] == "sent"
    assert len(messages.sent) == 1


def test_create_draft_never_sends_regardless_of_policy():
    messages = FakeMessages()
    service = FakeGmail(messages=messages)

    result = tools(service)["create_draft"](
        account="work", to="b@example.com", subject="Hi", body="x"
    )

    assert result["action"] == "drafted"
    assert messages.sent == []


def test_list_labels_returns_labels():
    service = FakeGmail(labels=FakeLabels(LABELS))
    assert tools(service)["list_labels"](account="personal")[0]["name"] == "INBOX"


def test_modify_labels_marks_a_message_read():
    messages = FakeMessages(messages={"m1": {}})
    service = FakeGmail(messages=messages, labels=FakeLabels(LABELS))

    tools(service)["modify_labels"](
        account="personal", message_id="m1", remove=["UNREAD"]
    )

    assert messages.modified[0][1]["removeLabelIds"] == ["UNREAD"]


def test_check_inboxes_sweeps_all_accounts():
    messages = FakeMessages(listing={"messages": []})
    result = tools(FakeGmail(messages=messages))["check_inboxes"]()

    assert [a["alias"] for a in result["accounts"]] == ["personal", "work"]


def test_all_ten_tools_are_registered():
    assert set(tools(FakeGmail())) == {
        "list_accounts",
        "search_mail",
        "read_message",
        "read_thread",
        "create_draft",
        "send_message",
        "send_draft",
        "list_labels",
        "modify_labels",
        "check_inboxes",
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_server.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gmail_mcp.server'`

- [ ] **Step 3: Write the implementation**

Create `gmail_mcp/server.py`:

```python
"""The MCP tool surface.

Tools validate arguments, delegate, and shape the reply. No Gmail API
logic lives here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from gmail_mcp.check import DEFAULT_MAX_PER_ACCOUNT, check_inboxes
from gmail_mcp.config import Account, Config, config_dir, load_config
from gmail_mcp.gmail import labels as labels_module
from gmail_mcp.gmail.client import ServiceCache
from gmail_mcp.gmail.compose import compose_and_deliver, send_existing_draft
from gmail_mcp.gmail.messages import read_message as _read_message
from gmail_mcp.gmail.messages import read_thread as _read_thread
from gmail_mcp.gmail.search import DEFAULT_MAX_RESULTS, search_messages
from gmail_mcp.storage import TokenStore, WatermarkStore

Recipients = str | list[str]


@dataclass
class Runtime:
    config: Config
    token_store: object
    watermarks: object
    services: object

    def account(self, alias: str) -> Account:
        return self.config.get(alias)

    def service(self, alias: str):
        return self.services.get(alias)


def build_runtime(directory: Path | None = None) -> Runtime:
    directory = directory or config_dir()
    config = load_config(directory / "config.toml")
    tokens = TokenStore(directory)
    return Runtime(
        config=config,
        token_store=tokens,
        watermarks=WatermarkStore(directory),
        services=ServiceCache(config, tokens),
    )


def create_server(runtime: Runtime) -> FastMCP:
    server = FastMCP("gmail-mcp")

    def list_accounts() -> list[dict]:
        """List every configured mailbox, its address, and its send policy.

        Call this first to learn the valid `account` aliases.
        """
        return [
            {
                "alias": a.alias,
                "email": a.email,
                "send_policy": a.send_policy,
            }
            for a in runtime.config.accounts
        ]

    def search_mail(
        account: str,
        query: str,
        max_results: int = DEFAULT_MAX_RESULTS,
        include_spam_trash: bool = False,
    ) -> list[dict]:
        """Search one mailbox using Gmail query syntax.

        `query` accepts the same operators as the Gmail search box, for
        example `is:unread from:boss@example.com newer_than:2d`. Prefer a
        precise query over fetching broadly and filtering afterwards.
        Message IDs returned here are only valid for this account.
        """
        runtime.account(account)
        return search_messages(
            runtime.service(account),
            query,
            max_results=max_results,
            include_spam_trash=include_spam_trash,
        )

    def read_message(account: str, message_id: str) -> dict:
        """Read one message in full, including its text body.

        Attachment names and sizes are reported; contents are not
        downloaded.
        """
        runtime.account(account)
        return _read_message(runtime.service(account), message_id)

    def read_thread(account: str, thread_id: str) -> dict:
        """Read a whole conversation, oldest first, quoted text removed."""
        runtime.account(account)
        return _read_thread(runtime.service(account), thread_id)

    def create_draft(
        account: str,
        to: Recipients,
        body: str,
        subject: str | None = None,
        cc: Recipients | None = None,
        bcc: Recipients | None = None,
        reply_to_message_id: str | None = None,
    ) -> dict:
        """Save a draft. Never sends, whatever the account policy.

        Pass `reply_to_message_id` to reply in-thread; the subject is
        derived from the original when omitted.
        """
        target = runtime.account(account)
        return compose_and_deliver(
            runtime.service(account),
            target,
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            reply_to_message_id=reply_to_message_id,
            force_draft=True,
        )

    def send_message(
        account: str,
        to: Recipients,
        body: str,
        subject: str | None = None,
        cc: Recipients | None = None,
        bcc: Recipients | None = None,
        reply_to_message_id: str | None = None,
        confirm: bool = False,
    ) -> dict:
        """Send a message, subject to the account's send policy.

        A `confirm` account requires `confirm=true` to transmit and saves
        a draft otherwise. A `draft_only` account never transmits. Check
        the `action` field of the result to see what actually happened.
        """
        target = runtime.account(account)
        return compose_and_deliver(
            runtime.service(account),
            target,
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            reply_to_message_id=reply_to_message_id,
            confirm=confirm,
        )

    def send_draft(account: str, draft_id: str, confirm: bool = False) -> dict:
        """Send an existing draft, subject to the same send policy."""
        target = runtime.account(account)
        return send_existing_draft(
            runtime.service(account), target, draft_id, confirm=confirm
        )

    def list_labels(account: str) -> list[dict]:
        """List every label on one mailbox."""
        runtime.account(account)
        return labels_module.list_labels(runtime.service(account))

    def modify_labels(
        account: str,
        message_id: str | None = None,
        thread_id: str | None = None,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> dict:
        """Add and remove labels by name on one message or thread.

        Use this to mark read or unread (the `UNREAD` label) and to
        archive (remove `INBOX`). Pass exactly one of `message_id` or
        `thread_id`.
        """
        runtime.account(account)
        return labels_module.modify_labels(
            runtime.service(account),
            message_id=message_id,
            thread_id=thread_id,
            add=add or [],
            remove=remove or [],
        )

    def check_inboxes_tool(
        accounts: list[str] | None = None,
        max_per_account: int = DEFAULT_MAX_PER_ACCOUNT,
    ) -> dict:
        """Summarise new unread inbox mail across mailboxes.

        Designed for scheduled use: returns counts plus a few compact
        items per account, never message bodies. Only mail arriving since
        the previous call is reported, so repeated runs do not repeat
        themselves. Accounts that fail are reported inline with an
        `error` field while the rest still return.
        """
        return check_inboxes(
            runtime.config,
            runtime.services,
            runtime.watermarks,
            aliases=accounts,
            max_per_account=max_per_account,
        )

    functions = {
        "list_accounts": list_accounts,
        "search_mail": search_mail,
        "read_message": read_message,
        "read_thread": read_thread,
        "create_draft": create_draft,
        "send_message": send_message,
        "send_draft": send_draft,
        "list_labels": list_labels,
        "modify_labels": modify_labels,
        "check_inboxes": check_inboxes_tool,
    }

    for name, function in functions.items():
        server.add_tool(function, name=name)

    server.tool_functions = functions
    return server


def main() -> None:
    create_server(build_runtime()).run(transport="stdio")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_server.py -v`
Expected: PASS — 12 tests

If `server.add_tool` rejects the `name` keyword on the installed `mcp` version, check the signature with `uv run python -c "from mcp.server.fastmcp import FastMCP; help(FastMCP.add_tool)"` and adapt the registration loop. Do not change the tool names or the `tool_functions` mapping.

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add gmail_mcp/server.py tests/test_server.py
git commit -m "feat: add MCP server exposing the ten Gmail tools"
```

---

### Task 12: Setup CLI, doctor, integration test, and documentation

**Files:**
- Create: `gmail_mcp/cli.py`
- Create: `tests/test_cli.py`
- Create: `tests/test_integration_oauth.py`
- Create: `README.md`
- Create: `docs/SETUP.md`
- Create: `config.example.toml`

**Interfaces:**
- Consumes: everything from Tasks 1 through 11.
- Produces:
  - `lookup_profile_email(credentials) -> str`
  - `cmd_auth_add(directory, alias, client_secret) -> int`
  - `cmd_auth_list(directory) -> int`
  - `cmd_auth_remove(directory, alias) -> int`
  - `cmd_doctor(directory) -> int`
  - `main(argv: list[str] | None = None) -> int`

All command functions return a process exit code and print to stdout, so they are testable with `capsys`.

- [ ] **Step 1: Write the failing CLI tests**

Create `tests/test_cli.py`:

```python
import json

import pytest

from gmail_mcp.cli import cmd_auth_list, cmd_auth_remove, cmd_doctor, main

CONFIG = """
[[accounts]]
alias = "personal"
email = "me@example.com"
send_policy = "confirm"

[[accounts]]
alias = "work"
email = "w@example.com"
send_policy = "send"
"""


@pytest.fixture
def home(tmp_path):
    (tmp_path / "config.toml").write_text(CONFIG)
    return tmp_path


def test_auth_list_marks_authenticated_accounts(home, capsys):
    (home / "tokens.json").write_text(json.dumps({"personal": "{}"}))

    code = cmd_auth_list(home, keyring_module=_BrokenKeyring())
    out = capsys.readouterr().out

    assert code == 0
    assert "personal" in out
    assert "work" in out
    assert "not authenticated" in out


def test_auth_list_reports_a_missing_config(tmp_path, capsys):
    code = cmd_auth_list(tmp_path, keyring_module=_BrokenKeyring())

    assert code == 1
    assert "config.toml" in capsys.readouterr().out


def test_auth_remove_deletes_the_token(home, capsys):
    (home / "tokens.json").write_text(json.dumps({"personal": "{}"}))

    code = cmd_auth_remove(home, "personal", keyring_module=_BrokenKeyring())
    out = capsys.readouterr().out

    assert code == 0
    assert json.loads((home / "tokens.json").read_text()) == {}
    assert "personal" in out


def test_auth_remove_rejects_an_unknown_alias(home, capsys):
    code = cmd_auth_remove(home, "nope", keyring_module=_BrokenKeyring())

    assert code == 1
    assert "personal" in capsys.readouterr().out


def test_doctor_reports_every_account_and_the_backend(home, capsys):
    addresses = {"personal": "me@example.com", "work": "w@example.com"}

    def profile(alias, store, directory):
        return addresses[alias]

    code = cmd_doctor(home, probe=profile, keyring_module=_BrokenKeyring())
    out = capsys.readouterr().out

    assert "personal" in out
    assert "work" in out
    assert "file:" in out
    assert code == 0


def test_doctor_flags_a_broken_account_without_failing_the_others(home, capsys):
    def probe(alias, store, directory):
        if alias == "work":
            raise RuntimeError("token revoked, run: gmail-mcp auth add work")
        return "me@example.com"

    code = cmd_doctor(home, probe=probe, keyring_module=_BrokenKeyring())
    out = capsys.readouterr().out

    assert code == 1
    assert "revoked" in out
    assert "personal" in out


def test_doctor_flags_a_mailbox_that_does_not_match_config(home, capsys):
    def probe(alias, store, directory):
        return "stranger@example.com"

    code = cmd_doctor(home, probe=probe, keyring_module=_BrokenKeyring())
    out = capsys.readouterr().out

    assert code == 1
    assert "stranger@example.com" in out


def test_main_rejects_an_unknown_command(capsys):
    with pytest.raises(SystemExit):
        main(["nonsense"])


class _BrokenKeyring:
    def get_password(self, service, username):
        raise RuntimeError("no backend")

    def set_password(self, service, username, password):
        raise RuntimeError("no backend")

    def delete_password(self, service, username):
        raise RuntimeError("no backend")

    def get_keyring(self):
        return self
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gmail_mcp.cli'`

- [ ] **Step 3: Write the CLI implementation**

Create `gmail_mcp/cli.py`:

```python
"""Setup CLI: authenticate accounts, inspect health, run the server."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import keyring as _keyring
from googleapiclient.discovery import build

from gmail_mcp.auth import authenticate_account, load_credentials
from gmail_mcp.config import ConfigError, config_dir, load_config
from gmail_mcp.storage import TokenStore


def lookup_profile_email(credentials) -> str:
    """Read the authenticated mailbox address."""
    service = build(
        "gmail", "v1", credentials=credentials, cache_discovery=False
    )
    return service.users().getProfile(userId="me").execute()["emailAddress"]


def _load(directory: Path):
    return load_config(directory / "config.toml")


def cmd_auth_add(
    directory: Path,
    alias: str,
    client_secret: Path,
    *,
    keyring_module=_keyring,
    profile_lookup=lookup_profile_email,
) -> int:
    try:
        config = _load(directory)
        account = config.get(alias)
    except ConfigError as exc:
        print(f"error: {exc}")
        return 1

    if not client_secret.exists():
        print(f"error: no OAuth client file at {client_secret}")
        return 1

    store = TokenStore(directory, keyring_module=keyring_module)
    print(f"Opening a browser to authenticate {account.email} as {alias!r}.")
    print("Sign in as that exact account. Use a private window if unsure.")

    try:
        email = authenticate_account(
            account, client_secret, store, profile_lookup
        )
    except Exception as exc:
        print(f"error: {exc}")
        return 1

    print(f"ok: {alias} authenticated as {email}")
    print(f"    tokens stored in {store.backend_name}")
    return 0


def cmd_auth_list(directory: Path, *, keyring_module=_keyring) -> int:
    try:
        config = _load(directory)
    except ConfigError as exc:
        print(f"error: {exc}")
        return 1

    store = TokenStore(directory, keyring_module=keyring_module)
    print(f"storage: {store.backend_name}\n")

    for account in config.accounts:
        state = (
            "authenticated"
            if store.get(account.alias)
            else "not authenticated"
        )
        print(
            f"  {account.alias:<14} {account.email:<28} "
            f"{account.send_policy:<11} {state}"
        )
    return 0


def cmd_auth_remove(directory: Path, alias: str, *, keyring_module=_keyring) -> int:
    try:
        config = _load(directory)
        config.get(alias)
    except ConfigError as exc:
        print(f"error: {exc}")
        return 1

    TokenStore(directory, keyring_module=keyring_module).delete(alias)
    print(f"ok: removed stored token for {alias!r}")
    return 0


def _default_probe(alias: str, store, directory: Path) -> str:
    credentials = load_credentials(alias, store)
    return lookup_profile_email(credentials)


def cmd_doctor(
    directory: Path, *, probe=_default_probe, keyring_module=_keyring
) -> int:
    """Check config, credentials, and Gmail reachability for every account."""
    try:
        config = _load(directory)
    except ConfigError as exc:
        print(f"error: {exc}")
        return 1

    store = TokenStore(directory, keyring_module=keyring_module)
    print(f"config:  {directory / 'config.toml'}")
    print(f"storage: {store.backend_name}\n")

    failures = 0
    for account in config.accounts:
        try:
            actual = probe(account.alias, store, directory)
        except Exception as exc:
            failures += 1
            print(f"  FAIL {account.alias:<14} {exc}")
            continue

        if actual.strip().lower() != account.email.strip().lower():
            failures += 1
            print(
                f"  FAIL {account.alias:<14} configured as {account.email} "
                f"but authenticated as {actual}. "
                f"Re-run: gmail-mcp auth add {account.alias}"
            )
            continue

        print(f"  ok   {account.alias:<14} {actual}")

    print()
    if failures:
        print(f"{failures} of {len(config.accounts)} account(s) need attention.")
        return 1

    print(f"All {len(config.accounts)} account(s) healthy.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gmail-mcp")
    parser.add_argument(
        "--config-dir", type=Path, default=None,
        help="override the configuration directory",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the MCP server on stdio")
    sub.add_parser("doctor", help="check every account's health")

    auth = sub.add_parser("auth", help="manage account authentication")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)

    add = auth_sub.add_parser("add", help="authenticate one account")
    add.add_argument("alias")
    add.add_argument(
        "--client-secret", type=Path, default=None,
        help="path to the OAuth client JSON "
             "(default: <config-dir>/client_secret.json)",
    )

    auth_sub.add_parser("list", help="show authentication status")
    remove = auth_sub.add_parser("remove", help="forget one account's token")
    remove.add_argument("alias")

    args = parser.parse_args(argv)
    directory = args.config_dir or config_dir()

    if args.command == "serve":
        from gmail_mcp.server import main as serve_main

        serve_main()
        return 0

    if args.command == "doctor":
        return cmd_doctor(directory)

    if args.auth_command == "add":
        secret = args.client_secret or directory / "client_secret.json"
        return cmd_auth_add(directory, args.alias, secret)
    if args.auth_command == "list":
        return cmd_auth_list(directory)
    return cmd_auth_remove(directory, args.alias)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS — 8 tests

- [ ] **Step 5: Write the opt-in integration test**

Create `tests/test_integration_oauth.py`:

```python
"""Opt-in end-to-end check against a real mailbox.

Runs only when GMAIL_MCP_INTEGRATION=1. Requires a config directory with
config.toml, client_secret.json, and at least one already-authenticated
account:

    GMAIL_MCP_INTEGRATION=1 \
    GMAIL_MCP_CONFIG_DIR=~/.config/gmail-mcp \
    GMAIL_MCP_ALIAS=personal \
    uv run pytest tests/test_integration_oauth.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GMAIL_MCP_INTEGRATION") != "1",
    reason="set GMAIL_MCP_INTEGRATION=1 to run against a real mailbox",
)


@pytest.fixture
def directory() -> Path:
    raw = os.environ.get("GMAIL_MCP_CONFIG_DIR")
    if not raw:
        pytest.fail("GMAIL_MCP_CONFIG_DIR is required for integration tests")
    return Path(raw).expanduser()


@pytest.fixture
def alias() -> str:
    return os.environ.get("GMAIL_MCP_ALIAS", "personal")


def test_stored_credentials_reach_the_right_mailbox(directory, alias):
    from gmail_mcp.auth import load_credentials
    from gmail_mcp.cli import lookup_profile_email
    from gmail_mcp.config import load_config
    from gmail_mcp.storage import TokenStore

    config = load_config(directory / "config.toml")
    account = config.get(alias)

    credentials = load_credentials(alias, TokenStore(directory))
    assert lookup_profile_email(credentials).lower() == account.email.lower()


def test_search_returns_results_from_the_live_account(directory, alias):
    from gmail_mcp.server import build_runtime

    runtime = build_runtime(directory)
    from gmail_mcp.gmail.search import search_messages

    results = search_messages(
        runtime.service(alias), "in:inbox", max_results=3
    )
    assert isinstance(results, list)
    for item in results:
        assert item["message_id"]
        assert "subject" in item


def test_check_inboxes_sweeps_the_live_configuration(directory):
    from gmail_mcp.check import check_inboxes
    from gmail_mcp.server import build_runtime

    runtime = build_runtime(directory)
    result = check_inboxes(
        runtime.config, runtime.services, runtime.watermarks
    )

    assert len(result["accounts"]) == len(runtime.config.accounts)
```

- [ ] **Step 6: Verify the integration test is skipped by default**

Run: `uv run pytest tests/test_integration_oauth.py -v`
Expected: 3 skipped

- [ ] **Step 7: Write the example config and documentation**

Create `config.example.toml`:

```toml
# Copy to your config directory as config.toml.
#   Linux:   ~/.config/gmail-mcp/config.toml
#   macOS:   ~/Library/Application Support/gmail-mcp/config.toml
#   Windows: %LOCALAPPDATA%\gmail-mcp\config.toml
#
# send_policy must be one of:
#   send        transmit immediately when asked
#   confirm     save a draft unless the call passes confirm=true
#   draft_only  never transmit; always save a draft

[[accounts]]
alias       = "personal"
email       = "you@yourdomain.com"
send_policy = "confirm"

[[accounts]]
alias       = "work-main"
email       = "main@yourdomain.com"
send_policy = "confirm"

[[accounts]]
alias       = "work-sales"
email       = "sales@yourdomain.com"
send_policy = "draft_only"

[[accounts]]
alias       = "work-support"
email       = "support@yourdomain.com"
send_policy = "draft_only"

[[accounts]]
alias       = "work-billing"
email       = "billing@yourdomain.com"
send_policy = "draft_only"
```

Create `docs/SETUP.md`:

````markdown
# Setup

## 1. Google Cloud project

One project serves all five accounts.

1. Create a project at <https://console.cloud.google.com/>.
2. **APIs & Services → Library →** enable **Gmail API**.
3. **APIs & Services → OAuth consent screen →** choose **Internal**.
   Internal is available because every mailbox is on one Workspace
   domain. It needs no Google verification, shows no "unverified app"
   warning, and — unlike an External screen in Testing status — does not
   expire refresh tokens after seven days.
4. Add the scope `https://www.googleapis.com/auth/gmail.modify`. Add
   nothing else.
5. **Credentials → Create credentials → OAuth client ID →
   Application type: Desktop app.**
6. Download the JSON and save it as `client_secret.json` in your config
   directory (see below).

## 2. Install

```bash
git clone <repo> && cd local_mcp_gmail
uv sync
```

## 3. Configure

Copy `config.example.toml` to your config directory as `config.toml` and
edit the aliases, addresses, and send policies:

| Platform | Config directory |
| --- | --- |
| Linux | `~/.config/gmail-mcp/` |
| macOS | `~/Library/Application Support/gmail-mcp/` |
| Windows | `%LOCALAPPDATA%\gmail-mcp\` |

`send_policy` is required on every account and must be `send`,
`confirm`, or `draft_only`. There is no default.

## 4. Authenticate each account

Run once per alias:

```bash
uv run gmail-mcp auth add personal
uv run gmail-mcp auth add work-main
uv run gmail-mcp auth add work-sales
uv run gmail-mcp auth add work-support
uv run gmail-mcp auth add work-billing
```

Each opens a browser. **Sign in as the exact address configured for that
alias.** Google reuses whichever session is already signed in, so when
authenticating several accounts in a row, either sign out between them or
use a private window. If the wrong mailbox is authenticated the command
refuses to save anything and tells you which address it saw.

Check the result:

```bash
uv run gmail-mcp auth list
uv run gmail-mcp doctor
```

`doctor` verifies every account's token refreshes and reaches the
intended mailbox. All five should report `ok`.

## 5. Connect Claude Desktop

Add to `claude_desktop_config.json`:

| Platform | Path |
| --- | --- |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "gmail": {
      "command": "/absolute/path/to/uv",
      "args": ["--directory", "/absolute/path/to/local_mcp_gmail",
               "run", "gmail-mcp", "serve"]
    }
  }
}
```

Use absolute paths for both. Claude Desktop does not inherit a login
shell's `PATH`, and a bare command name is the usual reason a server
silently fails to start. Find your `uv` with `which uv`.

Restart Claude Desktop, then ask it to list your accounts.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `has not been authenticated` | Run `gmail-mcp auth add <alias>`. |
| `was revoked or expired` | The password changed or access was removed. Run `gmail-mcp auth add <alias>` again. |
| `authenticated <other address>` | The browser was signed in as the wrong account. Sign out or use a private window and retry. |
| Server missing in Claude Desktop | Non-absolute paths in the config block. Check `which uv`. |
| `Not found in this mailbox` | The message ID came from a different account. Gmail IDs are account-scoped. |
| `storage: file:` in `doctor` | No OS keyring available. Tokens are in a `0600` file; expected on headless Linux. |
````

Create `README.md`:

````markdown
# gmail-mcp

A local MCP server that gives Claude Desktop access to several Gmail
mailboxes at once — searching, reading, drafting, sending, labelling, and
a scheduled inbox summary — without switching accounts.

Runs entirely on your machine over stdio. Nothing is hosted, and no mail
leaves your machine except to Google.

## Tools

| Tool | Purpose |
| --- | --- |
| `list_accounts` | Aliases, addresses, and send policies |
| `search_mail` | Gmail query syntax against one account |
| `read_message` | One message with its body |
| `read_thread` | A whole conversation, quoted text removed |
| `create_draft` | Save a draft, never send |
| `send_message` | Send, subject to the account's send policy |
| `send_draft` | Send an existing draft |
| `list_labels` | Labels on one account |
| `modify_labels` | Add and remove labels; also marks read and archives |
| `check_inboxes` | New unread mail across all accounts, for scheduled use |

Every mail tool takes an `account` alias, so the mailbox is always
explicit.

## Send safety

Each mailbox declares a `send_policy` in config, enforced in code rather
than in prompt text:

- `send` — transmits when asked.
- `confirm` — saves a draft unless the call explicitly confirms.
- `draft_only` — never transmits.

This matters because mail is untrusted input: a message can contain text
crafted to steer the model, and `check_inboxes` reads mail from the open
internet on a schedule. Configuration is the control on that, which is
why it is not a prompt instruction.

The server requests exactly one OAuth scope, `gmail.modify`, which cannot
permanently delete mail.

## Setup

See [docs/SETUP.md](docs/SETUP.md).

## Development

```bash
uv sync
uv run pytest
```

The suite runs entirely offline against a fake Gmail API. The OAuth
integration test is opt-in:

```bash
GMAIL_MCP_INTEGRATION=1 GMAIL_MCP_CONFIG_DIR=~/.config/gmail-mcp \
  uv run pytest tests/test_integration_oauth.py -v
```
````

- [ ] **Step 8: Run the whole suite**

Run: `uv run pytest -v`
Expected: PASS, with 3 integration tests skipped

- [ ] **Step 9: Verify the CLI is wired end to end**

Run:

```bash
uv run gmail-mcp --help
uv run gmail-mcp auth --help
```

Expected: both print usage without traceback.

- [ ] **Step 10: Commit**

```bash
git add gmail_mcp/cli.py tests/test_cli.py tests/test_integration_oauth.py \
        README.md docs/SETUP.md config.example.toml
git commit -m "feat: add setup CLI, doctor, integration test, and docs"
```

---

## Manual verification against the spec's success criteria

After Task 12, walk these with a real Google Cloud project and at least two
accounts. Criteria 1 through 5 are milestone 1 of the brief; the rest close
milestone 2.

- [ ] Each account authenticates through one browser login, and deliberately
      signing in as the wrong mailbox is rejected without saving a token.
- [ ] `search_mail`, `read_message`, `read_thread`, `create_draft`,
      `send_message`, `list_labels`, and `modify_labels` each work against at
      least two different aliases.
- [ ] A reply sent with `reply_to_message_id` appears in-thread in a
      non-Gmail client (Apple Mail or Outlook — Gmail groups by thread ID and
      will look correct even when the headers are wrong).
- [ ] All three send policies behave correctly against live accounts.
- [ ] `check_inboxes` returns a full sweep in a few seconds, and a second
      call moments later reports no repeats.
- [ ] Revoking one account's access at
      <https://myaccount.google.com/permissions> leaves the other accounts
      working, and `doctor` names the broken one.
- [ ] Restarting Claude Desktop requires no re-authentication.
