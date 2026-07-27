"""Setup CLI: authenticate accounts, inspect health, run the server."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import keyring as _keyring
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from gmail_mcp.auth import authenticate_account, load_credentials
from gmail_mcp.config import ConfigError, config_dir, load_config
from gmail_mcp.storage import TokenStore


class KeyringLike(Protocol):
    """Duck type for the `keyring` module and its test doubles.

    Matches both the real `keyring` module and fakes like the tests'
    `_BrokenKeyring`, which are plain objects rather than modules.
    """

    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...
    def get_keyring(self) -> object: ...


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
    keyring_module: KeyringLike = _keyring,
    profile_lookup: Callable[[Credentials], str] = lookup_profile_email,
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


def cmd_auth_list(directory: Path, *, keyring_module: KeyringLike = _keyring) -> int:
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


def cmd_auth_remove(
    directory: Path, alias: str, *, keyring_module: KeyringLike = _keyring
) -> int:
    try:
        config = _load(directory)
        config.get(alias)
    except ConfigError as exc:
        print(f"error: {exc}")
        return 1

    TokenStore(directory, keyring_module=keyring_module).delete(alias)
    print(f"ok: removed stored token for {alias!r}")
    return 0


def _default_probe(alias: str, store: TokenStore, directory: Path) -> str:
    credentials = load_credentials(alias, store)
    return lookup_profile_email(credentials)


def cmd_doctor(
    directory: Path,
    *,
    probe: Callable[[str, TokenStore, Path], str] = _default_probe,
    keyring_module: KeyringLike = _keyring,
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


def main(
    argv: list[str] | None = None,
    *,
    serve_entrypoint: Callable[[Path], None] | None = None,
) -> int:
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
        if serve_entrypoint is None:
            from gmail_mcp.server import main as serve_entrypoint

        serve_entrypoint(directory)
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
