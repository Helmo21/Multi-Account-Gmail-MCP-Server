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


def test_thread_collapse_before_truncate_sentinel_under_limit_after_collapse():
    """Sentinel in reply text that survives collapsing despite raw being over limit.

    Tests that truncation is applied post-collapse. Raw body exceeds 50K due to
    interleaved quoted lines, but after collapsing (removing all `>`-prefixed lines),
    body is well under 50K. The sentinel appears at different raw positions in the
    two orderings: old ordering truncates before collapsing and loses it; new ordering
    collapses first and preserves it.
    """
    first = message("m1")
    second = message("m2")

    # Build body with interleaved reply and quoted lines.
    # Structure: reply_block_1 (25K) + quoted_block_1 (35K) + reply_block_2 (15K)
    # Raw: 40K reply + 35K quoted = 75K (over 50K limit)
    # Collapsed: 40K reply (well under 50K limit, no truncation)

    # reply_block_1: 25K of reply text
    reply_1 = "x" * 25000

    # quoted_block_1: 35K of quoted lines (these will be removed by collapse_quoted)
    quoted_1 = "".join("> quoted line here\n" for _ in range(1666))  # ~35K

    # reply_block_2: 15K with sentinel at the start
    sentinel_marker = "SENTINEL_UNDER_LIMIT"
    reply_2 = sentinel_marker + "y" * (15000 - len(sentinel_marker))

    body_text = reply_1 + quoted_1 + reply_2

    # Verify dimensions
    collapsed_preview = reply_1 + reply_2
    assert len(body_text) > BODY_CHAR_LIMIT + 10000, f"Raw too small: {len(body_text)}"
    assert len(collapsed_preview) < BODY_CHAR_LIMIT, f"Collapsed too large: {len(collapsed_preview)}"

    second["payload"]["body"]["data"] = b64(body_text)
    service = FakeGmail(
        threads=FakeThreads(threads={"t-1": {"id": "t-1",
                                             "messages": [first, second]}})
    )

    result = read_thread(service, "t-1")
    body = result["messages"][1]["body"]

    # After collapsing, should have the sentinel and no truncation notice.
    # With old ordering (truncate then collapse): sentinel is lost (raw position >50K is cut)
    # With new ordering (collapse then collapse): sentinel preserved (collapsed position <50K)
    assert sentinel_marker in body, "Sentinel should be preserved after collapsing"
    assert "truncated" not in body.lower(), "Should not be truncated after collapsing"


def test_thread_collapse_before_truncate_sentinel_over_limit_after_collapse():
    """Sentinel in reply text survives truncation of collapsed body.

    Raw body has large quoted block followed by replies. After collapsing,
    body exceeds 50K and gets truncated. Sentinel in early reply survives
    the 50K truncation but is lost with the old ordering because it appears
    much later in the raw text (after the large quoted block).
    """
    first = message("m1")
    second = message("m2")

    # Build body: quoted_block (50K) + reply_block (40K with sentinel at ~30K) + reply_block (20K)
    # Raw: 50K quoted + 60K reply = 110K (well over limit)
    # Collapsed: 60K reply (over 50K limit, will be truncated)
    # Sentinel at position 30K in collapsed text survives truncation at 50K

    # quoted_block_1: 50K of quoted lines that will be removed by collapse
    quoted_1 = "".join("> quoted line here\n" for _ in range(2380))  # ~50K

    # reply_block_1: 40K with sentinel at position ~30K
    sentinel_marker = "SENTINEL_OVER_LIMIT"
    reply_1_part_a = "a" * 30000
    reply_1_part_b = sentinel_marker + "b" * (10000 - len(sentinel_marker))
    reply_1 = reply_1_part_a + reply_1_part_b  # 40K total

    # reply_block_2: 20K of additional reply
    reply_2 = "c" * 20000

    body_text = quoted_1 + reply_1 + reply_2

    # Verify dimensions
    collapsed_preview = reply_1 + reply_2
    assert len(body_text) > BODY_CHAR_LIMIT + 50000, f"Raw body too small: {len(body_text)}"
    assert len(collapsed_preview) > BODY_CHAR_LIMIT, f"Collapsed too small: {len(collapsed_preview)}"

    # Verify sentinel is at position that survives 50K truncation in collapsed text
    sentinel_pos = collapsed_preview.find(sentinel_marker)
    assert sentinel_pos < 50000, f"Sentinel at {sentinel_pos}, must be <50000 to survive truncation"
    # Verify sentinel is AFTER 50K in raw text (position within quoted + reply blocks)
    raw_sentinel_pos = len(quoted_1) + sentinel_pos
    assert raw_sentinel_pos > 50000, f"Sentinel at {raw_sentinel_pos} in raw, must be >50000 to test the bug"

    second["payload"]["body"]["data"] = b64(body_text)
    service = FakeGmail(
        threads=FakeThreads(threads={"t-1": {"id": "t-1",
                                             "messages": [first, second]}})
    )

    result = read_thread(service, "t-1")
    body = result["messages"][1]["body"]

    # After collapsing and truncating, should have sentinel and truncation notice.
    # With old ordering (truncate then collapse): truncates at 50K (in quoted block),
    #   collapses quoted away, sentinel is lost
    # With new ordering (collapse then truncate): collapses quoted away, sentinel
    #   at position 30K survives truncation at 50K
    assert sentinel_marker in body, "Sentinel should survive truncation of collapsed body"
    assert "truncated" in body.lower(), "Should have truncation notice"
