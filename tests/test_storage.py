import json
import stat
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from gmail_mcp.storage import SERVICE_NAME, StorageError, TokenStore, WatermarkStore


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


class LateFailingKeyring(FakeKeyring):
    """Keyring that works on probe but fails on later operations.

    Simulates D-Bus restart, session lock, or similar transient failures
    that happen after successful initialization.
    """

    def __init__(self):
        super().__init__()
        self.fail_after_probe = False

    def get_password(self, service, username):
        if self.fail_after_probe and username != "__probe__":
            raise RuntimeError("keyring became unavailable")
        return super().get_password(service, username)

    def set_password(self, service, username, password):
        if self.fail_after_probe:
            raise RuntimeError("keyring became unavailable")
        return super().set_password(service, username, password)

    def delete_password(self, service, username):
        if self.fail_after_probe:
            raise RuntimeError("keyring became unavailable")
        return super().delete_password(service, username)


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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_fallback_file_is_owner_only_despite_stale_wide_open_tmp_file(tmp_path):
    # A leftover tokens.json.tmp -- exactly what a crash between write and
    # rename leaves behind -- must not carry its old, wider mode onto the
    # real file: O_CREAT does not re-chmod a file that already exists, so
    # the store must fchmod it explicitly regardless.
    tmp_file = tmp_path / "tokens.json.tmp"
    tmp_file.write_text("{}")
    tmp_file.chmod(0o644)

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


def test_token_store_handles_corrupt_file(tmp_path):
    """TokenStore gracefully handles corrupted tokens.json, like WatermarkStore does."""
    (tmp_path / "tokens.json").write_text("not json{{")
    store = TokenStore(tmp_path, keyring_module=BrokenKeyring())

    assert store.get("personal") is None
    store.set("personal", "x")
    assert store.get("personal") == "x"


def test_keyring_failure_on_get_raises_storage_error(tmp_path):
    """When keyring backend becomes unavailable, get() raises StorageError."""
    kr = LateFailingKeyring()
    store = TokenStore(tmp_path, keyring_module=kr)
    store.set("personal", "x")

    kr.fail_after_probe = True
    with pytest.raises(StorageError, match="Keyring backend unavailable"):
        store.get("personal")

    # Anti-stranding: no fallback file created
    assert not (tmp_path / "tokens.json").exists()


def test_keyring_failure_on_set_raises_storage_error(tmp_path):
    """When keyring backend becomes unavailable, set() raises StorageError."""
    kr = LateFailingKeyring()
    store = TokenStore(tmp_path, keyring_module=kr)

    kr.fail_after_probe = True
    with pytest.raises(StorageError, match="Keyring backend unavailable"):
        store.set("personal", "x")

    # Anti-stranding: no fallback file created
    assert not (tmp_path / "tokens.json").exists()


def test_concurrent_set_across_aliases_does_not_lose_updates(tmp_path):
    # Real file-backend TokenStore (BrokenKeyring forces the fallback),
    # exercised the way check.py's thread pool actually hits it: several
    # aliases' TokenStore.set calls landing at the same time because
    # their access tokens expired in the same sweep. Every alias's write
    # must survive -- unsynchronized, this shared-temp-path
    # read-modify-write can silently drop an update or raise
    # FileNotFoundError from a rename collision.
    store = TokenStore(tmp_path, keyring_module=BrokenKeyring())
    aliases = [f"acct{i}" for i in range(8)]

    def write(alias):
        store.set(alias, json.dumps({"token": alias}))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, aliases))

    for alias in aliases:
        assert store.get(alias) == json.dumps({"token": alias})


def test_keyring_failure_on_delete_raises_storage_error(tmp_path):
    """When keyring backend becomes unavailable, delete() raises StorageError."""
    kr = LateFailingKeyring()
    store = TokenStore(tmp_path, keyring_module=kr)
    store.set("personal", "x")

    kr.fail_after_probe = True
    with pytest.raises(StorageError, match="Keyring backend unavailable"):
        store.delete("personal")

    # Anti-stranding: no fallback file created
    assert not (tmp_path / "tokens.json").exists()
