"""Building outgoing mail, including correct reply threading."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from email.message import EmailMessage

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
