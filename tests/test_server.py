import asyncio

import pytest

from gmail_mcp.config import Account, Config
from gmail_mcp.server import Runtime, create_server
from tests.fakes import FakeDrafts, FakeGmail, FakeLabels, FakeMessages, FakeThreads

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


def server_for(service):
    """Return the raw FastMCP server, for tests that need real MCP-layer
    argument validation (pydantic) rather than the direct-call bypass
    that ``tools()`` exercises.
    """
    return create_server(runtime(service))


def call_over_mcp(server, name, arguments):
    """Invoke a tool the way an MCP client would: through
    ``FastMCP.call_tool``, so pydantic's argument validation actually
    runs. ``tools()[name](...)`` calls the closure directly and skips
    this layer entirely.
    """
    return asyncio.run(server.call_tool(name, arguments))


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


# --- Regression coverage: strict `confirm` validation over the real MCP
# call path. `tools()[...]` calls the tool closures directly and skips
# FastMCP's pydantic argument validation entirely, so it cannot catch a
# regression here -- only `server.call_tool` (invoked through
# `call_over_mcp`) exercises the layer that actually parses arguments
# coming from a model. This is exactly the gap the review caught: the
# `isinstance(confirm, bool)` check in `resolve_send_action` never ran
# for MCP callers because pydantic's lax mode had already coerced
# strings like "true"/"false" and ints like 1/0 to a real bool before
# the tool function's body executed.


@pytest.mark.parametrize("bad_confirm", ["true", "false", "yes", "no", 1, 0])
def test_send_message_confirm_rejects_non_boolean_over_mcp(bad_confirm):
    messages = FakeMessages()
    server = server_for(FakeGmail(messages=messages))

    with pytest.raises(Exception) as exc:
        call_over_mcp(
            server,
            "send_message",
            {
                "account": "personal",
                "to": "b@example.com",
                "subject": "Hi",
                "body": "x",
                "confirm": bad_confirm,
            },
        )

    assert "boolean" in str(exc.value).lower()
    assert messages.sent == []


@pytest.mark.parametrize("bad_confirm", ["true", "false", 1, 0])
def test_send_draft_confirm_rejects_non_boolean_over_mcp(bad_confirm):
    drafts = FakeDrafts(drafts={"d1": {}})
    server = server_for(FakeGmail(drafts=drafts))

    with pytest.raises(Exception) as exc:
        call_over_mcp(
            server,
            "send_draft",
            {"account": "personal", "draft_id": "d1", "confirm": bad_confirm},
        )

    assert "boolean" in str(exc.value).lower()
    assert drafts.sent == []


def test_send_message_confirm_still_accepts_real_booleans_over_mcp():
    messages = FakeMessages()
    server = server_for(FakeGmail(messages=messages))

    call_over_mcp(
        server,
        "send_message",
        {
            "account": "personal",
            "to": "b@example.com",
            "subject": "Hi",
            "body": "x",
            "confirm": True,
        },
    )

    assert len(messages.sent) == 1


# --- Coverage gaps flagged in review: five of ten tools had no test
# exercising their argument plumbing. Each of these is small on purpose
# -- it only needs to prove the keyword reaches the underlying layer.


def test_search_mail_passes_max_results_and_include_spam_trash_through():
    messages = FakeMessages(listing={"messages": []})
    tools(FakeGmail(messages=messages))["search_mail"](
        account="personal",
        query="x",
        max_results=5,
        include_spam_trash=True,
    )

    call = messages.list_calls[-1]
    assert call["maxResults"] == 5
    assert call["includeSpamTrash"] is True


def test_modify_labels_targets_a_thread_when_thread_id_is_given():
    threads = FakeThreads()
    service = FakeGmail(threads=threads, labels=FakeLabels(LABELS))

    result = tools(service)["modify_labels"](
        account="personal", thread_id="t-1", remove=["UNREAD"]
    )

    assert threads.modified[0][0] == "t-1"
    assert threads.modified[0][1]["removeLabelIds"] == ["UNREAD"]
    assert result["target"] == "thread"


def test_check_inboxes_accepts_an_explicit_subset_of_aliases():
    messages = FakeMessages(listing={"messages": []})
    result = tools(FakeGmail(messages=messages))["check_inboxes"](
        accounts=["work"]
    )

    assert [a["alias"] for a in result["accounts"]] == ["work"]


def test_create_draft_reply_threads_off_the_original_message():
    messages = FakeMessages(messages={"m1": message(subject="Original")})
    drafts = FakeDrafts()
    service = FakeGmail(messages=messages, drafts=drafts)

    result = tools(service)["create_draft"](
        account="personal",
        to="b@example.com",
        body="x",
        reply_to_message_id="m1",
    )

    assert result["action"] == "drafted"
    assert drafts.created[0]["message"]["threadId"] == "t-1"


def test_send_draft_sends_an_existing_draft_for_a_send_policy_account():
    drafts = FakeDrafts(drafts={"d1": {}})
    service = FakeGmail(drafts=drafts)

    result = tools(service)["send_draft"](account="work", draft_id="d1")

    assert result["action"] == "sent"
    assert drafts.sent == [{"id": "d1"}]
