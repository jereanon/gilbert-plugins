"""WS RPC handler tests for the Mentra admin SPA panel.

Covers the five frame types exposed by ``MentraService.get_ws_handlers()``:
``mentra.mappings.{list,create,update,delete}`` plus the read-only
``mentra.sessions.list``. Each test drives the handler with a fake
connection carrying admin roles and asserts on the persisted row
shape + handler response envelope. A separate non-admin-rejection
test confirms the auth gate.

The fakes mirror the storage / resolver doubles from
``test_mentra_service.py`` to keep the tests self-contained — admin
panel handlers shouldn't need the full webhook stack to exercise.
"""

from __future__ import annotations

from typing import Any

import pytest

# ── Fakes ────────────────────────────────────────────────────────────


class _FakeBackend:
    """Storage backend with just the subset MentraService uses."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    async def put(
        self, collection: str, entity_id: str, data: dict[str, Any]
    ) -> None:
        self.rows[(collection, entity_id)] = dict(data)

    async def get(
        self, collection: str, entity_id: str
    ) -> dict[str, Any] | None:
        row = self.rows.get((collection, entity_id))
        return dict(row) if row is not None else None

    async def delete(self, collection: str, entity_id: str) -> None:
        self.rows.pop((collection, entity_id), None)

    async def query(self, q: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for (col, _), row in self.rows.items():
            if col != q.collection:
                continue
            keep = True
            for f in q.filters or []:
                if str(row.get(f.field) or "") != str(f.value):
                    keep = False
                    break
            if keep:
                out.append(dict(row))
        return out[: q.limit] if q.limit else out

    async def delete_query(self, q: Any) -> int:
        return 0


class _FakeStorage:
    def __init__(self) -> None:
        self._inner = _FakeBackend()

    @property
    def backend(self) -> _FakeBackend:
        return self._inner

    @property
    def raw_backend(self) -> _FakeBackend:
        return self._inner

    def create_namespaced(self, namespace: str) -> _FakeBackend:
        return self._inner


class _AdminConn:
    """Stand-in for a WsConnection that's authenticated as admin.

    The real ``WsConnection`` exposes ``roles`` as a frozenset[str]
    (via a property over ``user_ctx.roles``); the handler reads it
    via ``getattr(conn, "roles", ...)`` so any object with that
    attribute works."""

    def __init__(self, roles: frozenset[str] | set[str] = frozenset({"admin"})) -> None:
        self.roles = frozenset(roles)
        # ``user_level`` is the canonical numeric variant (0 = admin)
        # the rest of the codebase uses — set it consistent with the
        # role set so the fallback path stays sane.
        self.user_level = 0 if "admin" in self.roles else 100
        self.user_id = "admin_user"


class _UserConn:
    """Non-admin connection — must be rejected by every handler."""

    roles = frozenset({"user"})
    user_level = 100
    user_id = "rando"


# ── Fixtures / helpers ──────────────────────────────────────────────


def _make_service() -> tuple[Any, _FakeStorage]:
    """Instantiate ``MentraService`` with a bare storage capability
    wired — no need to drive ``start()`` for handler tests; the
    storage is the only dependency the handlers touch."""
    from gilbert_plugin_mentra.mentra_service import MentraService

    svc = MentraService()
    storage = _FakeStorage()
    # Wire storage directly — bypass start()/config plumbing which
    # the handlers don't depend on.
    svc._storage = storage  # type: ignore[attr-defined]  # noqa: SLF001
    return svc, storage


async def _seed_mapping(
    storage: _FakeStorage,
    *,
    entity_id: str = "map_alice",
    mentra_user_id: str = "alice@example.com",
    gilbert_user_id: str = "usr_alice",
    display_name: str = "Alice",
    roles: list[str] | None = None,
) -> dict[str, Any]:
    row = {
        "id": entity_id,
        "mentra_user_id": mentra_user_id,
        "gilbert_user_id": gilbert_user_id,
        "display_name": display_name,
        "roles": list(roles or ["user"]),
        "created_at": "2099-01-01T00:00:00Z",
    }
    await storage.backend.put("mentra_user_mappings", entity_id, row)
    return row


# ── Tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mappings_list_returns_admin_rows() -> None:
    """``mappings.list`` returns every row in the mappings collection,
    normalized to the wire shape."""
    svc, storage = _make_service()
    await _seed_mapping(storage)
    await _seed_mapping(
        storage,
        entity_id="map_bob",
        mentra_user_id="bob@example.com",
        gilbert_user_id="usr_bob",
        display_name="Bob",
        roles=["user", "admin"],
    )

    result = await svc._ws_mappings_list(  # noqa: SLF001
        _AdminConn(), {"id": "f1", "type": "mentra.mappings.list"}
    )

    assert result["type"] == "mentra.mappings.list.result"
    assert result["ref"] == "f1"
    mappings = result["mappings"]
    assert len(mappings) == 2
    ids = {m["mentra_user_id"] for m in mappings}
    assert ids == {"alice@example.com", "bob@example.com"}
    # Wire shape includes every documented field.
    for m in mappings:
        assert set(m.keys()) == {
            "id",
            "mentra_user_id",
            "gilbert_user_id",
            "display_name",
            "roles",
            "created_at",
        }


@pytest.mark.asyncio
async def test_mappings_create_persists_new_row() -> None:
    """``mappings.create`` mints an id, stamps ``created_at``, and
    persists into the ``mentra_user_mappings`` collection."""
    svc, storage = _make_service()

    result = await svc._ws_mappings_create(  # noqa: SLF001
        _AdminConn(),
        {
            "id": "f1",
            "type": "mentra.mappings.create",
            "mentra_user_id": "carol@example.com",
            "gilbert_user_id": "usr_carol",
            "display_name": "Carol",
            "roles": ["user"],
        },
    )

    assert result["type"] == "mentra.mappings.create.result"
    mapping = result["mapping"]
    assert mapping["mentra_user_id"] == "carol@example.com"
    assert mapping["gilbert_user_id"] == "usr_carol"
    assert mapping["display_name"] == "Carol"
    assert mapping["roles"] == ["user"]
    assert mapping["id"].startswith("map_")
    assert mapping["created_at"]  # non-empty ISO timestamp

    # Persisted under the same id.
    persisted = storage.backend.rows[
        ("mentra_user_mappings", mapping["id"])
    ]
    assert persisted["mentra_user_id"] == "carol@example.com"
    assert persisted["gilbert_user_id"] == "usr_carol"
    assert persisted["roles"] == ["user"]


@pytest.mark.asyncio
async def test_mappings_create_rejects_duplicate_mentra_user() -> None:
    """Two rows mapping the same Mentra account would silently
    shadow each other at lookup time — refuse the second create
    with a 409 so the admin can either edit the existing row or
    delete it first."""
    svc, storage = _make_service()
    await _seed_mapping(storage)

    result = await svc._ws_mappings_create(  # noqa: SLF001
        _AdminConn(),
        {
            "id": "f1",
            "mentra_user_id": "alice@example.com",
            "gilbert_user_id": "usr_alice_duplicate",
        },
    )
    assert result["type"] == "mentra.error"
    assert result["code"] == 409


@pytest.mark.asyncio
async def test_mappings_create_requires_both_ids() -> None:
    """Empty / missing user-id fields → 400 rather than persisting a
    half-populated row."""
    svc, _ = _make_service()
    result = await svc._ws_mappings_create(  # noqa: SLF001
        _AdminConn(),
        {
            "id": "f1",
            "mentra_user_id": "dave@example.com",
            # gilbert_user_id missing
        },
    )
    assert result["type"] == "mentra.error"
    assert result["code"] == 400


@pytest.mark.asyncio
async def test_mappings_update_patches_fields_in_place() -> None:
    """Partial update — change display_name + roles, leave the
    user-id fields untouched. Identity (``mapping_id``,
    ``created_at``) must survive the merge."""
    svc, storage = _make_service()
    await _seed_mapping(storage)

    result = await svc._ws_mappings_update(  # noqa: SLF001
        _AdminConn(),
        {
            "id": "f1",
            "type": "mentra.mappings.update",
            "mapping_id": "map_alice",
            "display_name": "Alice Updated",
            "roles": ["user", "admin"],
        },
    )

    assert result["type"] == "mentra.mappings.update.result"
    mapping = result["mapping"]
    assert mapping["display_name"] == "Alice Updated"
    assert mapping["roles"] == ["user", "admin"]
    # Untouched fields preserved.
    assert mapping["mentra_user_id"] == "alice@example.com"
    assert mapping["gilbert_user_id"] == "usr_alice"
    assert mapping["created_at"] == "2099-01-01T00:00:00Z"

    persisted = storage.backend.rows[
        ("mentra_user_mappings", "map_alice")
    ]
    assert persisted["display_name"] == "Alice Updated"
    assert persisted["roles"] == ["user", "admin"]


@pytest.mark.asyncio
async def test_mappings_update_rejects_missing_id() -> None:
    svc, _ = _make_service()
    result = await svc._ws_mappings_update(  # noqa: SLF001
        _AdminConn(), {"id": "f1", "display_name": "no id here"}
    )
    assert result["type"] == "mentra.error"
    assert result["code"] == 400


@pytest.mark.asyncio
async def test_mappings_update_404_for_unknown_id() -> None:
    svc, _ = _make_service()
    result = await svc._ws_mappings_update(  # noqa: SLF001
        _AdminConn(),
        {
            "id": "f1",
            "mapping_id": "map_does_not_exist",
            "display_name": "x",
        },
    )
    assert result["type"] == "mentra.error"
    assert result["code"] == 404


@pytest.mark.asyncio
async def test_mappings_delete_removes_row() -> None:
    svc, storage = _make_service()
    await _seed_mapping(storage)
    assert ("mentra_user_mappings", "map_alice") in storage.backend.rows

    result = await svc._ws_mappings_delete(  # noqa: SLF001
        _AdminConn(),
        {
            "id": "f1",
            "type": "mentra.mappings.delete",
            "mapping_id": "map_alice",
        },
    )

    assert result == {
        "type": "mentra.mappings.delete.result",
        "ref": "f1",
        "status": "ok",
    }
    assert (
        "mentra_user_mappings",
        "map_alice",
    ) not in storage.backend.rows


@pytest.mark.asyncio
async def test_mappings_delete_rejects_missing_id() -> None:
    svc, _ = _make_service()
    result = await svc._ws_mappings_delete(  # noqa: SLF001
        _AdminConn(), {"id": "f1"}
    )
    assert result["type"] == "mentra.error"
    assert result["code"] == 400


@pytest.mark.asyncio
async def test_sessions_list_returns_live_sessions_with_capabilities() -> None:
    """The session table shows every live session keyed by
    ``session_id`` with the Mentra/Gilbert ids, the connected_at
    timestamp the service stamps on admit, and the capabilities the
    cloud advertised. Sorted most-recent first."""
    from gilbert.interfaces.mentra import GlassesCapabilities

    svc, _ = _make_service()

    class _FakeSession:
        def __init__(
            self,
            *,
            session_id: str,
            user_id: str,
            gilbert_user_id: str,
            caps: GlassesCapabilities | None,
        ) -> None:
            self.session_id = session_id
            self.user_id = user_id
            self.gilbert_user_id = gilbert_user_id
            self.capabilities = caps

    svc._sessions["sess_old"] = _FakeSession(  # type: ignore[assignment]  # noqa: SLF001
        session_id="sess_old",
        user_id="alice@example.com",
        gilbert_user_id="usr_alice",
        caps=GlassesCapabilities(
            model_name="Even Realities G1",
            has_display=True,
            has_speaker=True,
            has_microphone=True,
        ),
    )
    svc._sessions["sess_new"] = _FakeSession(  # type: ignore[assignment]  # noqa: SLF001
        session_id="sess_new",
        user_id="bob@example.com",
        gilbert_user_id="usr_bob",
        caps=None,
    )
    svc._connected_at["sess_old"] = "2025-01-01T10:00:00Z"  # noqa: SLF001
    svc._connected_at["sess_new"] = "2025-01-01T12:00:00Z"  # noqa: SLF001

    result = await svc._ws_sessions_list(  # noqa: SLF001
        _AdminConn(), {"id": "f1", "type": "mentra.sessions.list"}
    )

    assert result["type"] == "mentra.sessions.list.result"
    sessions = result["sessions"]
    assert [s["session_id"] for s in sessions] == ["sess_new", "sess_old"]

    new_session = sessions[0]
    assert new_session["mentra_user_id"] == "bob@example.com"
    assert new_session["gilbert_user_id"] == "usr_bob"
    assert new_session["connected_at"] == "2025-01-01T12:00:00Z"
    assert new_session["capabilities"] == {}  # no caps received yet

    old_session = sessions[1]
    assert old_session["capabilities"]["modelName"] == "Even Realities G1"
    assert old_session["capabilities"]["hasDisplay"] is True
    assert old_session["capabilities"]["hasCamera"] is False


# ── Auth gate ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler_name",
    [
        "_ws_mappings_list",
        "_ws_mappings_create",
        "_ws_mappings_update",
        "_ws_mappings_delete",
        "_ws_sessions_list",
    ],
)
async def test_non_admin_rejected_by_every_handler(
    handler_name: str,
) -> None:
    """Every admin handler must refuse a non-admin connection with a
    structured 403 error — the panel is operator-only and we don't
    want regular users probing user-mapping data."""
    svc, storage = _make_service()
    await _seed_mapping(storage)  # ensure a row exists for the list/update paths
    handler = getattr(svc, handler_name)

    result = await handler(
        _UserConn(),
        {
            "id": "f1",
            "id_": "map_alice",  # unused — handler should reject before this matters
            "mentra_user_id": "anyone@example.com",
            "gilbert_user_id": "usr_x",
        },
    )

    assert result["type"] == "mentra.error"
    assert result["code"] == 403
    assert "admin" in result["message"].lower()
