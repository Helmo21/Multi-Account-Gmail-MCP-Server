"""Search and the compact message summary shape."""

from __future__ import annotations

from gmail_mcp.gmail.client import NotFoundError, execute

DEFAULT_MAX_RESULTS = 20
MAX_RESULTS_CAP = 100
METADATA_HEADERS = ["From", "To", "Subject", "Date"]


def header(payload: dict, name: str) -> str:
    """Return a header value, matching the name case-insensitively."""
    wanted = name.lower()
    for entry in payload.get("headers", []):
        if entry.get("name", "").lower() == wanted:
            return entry.get("value", "")
    return ""


def summarise(message: dict) -> dict:
    """Reduce a Gmail message resource to the compact shape tools return."""
    payload = message.get("payload", {})
    labels = message.get("labelIds", [])
    return {
        "message_id": message.get("id", ""),
        "thread_id": message.get("threadId", ""),
        "from": header(payload, "From"),
        "to": header(payload, "To"),
        "subject": header(payload, "Subject"),
        "date": header(payload, "Date"),
        "snippet": message.get("snippet", ""),
        "unread": "UNREAD" in labels,
        "labels": list(labels),
    }


def fetch_metadata(service, message_ids: list[str]) -> list[dict]:
    """Fetch metadata for each ID, skipping any that have vanished.

    Fetches are sequential on purpose. googleapiclient service objects
    are not thread-safe, so parallelism happens one level up, across
    accounts, where each account has its own service.
    """
    out = []
    for message_id in message_ids:
        request = service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=METADATA_HEADERS,
        )
        try:
            out.append(execute(request))
        except NotFoundError:
            continue
    return out


def search_messages(
    service,
    query: str,
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    include_spam_trash: bool = False,
) -> list[dict]:
    """Run a Gmail query and return compact summaries."""
    if max_results < 1:
        raise ValueError("max_results must be at least 1.")

    capped = min(max_results, MAX_RESULTS_CAP)
    listing = execute(
        service.users().messages().list(
            userId="me",
            q=query,
            maxResults=capped,
            includeSpamTrash=include_spam_trash,
        )
    )

    ids = [m["id"] for m in listing.get("messages", [])]
    return [summarise(m) for m in fetch_metadata(service, ids)]
