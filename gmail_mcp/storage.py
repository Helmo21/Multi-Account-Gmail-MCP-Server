"""Persistence for OAuth tokens and per-account check watermarks."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import keyring as _keyring

SERVICE_NAME = "gmail-mcp"
_TOKEN_FILE = "tokens.json"
_WATERMARK_FILE = "watermarks.json"

# Guards the file-backend read-modify-write in TokenStore and
# WatermarkStore. Both stage their update through a single fixed temp
# path (`tokens.json.tmp` / `watermarks.json.tmp`) per store, so two
# threads racing through it concurrently -- e.g. several accounts'
# access tokens expiring in the same hourly sweep -- can otherwise lose
# an update or hit a FileNotFoundError from a rename collision. This is
# single-process scope only: it does not coordinate across processes.
_LOCK = threading.Lock()


class StorageError(Exception):
    """Raised when the chosen token storage backend becomes unavailable."""

    pass


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
    # Create the temp file already private (0600), rather than at the
    # default umask and chmod-ing afterward: the latter leaves a window
    # where the file exists world-readable with token JSON already
    # written into it.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2))
    tmp.replace(path)


class TokenStore:
    """Stores opaque token JSON in the OS keyring, or a 0600 file.

    Backend is chosen once at construction and never changes. If the probe
    succeeds, the keyring is the backend. If it fails, the file is the backend.

    The fallback is deliberately not encrypted: the server starts
    unattended and can never prompt for a passphrase, so any key would
    have to sit beside the ciphertext.
    """

    def __init__(self, config_dir: Path, keyring_module=_keyring):
        self._dir = Path(config_dir)
        self._keyring = keyring_module
        self._uses_keyring = self._probe()

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
        if self._uses_keyring:
            return f"keyring:{self._keyring.get_keyring()}"
        return f"file:{self._file}"

    def get(self, alias: str) -> str | None:
        if self._uses_keyring:
            try:
                return self._keyring.get_password(SERVICE_NAME, alias)
            except Exception as exc:
                raise StorageError(
                    f"Keyring backend unavailable for alias '{alias}': {exc}"
                ) from exc
        return _read_json(self._file).get(alias)

    def set(self, alias: str, token_json: str) -> None:
        if self._uses_keyring:
            try:
                self._keyring.set_password(SERVICE_NAME, alias, token_json)
                return
            except Exception as exc:
                raise StorageError(
                    f"Keyring backend unavailable for alias '{alias}': {exc}"
                ) from exc
        with _LOCK:
            data = _read_json(self._file)
            data[alias] = token_json
            _write_json_private(self._file, data)

    def delete(self, alias: str) -> None:
        if self._uses_keyring:
            try:
                self._keyring.delete_password(SERVICE_NAME, alias)
                return
            except Exception as exc:
                raise StorageError(
                    f"Keyring backend unavailable for alias '{alias}': {exc}"
                ) from exc
        with _LOCK:
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
        with _LOCK:
            data = _read_json(self._path)
            data[alias] = int(epoch_seconds)
            _write_json_private(self._path, data)
