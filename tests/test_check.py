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


def test_truncated_account_does_not_advance_watermark_past_unreported_mail():
    # 5 new messages but only 2 fit; watermark must not jump to the
    # newest one, or the 3 unreported (older) messages are skipped
    # forever on the next sweep.
    config = Config(accounts=(Account("a", "a@example.com", "send"),))
    messages = inbox(*[msg(f"m{i}", NOW - i) for i in range(1, 6)])

    class Cache:
        def get(self, alias):
            return FakeGmail(messages=messages)

    marks = {"a": NOW - 1000}

    class Marks:
        def get(self, alias):
            return marks.get(alias)

        def set(self, alias, value):
            marks[alias] = value

    check_inboxes(config, Cache(), Marks(), max_per_account=2, now=NOW)

    assert marks["a"] == NOW - 1000


def test_truncated_first_run_freezes_the_lookback_window():
    # First-ever check, but more new mail than fits in one sweep. The
    # watermark must not stay unset, or the 24h lookback silently slides
    # forward on the next run and drops the unreported backlog.
    config = Config(accounts=(Account("a", "a@example.com", "send"),))
    messages = inbox(*[msg(f"m{i}", NOW - i) for i in range(1, 6)])

    class Cache:
        def get(self, alias):
            return FakeGmail(messages=messages)

    marks = {}

    class Marks:
        def get(self, alias):
            return marks.get(alias)

        def set(self, alias, value):
            marks[alias] = value

    check_inboxes(config, Cache(), Marks(), max_per_account=2, now=NOW)

    assert marks["a"] == NOW - FIRST_RUN_LOOKBACK_SECONDS


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
