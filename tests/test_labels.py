import pytest

from gmail_mcp.gmail.client import GmailError
from gmail_mcp.gmail.labels import list_labels, modify_labels, resolve_label_ids
from tests.fakes import FakeGmail, FakeLabels, FakeMessages, FakeThreads

LABELS = [
    {"id": "INBOX", "name": "INBOX", "type": "system"},
    {"id": "UNREAD", "name": "UNREAD", "type": "system"},
    {"id": "Label_7", "name": "Clients/Acme", "type": "user"},
]


def service_with_labels(**kwargs):
    return FakeGmail(labels=FakeLabels(LABELS), **kwargs)


def test_lists_labels_in_a_compact_shape():
    assert list_labels(service_with_labels()) == [
        {"id": "INBOX", "name": "INBOX", "type": "system"},
        {"id": "UNREAD", "name": "UNREAD", "type": "system"},
        {"id": "Label_7", "name": "Clients/Acme", "type": "user"},
    ]


def test_resolves_user_label_names_to_ids():
    assert resolve_label_ids(service_with_labels(), ["Clients/Acme"]) == ["Label_7"]


def test_resolves_system_labels():
    assert resolve_label_ids(service_with_labels(), ["INBOX", "UNREAD"]) == [
        "INBOX",
        "UNREAD",
    ]


def test_label_name_matching_ignores_case():
    assert resolve_label_ids(service_with_labels(), ["clients/acme"]) == ["Label_7"]


def test_unknown_label_lists_the_available_ones():
    with pytest.raises(GmailError) as exc:
        resolve_label_ids(service_with_labels(), ["Nope"])
    message = str(exc.value)
    assert "Nope" in message
    assert "Clients/Acme" in message


def test_empty_name_list_resolves_to_empty():
    assert resolve_label_ids(service_with_labels(), []) == []


def test_modifies_a_message():
    messages = FakeMessages(messages={"m1": {}})
    service = service_with_labels(messages=messages)

    modify_labels(service, message_id="m1", add=["Clients/Acme"], remove=["UNREAD"])

    assert messages.modified == [
        ("m1", {"addLabelIds": ["Label_7"], "removeLabelIds": ["UNREAD"]})
    ]


def test_modifies_a_thread():
    threads = FakeThreads(threads={"t1": {}})
    service = service_with_labels(threads=threads)

    modify_labels(service, thread_id="t1", remove=["INBOX"])

    assert threads.modified == [
        ("t1", {"addLabelIds": [], "removeLabelIds": ["INBOX"]})
    ]


def test_requires_exactly_one_target():
    with pytest.raises(ValueError, match="exactly one"):
        modify_labels(service_with_labels(), add=["INBOX"])

    with pytest.raises(ValueError, match="exactly one"):
        modify_labels(
            service_with_labels(), message_id="m1", thread_id="t1", add=["INBOX"]
        )


def test_requires_at_least_one_change():
    with pytest.raises(ValueError, match="add or remove"):
        modify_labels(service_with_labels(), message_id="m1")


def test_returns_what_changed():
    messages = FakeMessages(messages={"m1": {}})
    result = modify_labels(
        service_with_labels(messages=messages), message_id="m1", remove=["UNREAD"]
    )

    assert result == {
        "target": "message",
        "id": "m1",
        "added": [],
        "removed": ["UNREAD"],
    }


def test_modifies_labels_fetches_label_list_once_for_both_add_and_remove():
    labels = FakeLabels(LABELS)
    messages = FakeMessages(messages={"m1": {}})
    service = FakeGmail(labels=labels, messages=messages)

    modify_labels(service, message_id="m1", add=["Clients/Acme"], remove=["UNREAD"])

    assert len(labels.list_calls) == 1


def test_empty_string_message_id_with_thread_id_does_not_provide_two_targets():
    threads = FakeThreads(threads={"t1": {}})
    service = service_with_labels(threads=threads)

    modify_labels(
        service, message_id="", thread_id="t1", add=["INBOX"]
    )

    assert threads.modified == [
        ("t1", {"addLabelIds": ["INBOX"], "removeLabelIds": []})
    ]


def test_empty_string_thread_id_with_message_id_does_not_provide_two_targets():
    messages = FakeMessages(messages={"m1": {}})
    service = service_with_labels(messages=messages)

    modify_labels(
        service, message_id="m1", thread_id="", add=["INBOX"]
    )

    assert messages.modified == [
        ("m1", {"addLabelIds": ["INBOX"], "removeLabelIds": []})
    ]
