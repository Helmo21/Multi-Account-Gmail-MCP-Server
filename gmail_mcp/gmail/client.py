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
