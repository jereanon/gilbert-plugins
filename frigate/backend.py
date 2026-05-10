"""Frigate camera-event backend.

Implements ``CameraEventBackend`` using Frigate's MQTT event stream
(``aiomqtt``) for pushed detection events and Frigate's HTTP API
(``httpx``) for snapshot/clip retrieval. Single-layer reconnect: any
``MqttError`` exits the inner client; the service's outer loop
re-invokes ``connect()``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from gilbert.interfaces.camera import (
    CameraBackendError,
    CameraEvent,
    CameraEventBackend,
    CameraInfo,
    SnapshotRef,
)
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.tools import ToolParameterType

from .http_client import FrigateHTTP
from .mqtt_client import FrigateMQTT

logger = logging.getLogger(__name__)


def _build_tls_params(settings: dict[str, Any]) -> Any:
    """Construct an ``aiomqtt.TLSParameters`` instance from settings, or None.

    ``aiomqtt`` is optional in tests; importing lazily keeps the
    plugin's ``backend`` module importable without the dependency.

    Note that ``tls_insecure`` (verification skip) and SNI override are
    NOT fields of ``TLSParameters`` in aiomqtt v2 — they are kwargs on
    ``aiomqtt.Client`` itself. Those flow through
    :func:`_build_client_tls_kwargs`.
    """
    if not settings.get("mqtt_tls"):
        return None
    try:
        import aiomqtt
    except ImportError:
        return None
    kwargs: dict[str, Any] = {}
    ca_cert = str(settings.get("mqtt_tls_ca_cert") or "")
    if ca_cert:
        kwargs["ca_certs"] = ca_cert
    client_cert = str(settings.get("mqtt_tls_client_cert") or "")
    if client_cert:
        kwargs["certfile"] = client_cert
    client_key = str(settings.get("mqtt_tls_client_key") or "")
    if client_key:
        kwargs["keyfile"] = client_key
    return aiomqtt.TLSParameters(**kwargs)


def _build_client_tls_kwargs(settings: dict[str, Any]) -> dict[str, Any]:
    """Extra kwargs for ``aiomqtt.Client(...)`` carrying TLS-related flags.

    aiomqtt v2 accepts ``tls_insecure: bool`` directly on the client
    constructor; SNI / cert-CN override (``server_hostname``) is also
    a client-constructor kwarg.
    """
    if not settings.get("mqtt_tls"):
        return {}
    out: dict[str, Any] = {}
    if settings.get("mqtt_tls_insecure"):
        out["tls_insecure"] = True
    server_hostname = str(settings.get("mqtt_tls_server_hostname") or "")
    if server_hostname:
        out["server_hostname"] = server_hostname
    return out


class FrigateCameraBackend(CameraEventBackend):
    backend_name = "frigate"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="mqtt_host",
                type=ToolParameterType.STRING,
                description="MQTT broker hostname (Frigate publishes here).",
                default="",
                restart_required=True,
            ),
            ConfigParam(
                key="mqtt_port",
                type=ToolParameterType.INTEGER,
                description="MQTT broker port (default 1883, 8883 for TLS).",
                default=1883,
                restart_required=True,
            ),
            ConfigParam(
                key="mqtt_topic_prefix",
                type=ToolParameterType.STRING,
                description=(
                    "Frigate's MQTT topic prefix "
                    "(matches Frigate's mqtt.topic_prefix; default ``frigate``)."
                ),
                default="frigate",
                restart_required=True,
            ),
            ConfigParam(
                key="mqtt_username",
                type=ToolParameterType.STRING,
                description="MQTT broker username (optional).",
                default="",
            ),
            ConfigParam(
                key="mqtt_password",
                type=ToolParameterType.STRING,
                description="MQTT broker password (optional).",
                default="",
                sensitive=True,
            ),
            ConfigParam(
                key="mqtt_client_id",
                type=ToolParameterType.STRING,
                description="MQTT client id (default ``gilbert-cameras``).",
                default="gilbert-cameras",
            ),
            ConfigParam(
                key="mqtt_tls",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Enable TLS for the MQTT connection. The mqtt_tls_* "
                    "params below configure the certificate material."
                ),
                default=False,
            ),
            ConfigParam(
                key="mqtt_tls_ca_cert",
                type=ToolParameterType.STRING,
                description=(
                    "CA certificate (PEM file path or inline PEM) used "
                    "to verify the broker's certificate. Required for "
                    "self-signed brokers (typical home Mosquitto)."
                ),
                default="",
                multiline=True,
            ),
            ConfigParam(
                key="mqtt_tls_client_cert",
                type=ToolParameterType.STRING,
                description=(
                    "Client certificate (PEM) for mutual TLS. Optional."
                ),
                default="",
                sensitive=True,
                multiline=True,
            ),
            ConfigParam(
                key="mqtt_tls_client_key",
                type=ToolParameterType.STRING,
                description=(
                    "Client private key (PEM) for mutual TLS. Optional."
                ),
                default="",
                sensitive=True,
                multiline=True,
            ),
            ConfigParam(
                key="mqtt_tls_insecure",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Skip TLS hostname / certificate verification. "
                    "DISABLES MITM PROTECTION — only use for self-"
                    "signed brokers where you don't want to ship the CA."
                ),
                default=False,
            ),
            ConfigParam(
                key="mqtt_tls_server_hostname",
                type=ToolParameterType.STRING,
                description=(
                    "SNI / cert-CN override (use when the broker cert's "
                    "CN doesn't match the IP / mDNS name)."
                ),
                default="",
            ),
            ConfigParam(
                key="http_base_url",
                type=ToolParameterType.STRING,
                description=(
                    "Frigate web base URL "
                    "(e.g. ``http://frigate.local:5000``)."
                ),
                default="",
                restart_required=True,
            ),
            ConfigParam(
                key="http_auth_mode",
                type=ToolParameterType.STRING,
                description=(
                    "HTTP auth mode: ``none`` for unauthenticated LAN "
                    "deploys, ``bearer`` for proxy-style or Frigate "
                    "API keys."
                ),
                default="none",
                choices=("none", "bearer"),
            ),
            ConfigParam(
                key="http_token",
                type=ToolParameterType.STRING,
                description=(
                    "Bearer token for the Frigate HTTP API. Ignored "
                    "when http_auth_mode=none."
                ),
                default="",
                sensitive=True,
            ),
            ConfigParam(
                key="verify_ssl",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Verify the Frigate HTTP server's TLS certificate. "
                    "Default true; toggle off for self-signed installs."
                ),
                default=True,
            ),
            ConfigParam(
                key="cameras_filter",
                type=ToolParameterType.ARRAY,
                description=(
                    "Restrict to a subset of cameras the broker reports; "
                    "empty = all."
                ),
                default=[],
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Probe Frigate's HTTP /api/version, the MQTT broker, "
                    "and the LWT topic. Reports each result."
                ),
            ),
        ]

    def __init__(self) -> None:
        self._settings: dict[str, Any] = {}
        self._http: FrigateHTTP | None = None
        self._mqtt: FrigateMQTT | None = None
        self._connected: bool = False

    # ── Lifecycle ────────────────────────────────────────────────────

    async def initialize(self, config: dict[str, object]) -> None:
        self._settings = dict(config or {})
        self._http = FrigateHTTP(
            base_url=str(self._settings.get("http_base_url") or ""),
            auth_mode=str(self._settings.get("http_auth_mode") or "none"),
            token=str(self._settings.get("http_token") or ""),
            verify_ssl=bool(self._settings.get("verify_ssl", True)),
        )

    async def close(self) -> None:
        if self._mqtt is not None:
            await self._mqtt.stop()
            self._mqtt = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self._connected = False

    async def connect(self) -> None:
        if self._mqtt is None:
            tls_params = _build_tls_params(self._settings)
            extra_tls_kwargs = _build_client_tls_kwargs(self._settings)
            self._mqtt = FrigateMQTT(
                host=str(self._settings.get("mqtt_host") or ""),
                port=int(self._settings.get("mqtt_port") or 1883),
                prefix=str(self._settings.get("mqtt_topic_prefix") or "frigate"),
                username=str(self._settings.get("mqtt_username") or ""),
                password=str(self._settings.get("mqtt_password") or ""),
                tls_params=tls_params,
                client_id=str(
                    self._settings.get("mqtt_client_id") or "gilbert-cameras"
                ),
                http_base_url=str(self._settings.get("http_base_url") or ""),
                http_token=str(self._settings.get("http_token") or ""),
                client_factory=self._settings.get("_client_factory"),
                extra_client_kwargs=extra_tls_kwargs,
                backend_name=self.backend_name,
            )
        await self._mqtt.start()
        self._connected = True

    async def disconnect(self) -> None:
        if self._mqtt is not None:
            await self._mqtt.stop()
            self._mqtt = None
        self._connected = False

    async def stream_events(self) -> AsyncIterator[CameraEvent]:
        if self._mqtt is None:
            raise CameraBackendError("Frigate backend not connected")
        async for ev in self._mqtt.events():
            yield ev

    # ── Camera enumeration ──────────────────────────────────────────

    async def list_cameras(self) -> list[CameraInfo]:
        if self._http is None:
            return []
        rows = await self._http.list_cameras()
        cameras_filter = self._settings.get("cameras_filter") or []
        if not isinstance(cameras_filter, list):
            cameras_filter = []
        out: list[CameraInfo] = []
        for row in rows:
            name = str(row.get("name") or "")
            if not name:
                continue
            if cameras_filter and name not in cameras_filter:
                continue
            settings = row.get("settings") or {}
            objects = settings.get("objects") if isinstance(settings, dict) else {}
            track = objects.get("track") if isinstance(objects, dict) else []
            zones = (
                list((settings.get("zones") or {}).keys())
                if isinstance(settings.get("zones"), dict)
                else []
            )
            audio_cfg = settings.get("audio") if isinstance(settings, dict) else {}
            has_audio = bool(audio_cfg.get("enabled")) if isinstance(audio_cfg, dict) else False
            out.append(
                CameraInfo(
                    name=name,
                    labels=tuple(track or ()),
                    zones=tuple(zones),
                    has_audio=has_audio,
                )
            )
        return out

    # ── Snapshot / clip retrieval ───────────────────────────────────

    async def get_snapshot(
        self,
        camera: str,
        event_id: str | None = None,
        *,
        max_height: int | None = None,
    ) -> SnapshotRef | None:
        if self._http is None or not event_id:
            return None
        height = max_height if max_height is not None else 720
        result = await self._http.get_snapshot(event_id, height=height)
        if result is None:
            return None
        data, media_type = result
        return SnapshotRef(data=data, media_type=media_type)

    async def get_clip_url(self, event_id: str) -> str | None:
        if self._http is None:
            return None
        return self._http.build_clip_url(event_id) or None

    def backend_auth_headers(self) -> dict[str, str]:
        if self._http is None:
            return {}
        return self._http.auth_headers()

    # ── Config actions ──────────────────────────────────────────────

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        if key == "test_connection":
            return await self._action_test_connection()
        return ConfigActionResult(
            status="error", message=f"Unknown action: {key}"
        )

    async def _action_test_connection(self) -> ConfigActionResult:
        parts: list[str] = []
        success = True

        # HTTP probe
        version = ""
        if self._http is not None:
            try:
                version = await self._http.get_version()
            except Exception as exc:
                parts.append(f"HTTP error: {exc}")
                success = False
            else:
                if version:
                    parts.append(f"HTTP ok (Frigate {version})")
                    if version < "0.13.0":
                        parts.append(
                            f"WARNING: Frigate {version} is older than "
                            f"the supported 0.13.0 minimum"
                        )
                else:
                    parts.append("HTTP probe returned no version")
                    success = False
        else:
            parts.append("HTTP probe skipped (backend not initialized)")
            success = False

        # MQTT probe — short connect+subscribe attempt with a strict
        # deadline. Reuses the configured TLS / auth params.
        try:
            await self._probe_mqtt()
            parts.append("MQTT ok (subscribe successful)")
        except Exception as exc:
            parts.append(f"MQTT error: {exc}")
            success = False

        return ConfigActionResult(
            status="ok" if success else "error",
            message="; ".join(parts),
        )

    async def _probe_mqtt(self) -> None:
        try:
            import aiomqtt
        except ImportError as exc:
            raise CameraBackendError(
                "aiomqtt is not installed"
            ) from exc
        host = str(self._settings.get("mqtt_host") or "")
        if not host:
            raise CameraBackendError("mqtt_host is not configured")
        port = int(self._settings.get("mqtt_port") or 1883)
        prefix = str(self._settings.get("mqtt_topic_prefix") or "frigate")
        kwargs: dict[str, Any] = {
            "hostname": host,
            "port": port,
            "client_id": str(
                self._settings.get("mqtt_client_id") or "gilbert-cameras-probe"
            ),
        }
        username = str(self._settings.get("mqtt_username") or "")
        password = str(self._settings.get("mqtt_password") or "")
        if username:
            kwargs["username"] = username
        if password:
            kwargs["password"] = password
        tls_params = _build_tls_params(self._settings)
        if tls_params is not None:
            kwargs["tls_params"] = tls_params

        # The injected factory (if any) overrides aiomqtt.Client.
        factory = self._settings.get("_client_factory") or aiomqtt.Client
        async with factory(**kwargs) as client:
            await client.subscribe(f"{prefix}/+/events")


__all__ = ["FrigateCameraBackend"]
