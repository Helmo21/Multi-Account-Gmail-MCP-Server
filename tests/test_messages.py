import base64

import pytest

from gmail_mcp.gmail.client import NotFoundError
from gmail_mcp.gmail.messages import (
    BODY_CHAR_LIMIT,
    collapse_quoted,
    extract_body,
    html_to_text,
    list_attachments,
    read_message,
    read_thread,
)
from tests.fakes import FakeGmail, FakeMessages, FakeThreads


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


def part(mime, text=None, filename=None, size=0, parts=None):
    node = {"mimeType": mime, "headers": [], "body": {"size": size}}
    if text is not None:
        node["body"]["data"] = b64(text)
        node["body"]["size"] = len(text)
    if filename:
        node["filename"] = filename
        node["body"]["attachmentId"] = "att-1"
    if parts:
        node["parts"] = parts
    return node


def message(mid="m1", payload=None, labels=("INBOX",)):
    return {
        "id": mid,
        "threadId": "t-1",
        "snippet": "snip",
        "labelIds": list(labels),
        "internalDate": "1700000000000",
        "payload": payload
        or {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "a@example.com"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Subject", "value": "Hi"},
                {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
                {"name": "Message-ID", "value": "<abc@example.com>"},
            ],
            "body": {"data": b64("plain body"), "size": 10},
        },
    }


def test_extracts_simple_plain_body():
    assert extract_body(message()["payload"]) == "plain body"


def test_prefers_plain_text_over_html_in_multipart():
    payload = part(
        "multipart/alternative",
        parts=[part("text/html", "<p>html body</p>"),
               part("text/plain", "plain body")],
    )
    assert extract_body(payload) == "plain body"


def test_falls_back_to_html_when_no_plain_part():
    payload = part(
        "multipart/alternative",
        parts=[part("text/html", "<p>Hello <b>world</b></p>")],
    )
    assert extract_body(payload) == "Hello world"


def test_finds_body_in_nested_multipart():
    payload = part(
        "multipart/mixed",
        parts=[
            part("multipart/alternative", parts=[part("text/plain", "deep body")]),
            part("application/pdf", filename="doc.pdf", size=99),
        ],
    )
    assert extract_body(payload) == "deep body"


def test_missing_body_returns_empty_string():
    assert extract_body({"mimeType": "text/plain", "body": {}}) == ""


def test_long_body_is_truncated_with_a_notice():
    payload = part("text/plain", "x" * (BODY_CHAR_LIMIT + 500))
    body = extract_body(payload)

    assert len(body) < BODY_CHAR_LIMIT + 200
    assert "truncated" in body.lower()


def test_html_to_text_drops_tags_and_scripts():
    html = "<style>a{}</style><script>x()</script><p>Hi</p><br><p>There</p>"
    assert html_to_text(html) == "Hi\nThere"


def test_html_to_text_unescapes_entities():
    assert html_to_text("<p>A &amp; B &lt;yes&gt;</p>") == "A & B <yes>"


def test_collapse_quoted_removes_quoted_lines():
    text = "My reply\n\nOn Mon someone wrote:\n> old thing\n> more old"
    assert collapse_quoted(text) == "My reply"


def test_collapse_quoted_keeps_text_without_quotes():
    assert collapse_quoted("Just a note") == "Just a note"


def test_collapse_quoted_handles_forwarded_marker():
    text = "See below\n\n---------- Forwarded message ---------\nold stuff"
    assert collapse_quoted(text) == "See below"


def test_lists_attachment_metadata_only():
    payload = part(
        "multipart/mixed",
        parts=[part("text/plain", "body"),
               part("application/pdf", filename="doc.pdf", size=1234)],
    )
    assert list_attachments(payload) == [
        {"filename": "doc.pdf", "mime_type": "application/pdf", "size": 1234}
    ]


def test_read_message_returns_headers_body_and_attachments():
    service = FakeGmail(messages=FakeMessages(messages={"m1": message()}))
    result = read_message(service, "m1")

    assert result["subject"] == "Hi"
    assert result["body"] == "plain body"
    assert result["message_id_header"] == "<abc@example.com>"
    assert result["attachments"] == []
    assert result["unread"] is False


def test_read_message_missing_id_mentions_other_mailboxes():
    service = FakeGmail(messages=FakeMessages(messages={}))
    with pytest.raises(NotFoundError) as exc:
        read_message(service, "nope")
    assert "different" in str(exc.value).lower()


def test_read_thread_returns_messages_in_order_with_quotes_collapsed():
    first = message("m1")
    second = message("m2")
    second["payload"]["body"]["data"] = b64(
        "Reply text\n\nOn Mon someone wrote:\n> quoted"
    )
    service = FakeGmail(
        threads=FakeThreads(threads={"t-1": {"id": "t-1",
                                             "messages": [first, second]}})
    )

    result = read_thread(service, "t-1")

    assert result["thread_id"] == "t-1"
    assert [m["message_id"] for m in result["messages"]] == ["m1", "m2"]
    assert result["messages"][1]["body"] == "Reply text"


def test_read_thread_missing_id_raises_not_found():
    service = FakeGmail(threads=FakeThreads(threads={}))
    with pytest.raises(NotFoundError):
        read_thread(service, "nope")
