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
    reported inline and never fails the sweep, and its watermark is left
    untouched so nothing it couldn't report is skipped on the next run.

    The same "never skip unreported mail" rule applies when an account's
    new mail exceeds ``max_per_account``: the watermark only advances to
    the newest message once everything new has actually been reported
    (``truncated == 0``). If the very first check for an account is
    truncated, the lookback boundary is still recorded (rather than left
    unset) so the next run doesn't let the 24h lookback window silently
    slide forward and drop the unreported backlog.
    """
    now = now if now is not None else int(time.time())

    if aliases is None:
        targets = list(config.accounts)
    else:
        targets = [config.get(alias) for alias in aliases]

    def one(account) -> dict:
        try:
            service = service_cache.get(account.alias)
            watermark = watermarks.get(account.alias)
            result = check_account(
                service,
                account.alias,
                watermark,
                max_items=max_per_account,
                now=now,
            )
        except Exception as exc:
            return {"alias": account.alias, "error": str(exc)}

        if result["truncated"] == 0:
            if result["newest"] is not None:
                watermarks.set(account.alias, result["newest"])
        elif watermark is None:
            watermarks.set(account.alias, now - FIRST_RUN_LOOKBACK_SECONDS)
        return result

    if not targets:
        return {"accounts": [], "checked_at": now}

    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        results = list(pool.map(one, targets))

    return {"accounts": results, "checked_at": now}
