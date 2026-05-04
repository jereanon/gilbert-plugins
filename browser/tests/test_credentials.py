"""Tests for the encrypted-at-rest browser credential store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from gilbert_plugin_browser.credentials import (
    COLLECTION,
    BrowserCredential,
    CredentialStore,
)

from gilbert.interfaces.storage import FilterOp


class FakeStorage:
    """In-memory fake of the StorageBackend bits CredentialStore uses."""

    def __init__(self) -> None:
        self._collections: dict[str, dict[str, dict[str, Any]]] = {}

    async def put(self, collection: str, entity_id: str, data: dict[str, Any]) -> None:
        self._collections.setdefault(collection, {})[entity_id] = dict(data)

    async def get(self, collection: str, entity_id: str) -> dict[str, Any] | None:
        return self._collections.get(collection, {}).get(entity_id)

    async def delete(self, collection: str, entity_id: str) -> None:
        self._collections.get(collection, {}).pop(entity_id, None)

    async def query(self, query):
        rows = list(self._collections.get(query.collection, {}).values())
        for f in query.filters:
            if f.op is FilterOp.EQ:
                rows = [r for r in rows if r.get(f.field) == f.value]
        return rows


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.mark.asyncio
async def test_credential_round_trip(tmp_path: Path, fake_storage: FakeStorage):
    store = CredentialStore(storage=fake_storage, key_path=tmp_path / "fernet.key")
    await store.start()
    saved = await store.save(
        BrowserCredential(
            user_id="u1",
            site="example.com",
            label="Main",
            username="alice",
            password="s3cret",
            login_url="https://example.com/login",
        )
    )
    assert saved.id
    loaded = await store.get(saved.id, "u1")
    assert loaded.username == "alice"
    assert loaded.password == "s3cret"
    assert loaded.login_url == "https://example.com/login"


@pytest.mark.asyncio
async def test_credential_persists_across_restart(
    tmp_path: Path, fake_storage: FakeStorage
):
    key = tmp_path / "fernet.key"
    s1 = CredentialStore(storage=fake_storage, key_path=key)
    await s1.start()
    saved = await s1.save(
        BrowserCredential(
            user_id="u1", site="x", label="", username="alice", password="p"
        )
    )

    # Re-instantiate; same key file means same Fernet → can decrypt.
    s2 = CredentialStore(storage=fake_storage, key_path=key)
    await s2.start()
    loaded = await s2.get(saved.id, "u1")
    assert loaded.password == "p"


@pytest.mark.asyncio
async def test_users_cannot_read_each_others_credentials(
    tmp_path: Path, fake_storage: FakeStorage
):
    store = CredentialStore(storage=fake_storage, key_path=tmp_path / "fernet.key")
    await store.start()
    saved = await store.save(
        BrowserCredential(
            user_id="u1", site="x", label="", username="a", password="p"
        )
    )
    with pytest.raises(PermissionError):
        await store.get(saved.id, "u2")


@pytest.mark.asyncio
async def test_list_for_user_strips_passwords(
    tmp_path: Path, fake_storage: FakeStorage
):
    store = CredentialStore(storage=fake_storage, key_path=tmp_path / "fernet.key")
    await store.start()
    await store.save(
        BrowserCredential(
            user_id="u1", site="x", label="A", username="alice", password="p"
        )
    )
    await store.save(
        BrowserCredential(
            user_id="u1", site="y", label="B", username="bob", password="q"
        )
    )
    await store.save(
        BrowserCredential(
            user_id="u2", site="z", label="C", username="carol", password="r"
        )
    )

    creds = await store.list_for_user("u1")
    assert len(creds) == 2
    assert {c.username for c in creds} == {"alice", "bob"}
    # Passwords NEVER round-trip through the list endpoint.
    assert all(c.password == "" for c in creds)


@pytest.mark.asyncio
async def test_delete_requires_ownership(
    tmp_path: Path, fake_storage: FakeStorage
):
    store = CredentialStore(storage=fake_storage, key_path=tmp_path / "fernet.key")
    await store.start()
    saved = await store.save(
        BrowserCredential(
            user_id="u1", site="x", label="", username="a", password="p"
        )
    )
    with pytest.raises(PermissionError):
        await store.delete(saved.id, "u2")
    # Original is still there.
    assert await store.get(saved.id, "u1") is not None
    # Owner-delete works.
    await store.delete(saved.id, "u1")
    with pytest.raises(KeyError):
        await store.get(saved.id, "u1")


@pytest.mark.asyncio
async def test_key_file_is_mode_0600(tmp_path: Path, fake_storage: FakeStorage):
    import stat

    key_path = tmp_path / "fernet.key"
    store = CredentialStore(storage=fake_storage, key_path=key_path)
    await store.start()
    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert mode == 0o600


@pytest.mark.asyncio
async def test_storage_rows_do_not_contain_plaintext_password(
    tmp_path: Path, fake_storage: FakeStorage
):
    store = CredentialStore(storage=fake_storage, key_path=tmp_path / "fernet.key")
    await store.start()
    await store.save(
        BrowserCredential(
            user_id="u1", site="x", label="", username="alice", password="hunter2"
        )
    )
    rows = list(fake_storage._collections[COLLECTION].values())
    serialized = repr(rows)
    assert "hunter2" not in serialized
    assert "alice" not in serialized
