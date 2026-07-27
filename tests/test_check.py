from datetime import datetime, timezone

import pytest

from gmail_mcp.check import (
    FIRST_RUN_LOOKBACK_SECONDS,
    SNIPPET_LIMIT,
    check_account,
    check_inboxes,
)
from gmail_mcp.config import Account, Config
from tests.fakes import FakeGmail, FakeLabels, FakeMessages, make_http_error

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


class ServiceCacheStub:
    """A stand-in for ServiceCache: aliases map to a fake service or, if
    the mapped value is an Exception instance, raise it instead (this is
    how these tests simulate a revoked token or similar per-account
    failure). `default` covers any alias not explicitly listed."""

    def __init__(self, services=None, default=None):
        self.services = dict(services or {})
        self.default = default

    def get(self, alias):
        entry = self.services.get(alias, self.default)
        if isinstance(entry, Exception):
            raise entry
        return entry


class WatermarkStub:
    """A dict-backed stand-in for WatermarkStore, for tests that don't
    need to exercise real file persistence or its concurrency hazards."""

    def __init__(self, marks=None):
        self.marks = dict(marks or {})

    def get(self, alias):
        return self.marks.get(alias)

    def set(self, alias, value):
        self.marks[alias] = value


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


def test_received_comes_from_internal_date_not_the_sender_controlled_header():
    # The Date header in msg() is a fixed, unrelated value ("1 Jan 2024")
    # regardless of the epoch passed in -- exactly the kind of stale,
    # spoofable header `received` must not be built from.
    epoch = NOW - 60
    messages = inbox(msg("m1", epoch))
    result = check_account(FakeGmail(messages=messages), "personal", None,
                           max_items=10, now=NOW)

    expected = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert result["items"][0]["received"] == expected
    assert result["items"][0]["received"] != "Mon, 1 Jan 2024 00:00:00 +0000"


def test_total_unread_comes_from_the_inbox_label_count():
    messages = inbox(msg("m1", NOW - 60), total=2)
    service = FakeGmail(
        messages=messages, labels=FakeLabels(unread_counts={"INBOX": 437})
    )

    result = check_account(service, "personal", None, max_items=10, now=NOW)

    # 437 (the mailbox's true unread backlog), not 2 (this query's
    # resultSizeEstimate for "new since the watermark").
    assert result["total_unread"] == 437
    assert result["new_count"] == 1


def test_total_unread_falls_back_when_the_label_lookup_fails():
    # Default FakeLabels() has no unread_counts, so labels().get() 404s.
    # The sweep must still return a number, not fail.
    messages = inbox(msg("m1", NOW - 60), total=9)
    result = check_account(FakeGmail(messages=messages), "personal", None,
                           max_items=10, now=NOW)

    assert result["total_unread"] == 9


def test_sweep_advances_watermark_only_for_successful_accounts():
    config = Config(
        accounts=(
            Account("good", "g@example.com", "send"),
            Account("bad", "b@example.com", "send"),
        )
    )
    good = FakeGmail(messages=inbox(msg("m1", NOW - 100)))
    cache = ServiceCacheStub(
        services={"good": good},
        default=RuntimeError("token revoked, run: gmail-mcp auth add bad"),
    )
    watermarks = WatermarkStub()

    result = check_inboxes(config, cache, watermarks, now=NOW)

    by_alias = {a["alias"]: a for a in result["accounts"]}
    assert by_alias["good"]["new_count"] == 1
    assert "revoked" in by_alias["bad"]["error"]
    # "good" advances to the newest message it actually reported. "bad"
    # never got to check_account at all, but since this is its first
    # run, its lookback boundary is still frozen (Finding 3) rather than
    # left unset -- see test_failed_first_run_freezes_the_lookback_window
    # for that behaviour in isolation.
    assert watermarks.marks == {
        "good": NOW - 100,
        "bad": NOW - FIRST_RUN_LOOKBACK_SECONDS,
    }


def test_sweep_returns_every_account_even_when_one_fails():
    config = Config(
        accounts=(
            Account("a", "a@example.com", "send"),
            Account("b", "b@example.com", "send"),
            Account("c", "c@example.com", "send"),
        )
    )
    cache = ServiceCacheStub(
        services={
            "a": FakeGmail(messages=inbox()),
            "b": RuntimeError("boom"),
            "c": FakeGmail(messages=inbox()),
        }
    )

    result = check_inboxes(config, cache, WatermarkStub(), now=NOW)

    assert [a["alias"] for a in result["accounts"]] == ["a", "b", "c"]


def test_sweep_can_be_limited_to_named_accounts():
    config = Config(
        accounts=(
            Account("a", "a@example.com", "send"),
            Account("b", "b@example.com", "send"),
        )
    )
    cache = ServiceCacheStub(default=FakeGmail(messages=inbox()))

    result = check_inboxes(
        config, cache, WatermarkStub(), aliases=["b"], now=NOW
    )

    assert [a["alias"] for a in result["accounts"]] == ["b"]


def test_sweep_rejects_an_unknown_alias():
    from gmail_mcp.config import UnknownAliasError

    config = Config(accounts=(Account("a", "a@example.com", "send"),))
    cache = ServiceCacheStub(default=FakeGmail())

    with pytest.raises(UnknownAliasError):
        check_inboxes(config, cache, WatermarkStub(), aliases=["zzz"], now=NOW)


