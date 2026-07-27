import pytest

from gmail_mcp.config import Account, Config
from gmail_mcp.server import Runtime, create_server
from tests.fakes import FakeGmail, FakeLabels, FakeMessages, FakeThreads

LABELS = [
    {"id": "INBOX", "name": "INBOX", "type": "system"},
    {"id": "UNREAD", "name": "UNREAD", "type": "system"},
]


def message(mid="m1", subject="Hi"):
    return {
        "id": mid,
        "threadId": "t-1",
        "snippet": "snip",
        "labelIds": ["INBOX", "UNREAD"],
        "internalDate": "1700000000000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "a@example.com"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
            ],
            "body": {"data": "cGxhaW4gYm9keQ==", "size": 10},
        },
    }


class StubCache:
    def __init__(self, service):
        self.service = service

    def get(self, alias):
        return self.service


class StubMarks:
    def __init__(self):
        self.data = {}

    def get(self, alias):
        return self.data.get(alias)

    def set(self, alias, value):
        self.data[alias] = value


def runtime(service):
    config = Config(
        accounts=(
            Account("personal", "me@example.com", "confirm"),
            Account("work", "w@example.com", "send"),
        )
    )
    return Runtime(
        config=config,
        token_store=None,
        watermarks=StubMarks(),
        services=StubCache(service),
    )


def tools(service):
    return create_server(runtime(service)).tool_functions


def test_list_accounts_exposes_aliases_and_policies():
    result = tools(FakeGmail())["list_accounts"]()

    assert result == [
        {"alias": "personal", "email": "me@example.com", "send_policy": "confirm"},
        {"alias": "work", "email": "w@example.com", "send_policy": "send"},
    ]


def test_search_mail_returns_summaries():
    messages = FakeMessages(
        listing={"messages": [{"id": "m1"}]}, messages={"m1": message()}
    )
    result = tools(FakeGmail(messages=messages))["search_mail"](
        account="personal", query="is:unread"
    )

    assert result[0]["subject"] == "Hi"


def test_unknown_alias_is_a_clear_error_naming_valid_aliases():
    config = Config(accounts=(Account("personal", "me@example.com", "send"),))
    server = create_server(
        Runtime(config=config, token_store=None, watermarks=StubMarks(),
                services=StubCache(FakeGmail()))
    )

    with pytest.raises(Exception) as exc:
        server.tool_functions["search_mail"](account="nope", query="x")

    assert "personal" in str(exc.value)


def test_read_message_returns_the_body():
    messages = FakeMessages(messages={"m1": message()})
    result = tools(FakeGmail(messages=messages))["read_message"](
        account="personal", message_id="m1"
    )

    assert result["body"] == "plain body"


def test_read_thread_returns_ordered_messages():
    threads = FakeThreads(threads={"t-1": {"id": "t-1", "messages": [message()]}})
    result = tools(FakeGmail(threads=threads))["read_thread"](
        account="personal", thread_id="t-1"
    )

    assert len(result["messages"]) == 1


def test_send_message_honours_the_confirm_policy():
    messages = FakeMessages()
    service = FakeGmail(messages=messages)

    result = tools(service)["send_message"](
        account="personal", to="b@example.com", subject="Hi", body="x"
    )

    assert result["action"] == "drafted"
    assert messages.sent == []


def test_send_message_sends_for_a_send_policy_account():
    messages = FakeMessages()
    service = FakeGmail(messages=messages)

    result = tools(service)["send_message"](
        account="work", to="b@example.com", subject="Hi", body="x"
    )

    assert result["action"] == "sent"
    assert len(messages.sent) == 1


def test_create_draft_never_sends_regardless_of_policy():
    messages = FakeMessages()
    service = FakeGmail(messages=messages)

    result = tools(service)["create_draft"](
        account="work", to="b@example.com", subject="Hi", body="x"
    )

    assert result["action"] == "drafted"
    assert messages.sent == []


def test_list_labels_returns_labels():
    service = FakeGmail(labels=FakeLabels(LABELS))
    assert tools(service)["list_labels"](account="personal")[0]["name"] == "INBOX"


def test_modify_labels_marks_a_message_read():
    messages = FakeMessages(messages={"m1": {}})
    service = FakeGmail(messages=messages, labels=FakeLabels(LABELS))

    tools(service)["modify_labels"](
        account="personal", message_id="m1", remove=["UNREAD"]
    )

    assert messages.modified[0][1]["removeLabelIds"] == ["UNREAD"]


def test_check_inboxes_sweeps_all_accounts():
    messages = FakeMessages(listing={"messages": []})
    result = tools(FakeGmail(messages=messages))["check_inboxes"]()

    assert [a["alias"] for a in result["accounts"]] == ["personal", "work"]


def test_all_ten_tools_are_registered():
    assert set(tools(FakeGmail())) == {
        "list_accounts",
        "search_mail",
        "read_message",
        "read_thread",
        "create_draft",
        "send_message",
        "send_draft",
        "list_labels",
        "modify_labels",
        "check_inboxes",
    }
