import base64
from email.parser import BytesParser
from email.policy import default

import pytest

from gmail_mcp.gmail.compose import (
    build_mime,
    encode,
    fetch_reply_context,
    reply_subject,
)
from tests.fakes import FakeGmail, FakeMessages


def decode(raw):
    return BytesParser(policy=default).parsebytes(base64.urlsafe_b64decode(raw.encode()))


def test_sets_the_basic_headers():
    msg = build_mime(to="a@example.com", subject="Hi", body="Hello")

    assert msg["To"] == "a@example.com"
    assert msg["Subject"] == "Hi"
    assert msg.get_content().strip() == "Hello"


def test_accepts_a_list_of_recipients():
    msg = build_mime(
        to=["a@example.com", "b@example.com"], subject="Hi", body="x"
    )
    assert msg["To"] == "a@example.com, b@example.com"


def test_sets_cc_and_bcc():
    msg = build_mime(
        to="a@example.com", subject="Hi", body="x",
        cc=["c@example.com"], bcc=["d@example.com"],
    )
    assert msg["Cc"] == "c@example.com"
    assert msg["Bcc"] == "d@example.com"


def test_omits_cc_and_bcc_when_absent():
    msg = build_mime(to="a@example.com", subject="Hi", body="x")
    assert msg["Cc"] is None
    assert msg["Bcc"] is None


def test_empty_recipient_list_is_rejected():
    with pytest.raises(ValueError, match="recipient"):
        build_mime(to=[], subject="Hi", body="x")


def test_unicode_subject_and_body_survive_a_roundtrip():
    msg = build_mime(to="a@example.com", subject="Réunion 会議", body="Café ☕")
    restored = decode(encode(msg))

    assert restored["Subject"] == "Réunion 会議"
    assert "Café ☕" in restored.get_content()


def test_reply_headers_are_set_when_supplied():
    msg = build_mime(
        to="a@example.com", subject="Re: Hi", body="x",
        in_reply_to="<abc@example.com>",
        references="<root@example.com> <abc@example.com>",
    )
    assert msg["In-Reply-To"] == "<abc@example.com>"
    assert msg["References"] == "<root@example.com> <abc@example.com>"


def test_encode_produces_url_safe_base64():
    raw = encode(build_mime(to="a@example.com", subject="Hi", body="x"))
    assert "+" not in raw and "/" not in raw
    assert decode(raw)["Subject"] == "Hi"


def test_reply_subject_adds_one_prefix():
    assert reply_subject("Hi") == "Re: Hi"


def test_reply_subject_does_not_stack_prefixes():
    assert reply_subject("Re: Hi") == "Re: Hi"
    assert reply_subject("RE: Hi") == "RE: Hi"


def test_fetch_reply_context_reads_threading_headers():
    original = {
        "id": "m1",
        "threadId": "t-9",
        "payload": {
            "headers": [
                {"name": "Message-ID", "value": "<abc@example.com>"},
                {"name": "References", "value": "<root@example.com>"},
                {"name": "Subject", "value": "Quarterly numbers"},
            ]
        },
    }
    service = FakeGmail(messages=FakeMessages(messages={"m1": original}))

    context = fetch_reply_context(service, "m1")

    assert context.thread_id == "t-9"
    assert context.message_id_header == "<abc@example.com>"
    assert context.references == "<root@example.com> <abc@example.com>"
    assert context.subject == "Quarterly numbers"


def test_fetch_reply_context_starts_references_when_absent():
    original = {
        "id": "m1",
        "threadId": "t-9",
        "payload": {"headers": [{"name": "Message-ID", "value": "<abc@x>"}]},
    }
    service = FakeGmail(messages=FakeMessages(messages={"m1": original}))

    assert fetch_reply_context(service, "m1").references == "<abc@x>"
