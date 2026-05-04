"""Tests for the credential WS RPC handlers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gilbert_plugin_browser.browser_service import BrowserService
from gilbert_plugin_browser.credentials import (
    COLLECTION,
    BrowserCredential,
    CredentialStore,
)
from gilbert_plugin_browser.tests.test_credentials import FakeStorage  # type: ignore


def _conn(user_id: str) -> MagicMock:
    conn = MagicMock()
    conn.user_id = user_id
    return conn


@pytest.fixture
async def service(tmp_path: Path) -> BrowserService:
    storage = FakeStorage()
    svc = BrowserService(data_dir=tmp_path, storage=storage)
    svc._creds = CredentialStore(storage=storage, key_path=tmp_path / "fernet.key")
    await svc._creds.start()
    return svc


@pytest.mark.asyncio
async def test_save_then_list_omits_password(service: BrowserService):
    save = await service._ws_credentials_save(
        _conn("u1"),
        {
            "id": "1",
            "type": "browser.credentials.save",
            "site": "x.test",
            "label": "Main",
            "username": "alice",
            "password": "s3cret",
            "login_url": "https://x.test/login",
        },
    )
    assert save["ok"]
    cred_id = save["id"]

    out = await service._ws_credentials_list(_conn("u1"), {"id": "2"})
    assert out["type"] == "browser.credentials.list.result"
    assert len(out["credentials"]) == 1
    cred = out["credentials"][0]
    assert cred["id"] == cred_id
    assert cred["username"] == "alice"
    # Password is NEVER returned.
    assert "password" not in cred


@pytest.mark.asyncio
async def test_save_requires_site_and_username(service: BrowserService):
    out = await service._ws_credentials_save(
        _conn("u1"),
        {"id": "1", "site": "", "username": "alice", "password": "p"},
    )
    assert out["type"] == "gilbert.error"
    assert "site" in out["error"].lower()


@pytest.mark.asyncio
async def test_save_requires_password_on_create(service: BrowserService):
    out = await service._ws_credentials_save(
        _conn("u1"),
        {"id": "1", "site": "x", "username": "a", "password": ""},
    )
    assert out["type"] == "gilbert.error"
    assert "password" in out["error"].lower()


@pytest.mark.asyncio
async def test_save_update_keeps_existing_password_when_blank(
    service: BrowserService,
):
    saved = await service._creds.save(  # type: ignore[union-attr]
        BrowserCredential(
            user_id="u1",
            site="x",
            label="L",
            username="a",
            password="original",
            login_url="https://x.test/login",
        )
    )
    out = await service._ws_credentials_save(
        _conn("u1"),
        {
            "id": "1",
            "credential_id": saved.id,
            "site": "x",
            "label": "L2",
            "username": "a",
            "password": "",  # blank → keep existing
        },
    )
    assert out["ok"]
    loaded = await service._creds.get(saved.id, "u1")  # type: ignore[union-attr]
    assert loaded.password == "original"
    assert loaded.label == "L2"


@pytest.mark.asyncio
async def test_unauthenticated_calls_rejected(service: BrowserService):
    out = await service._ws_credentials_list(_conn(""), {"id": "1"})
    assert out["type"] == "gilbert.error"
    assert out["code"] == 401


@pytest.mark.asyncio
async def test_delete_is_idempotent_for_missing_ids(service: BrowserService):
    out = await service._ws_credentials_delete(
        _conn("u1"), {"id": "1", "credential_id": "does-not-exist"}
    )
    assert out["ok"]


@pytest.mark.asyncio
async def test_user_cannot_delete_anothers_credential(service: BrowserService):
    saved = await service._creds.save(  # type: ignore[union-attr]
        BrowserCredential(
            user_id="u1", site="x", label="", username="a", password="p"
        )
    )
    out = await service._ws_credentials_delete(
        _conn("u2"), {"id": "1", "credential_id": saved.id}
    )
    assert out["type"] == "gilbert.error"
    assert out["code"] == 403
    # Original cred still readable by owner.
    cred = await service._creds.get(saved.id, "u1")  # type: ignore[union-attr]
    assert cred.username == "a"


@pytest.mark.asyncio
async def test_get_ws_handlers_includes_credentials_and_vnc(service: BrowserService):
    handlers = service.get_ws_handlers()
    assert {
        "browser.credentials.list",
        "browser.credentials.save",
        "browser.credentials.delete",
        "browser.vnc.start",
        "browser.vnc.stop",
        "browser.vnc.list",
    } <= set(handlers.keys())


# Touch COLLECTION so the import isn't flagged as unused — useful as a
# sentinel that the module reorganization didn't drop the constant.
assert COLLECTION == "browser_credentials"
