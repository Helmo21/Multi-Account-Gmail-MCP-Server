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
