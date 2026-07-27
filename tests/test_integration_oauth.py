"""Opt-in end-to-end check against a real mailbox.

Runs only when GMAIL_MCP_INTEGRATION=1. Requires a config directory with
config.toml, client_secret.json, and at least one already-authenticated
account:

    GMAIL_MCP_INTEGRATION=1 \
    GMAIL_MCP_CONFIG_DIR=~/.config/gmail-mcp \
    GMAIL_MCP_ALIAS=personal \
    uv run pytest tests/test_integration_oauth.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GMAIL_MCP_INTEGRATION") != "1",
    reason="set GMAIL_MCP_INTEGRATION=1 to run against a real mailbox",
)


@pytest.fixture
def directory() -> Path:
    raw = os.environ.get("GMAIL_MCP_CONFIG_DIR")
    if not raw:
        pytest.fail("GMAIL_MCP_CONFIG_DIR is required for integration tests")
    return Path(raw).expanduser()


@pytest.fixture
def alias() -> str:
    return os.environ.get("GMAIL_MCP_ALIAS", "personal")


def test_stored_credentials_reach_the_right_mailbox(directory, alias):
    from gmail_mcp.auth import load_credentials
    from gmail_mcp.cli import lookup_profile_email
    from gmail_mcp.config import load_config
    from gmail_mcp.storage import TokenStore

    config = load_config(directory / "config.toml")
    account = config.get(alias)

    credentials = load_credentials(alias, TokenStore(directory))
    assert lookup_profile_email(credentials).lower() == account.email.lower()


def test_search_returns_results_from_the_live_account(directory, alias):
    from gmail_mcp.server import build_runtime

    runtime = build_runtime(directory)
    from gmail_mcp.gmail.search import search_messages

    results = search_messages(
        runtime.service(alias), "in:inbox", max_results=3
    )
    assert isinstance(results, list)
    for item in results:
        assert item["message_id"]
        assert "subject" in item


def test_check_inboxes_sweeps_the_live_configuration(directory):
    from gmail_mcp.check import check_inboxes
    from gmail_mcp.server import build_runtime

    runtime = build_runtime(directory)
    result = check_inboxes(
        runtime.config, runtime.services, runtime.watermarks
    )

    assert len(result["accounts"]) == len(runtime.config.accounts)
