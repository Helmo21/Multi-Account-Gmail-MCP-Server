"""The scheduled multi-account inbox sweep.

Returns facts, never judgments. Keyword heuristics for "urgent" go stale
and misfire; the model judges from sender and subject with the user's
context in hand.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from gmail_mcp.config import Config
from gmail_mcp.gmail.client import execute
from gmail_mcp.gmail.search import fetch_metadata, summarise

DEFAULT_MAX_PER_ACCOUNT = 10
FIRST_RUN_LOOKBACK_SECONDS = 86_400
SNIPPET_LIMIT = 140

# Gmail's messages.list is fetched as a single page; there is no
# nextPageToken pagination here. new_count and truncated are therefore
# both bounded by this ceiling: if an account has more matching messages
# than this, they are silently absent from both numbers, not just from
# `items`. total_unread does not share this limit -- see
# _inbox_unread_total, which comes from a separate, unpaginated count.
_LIST_CEILING = 100


def _iso(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _internal_seconds(message: dict) -> int:
    return int(message.get("internalDate", "0")) // 1000


def _compact(message: dict) -> dict:
    full = summarise(message)
    return {
        "message_id": full["message_id"],
        "thread_id": full["thread_id"],
        "from": full["from"],
        "subject": full["subject"],
        "snippet": full["snippet"][:SNIPPET_LIMIT],
        # Derived from internalDate (Gmail's own receipt clock), not the
        # RFC 2822 Date header summarise() also carries: that header is
        # sender-controlled, sometimes absent, and a different clock
        # than the one `newest`/the watermark are measured in.
        "received": _iso(_internal_seconds(message)),
    }


def _inbox_unread_total(service, *, fallback: int) -> int:
    """Best-effort true unread count for the whole inbox.

    This is a supplementary call: if it fails for any reason (revoked
    token, rate limit, label missing on this account), the sweep must
    not fail because of it -- fall back to the caller's estimate.
    """
    try:
        label = execute(service.users().labels().get(userId="me", id="INBOX"))
    except Exception:
        return fallback
    return label.get("messagesUnread", fallback)


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

    total_unread = _inbox_unread_total(
        service, fallback=listing.get("resultSizeEstimate", len(ids))
    )

    return {
        "alias": alias,
        "new_count": len(ids),
        "total_unread": total_unread,
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

    Accounts run in parallel, capped at 8 concurrent workers; each has
    its own Gmail service object, so no transport is shared across
    threads. One account's failure is reported inline and never fails
    the sweep.

    Watermark persistence happens only after every account has finished,
    serially, in this thread -- never from inside the pool.
    `WatermarkStore.set` is an unsynchronized read-modify-write over a
    single shared file, so calling it concurrently would race: two
    threads could both read the same on-disk state and one's write
    would silently clobber the other's, or one thread's temp-file rename
    could pull the file out from under another thread's write, raising
    and aborting the whole sweep -- discarding every account's results,
    not just the one that collided. Collecting a
    ``(result, watermark_to_write)`` pair per account and applying the
    writes after ``pool.map`` has fully drained avoids both failure
    modes without needing a lock.

    A watermark only advances to the newest message once everything new
    has actually been reported (``truncated == 0``). Whenever a first
    run (watermark was ``None``) does not produce a usable watermark
    this way -- because the account errored, its inbox was empty, or its
    new mail was truncated -- the 24h lookback boundary is persisted
    anyway, rather than left unset. Otherwise the next run would
    recompute ``now - FIRST_RUN_LOOKBACK_SECONDS`` from a later clock and
    let the window silently slide forward, excluding mail that fell
    inside the first run's window but has since aged out of a fresh one.
    Once a watermark already exists, a failed or truncated run simply
    leaves it untouched -- it is already anchored, so there is nothing to
    freeze.

    Sustained truncation (new mail chronically outnumbering
    ``max_per_account``) is not self-resolving: the watermark pins at
    that frozen boundary indefinitely, the same newest
    ``max_per_account`` items are re-reported unchanged on every sweep,
    and the older already-new mail sitting behind them is never
    surfaced on its own until the backlog ahead of it clears (marked
    read, moved out of the inbox, or the account catches up). That mail
    is not lost -- raising ``max_per_account`` recovers it immediately --
    but it will not appear unprompted while the backlog persists.

    Separately, the ``after:`` operator's boundary has only second
    granularity, so a message arriving in the same second as the stored
    watermark can be skipped or duplicated on the next sweep, depending
    on Gmail's own inclusive/exclusive semantics for that second.
    """
    now = now if now is not None else int(time.time())

    if aliases is None:
        targets = list(config.accounts)
    else:
        targets = [config.get(alias) for alias in aliases]

    if not targets:
        return {"accounts": [], "checked_at": now}

    def one(account) -> tuple[dict, int | None]:
        watermark = None
        try:
            watermark = watermarks.get(account.alias)
            service = service_cache.get(account.alias)
            result = check_account(
                service,
                account.alias,
                watermark,
                max_items=max_per_account,
                now=now,
            )
        except Exception as exc:
            write = now - FIRST_RUN_LOOKBACK_SECONDS if watermark is None else None
            return {"alias": account.alias, "error": str(exc)}, write

        if result["truncated"] == 0 and result["newest"] is not None:
            write = result["newest"]
        elif watermark is None:
            write = now - FIRST_RUN_LOOKBACK_SECONDS
        else:
            write = None
        return result, write

    with ThreadPoolExecutor(max_workers=min(len(targets), 8)) as pool:
        outcomes = list(pool.map(one, targets))

    for account, (_, write) in zip(targets, outcomes):
        if write is None:
            continue
        try:
            watermarks.set(account.alias, write)
        except Exception:
            # Persistence is best-effort here: the sweep's results are
            # already computed and must reach the caller regardless of
            # whether the on-disk write succeeds.
            pass

    return {
        "accounts": [result for result, _ in outcomes],
        "checked_at": now,
    }
