import json

import pytest
from google.auth.exceptions import RefreshError

from gmail_mcp.auth import (
    SCOPES,
    AccountMismatchError,
    CredentialMissingError,
    CredentialRevokedError,
    authenticate_account,
    load_credentials,
)
from gmail_mcp.config import Account


class FakeCreds:
    def __init__(self, valid=True, expired=False, refresh_token="rt"):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.refreshed = False

    def refresh(self, request):
        self.refreshed = True
        self.valid = True
        self.expired = False

    def to_json(self):
        return json.dumps({"token": "access", "refresh_token": self.refresh_token})


class DeadCreds(FakeCreds):
    def refresh(self, request):
        raise RefreshError("Token has been expired or revoked.")


class MemoryStore:
    def __init__(self, initial=None):
        self.data = dict(initial or {})

    def get(self, alias):
        return self.data.get(alias)

    def set(self, alias, token_json):
        self.data[alias] = token_json

    def delete(self, alias):
        self.data.pop(alias, None)


def test_scope_is_exactly_gmail_modify():
    assert SCOPES == ["https://www.googleapis.com/auth/gmail.modify"]


def test_missing_token_names_the_setup_command():
    store = MemoryStore()
    with pytest.raises(CredentialMissingError) as exc:
        load_credentials("personal", store, loader=lambda info, scopes: None)
    assert "gmail-mcp auth add personal" in str(exc.value)


def test_valid_credential_is_returned_without_refresh():
    creds = FakeCreds(valid=True, expired=False)
    store = MemoryStore({"personal": "{}"})

    result = load_credentials("personal", store, loader=lambda i, s: creds)

    assert result is creds
    assert creds.refreshed is False


def test_expired_credential_is_refreshed_and_persisted():
    creds = FakeCreds(valid=False, expired=True)
    store = MemoryStore({"personal": "{}"})

    load_credentials(
        "personal", store, loader=lambda i, s: creds, request_factory=lambda: None
    )

    assert creds.refreshed is True
    assert json.loads(store.data["personal"])["refresh_token"] == "rt"


def test_revoked_credential_names_the_alias_and_the_fix():
    store = MemoryStore({"work-sales": "{}"})
    with pytest.raises(CredentialRevokedError) as exc:
        load_credentials(
            "work-sales",
            store,
            loader=lambda i, s: DeadCreds(valid=False, expired=True),
            request_factory=lambda: None,
        )
    message = str(exc.value)
    assert "work-sales" in message
    assert "gmail-mcp auth add work-sales" in message


def test_authenticate_saves_token_when_email_matches(tmp_path):
    account = Account(alias="personal", email="me@example.com", send_policy="send")
    store = MemoryStore()
    creds = FakeCreds()

    email = authenticate_account(
        account,
        tmp_path / "client_secret.json",
        store,
        profile_lookup=lambda c: "me@example.com",
        flow_factory=lambda path, scopes: _FakeFlow(creds),
    )

    assert email == "me@example.com"
    assert json.loads(store.data["personal"])["refresh_token"] == "rt"


def test_authenticate_rejects_wrong_mailbox_and_saves_nothing(tmp_path):
    account = Account(alias="personal", email="me@example.com", send_policy="send")
    store = MemoryStore()

    with pytest.raises(AccountMismatchError) as exc:
        authenticate_account(
            account,
            tmp_path / "client_secret.json",
            store,
            profile_lookup=lambda c: "someone.else@example.com",
            flow_factory=lambda path, scopes: _FakeFlow(FakeCreds()),
        )

    message = str(exc.value)
    assert "me@example.com" in message
    assert "someone.else@example.com" in message
    assert "personal" in message
    assert store.data == {}


def test_authenticate_ignores_address_case(tmp_path):
    account = Account(alias="personal", email="Me@Example.com", send_policy="send")
    store = MemoryStore()

    authenticate_account(
        account,
        tmp_path / "client_secret.json",
        store,
        profile_lookup=lambda c: "me@example.com",
        flow_factory=lambda path, scopes: _FakeFlow(FakeCreds()),
    )

    assert "personal" in store.data


def test_oauth_flow_requests_offline_access_and_forces_consent(tmp_path):
    flow = _FakeFlow(FakeCreds())
    account = Account(alias="personal", email="me@example.com", send_policy="send")

    authenticate_account(
        account,
        tmp_path / "client_secret.json",
        MemoryStore(),
        profile_lookup=lambda c: "me@example.com",
        flow_factory=lambda path, scopes: flow,
    )

    assert flow.kwargs["access_type"] == "offline"
    assert flow.kwargs["prompt"] == "consent"
    assert flow.kwargs["port"] == 0


class _FakeFlow:
    def __init__(self, creds):
        self._creds = creds
        self.kwargs = {}

    def run_local_server(self, **kwargs):
        self.kwargs = kwargs
        return self._creds
