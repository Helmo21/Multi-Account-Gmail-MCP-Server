import json
import stat
import sys

import pytest

from gmail_mcp.storage import SERVICE_NAME, TokenStore, WatermarkStore


class FakeKeyring:
    """Stands in for the keyring module with a working backend."""

    def __init__(self):
        self.data: dict[tuple[str, str], str] = {}
        self.name = "FakeBackend"

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def get_password(self, service, username):
        return self.data.get((service, username))

    def delete_password(self, service, username):
        self.data.pop((service, username), None)

    def get_keyring(self):
        return self

    def __str__(self):
        return self.name


class BrokenKeyring(FakeKeyring):
    """Stands in for a machine with no usable keyring backend."""

    def set_password(self, service, username, password):
        raise RuntimeError("no backend")

    def get_password(self, service, username):
        raise RuntimeError("no backend")

    def delete_password(self, service, username):
        raise RuntimeError("no backend")


def test_roundtrips_through_keyring(tmp_path):
    fake = FakeKeyring()
    store = TokenStore(tmp_path, keyring_module=fake)
    store.set("personal", '{"token": "abc"}')

    assert store.get("personal") == '{"token": "abc"}'
    assert fake.data[(SERVICE_NAME, "personal")] == '{"token": "abc"}'
    assert not (tmp_path / "tokens.json").exists()


def test_keyring_backend_is_named(tmp_path):
    store = TokenStore(tmp_path, keyring_module=FakeKeyring())
    assert store.backend_name.startswith("keyring:")


def test_missing_token_returns_none(tmp_path):
    store = TokenStore(tmp_path, keyring_module=FakeKeyring())
    assert store.get("personal") is None


def test_delete_removes_token(tmp_path):
    store = TokenStore(tmp_path, keyring_module=FakeKeyring())
    store.set("personal", "x")
    store.delete("personal")
    assert store.get("personal") is None


def test_falls_back_to_file_when_keyring_unusable(tmp_path):
    store = TokenStore(tmp_path, keyring_module=BrokenKeyring())
    store.set("personal", '{"token": "abc"}')

    assert store.get("personal") == '{"token": "abc"}'
    assert store.backend_name.startswith("file:")
    assert json.loads((tmp_path / "tokens.json").read_text()) == {
        "personal": '{"token": "abc"}'
    }


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_fallback_file_is_owner_only(tmp_path):
    store = TokenStore(tmp_path, keyring_module=BrokenKeyring())
    store.set("personal", "x")

    mode = stat.S_IMODE((tmp_path / "tokens.json").stat().st_mode)
    assert mode == 0o600


def test_fallback_delete_removes_entry(tmp_path):
    store = TokenStore(tmp_path, keyring_module=BrokenKeyring())
    store.set("personal", "x")
    store.set("work", "y")
    store.delete("personal")

    assert store.get("personal") is None
    assert store.get("work") == "y"


def test_watermark_roundtrips(tmp_path):
    marks = WatermarkStore(tmp_path)
    assert marks.get("personal") is None

    marks.set("personal", 1700000000)
    assert marks.get("personal") == 1700000000
    assert WatermarkStore(tmp_path).get("personal") == 1700000000


def test_watermark_is_per_account(tmp_path):
    marks = WatermarkStore(tmp_path)
    marks.set("personal", 100)
    marks.set("work", 200)

    assert marks.get("personal") == 100
    assert marks.get("work") == 200


def test_watermark_survives_corrupt_file(tmp_path):
    (tmp_path / "watermarks.json").write_text("not json{{")
    marks = WatermarkStore(tmp_path)

    assert marks.get("personal") is None
    marks.set("personal", 5)
    assert marks.get("personal") == 5
