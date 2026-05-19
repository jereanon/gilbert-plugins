"""Andon FM service — tools, slash commands, scraper, WS handlers.

Three layers in one service:

- **v1** — AI tools and slash commands that hand a station's Live365
  stream URL to ``SpeakerService.play_on_speakers`` so users can listen
  on Sonos, the local box, or their browser tab.
- **v2** — a scheduler-driven scraper that polls ``andonlabs.com/radio``
  for each station's current programming block + listener count, caches
  the result, exposes it as an AI tool, and publishes a
  ``andon_fm.now_playing.changed`` event whenever a block transitions.
- **v3** — WebSocket RPCs and an event channel that drive the
  full-page tuner under Media (``frontend/AndonFmPage.tsx``), plus a
  speaker-list RPC for the per-play picker dialog.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam, ConfigurationReader
from gilbert.interfaces.events import Event, EventBus, EventBusProvider
from gilbert.interfaces.scheduler import Schedule, SchedulerProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.speaker import CachedSpeakerLister
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

from .scraper import NowPlayingSnapshot, fetch_now_playing
from .stations import BUNDLED_STATIONS, find_station

logger = logging.getLogger(__name__)

# Job names — keep stable, the scheduler keys jobs by name.
_REFRESH_JOB = "andon_fm.refresh_now_playing"
_INITIAL_JOB = "andon_fm.initial_fetch"

# How long a cached snapshot is allowed to be reused before we tell
# the UI it's stale. The scheduler tick controls actual refresh — this
# only affects the staleness flag we surface.
_STALE_AFTER_SECONDS = 600


class AndonFmService(Service):
    """Play, tune, and inspect the four Andon FM AI radio stations."""

    slash_namespace = "radio"

    def __init__(self) -> None:
        # ── runtime config (loaded in start, updated in on_config_changed) ──
        self._enabled: bool = False
        self._default_speakers: list[str] = ["my browser"]
        self._default_volume: int = 60
        self._scraper_enabled: bool = True
        self._scrape_interval: int = 90

        # ── resolved capabilities ──
        self._speaker_svc: Any = None
        self._scheduler: SchedulerProvider | None = None
        self._bus: EventBus | None = None

        # ── http + cache ──
        self._http: httpx.AsyncClient | None = None
        self._now_playing: dict[str, NowPlayingSnapshot] = {}
        self._last_fetch_ok: float = 0.0
        self._last_fetch_error: str = ""

    # ── Service protocol ─────────────────────────────────────────────

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="andon_fm",
            capabilities=frozenset({"andon_fm", "ai_tools", "ws_handlers"}),
            requires=frozenset({"speaker_control"}),
            optional=frozenset({"configuration", "scheduler", "event_bus"}),
            toggleable=True,
            toggle_description=(
                "Andon FM — AI-hosted internet radio (Thinking Frequencies, "
                "OpenAIR, Backlink Broadcast, Grok and Roll)"
            ),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None and isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section_safe(self.config_namespace)
            # ``enabled`` is owned by the framework-level service
            # toggle (Settings → Services) — default OFF to match
            # the toggle's default. Fresh installs see the row in
            # the Services list and opt in explicitly.
            if not section.get("enabled", False):
                logger.info("Andon FM disabled in settings — skipping start")
                return
            self._apply_section(section)

        self._speaker_svc = resolver.require_capability("speaker_control")

        scheduler = resolver.get_capability("scheduler")
        if isinstance(scheduler, SchedulerProvider):
            self._scheduler = scheduler

        bus_provider = resolver.get_capability("event_bus")
        if isinstance(bus_provider, EventBusProvider):
            self._bus = bus_provider.bus

        self._http = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        self._enabled = True

        if self._scraper_enabled:
            self._schedule_scraper()
        logger.info(
            "Andon FM service started — %d stations, scraper=%s, interval=%ds",
            len(BUNDLED_STATIONS),
            "on" if self._scraper_enabled else "off",
            self._scrape_interval,
        )

    async def stop(self) -> None:
        self._unschedule_scraper()
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None
        self._enabled = False

    # ── Configurable protocol ────────────────────────────────────────

    @property
    def config_namespace(self) -> str:
        return "andon_fm"

    @property
    def config_category(self) -> str:
        return "Media"

    def config_params(self) -> list[ConfigParam]:
        # ``enabled`` is supplied automatically by the framework's
        # service-toggle section because ``service_info().toggleable``
        # is True; we don't redeclare it here.
        return [
            ConfigParam(
                key="default_target_speakers",
                type=ToolParameterType.ARRAY,
                description=(
                    "Speakers used when no speaker is specified in a /radio "
                    "command or AI tool call. Use the magic alias 'my browser' "
                    "to default to the caller's tab."
                ),
                default=["my browser"],
                choices_from="speakers",
            ),
            ConfigParam(
                key="default_volume",
                type=ToolParameterType.INTEGER,
                description="Default playback volume (0-100) when none is given.",
                default=60,
            ),
            ConfigParam(
                key="scraper_enabled",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Scrape the Andon FM web page for each station's current "
                    "programming block and listener count. Disable if you only "
                    "want playback (no metadata)."
                ),
                default=True,
                restart_required=True,
            ),
            ConfigParam(
                key="scrape_interval_seconds",
                type=ToolParameterType.INTEGER,
                description=(
                    "How often to refresh now-playing metadata. Blocks are "
                    "usually 30-60 minutes long, so a low interval mostly "
                    "wastes requests; 90s is the default."
                ),
                default=90,
                restart_required=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        # Only non-restart fields apply live; the restart-required ones
        # update on next start.
        if "default_target_speakers" in config:
            raw = config.get("default_target_speakers") or []
            if isinstance(raw, list):
                self._default_speakers = [str(x) for x in raw if str(x).strip()]
        if "default_volume" in config:
            try:
                self._default_volume = max(0, min(100, int(config["default_volume"])))
            except (TypeError, ValueError):
                pass

    def _apply_section(self, section: dict[str, Any]) -> None:
        raw_speakers = section.get("default_target_speakers", self._default_speakers)
        if isinstance(raw_speakers, list):
            self._default_speakers = [str(x) for x in raw_speakers if str(x).strip()]
        try:
            self._default_volume = max(
                0, min(100, int(section.get("default_volume", self._default_volume)))
            )
        except (TypeError, ValueError):
            pass
        self._scraper_enabled = bool(
            section.get("scraper_enabled", self._scraper_enabled)
        )
        try:
            self._scrape_interval = max(
                15, int(section.get("scrape_interval_seconds", self._scrape_interval))
            )
        except (TypeError, ValueError):
            pass

    # ── ToolProvider protocol ────────────────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "andon_fm"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        station_param = ToolParameter(
            name="station",
            type=ToolParameterType.STRING,
            description=(
                "Station to play. Accepts the display name "
                "('Thinking Frequencies', 'OpenAIR', 'Backlink Broadcast', "
                "'Grok and Roll'), a substring, the host model "
                "('Claude'/'GPT'/'Gemini'/'Grok'), or the station UUID."
            ),
        )
        speakers_param = ToolParameter(
            name="speakers",
            type=ToolParameterType.ARRAY,
            description=(
                "Speaker names to play through. Omit to use the configured "
                "defaults (typically the caller's browser tab). Magic aliases "
                "'my browser' / 'me' resolve to the caller's own tab."
            ),
            required=False,
        )
        volume_param = ToolParameter(
            name="volume",
            type=ToolParameterType.INTEGER,
            description="Volume 0-100. Omit to use the configured default.",
            required=False,
        )
        return [
            ToolDefinition(
                name="andon_list_stations",
                description=(
                    "List the four Andon FM stations: name, AI host, current "
                    "programming block (if known), and listener count."
                ),
                parameters=[],
                required_role="user",
                slash_command="list",
                slash_help="List Andon FM stations and what's on each.",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="andon_play_station",
                description=(
                    "Tune in to an Andon FM station on one or more speakers. "
                    "Streams the station's Live365 MP3 endpoint through "
                    "Gilbert's existing speaker dispatch (Sonos, local, "
                    "browser tab)."
                ),
                parameters=[station_param, speakers_param, volume_param],
                required_role="user",
                slash_command="play",
                slash_help="Play a station: /radio.play <station> [speakers]",
            ),
            ToolDefinition(
                name="andon_stop_station",
                description=(
                    "Stop Andon FM playback on the given speakers (or the "
                    "default targets if none are given)."
                ),
                parameters=[speakers_param],
                required_role="user",
                slash_command="stop",
                slash_help="Stop radio playback: /radio.stop [speakers]",
            ),
            ToolDefinition(
                name="andon_now_playing",
                description=(
                    "Get the current programming block for one Andon FM "
                    "station, or all four if none is specified. Returns "
                    "block name, description, start time, and listener count."
                ),
                parameters=[
                    ToolParameter(
                        name="station",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional. Station name, host, or UUID. Omit "
                            "for a summary across all stations."
                        ),
                        required=False,
                    ),
                ],
                required_role="user",
                slash_command="now",
                slash_help="What's playing on Andon FM: /radio.now [station]",
                parallel_safe=True,
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "andon_list_stations":
            return self._render_stations_list()
        if name == "andon_play_station":
            return await self._tool_play(arguments)
        if name == "andon_stop_station":
            return await self._tool_stop(arguments)
        if name == "andon_now_playing":
            return self._render_now_playing(arguments.get("station") or "")
        raise KeyError(f"Unknown Andon FM tool: {name}")

    # ── tool helpers ─────────────────────────────────────────────────

    async def _tool_play(self, arguments: dict[str, Any]) -> str:
        raw_station = str(arguments.get("station") or "").strip()
        if not raw_station:
            return "Pick a station: Thinking Frequencies, OpenAIR, Backlink Broadcast, or Grok and Roll."
        station = find_station(raw_station)
        if station is None:
            available = ", ".join(s.name for s in BUNDLED_STATIONS)
            return f"Unknown Andon FM station '{raw_station}'. Available: {available}."

        speakers = self._coerce_speakers(arguments.get("speakers"))
        volume = self._coerce_volume(arguments.get("volume"))

        try:
            await self._speaker_svc.play_on_speakers(
                uri=station.stream_url,
                speaker_names=speakers,
                volume=volume,
                title=f"Andon FM · {station.name}",
                kind="andon_fm",
            )
        except Exception as exc:  # noqa: BLE001 - surface to caller
            logger.exception("Andon FM play failed for %s", station.name)
            return f"Couldn't start {station.name}: {exc}"

        targets = ", ".join(speakers) if speakers else "default speakers"
        return f"Tuned in to {station.name} ({station.host}) on {targets}."

    async def _tool_stop(self, arguments: dict[str, Any]) -> str:
        speakers = self._coerce_speakers(arguments.get("speakers"))
        try:
            await self._speaker_svc.stop_speakers(speaker_names=speakers or None)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Andon FM stop failed")
            return f"Couldn't stop playback: {exc}"
        targets = ", ".join(speakers) if speakers else "default speakers"
        return f"Stopped Andon FM on {targets}."

    def _coerce_speakers(self, raw: Any) -> list[str]:
        if raw is None:
            return list(self._default_speakers)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return list(self._default_speakers)
        cleaned = [str(x).strip() for x in raw if str(x).strip()]
        return cleaned if cleaned else list(self._default_speakers)

    def _coerce_volume(self, raw: Any) -> int | None:
        if raw is None or raw == "":
            return self._default_volume if self._default_volume > 0 else None
        try:
            return max(0, min(100, int(raw)))
        except (TypeError, ValueError):
            return self._default_volume if self._default_volume > 0 else None

    def _render_stations_list(self) -> str:
        lines = ["Andon FM stations:"]
        for s in BUNDLED_STATIONS:
            snap = self._now_playing.get(s.id)
            extra = ""
            if snap is not None:
                bits: list[str] = []
                if snap.block.name:
                    bits.append(snap.block.name)
                if snap.listeners:
                    bits.append(f"{snap.listeners} listening")
                if bits:
                    extra = f" — {' · '.join(bits)}"
            lines.append(f"- {s.name} (hosted by {s.host}){extra}")
        return "\n".join(lines)

    def _render_now_playing(self, query: str) -> str:
        targets: list = []
        if query:
            station = find_station(query)
            if station is None:
                return f"No station matches '{query}'."
            targets = [station]
        else:
            targets = list(BUNDLED_STATIONS)

        if not self._now_playing:
            return (
                "No now-playing data yet. The scraper refreshes every "
                f"{self._scrape_interval}s; try again in a moment."
            )

        lines: list[str] = []
        for s in targets:
            snap = self._now_playing.get(s.id)
            if snap is None or not snap.block.name:
                lines.append(f"{s.name}: (no block info)")
                continue
            line = f"{s.name} — {snap.block.name}"
            if snap.listeners:
                line += f" ({snap.listeners} listening)"
            if snap.block.description:
                line += f"\n  {snap.block.description}"
            lines.append(line)
        return "\n".join(lines)

    # ── scraper / scheduler wiring ───────────────────────────────────

    def _schedule_scraper(self) -> None:
        if self._scheduler is None:
            logger.debug("Andon FM scraper enabled but no scheduler — skipping")
            return
        self._scheduler.add_job(
            name=_REFRESH_JOB,
            schedule=Schedule.every(self._scrape_interval),
            callback=self._refresh_now_playing,
            system=True,
        )
        # Kick off an initial fetch ~2s after start so the dashboard
        # has data before the first interval tick.
        self._scheduler.add_job(
            name=_INITIAL_JOB,
            schedule=Schedule.once_after(2.0),
            callback=self._refresh_now_playing,
            system=True,
        )

    def _unschedule_scraper(self) -> None:
        if self._scheduler is None:
            return
        for job in (_REFRESH_JOB, _INITIAL_JOB):
            try:
                self._scheduler.remove_job(job, force=True)
            except Exception:
                pass

    async def _refresh_now_playing(self) -> None:
        """Scheduler callback — fetch the page, diff, publish events."""
        if self._http is None:
            return
        snapshots = await fetch_now_playing(self._http, BUNDLED_STATIONS)
        now = time.time()
        if not snapshots:
            self._last_fetch_error = "page fetch / parse failed"
            return
        self._last_fetch_ok = now
        self._last_fetch_error = ""

        changes: list[dict[str, Any]] = []
        for sid, snap in snapshots.items():
            stamped = NowPlayingSnapshot(
                station_id=snap.station_id,
                block=snap.block,
                fetched_at=now,
                listeners=snap.listeners,
                tweets=snap.tweets,
            )
            prev = self._now_playing.get(sid)
            self._now_playing[sid] = stamped
            if prev is None or prev.block.name != stamped.block.name:
                changes.append(self._snapshot_to_event_data(stamped))

        if changes and self._bus is not None:
            for payload in changes:
                try:
                    await self._bus.publish(
                        Event(
                            event_type="andon_fm.now_playing.changed",
                            data=payload,
                            source="andon_fm",
                        )
                    )
                except Exception:
                    logger.debug("Andon FM: event publish failed", exc_info=True)

    def _snapshot_to_event_data(self, snap: NowPlayingSnapshot) -> dict[str, Any]:
        # The station name + image_url get echoed into the event so SPA
        # subscribers don't need a second lookup against the catalog.
        station = next((s for s in BUNDLED_STATIONS if s.id == snap.station_id), None)
        return {
            "station_id": snap.station_id,
            "station_name": station.name if station else "",
            "station_image_url": station.image_url if station else "",
            "block": snap.block.to_dict(),
            "listeners": snap.listeners,
            "fetched_at": snap.fetched_at,
        }

    # ── WsHandlerProvider protocol (v3 dashboard) ────────────────────

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "andon_fm.stations.list": self._ws_stations_list,
            "andon_fm.speakers.list": self._ws_speakers_list,
            "andon_fm.play": self._ws_play,
            "andon_fm.stop": self._ws_stop,
            "andon_fm.now_playing.get": self._ws_now_playing_get,
        }

    def _stations_payload(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        now = time.time()
        for s in BUNDLED_STATIONS:
            snap = self._now_playing.get(s.id)
            stale = (
                snap is None
                or snap.fetched_at <= 0
                or (now - snap.fetched_at) > _STALE_AFTER_SECONDS
            )
            entry: dict[str, Any] = {
                "id": s.id,
                "name": s.name,
                "host": s.host,
                "twitter": s.twitter,
                "stream_url": s.stream_url,
                "image_url": s.image_url,
                "stale": stale,
            }
            if snap is not None:
                entry["block"] = snap.block.to_dict()
                entry["listeners"] = snap.listeners
                entry["fetched_at"] = snap.fetched_at
                entry["tweets"] = list(snap.tweets)
            else:
                entry["block"] = None
                entry["listeners"] = 0
                entry["fetched_at"] = 0.0
                entry["tweets"] = []
            out.append(entry)
        return out

    async def _ws_speakers_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        """Return cached speakers + the browser-tab magic alias.

        Populates the per-play picker dialog. We read the speaker
        service's cached list via the ``CachedSpeakerLister`` protocol
        rather than triggering a fresh discovery (a per-open network
        fan-out would be wasteful — the service refreshes its cache
        on backend events). The ``my browser`` virtual entry is
        prepended unconditionally so users can route to the current
        tab without picking a physical device.

        If the resolved speaker service doesn't satisfy the protocol —
        a misconfigured composition where ``speaker_control`` is
        provided by something with no cache — the dialog still
        renders the browser-tab alias, not an empty list.
        """
        speakers_payload: list[dict[str, Any]] = [
            {
                "id": "my browser",
                "name": "This browser tab",
                "model": "",
                "backend": "browser_tab",
                "group_name": "",
                "is_virtual": True,
            }
        ]
        if isinstance(self._speaker_svc, CachedSpeakerLister):
            try:
                cached = list(self._speaker_svc.cached_speakers)
            except Exception:  # noqa: BLE001
                logger.exception("Andon FM ws speakers.list cache read failed")
                cached = []
            for s in cached:
                speakers_payload.append(
                    {
                        "id": s.name,
                        "name": s.name,
                        "model": getattr(s, "model", "") or "",
                        "backend": getattr(s, "backend_name", "") or "",
                        "group_name": getattr(s, "group_name", "") or "",
                        "is_virtual": False,
                    }
                )
        return {
            "type": "andon_fm.speakers.list.result",
            "ref": frame.get("id"),
            "speakers": speakers_payload,
            "defaults": {
                "speakers": list(self._default_speakers),
                "volume": self._default_volume,
            },
        }

    async def _ws_stations_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "type": "andon_fm.stations.list.result",
            "ref": frame.get("id"),
            "stations": self._stations_payload(),
            "defaults": {
                "speakers": list(self._default_speakers),
                "volume": self._default_volume,
            },
            "last_fetch_ok": self._last_fetch_ok,
            "last_fetch_error": self._last_fetch_error,
        }

    async def _ws_now_playing_get(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "type": "andon_fm.now_playing.get.result",
            "ref": frame.get("id"),
            "stations": self._stations_payload(),
        }

    async def _ws_play(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        station_query = str(frame.get("station") or "").strip()
        station = find_station(station_query)
        if station is None:
            return {
                "type": "andon_fm.play.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": f"Unknown station: {station_query}",
            }
        speakers = self._coerce_speakers(frame.get("speakers"))
        volume = self._coerce_volume(frame.get("volume"))
        try:
            await self._speaker_svc.play_on_speakers(
                uri=station.stream_url,
                speaker_names=speakers,
                volume=volume,
                title=f"Andon FM · {station.name}",
                kind="andon_fm",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Andon FM ws play failed for %s", station.name)
            return {
                "type": "andon_fm.play.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": str(exc),
            }
        return {
            "type": "andon_fm.play.result",
            "ref": frame.get("id"),
            "ok": True,
            "station_id": station.id,
            "speakers": speakers,
        }

    async def _ws_stop(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        speakers = self._coerce_speakers(frame.get("speakers"))
        try:
            await self._speaker_svc.stop_speakers(speaker_names=speakers or None)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Andon FM ws stop failed")
            return {
                "type": "andon_fm.stop.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": str(exc),
            }
        return {
            "type": "andon_fm.stop.result",
            "ref": frame.get("id"),
            "ok": True,
            "speakers": speakers,
        }


__all__ = ["AndonFmService"]
