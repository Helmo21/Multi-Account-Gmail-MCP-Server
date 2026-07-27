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
