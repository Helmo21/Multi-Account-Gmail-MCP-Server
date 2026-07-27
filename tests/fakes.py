"""Offline stand-ins for the Gmail API client."""

from __future__ import annotations

import json

import httplib2
from googleapiclient.errors import HttpError


def make_http_error(status: int, reason: str = "error") -> HttpError:
    response = httplib2.Response({"status": status, "reason": reason})
    response.status = status
    content = json.dumps(
        {"error": {"code": status, "message": reason}}
    ).encode()
    return HttpError(response, content, uri="https://gmail.example/test")


class FakeRequest:
    """A request whose execute() returns a result or raises."""

    def __init__(self, result=None, error=None, errors_then=None):
        self.result = result
        self.error = error
        self.errors_then = list(errors_then or [])
        self.execute_count = 0

    def execute(self):
        self.execute_count += 1
        if self.errors_then:
            raise self.errors_then.pop(0)
        if self.error:
            raise self.error
        return self.result


class FakeMessages:
    def __init__(self, listing=None, messages=None):
        self.listing = listing if listing is not None else {"messages": []}
        self.messages = messages or {}
        self.modified: list[tuple[str, dict]] = []
        self.sent: list[dict] = []
        self.list_calls: list[dict] = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return FakeRequest(self.listing)

    def get(self, *, userId, id, **kwargs):
        if id not in self.messages:
            return FakeRequest(error=make_http_error(404, "Not Found"))
        return FakeRequest(self.messages[id])

    def modify(self, *, userId, id, body):
        self.modified.append((id, body))
        return FakeRequest({"id": id, **body})

    def send(self, *, userId, body):
        self.sent.append(body)
        return FakeRequest({"id": "sent-1", "threadId": body.get("threadId", "t-1")})


class FakeThreads:
    def __init__(self, threads=None):
        self.threads = threads or {}
        self.modified: list[tuple[str, dict]] = []

    def get(self, *, userId, id, **kwargs):
        if id not in self.threads:
            return FakeRequest(error=make_http_error(404, "Not Found"))
        return FakeRequest(self.threads[id])

    def modify(self, *, userId, id, body):
        self.modified.append((id, body))
        return FakeRequest({"id": id, **body})


class FakeDrafts:
    def __init__(self, drafts=None):
        self.drafts = drafts or {}
        self.created: list[dict] = []
        self.sent: list[dict] = []

    def create(self, *, userId, body):
        self.created.append(body)
        draft_id = f"draft-{len(self.created)}"
        return FakeRequest(
            {"id": draft_id, "message": {"id": f"msg-{len(self.created)}"}}
        )

    def send(self, *, userId, body):
        self.sent.append(body)
        if body["id"] not in self.drafts:
            return FakeRequest(error=make_http_error(404, "Not Found"))
        return FakeRequest({"id": "sent-1", "threadId": "t-1"})


class FakeLabels:
    def __init__(self, labels=None):
        self.labels = labels or []

    def list(self, *, userId):
        return FakeRequest({"labels": self.labels})


class FakeUsers:
    def __init__(self, messages=None, threads=None, drafts=None, labels=None,
                 profile="me@example.com"):
        self._messages = messages or FakeMessages()
        self._threads = threads or FakeThreads()
        self._drafts = drafts or FakeDrafts()
        self._labels = labels or FakeLabels()
        self._profile = profile

    def messages(self):
        return self._messages

    def threads(self):
        return self._threads

    def drafts(self):
        return self._drafts

    def labels(self):
        return self._labels

    def getProfile(self, *, userId):
        return FakeRequest({"emailAddress": self._profile})


class FakeGmail:
    def __init__(self, **kwargs):
        self._users = FakeUsers(**kwargs)

    def users(self):
        return self._users
