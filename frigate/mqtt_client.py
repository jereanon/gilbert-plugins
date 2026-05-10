"""Frigate MQTT subscriber.

Single-layer reconnect: this client opens **one** ``aiomqtt.Client`` per
``run`` invocation. On any ``MqttError`` (transport drop, auth rejection,
broker shutdown), it exits the ``async with`` block, drains a sentinel
into the consumer queue, and raises ``CameraBackendError``. The plugin
does NOT loop internally — the *service*'s ``_run_stream_consumer``
handles backoff and re-entry by calling ``backend.connect()`` again.
This keeps the two layers from disagreeing about backoff semantics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from gilbert.interfaces.camera import (
    CameraBackendError,
    CameraEvent,
    CameraEventPhase,
)

logger = logging.getLogger(__name__)

# Sentinels used to flow non-event signals through the consumer queue.
_STOP = object()
_LWT_ONLINE = object()
_LWT_OFFLINE = object()

# Update-event dedup tunables.
_SCORE_DELTA_DEDUP = 0.05


@dataclass
class _LastEventState:
    """Last-seen state for an event_id, used to dedup ``update`` traffic."""

    score: float = 0.0
    snapshot_frame_time: float = 0.0
    zones: tuple[str, ...] = ()


class FrigateMQTT:
    """Subscriber for Frigate's MQTT event stream.

    Constructor takes an injectable ``client_factory`` so tests can pass
    a fake ``aiomqtt.Client`` substitute without globally monkeypatching
    the import.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        prefix: str,
        username: str = "",
        password: str = "",
        tls_params: Any = None,
        client_id: str = "gilbert-cameras",
        http_base_url: str = "",
        http_token: str = "",
        client_factory: Callable[..., Any] | None = None,
        extra_client_kwargs: dict[str, Any] | None = None,
        backend_name: str = "frigate",
    ) -> None:
        self._host = host
        self._port = port
        self._prefix = prefix.rstrip("/")
        self._username = username
        self._password = password
        self._tls_params = tls_params
        self._client_id = client_id
        self._http_base_url = http_base_url.rstrip("/")
        self._http_token = http_token
        self._extra_client_kwargs = dict(extra_client_kwargs or {})
        self._backend_name = backend_name
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1000)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_state: dict[str, _LastEventState] = {}
        self._frigate_online: bool | None = None

        if client_factory is None:
            try:
                import aiomqtt

                self._client_factory: Callable[..., Any] = aiomqtt.Client
            except ImportError:  # pragma: no cover — installed via pyproject
                self._client_factory = None  # type: ignore[assignment]
        else:
            self._client_factory = client_factory

    # ── Public API used by FrigateCameraBackend ─────────────────────

    async def start(self) -> None:
        if self._client_factory is None:
            raise CameraBackendError(
                "aiomqtt is not installed — install gilbert-plugin-frigate"
            )
        self._stop.clear()
        # Drain any leftover sentinels from a prior run.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._task = asyncio.create_task(self._run(), name="frigate-mqtt-run")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        try:
            self._queue.put_nowait(_STOP)
        except asyncio.QueueFull:
            pass

    async def events(self) -> AsyncIterator[CameraEvent]:
        """Yield ``CameraEvent``s as they arrive from the MQTT stream.

        Translates LWT sentinels into ``CameraBackendError`` so the
        service's outer loop publishes the right
        ``camera.backend.disconnected`` event on Frigate-down.
        """
        while True:
            item = await self._queue.get()
            if item is _STOP:
                return
            if item is _LWT_OFFLINE:
                # Frigate-the-detector is down; signal the service so
                # it can publish camera.backend.disconnected. Re-raise
                # as CameraBackendError so the consumer loop reconnects
                # (LWT online will re-register us as healthy).
                raise CameraBackendError("frigate offline")
            if item is _LWT_ONLINE:
                # Just a status flip; no camera event to yield.
                continue
            if isinstance(item, CameraEvent):
                yield item

    # ── Internal run loop ───────────────────────────────────────────

    async def _run(self) -> None:
        try:
            kwargs: dict[str, Any] = {
                "hostname": self._host,
                "port": self._port,
                "client_id": self._client_id,
            }
            if self._username:
                kwargs["username"] = self._username
            if self._password:
                kwargs["password"] = self._password
            if self._tls_params is not None:
                kwargs["tls_params"] = self._tls_params
            kwargs.update(self._extra_client_kwargs)

            async with self._client_factory(**kwargs) as client:
                await client.subscribe(f"{self._prefix}/events")
                await client.subscribe(f"{self._prefix}/available")
                async for msg in client.messages:
                    if self._stop.is_set():
                        break
                    try:
                        self._on_message(msg)
                    except Exception:
                        logger.exception(
                            "Failed to dispatch frigate MQTT message"
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # MqttError lives in aiomqtt; treat all
            # transport-related exceptions the same way: surface as
            # CameraBackendError after pushing the LWT-style sentinel
            # so the consumer's events() iterator raises and the
            # service's outer loop reconnects.
            self._enqueue(_LWT_OFFLINE)
            logger.warning("Frigate MQTT transport error: %s", exc)
        finally:
            self._enqueue(_STOP)

    # ── Message dispatch ────────────────────────────────────────────

    def _on_message(self, msg: Any) -> None:
        topic = str(getattr(msg, "topic", ""))
        # aiomqtt.Topic has a ``.value`` attr; fall back to str()
        topic_value = getattr(getattr(msg, "topic", None), "value", topic)
        payload = getattr(msg, "payload", b"")
        if isinstance(topic_value, str):
            topic = topic_value

        if topic == f"{self._prefix}/events":
            self._handle_events_payload(payload)
            return
        if topic == f"{self._prefix}/available":
            self._handle_lwt_payload(payload)
            return
        # Anything else — silently ignore.

    def _handle_events_payload(self, payload: Any) -> None:
        if isinstance(payload, bytes | bytearray):
            try:
                data = json.loads(payload.decode("utf-8", errors="replace"))
            except (ValueError, UnicodeDecodeError):
                logger.warning(
                    "Frigate MQTT events payload is not valid JSON; dropping"
                )
                return
        elif isinstance(payload, str):
            try:
                data = json.loads(payload)
            except ValueError:
                logger.warning(
                    "Frigate MQTT events payload is not valid JSON; dropping"
                )
                return
        else:
            return

        ev = self._payload_to_event(data)
        if ev is not None:
            self._enqueue(ev)

    def _handle_lwt_payload(self, payload: Any) -> None:
        if isinstance(payload, bytes | bytearray):
            text = payload.decode("utf-8", errors="replace").strip().lower()
        elif isinstance(payload, str):
            text = payload.strip().lower()
        else:
            return
        if text == "online":
            if self._frigate_online is False:
                self._enqueue(_LWT_ONLINE)
            self._frigate_online = True
        elif text == "offline":
            if self._frigate_online is not False:
                self._enqueue(_LWT_OFFLINE)
            self._frigate_online = False

    # ── Payload normalization ───────────────────────────────────────

    def _payload_to_event(
        self, data: dict[str, Any]
    ) -> CameraEvent | None:
        if not isinstance(data, dict):
            return None
        type_ = str(data.get("type") or "").lower()
        after = data.get("after") or {}
        if not isinstance(after, dict):
            return None
        if after.get("false_positive") is True:
            return None
        event_id = str(after.get("id") or "")
        camera = str(after.get("camera") or "")
        label = str(after.get("label") or "")
        if not event_id or not camera or not label:
            logger.debug(
                "Dropping Frigate event with missing required field "
                "(event_id=%r camera=%r label=%r)",
                event_id,
                camera,
                label,
            )
            return None

        sub_label = _parse_sub_label(after.get("sub_label"))
        is_end = type_ == "end"
        phase = CameraEventPhase.ENDED if is_end else CameraEventPhase.ACTIVE

        score = (
            float(after.get("top_score") or 0.0)
            if is_end
            else float(after.get("score") or 0.0)
        )

        try:
            started_at = int(round(float(after.get("start_time") or 0) * 1000))
        except (TypeError, ValueError):
            started_at = 0
        end_time = after.get("end_time")
        try:
            ended_at = int(round(float(end_time) * 1000)) if end_time else 0
        except (TypeError, ValueError):
            ended_at = 0

        zones_raw = after.get("current_zones")
        if isinstance(zones_raw, list):
            zones = tuple(str(z) for z in zones_raw)
        else:
            zones = ()

        snapshot = after.get("snapshot") or {}
        try:
            snapshot_frame_time = float(
                snapshot.get("frame_time") if isinstance(snapshot, dict) else 0
            )
        except (TypeError, ValueError):
            snapshot_frame_time = 0.0

        # Update dedup — drop ``update`` events whose score change is
        # below the threshold AND no new zones AND no fresh snapshot.
        if type_ == "update":
            prev = self._last_state.get(event_id)
            if prev is not None:
                same_zones = prev.zones == zones
                same_snapshot = (
                    snapshot_frame_time == prev.snapshot_frame_time
                )
                small_score_change = (
                    abs(score - prev.score) < _SCORE_DELTA_DEDUP
                )
                if same_zones and same_snapshot and small_score_change:
                    return None

        self._last_state[event_id] = _LastEventState(
            score=score,
            snapshot_frame_time=snapshot_frame_time,
            zones=zones,
        )

        # Stamp Gilbert-proxied URLs server-side (the service overrides
        # them anyway via ``_stamp_proxied_urls``, but supplying them
        # here keeps the dataclass self-consistent for plugin tests).
        snapshot_url = (
            f"/api/cameras/events/{event_id}/snapshot.jpg"
            if bool(after.get("has_snapshot"))
            else ""
        )
        clip_url = (
            f"/api/cameras/events/{event_id}/clip.mp4"
            if bool(after.get("has_clip"))
            else ""
        )
        direct_snapshot_url = ""
        direct_clip_url = ""
        if self._http_base_url:
            direct_snapshot_url = (
                f"{self._http_base_url}/api/events/{event_id}/snapshot.jpg"
            )
            direct_clip_url = (
                f"{self._http_base_url}/api/events/{event_id}/clip.mp4"
            )

        return CameraEvent(
            event_id=event_id,
            camera=camera,
            label=label,
            sub_label=sub_label,
            phase=phase,
            score=score,
            started_at=started_at,
            ended_at=ended_at,
            zones=zones,
            snapshot_url=snapshot_url,
            clip_url=clip_url,
            has_snapshot=bool(after.get("has_snapshot")),
            has_clip=bool(after.get("has_clip")),
            source_backend=self._backend_name,
            direct_snapshot_url=direct_snapshot_url,
            direct_clip_url=direct_clip_url,
            raw=data,
        )

    # ── Queue helpers ───────────────────────────────────────────────

    def _enqueue(self, item: Any) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning(
                "Frigate MQTT event queue full; dropping event "
                "(consider widening the bound)"
            )


def _parse_sub_label(raw: Any) -> str:
    """Defensive sub_label parsing — Frigate emits 3 distinct shapes.

    - List form: ``["jeff", 0.93]`` → ``"jeff"``.
    - String form: ``"jeff"`` → ``"jeff"``.
    - Null / missing → ``""``.
    """
    if isinstance(raw, list) and raw:
        first = raw[0]
        return first if isinstance(first, str) else ""
    if isinstance(raw, str):
        return raw
    return ""


__all__ = [
    "FrigateMQTT",
    "_LWT_OFFLINE",
    "_LWT_ONLINE",
    "_STOP",
    "_parse_sub_label",
]


# Reference markers for future tooling; ``time`` is imported for future
# use (last-message-at status tracking) without forcing another import
# round-trip in tests that monkey-patch the module.
_ = time
