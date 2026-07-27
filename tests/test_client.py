import pytest

from gmail_mcp.gmail.client import (
    NotFoundError,
    RateLimitedError,
    ServiceCache,
    execute,
)
from tests.fakes import FakeRequest, make_http_error


def test_returns_result_on_success():
    assert execute(FakeRequest({"ok": True})) == {"ok": True}


def test_retries_on_rate_limit_then_succeeds():
    request = FakeRequest({"ok": True}, errors_then=[make_http_error(429)])
    slept = []

    assert execute(request, sleep=slept.append) == {"ok": True}
    assert request.execute_count == 2
    assert len(slept) == 1


def test_retries_on_server_error_then_succeeds():
    request = FakeRequest({"ok": True}, errors_then=[make_http_error(503)])
    assert execute(request, sleep=lambda s: None) == {"ok": True}
    assert request.execute_count == 2


def test_gives_up_after_three_attempts():
    request = FakeRequest(error=make_http_error(429))
    with pytest.raises(RateLimitedError):
        execute(request, sleep=lambda s: None)
    assert request.execute_count == 3


def test_backoff_grows_between_attempts():
    request = FakeRequest(error=make_http_error(500))
    slept = []
    with pytest.raises(RateLimitedError):
        execute(request, sleep=slept.append)
    assert len(slept) == 2
    assert slept[1] > slept[0]


def test_404_says_the_id_may_belong_to_another_mailbox():
    request = FakeRequest(error=make_http_error(404, "Not Found"))
    with pytest.raises(NotFoundError) as exc:
        execute(request, sleep=lambda s: None)
    assert "different" in str(exc.value).lower()
    assert request.execute_count == 1


def test_403_is_not_retried():
    request = FakeRequest(error=make_http_error(403, "Forbidden"))
    with pytest.raises(Exception):
        execute(request, sleep=lambda s: None)
    assert request.execute_count == 1


def test_service_cache_builds_once_per_alias(tmp_path):
    from gmail_mcp.config import Account, Config

    config = Config(
        accounts=(
            Account("personal", "me@example.com", "send"),
            Account("work", "w@example.com", "send"),
        )
    )
    built = []

    cache = ServiceCache(
        config,
        store=None,
        credential_loader=lambda alias, store: f"creds-{alias}",
        service_builder=lambda creds: built.append(creds) or f"svc-{creds}",
    )

    assert cache.get("personal") == "svc-creds-personal"
    assert cache.get("personal") == "svc-creds-personal"
    assert cache.get("work") == "svc-creds-work"
    assert built == ["creds-personal", "creds-work"]


def test_service_cache_rejects_unknown_alias(tmp_path):
    from gmail_mcp.config import Account, Config
    from gmail_mcp.config import UnknownAliasError

    config = Config(accounts=(Account("personal", "me@example.com", "send"),))
    cache = ServiceCache(
        config,
        store=None,
        credential_loader=lambda alias, store: "creds",
        service_builder=lambda creds: "svc",
    )

    with pytest.raises(UnknownAliasError):
        cache.get("nope")


def test_clear_forces_rebuild():
    from gmail_mcp.config import Account, Config

    config = Config(accounts=(Account("personal", "me@example.com", "send"),))
    built = []
    cache = ServiceCache(
        config,
        store=None,
        credential_loader=lambda alias, store: "c",
        service_builder=lambda creds: built.append(creds) or "svc",
    )

    cache.get("personal")
    cache.clear()
    cache.get("personal")
    assert len(built) == 2
