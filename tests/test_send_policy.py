import pytest

from gmail_mcp.config import Account
from gmail_mcp.gmail.compose import (
    SendAction,
    compose_and_deliver,
    resolve_send_action,
    send_existing_draft,
)
from tests.fakes import FakeDrafts, FakeGmail, FakeMessages


def account(policy):
    return Account(alias="a", email="a@example.com", send_policy=policy)


@pytest.mark.parametrize(
    "policy,confirm,expected",
    [
        ("send", False, SendAction.SEND),
        ("send", True, SendAction.SEND),
        ("confirm", False, SendAction.DRAFT),
        ("confirm", True, SendAction.SEND),
        ("draft_only", False, SendAction.DRAFT),
        ("draft_only", True, SendAction.DRAFT),
    ],
)
def test_policy_gate_truth_table(policy, confirm, expected):
    assert resolve_send_action(policy, confirm) is expected


def test_unknown_policy_is_rejected_rather_than_defaulted():
    with pytest.raises(ValueError, match="anything-goes"):
        resolve_send_action("anything-goes", True)


def test_send_policy_transmits_immediately():
    messages = FakeMessages()
    service = FakeGmail(messages=messages)

    result = compose_and_deliver(
        service, account("send"), to="b@example.com", subject="Hi", body="x"
    )

    assert result["action"] == "sent"
    assert result["message_id"] == "sent-1"
    assert len(messages.sent) == 1


def test_confirm_policy_without_confirmation_creates_a_draft():
    drafts = FakeDrafts()
    messages = FakeMessages()
    service = FakeGmail(messages=messages, drafts=drafts)

    result = compose_and_deliver(
        service, account("confirm"), to="b@example.com", subject="Hi", body="x"
    )

    assert result["action"] == "drafted"
    assert result["draft_id"] == "draft-1"
    assert messages.sent == []
    assert "confirm" in result["note"].lower()


def test_confirm_policy_with_confirmation_sends():
    messages = FakeMessages()
    service = FakeGmail(messages=messages)

    result = compose_and_deliver(
        service, account("confirm"), to="b@example.com", subject="Hi",
        body="x", confirm=True,
    )

    assert result["action"] == "sent"
    assert len(messages.sent) == 1


def test_draft_only_policy_never_sends_even_when_confirmed():
    messages = FakeMessages()
    drafts = FakeDrafts()
    service = FakeGmail(messages=messages, drafts=drafts)

    result = compose_and_deliver(
        service, account("draft_only"), to="b@example.com", subject="Hi",
        body="x", confirm=True,
    )

    assert result["action"] == "drafted"
    assert messages.sent == []
    assert "draft_only" in result["note"]


def test_force_draft_overrides_a_send_policy():
    messages = FakeMessages()
    service = FakeGmail(messages=messages, drafts=FakeDrafts())

    result = compose_and_deliver(
        service, account("send"), to="b@example.com", subject="Hi",
        body="x", force_draft=True,
    )

    assert result["action"] == "drafted"
    assert messages.sent == []


def test_reply_sets_threading_headers_and_thread_id():
    original = {
        "id": "m1",
        "threadId": "t-9",
        "payload": {
            "headers": [
                {"name": "Message-ID", "value": "<abc@example.com>"},
                {"name": "Subject", "value": "Numbers"},
            ]
        },
    }
    messages = FakeMessages(messages={"m1": original})
    service = FakeGmail(messages=messages)

    compose_and_deliver(
        service, account("send"), to="b@example.com", subject=None,
        body="x", reply_to_message_id="m1",
    )

    body = messages.sent[0]
    assert body["threadId"] == "t-9"

    import base64
    from email import message_from_bytes

    mime = message_from_bytes(base64.urlsafe_b64decode(body["raw"].encode()))
    assert mime["In-Reply-To"] == "<abc@example.com>"
    assert mime["Subject"] == "Re: Numbers"


def test_explicit_subject_wins_over_derived_reply_subject():
    original = {
        "id": "m1",
        "threadId": "t-9",
        "payload": {"headers": [{"name": "Subject", "value": "Numbers"}]},
    }
    messages = FakeMessages(messages={"m1": original})
    service = FakeGmail(messages=messages)

    compose_and_deliver(
        service, account("send"), to="b@example.com", subject="My subject",
        body="x", reply_to_message_id="m1",
    )

    import base64
    from email import message_from_bytes

    mime = message_from_bytes(
        base64.urlsafe_b64decode(messages.sent[0]["raw"].encode())
    )
    assert mime["Subject"] == "My subject"


def test_new_message_without_subject_is_rejected():
    with pytest.raises(ValueError, match="subject"):
        compose_and_deliver(
            FakeGmail(), account("send"), to="b@example.com",
            subject=None, body="x",
        )


def test_send_existing_draft_respects_confirm_policy():
    drafts = FakeDrafts(drafts={"draft-1": {}})
    service = FakeGmail(drafts=drafts)

    result = send_existing_draft(service, account("confirm"), "draft-1")

    assert result["action"] == "drafted"
    assert drafts.sent == []
    assert result["draft_id"] == "draft-1"


def test_send_existing_draft_sends_when_confirmed():
    drafts = FakeDrafts(drafts={"draft-1": {}})
    service = FakeGmail(drafts=drafts)

    result = send_existing_draft(
        service, account("confirm"), "draft-1", confirm=True
    )

    assert result["action"] == "sent"
    assert drafts.sent == [{"id": "draft-1"}]


def test_send_existing_draft_blocked_by_draft_only():
    drafts = FakeDrafts(drafts={"draft-1": {}})
    service = FakeGmail(drafts=drafts)

    result = send_existing_draft(
        service, account("draft_only"), "draft-1", confirm=True
    )

    assert result["action"] == "drafted"
    assert drafts.sent == []
