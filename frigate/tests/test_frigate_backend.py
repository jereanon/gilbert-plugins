"""Tests for the FrigateCameraBackend public surface.

Exercises ``backend_config_params``, registration in the global
``CameraEventBackend._registry``, the ``test_connection`` action, and
the http auth-header / TLS-params plumbing.
"""

from __future__ import annotations

from typing import Any

import pytest
from gilbert_plugin_frigate import backend as backend_mod
from gilbert_plugin_frigate.backend import (
    FrigateCameraBackend,
    _build_tls_params,
)
from gilbert_plugin_frigate.http_client import FrigateHTTP

from gilbert.interfaces.camera import CameraEventBackend


def test_backend_registered() -> None:
    assert (
        CameraEventBackend.registered_backends().get("frigate")
        is FrigateCameraBackend
    )


def test_backend_config_params_includes_tls_and_http_keys() -> None:
    keys = {p.key for p in FrigateCameraBackend.backend_config_params()}
    assert {
        "mqtt_host",
        "mqtt_port",
        "mqtt_topic_prefix",
        "mqtt_username",
        "mqtt_password",
        "mqtt_tls",
        "mqtt_tls_ca_cert",
        "mqtt_tls_client_cert",
        "mqtt_tls_client_key",
        "mqtt_tls_insecure",
        "mqtt_tls_server_hostname",
        "http_base_url",
        "http_auth_mode",
        "http_token",
        "verify_ssl",
        "cameras_filter",
    } <= keys


def test_http_auth_modes() -> None:
    none_client = FrigateHTTP(base_url="http://x", auth_mode="none", token="t")
    assert none_client.auth_headers() == {}

    bearer_client = FrigateHTTP(
        base_url="http://x", auth_mode="bearer", token="abc"
    )
    assert bearer_client.auth_headers() == {"Authorization": "Bearer abc"}


def test_tls_params_constructed_from_config() -> None:
    settings = {
        "mqtt_tls": True,
        "mqtt_tls_ca_cert": "/etc/ca.pem",
        "mqtt_tls_client_cert": "/etc/client.pem",
        "mqtt_tls_client_key": "/etc/client.key",
    }
    params = _build_tls_params(settings)
    if params is None:
        pytest.skip("aiomqtt not importable in this environment")
    # aiomqtt.TLSParameters is a dataclass-style container; verify the
    # attrs we care about made it through.
    assert getattr(params, "ca_certs", None) == "/etc/ca.pem"
    assert getattr(params, "certfile", None) == "/etc/client.pem"
    assert getattr(params, "keyfile", None) == "/etc/client.key"


def test_tls_params_none_when_disabled() -> None:
    assert _build_tls_params({"mqtt_tls": False}) is None


def test_client_tls_kwargs_includes_insecure_and_sni() -> None:
    from gilbert_plugin_frigate.backend import _build_client_tls_kwargs

    extras = _build_client_tls_kwargs(
        {
            "mqtt_tls": True,
            "mqtt_tls_insecure": True,
            "mqtt_tls_server_hostname": "broker.lan",
        }
    )
    assert extras["tls_insecure"] is True
    assert extras["server_hostname"] == "broker.lan"


def test_client_tls_kwargs_empty_when_tls_disabled() -> None:
    from gilbert_plugin_frigate.backend import _build_client_tls_kwargs

    assert _build_client_tls_kwargs({"mqtt_tls": False}) == {}


@pytest.mark.asyncio
async def test_test_connection_action_no_http_base_returns_error() -> None:
    backend = FrigateCameraBackend()
    await backend.initialize({"http_base_url": ""})
    result = await backend.invoke_backend_action("test_connection", {})
    # No http_base_url means HTTP probe returns no version (success=False).
    assert result.status == "error"


@pytest.mark.asyncio
async def test_test_connection_unknown_action_returns_error() -> None:
    backend = FrigateCameraBackend()
    await backend.initialize({})
    result = await backend.invoke_backend_action("nope", {})
    assert result.status == "error"
    assert "Unknown action" in result.message


@pytest.mark.asyncio
async def test_test_connection_success_path_with_fake_factory() -> None:
    """The probe succeeds when both HTTP and MQTT come back clean."""

    class _FakeMqtt:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> _FakeMqtt:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def subscribe(self, topic: str) -> None:
            return None

    backend = FrigateCameraBackend()
    await backend.initialize(
        {
            "http_base_url": "http://localhost:1",
            "mqtt_host": "localhost",
            "mqtt_port": 1883,
            "_client_factory": _FakeMqtt,
        }
    )
    # Stub out the HTTP version probe so we don't actually hit a server.
    assert backend._http is not None

    async def fake_get_version() -> str:
        return "0.13.4"

    backend._http.get_version = fake_get_version  # type: ignore[method-assign]
    result = await backend.invoke_backend_action("test_connection", {})
    assert result.status == "ok"
    assert "Frigate 0.13.4" in result.message
    assert "MQTT ok" in result.message


@pytest.mark.asyncio
async def test_test_connection_old_frigate_warning() -> None:
    class _FakeMqtt:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeMqtt:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def subscribe(self, topic: str) -> None:
            return None

    backend = FrigateCameraBackend()
    await backend.initialize(
        {
            "http_base_url": "http://localhost:1",
            "mqtt_host": "localhost",
            "_client_factory": _FakeMqtt,
        }
    )

    async def fake_get_version() -> str:
        return "0.12.0"

    backend._http.get_version = fake_get_version  # type: ignore[method-assign]
    result = await backend.invoke_backend_action("test_connection", {})
    # Successful probe but the message warns about the old version.
    assert "WARNING" in result.message


def test_module_exports() -> None:
    # Verify the side-effect import path used by plugin.setup() works.
    assert hasattr(backend_mod, "FrigateCameraBackend")
