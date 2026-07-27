import json

import pytest

from gmail_mcp.cli import cmd_auth_list, cmd_auth_remove, cmd_doctor, main

CONFIG = """
[[accounts]]
alias = "personal"
email = "me@example.com"
send_policy = "confirm"

[[accounts]]
alias = "work"
email = "w@example.com"
send_policy = "send"
"""


@pytest.fixture
def home(tmp_path):
    (tmp_path / "config.toml").write_text(CONFIG)
    return tmp_path


def test_auth_list_marks_authenticated_accounts(home, capsys):
    (home / "tokens.json").write_text(json.dumps({"personal": "{}"}))

    code = cmd_auth_list(home, keyring_module=_BrokenKeyring())
    out = capsys.readouterr().out

    assert code == 0
    assert "personal" in out
    assert "work" in out
    assert "not authenticated" in out


def test_auth_list_reports_a_missing_config(tmp_path, capsys):
    code = cmd_auth_list(tmp_path, keyring_module=_BrokenKeyring())

    assert code == 1
    assert "config.toml" in capsys.readouterr().out


def test_auth_remove_deletes_the_token(home, capsys):
    (home / "tokens.json").write_text(json.dumps({"personal": "{}"}))

    code = cmd_auth_remove(home, "personal", keyring_module=_BrokenKeyring())
    out = capsys.readouterr().out

    assert code == 0
    assert json.loads((home / "tokens.json").read_text()) == {}
    assert "personal" in out


def test_auth_remove_rejects_an_unknown_alias(home, capsys):
    code = cmd_auth_remove(home, "nope", keyring_module=_BrokenKeyring())

    assert code == 1
    assert "personal" in capsys.readouterr().out


def test_doctor_reports_every_account_and_the_backend(home, capsys):
    addresses = {"personal": "me@example.com", "work": "w@example.com"}

    def profile(alias, store, directory):
        return addresses[alias]

    code = cmd_doctor(home, probe=profile, keyring_module=_BrokenKeyring())
    out = capsys.readouterr().out

    assert "personal" in out
    assert "work" in out
    assert "file:" in out
    assert code == 0


def test_doctor_flags_a_broken_account_without_failing_the_others(home, capsys):
    def probe(alias, store, directory):
        if alias == "work":
            raise RuntimeError("token revoked, run: gmail-mcp auth add work")
        return "me@example.com"

    code = cmd_doctor(home, probe=probe, keyring_module=_BrokenKeyring())
    out = capsys.readouterr().out

    assert code == 1
    assert "revoked" in out
    assert "personal" in out


def test_doctor_flags_a_mailbox_that_does_not_match_config(home, capsys):
    def probe(alias, store, directory):
        return "stranger@example.com"

    code = cmd_doctor(home, probe=probe, keyring_module=_BrokenKeyring())
    out = capsys.readouterr().out

    assert code == 1
    assert "stranger@example.com" in out


def test_main_rejects_an_unknown_command(capsys):
    with pytest.raises(SystemExit):
        main(["nonsense"])


class _BrokenKeyring:
    def get_password(self, service, username):
        raise RuntimeError("no backend")

    def set_password(self, service, username, password):
        raise RuntimeError("no backend")

    def delete_password(self, service, username):
        raise RuntimeError("no backend")

    def get_keyring(self):
        return self
