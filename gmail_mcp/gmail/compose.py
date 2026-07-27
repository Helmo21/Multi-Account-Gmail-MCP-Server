"""Building outgoing mail, including correct reply threading."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from email.message import EmailMessage
from enum import StrEnum
from types import MappingProxyType

from gmail_mcp.config import VALID_SEND_POLICIES, Account
from gmail_mcp.gmail.client import execute
from gmail_mcp.gmail.search import header

Recipients = str | list[str]


@dataclass(frozen=True)
class ReplyContext:
    thread_id: str
    message_id_header: str
    references: str
    subject: str


def _join(value: Recipients | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    joined = ", ".join(v.strip() for v in value if v and v.strip())
    return joined or None


def build_mime(
    *,
    to: Recipients,
    subject: str,
    body: str,
    cc: Recipients | None = None,
    bcc: Recipients | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> EmailMessage:
    """Assemble an outgoing message."""
    recipients = _join(to)
    if not recipients:
        raise ValueError("At least one recipient is required.")

    message = EmailMessage()
    message["To"] = recipients
    message["Subject"] = subject

    if cc_value := _join(cc):
        message["Cc"] = cc_value
    if bcc_value := _join(bcc):
        message["Bcc"] = bcc_value

    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references

    message.set_content(body)
    return message


def encode(message: EmailMessage) -> str:
    """Serialise to the base64url form the Gmail API expects."""
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def reply_subject(subject: str) -> str:
    """Prefix with Re: unless it already is one."""
    if subject.strip().lower().startswith("re:"):
        return subject
    return f"Re: {subject}"


def fetch_reply_context(service, message_id: str) -> ReplyContext:
    """Read the headers needed to make a reply thread correctly.

    Without In-Reply-To and References, replies arrive as orphan
    messages in the recipient's client even though Gmail groups them
    correctly on the sender's side.
    """
    raw = execute(
        service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["Message-ID", "References", "Subject"],
        )
    )
    payload = raw.get("payload", {})
    parent = header(payload, "Message-ID")
    existing = header(payload, "References")

    return ReplyContext(
        thread_id=raw.get("threadId", ""),
        message_id_header=parent,
        references=f"{existing} {parent}".strip(),
        subject=header(payload, "Subject"),
    )


class SendAction(StrEnum):
    SEND = "send"
    DRAFT = "draft"


_GATE = MappingProxyType({
    ("send", False): SendAction.SEND,
    ("send", True): SendAction.SEND,
    ("confirm", False): SendAction.DRAFT,
    ("confirm", True): SendAction.SEND,
    ("draft_only", False): SendAction.DRAFT,
    ("draft_only", True): SendAction.DRAFT,
})

assert {policy for policy, _ in _GATE} == VALID_SEND_POLICIES, (
    "_GATE is out of sync with gmail_mcp.config.VALID_SEND_POLICIES"
)

_NOTES = MappingProxyType({
    "confirm": (
        "Account policy is 'confirm', so this was saved as a draft. "
        "Call again with confirm=true to send it."
    ),
    "draft_only": (
        "Account policy is 'draft_only', so this was saved as a draft "
        "and will never be sent by this server."
    ),
    "send": "Saved as a draft because a draft was explicitly requested.",
})


def resolve_send_action(policy: str, confirm: bool) -> SendAction:
    """Decide whether to transmit, from configuration alone.

    The decision lives here rather than in prompt text so it holds
    regardless of what the model has been told in conversation.

    ``confirm`` must be an actual ``bool``. It is deliberately not
    coerced: a loosely-typed caller (an MCP tool argument filled in by
    a model, for instance) could pass a truthy-but-wrong value like
    the string ``"false"``, and coercion would silently transmit mail
    that was never actually confirmed. Every other unexpected input to
    this function fails closed (an unknown policy raises); this keeps
    that property for ``confirm`` too, rather than treating it as the
    one fail-open input in the gate.
    """
    if not isinstance(confirm, bool):
        raise ValueError(
            f"confirm must be a boolean, got {type(confirm).__name__}. "
            "Refusing to interpret it as a confirmation."
        )
    try:
        return _GATE[(policy, confirm)]
    except KeyError:
        raise ValueError(
            f"Unknown send policy {policy!r}. "
            "Must be one of: send, confirm, draft_only."
        ) from None


def _create_draft(service, raw: str, thread_id: str | None) -> dict:
    message: dict = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    return execute(
        service.users().drafts().create(
            userId="me", body={"message": message}
        )
    )


def _send_raw(service, raw: str, thread_id: str | None) -> dict:
    body: dict = {"raw": raw}
    if thread_id:
        body["threadId"] = thread_id
    return execute(service.users().messages().send(userId="me", body=body))


def compose_and_deliver(
    service,
    account: Account,
    *,
    to: Recipients,
    subject: str | None,
    body: str,
    cc: Recipients | None = None,
    bcc: Recipients | None = None,
    reply_to_message_id: str | None = None,
    confirm: bool = False,
    force_draft: bool = False,
) -> dict:
    """Compose a message and either send it or save it as a draft."""
    context = (
        fetch_reply_context(service, reply_to_message_id)
        if reply_to_message_id
        else None
    )

    if subject is None:
        if context is None:
            raise ValueError(
                "A subject is required unless replying to a message."
            )
        subject = reply_subject(context.subject)

    mime = build_mime(
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        in_reply_to=context.message_id_header if context else None,
        references=context.references if context else None,
    )
    raw = encode(mime)
    thread_id = context.thread_id if context else None

    action = resolve_send_action(account.send_policy, confirm)
    overridden_by_force_draft = force_draft and action is SendAction.SEND
    if force_draft:
        action = SendAction.DRAFT

    if action is SendAction.SEND:
        sent = _send_raw(service, raw, thread_id)
        return {
            "action": "sent",
            "policy": account.send_policy,
            "account": account.alias,
            "message_id": sent.get("id", ""),
            "thread_id": sent.get("threadId", ""),
            "note": "Message sent.",
        }

    draft = _create_draft(service, raw, thread_id)
    note = (
        _NOTES["send"]
        if overridden_by_force_draft
        else _NOTES[account.send_policy]
    )
    return {
        "action": "drafted",
        "policy": account.send_policy,
        "account": account.alias,
        "draft_id": draft.get("id", ""),
        "note": note,
    }


def send_existing_draft(
    service, account: Account, draft_id: str, *, confirm: bool = False
) -> dict:
    """Send a previously created draft, subject to the same gate."""
    action = resolve_send_action(account.send_policy, confirm)

    if action is SendAction.DRAFT:
        return {
            "action": "drafted",
            "policy": account.send_policy,
            "account": account.alias,
            "draft_id": draft_id,
            "note": _NOTES[account.send_policy],
        }

    sent = execute(
        service.users().drafts().send(userId="me", body={"id": draft_id})
    )
    return {
        "action": "sent",
        "policy": account.send_policy,
        "account": account.alias,
        "message_id": sent.get("id", ""),
        "thread_id": sent.get("threadId", ""),
        "note": "Draft sent.",
    }
