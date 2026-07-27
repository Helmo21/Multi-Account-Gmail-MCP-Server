import pytest

from gmail_mcp.config import (
    Config,
    ConfigError,
    UnknownAliasError,
    load_config,
)

VALID = """
[[accounts]]
alias = "personal"
email = "me@example.com"
send_policy = "confirm"

[[accounts]]
alias = "work-sales"
email = "sales@example.com"
send_policy = "draft_only"
"""


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_loads_accounts_in_order(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert cfg.aliases == ["personal", "work-sales"]
    assert cfg.accounts[1].email == "sales@example.com"
    assert cfg.accounts[1].send_policy == "draft_only"


def test_get_resolves_alias(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert cfg.get("personal").email == "me@example.com"


def test_unknown_alias_lists_valid_aliases(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    with pytest.raises(UnknownAliasError) as exc:
        cfg.get("nope")
    message = str(exc.value)
    assert "nope" in message
    assert "personal" in message
    assert "work-sales" in message


def test_missing_send_policy_is_rejected(tmp_path):
    text = '[[accounts]]\nalias = "a"\nemail = "a@example.com"\n'
    with pytest.raises(ConfigError, match="send_policy"):
        load_config(write(tmp_path, text))


def test_unrecognised_send_policy_is_rejected(tmp_path):
    text = (
        '[[accounts]]\nalias = "a"\nemail = "a@example.com"\n'
        'send_policy = "yolo"\n'
    )
    with pytest.raises(ConfigError, match="yolo"):
        load_config(write(tmp_path, text))


def test_duplicate_alias_is_rejected(tmp_path):
    text = (
        '[[accounts]]\nalias = "a"\nemail = "a@example.com"\n'
        'send_policy = "send"\n'
        '[[accounts]]\nalias = "a"\nemail = "b@example.com"\n'
        'send_policy = "send"\n'
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(write(tmp_path, text))


def test_empty_config_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="no accounts"):
        load_config(write(tmp_path, ""))


def test_missing_file_names_the_path(tmp_path):
    with pytest.raises(ConfigError, match="config.toml"):
        load_config(tmp_path / "config.toml")


def test_config_is_immutable(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert isinstance(cfg, Config)
    with pytest.raises(Exception):
        cfg.accounts[0].alias = "changed"
