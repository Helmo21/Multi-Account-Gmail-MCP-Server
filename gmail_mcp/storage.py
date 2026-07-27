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
