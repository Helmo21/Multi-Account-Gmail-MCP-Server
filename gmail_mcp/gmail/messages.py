"""Reading messages and threads, and turning MIME into readable text."""

from __future__ import annotations

import base64
import binascii
import html
import re

from gmail_mcp.gmail.client import execute
from gmail_mcp.gmail.search import header, summarise

BODY_CHAR_LIMIT = 50_000

_QUOTE_MARKERS = (
    re.compile(r"^On .*wrote:\s*$", re.IGNORECASE),
    re.compile(r"^-+\s*Forwarded message\s*-+", re.IGNORECASE),
    re.compile(r"^-+\s*Original Message\s*-+", re.IGNORECASE),
)


def _decode(data: str) -> str:
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded.encode()).decode(
            "utf-8", errors="replace"
        )
    except binascii.Error:
        return ""


def html_to_text(source: str) -> str:
    """Crude but predictable HTML flattening for display."""
    text = re.sub(
        r"<(script|style)[^>]*>.*?</\1>", "", source,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def collapse_quoted(text: str) -> str:
    """Drop quoted replies and forwarded tails."""
    kept: list[str] = []
    for line in text.splitlines():
        if any(marker.match(line.strip()) for marker in _QUOTE_MARKERS):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _walk(payload: dict):
    yield payload
    for child in payload.get("parts", []) or []:
        yield from _walk(child)


def _part_text(node: dict) -> str:
    data = node.get("body", {}).get("data")
    return _decode(data) if data else ""


def _extract_body_raw(payload: dict) -> str:
    """Extract the readable body without truncation, preferring text/plain over HTML."""
    plain, rich = "", ""
    for node in _walk(payload):
        if node.get("filename"):
            continue
        mime = node.get("mimeType", "")
        if mime == "text/plain" and not plain:
            plain = _part_text(node)
        elif mime == "text/html" and not rich:
            rich = _part_text(node)

    return plain or (html_to_text(rich) if rich else "")


def _apply_truncation(body: str) -> str:
    """Apply character limit and append truncation notice if needed."""
    if len(body) > BODY_CHAR_LIMIT:
        return body[:BODY_CHAR_LIMIT] + "\n\n[body truncated]"
    return body


def extract_body(payload: dict) -> str:
    """Return the readable body, preferring text/plain over HTML."""
    return _apply_truncation(_extract_body_raw(payload))


def list_attachments(payload: dict) -> list[dict]:
    """Report attachment names and sizes. Contents are never fetched."""
    return [
        {
            "filename": node["filename"],
            "mime_type": node.get("mimeType", ""),
            "size": node.get("body", {}).get("size", 0),
        }
        for node in _walk(payload)
        if node.get("filename")
    ]


def _render(message: dict, *, collapse: bool) -> dict:
    payload = message.get("payload", {})
    record = summarise(message)
    if collapse:
        # Extract raw, collapse, then apply limit
        raw_body = _extract_body_raw(payload)
        collapsed_body = collapse_quoted(raw_body)
        record["body"] = _apply_truncation(collapsed_body)
    else:
        # Extract with limit already applied
        record["body"] = extract_body(payload)
    record["message_id_header"] = header(payload, "Message-ID")
    record["attachments"] = list_attachments(payload)
    return record


def read_message(service, message_id: str) -> dict:
    """Fetch one message in full."""
    raw = execute(
        service.users().messages().get(
            userId="me", id=message_id, format="full"
        )
    )
    return _render(raw, collapse=False)


def read_thread(service, thread_id: str) -> dict:
    """Fetch a whole thread, oldest first, with quoted text collapsed."""
    raw = execute(
        service.users().threads().get(
            userId="me", id=thread_id, format="full"
        )
    )
    return {
        "thread_id": raw.get("id", thread_id),
        "messages": [
            _render(m, collapse=True) for m in raw.get("messages", [])
        ],
    }
