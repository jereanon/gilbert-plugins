"""Tests for the ngrok tunnel backend."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from gilbert_plugin_ngrok.ngrok_tunnel import NgrokTunnel


def _install_fake_pyngrok(
    connect_side_effect: list,
    kill: MagicMock | None = None,
) -> tuple[ModuleType, MagicMock]:
    """Register a stub ``pyngrok`` package so ``NgrokTunnel.connect``
    imports our fake instead of hitting the real library. Returns the
    stub ``ngrok`` submodule and the ``connect`` mock so tests can
    assert call counts.

    Calling this more than once in a single test preserves the
    existing ``PyngrokNgrokHTTPError`` class — otherwise each install
    would mint a new class object and callers holding the old one
    would produce errors that ``except PyngrokNgrokHTTPError`` in the
    backend wouldn't catch."""
    existing_exc_mod = sys.modules.get("pyngrok.exception")
    if existing_exc_mod is not None:
        http_error_cls = existing_exc_mod.PyngrokNgrokHTTPError  # type: ignore[attr-defined]
        exception_mod = existing_exc_mod
    else:
        class PyngrokNgrokHTTPError(Exception):
            pass

        exception_mod = ModuleType("pyngrok.exception")
        exception_mod.PyngrokNgrokHTTPError = PyngrokNgrokHTTPError  # type: ignore[attr-defined]
        http_error_cls = PyngrokNgrokHTTPError

    pyngrok = ModuleType("pyngrok")
    ngrok_mod = ModuleType("pyngrok.ngrok")
    conf_mod = ModuleType("pyngrok.conf")

    connect_mock = MagicMock(side_effect=connect_side_effect)
    ngrok_mod.connect = connect_mock
    ngrok_mod.kill = kill or MagicMock()
    conf_mod.get_default = MagicMock(return_value=SimpleNamespace(auth_token=""))

    pyngrok.ngrok = ngrok_mod  # type: ignore[attr-defined]
    pyngrok.conf = conf_mod  # type: ignore[attr-defined]
    pyngrok.exception = exception_mod  # type: ignore[attr-defined]

    sys.modules["pyngrok"] = pyngrok
    sys.modules["pyngrok.ngrok"] = ngrok_mod
    sys.modules["pyngrok.conf"] = conf_mod
    sys.modules["pyngrok.exception"] = exception_mod

    # Quiet the "unused" lint warning for the single-install case.
    _ = http_error_cls

    return ngrok_mod, connect_mock


@pytest.fixture(autouse=True)
def _cleanup_pyngrok_modules():
    """Remove the fake ``pyngrok`` submodules after each test so they
    don't leak into unrelated tests."""
    yield
    for name in ("pyngrok", "pyngrok.ngrok", "pyngrok.conf", "pyngrok.exception"):
        sys.modules.pop(name, None)


@pytest.mark.asyncio
async def test_connect_happy_path() -> None:
    tunnel_obj = SimpleNamespace(public_url="https://abc.ngrok-free.dev")
    _, connect_mock = _install_fake_pyngrok([tunnel_obj])

    backend = NgrokTunnel()
    url = await backend.connect(8000, {"api_key": "k", "domain": "abc.ngrok-free.dev"})

    assert url == "https://abc.ngrok-free.dev"
    connect_mock.assert_called_once()
    assert connect_mock.call_args.kwargs == {"addr": "8000", "domain": "abc.ngrok-free.dev"}


@pytest.mark.asyncio
async def test_connect_upgrades_http_to_https() -> None:
    tunnel_obj = SimpleNamespace(public_url="http://abc.ngrok-free.dev")
    _install_fake_pyngrok([tunnel_obj])

    backend = NgrokTunnel()
    url = await backend.connect(8000, {})

    assert url == "https://abc.ngrok-free.dev"


@pytest.mark.asyncio
async def test_connect_recovers_from_stale_endpoint() -> None:
    """ERR_NGROK_334 (endpoint already online) on first attempt should
    trigger a cleanup + retry and succeed on the second attempt. This
    is the common case when Gilbert crashes and the supervisor
    restarts before the orphan ngrok subprocess gets reaped."""
    # Install the fakes FIRST so the one ``PyngrokNgrokHTTPError`` class
    # built by ``_install_fake_pyngrok`` is what both the backend and
    # this test reference. Two distinct class objects would make the
    # backend's ``except PyngrokNgrokHTTPError`` miss the raised error.
    _install_fake_pyngrok([Exception("placeholder — overwritten below")])
    from pyngrok.exception import PyngrokNgrokHTTPError

    tunnel_obj = SimpleNamespace(public_url="https://abc.ngrok-free.dev")
    stale_error = PyngrokNgrokHTTPError(
        'ngrok client exception, API returned 502: {"error_code":103,'
        '"status_code":502,"msg":"failed to start tunnel",'
        '"details":{"err":"failed to start tunnel: The endpoint is '
        "already online. ERR_NGROK_334\"}}"
    )
    _install_fake_pyngrok([stale_error, tunnel_obj])

    backend = NgrokTunnel()
    with patch.object(backend, "_kill_stale_ngrok", new=AsyncMock()) as cleanup, \
            patch("asyncio.sleep", new=AsyncMock()):
        url = await backend.connect(8000, {})

    assert url == "https://abc.ngrok-free.dev"
    cleanup.assert_awaited_once()
    from pyngrok import ngrok
    assert ngrok.connect.call_count == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_connect_propagates_unrelated_http_error() -> None:
    """Errors other than ERR_NGROK_334 must not trigger the retry
    path — otherwise e.g. a bad auth token would silently mask the
    real problem behind a ``pkill`` that accomplishes nothing."""
    _install_fake_pyngrok([Exception("placeholder — overwritten below")])
    from pyngrok.exception import PyngrokNgrokHTTPError

    auth_error = PyngrokNgrokHTTPError("ERR_NGROK_105 authentication failed")
    _install_fake_pyngrok([auth_error])

    backend = NgrokTunnel()
    with patch.object(backend, "_kill_stale_ngrok", new=AsyncMock()) as cleanup:
        with pytest.raises(PyngrokNgrokHTTPError):
            await backend.connect(8000, {})

    cleanup.assert_not_awaited()
