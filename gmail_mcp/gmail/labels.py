"""Label listing and modification.

Marking read or unread and archiving are label operations: they add or
remove the UNREAD and INBOX system labels. One tool covers all of it.
"""

from __future__ import annotations

from collections.abc import Sequence

from gmail_mcp.gmail.client import GmailError, execute


def list_labels(service) -> list[dict]:
    """Return every label on the account."""
    raw = execute(service.users().labels().list(userId="me"))
    return [
        {
            "id": label.get("id", ""),
            "name": label.get("name", ""),
            "type": label.get("type", ""),
        }
        for label in raw.get("labels", [])
    ]


def _resolve_names_with_mapping(
    names: Sequence[str], by_name: dict[str, str], all_labels: list[dict]
) -> list[str]:
    """Resolve label names to IDs using a pre-built name-to-ID mapping."""
    resolved = []
    for name in names:
        label_id = by_name.get(name.strip().lower())
        if label_id is None:
            available = ", ".join(sorted(label["name"] for label in all_labels))
            raise GmailError(
                f"No label named {name!r} on this account. Available: {available}."
            )
        resolved.append(label_id)
    return resolved


def resolve_label_ids(service, names: Sequence[str]) -> list[str]:
    """Map label names to Gmail label IDs, case-insensitively."""
    if not names:
        return []

    labels = list_labels(service)
    by_name = {label["name"].lower(): label["id"] for label in labels}
    return _resolve_names_with_mapping(names, by_name, labels)


def modify_labels(
    service,
    *,
    message_id: str | None = None,
    thread_id: str | None = None,
    add: Sequence[str] = (),
    remove: Sequence[str] = (),
) -> dict:
    """Add and remove labels on exactly one message or thread."""
    if bool(message_id) == bool(thread_id):
        raise ValueError(
            "Pass exactly one of message_id or thread_id."
        )
    if not add and not remove:
        raise ValueError("Pass at least one label to add or remove.")

    labels = list_labels(service)
    by_name = {label["name"].lower(): label["id"] for label in labels}

    body = {
        "addLabelIds": _resolve_names_with_mapping(add, by_name, labels),
        "removeLabelIds": _resolve_names_with_mapping(remove, by_name, labels),
    }

    if message_id:
        execute(
            service.users().messages().modify(
                userId="me", id=message_id, body=body
            )
        )
        target, target_id = "message", message_id
    else:
        execute(
            service.users().threads().modify(
                userId="me", id=thread_id, body=body
            )
        )
        target, target_id = "thread", thread_id

    return {
        "target": target,
        "id": target_id,
        "added": body["addLabelIds"],
        "removed": body["removeLabelIds"],
    }