def test_api_failure_is_reported_as_an_account_error():
    config = Config(accounts=(Account("a", "a@example.com", "send"),))
    broken = FakeMessages(listing={"messages": []})
    broken.list = lambda **kw: (_ for _ in ()).throw(make_http_error(500))
    cache = ServiceCacheStub(services={"a": FakeGmail(messages=broken)})

    result = check_inboxes(config, cache, WatermarkStub(), now=NOW)

    assert "error" in result["accounts"][0]
    assert "new_count" not in result["accounts"][0]


def test_truncated_account_does_not_advance_watermark_past_unreported_mail():
    # 5 new messages but only 2 fit; watermark must not jump to the
    # newest one, or the 3 unreported (older) messages are skipped
    # forever on the next sweep.
    config = Config(accounts=(Account("a", "a@example.com", "send"),))
    messages = inbox(*[msg(f"m{i}", NOW - i) for i in range(1, 6)])
    cache = ServiceCacheStub(default=FakeGmail(messages=messages))
    watermarks = WatermarkStub({"a": NOW - 1000})

    check_inboxes(config, cache, watermarks, max_per_account=2, now=NOW)

    assert watermarks.marks["a"] == NOW - 1000


def test_truncated_first_run_freezes_the_lookback_window():
    # First-ever check, but more new mail than fits in one sweep. The
    # watermark must not stay unset, or the 24h lookback silently slides
    # forward on the next run and drops the unreported backlog.
    config = Config(accounts=(Account("a", "a@example.com", "send"),))
    messages = inbox(*[msg(f"m{i}", NOW - i) for i in range(1, 6)])
    cache = ServiceCacheStub(default=FakeGmail(messages=messages))
    watermarks = WatermarkStub()

    check_inboxes(config, cache, watermarks, max_per_account=2, now=NOW)

    assert watermarks.marks["a"] == NOW - FIRST_RUN_LOOKBACK_SECONDS


def test_empty_first_run_freezes_the_lookback_window():
    # Same sliding-window hazard as a truncated first run: nothing to
    # advance to, but leaving the watermark unset would let the next
    # run's lookback boundary drift forward with the clock.
    config = Config(accounts=(Account("a", "a@example.com", "send"),))
    cache = ServiceCacheStub(default=FakeGmail(messages=inbox()))
    watermarks = WatermarkStub()

    check_inboxes(config, cache, watermarks, now=NOW)

    assert watermarks.marks["a"] == NOW - FIRST_RUN_LOOKBACK_SECONDS


def test_failed_first_run_freezes_the_lookback_window():
    # Same hazard again, on the error path: the account has never been
    # checked before, so there is no anchored watermark to protect --
    # the boundary must still be frozen even though check_account never
    # ran at all.
    config = Config(accounts=(Account("a", "a@example.com", "send"),))
    cache = ServiceCacheStub(services={"a": RuntimeError("token revoked")})
    watermarks = WatermarkStub()

    result = check_inboxes(config, cache, watermarks, now=NOW)

    assert "error" in result["accounts"][0]
    assert watermarks.marks["a"] == NOW - FIRST_RUN_LOOKBACK_SECONDS


def test_truncated_message_is_surfaced_once_newer_mail_is_read():
    # End-to-end justification for the truncation/watermark fix: mail
    # that was truncated (uncounted in `items`) on one sweep must still
    # show up on a later sweep once the backlog ahead of it clears,
    # rather than being silently dropped forever.
    config = Config(accounts=(Account("a", "a@example.com", "send"),))
    all_messages = [msg(f"m{i}", NOW - i) for i in range(1, 6)]
    fake_messages = inbox(*all_messages)
    cache = ServiceCacheStub(default=FakeGmail(messages=fake_messages))
    watermarks = WatermarkStub()

    first = check_inboxes(config, cache, watermarks, max_per_account=2, now=NOW)
    first_ids = {item["message_id"] for item in first["accounts"][0]["items"]}
    assert first_ids == {"m1", "m2"}
    assert first["accounts"][0]["truncated"] == 3

    # m1 and m2 have since been marked read: they drop out of the
    # is:unread listing, leaving the three that were truncated before.
    remaining = all_messages[2:]
    fake_messages.listing = {
        "messages": [{"id": m["id"]} for m in remaining],
        "resultSizeEstimate": len(remaining),
    }

    second = check_inboxes(
        config, cache, watermarks, max_per_account=2, now=NOW + 60
    )
    second_ids = {item["message_id"] for item in second["accounts"][0]["items"]}
    assert "m3" in second_ids


def test_concurrent_sweep_does_not_lose_or_corrupt_watermarks(tmp_path):
    # A real WatermarkStore, not a stub: this is the only way to see the
    # shared-temp-file race in WatermarkStore.set when several accounts'
    # writes would otherwise happen concurrently from the thread pool.
    from gmail_mcp.storage import WatermarkStore

    accounts = tuple(
        Account(f"acct{i}", f"a{i}@example.com", "send") for i in range(8)
    )
    config = Config(accounts=accounts)
    store = WatermarkStore(tmp_path)
    cache = ServiceCacheStub(
        default=FakeGmail(messages=inbox(msg("m1", NOW - 60)))
    )

    result = check_inboxes(config, cache, store, now=NOW)

    assert [a["alias"] for a in result["accounts"]] == [
        acc.alias for acc in accounts
    ]
    for account in accounts:
        assert store.get(account.alias) == NOW - 60
