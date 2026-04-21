"""Sonos speaker backend — S2 local WebSocket API via aiosonos.

Replaces the previous SoCo implementation. aiosonos speaks Sonos's
S2-only local WebSocket API on port 1443 — event-driven, declarative
grouping, native short-clip announcements (no snapshot/restore dance,
the ``audio_clip`` API auto-ducks + auto-restores). S1 speakers are
NOT supported; run ``scripts/check_sonos_s2.py`` before relying on
this plugin.

Discovery is handled by zeroconf (Sonos advertises on
``_sonos._tcp.local.``). Each discovered speaker gets a dedicated
``SonosLocalApiClient`` connection — that's how aiosonos is designed
in Music Assistant (its parent project): one client per player.
Player-level operations go through that client's ``player`` object;
group-level operations can be invoked through any client in the
same household.
"""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
from dataclasses import dataclass
from typing import Any

import aiohttp
import httpx
from aiosonos import SonosLocalApiClient
from aiosonos.api.models import (
    Container,
    PlayBackState,
)
from aiosonos.const import EventType
from aiosonos.exceptions import (
    CannotConnect,
    ConnectionClosed,
    ConnectionFailed,
    FailedCommand,
    SonosException,
)
from aiosonos.utils import get_discovery_info
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from gilbert.interfaces.configuration import ConfigAction, ConfigActionResult
from gilbert.interfaces.speaker import (
    NowPlaying,
    PlaybackState,
    PlayRequest,
    SpeakerBackend,
    SpeakerGroup,
    SpeakerInfo,
)

from .sonos_smapi import SmapiError, SonosSmapiClient

logger = logging.getLogger(__name__)

# Sonos speakers advertise over mDNS as ``_sonos._tcp.local.``. Zeroconf
# fires a ServiceStateChange event for each discovered instance — we
# don't need to broadcast SSDP ourselves.
_SONOS_SERVICE_TYPE = "_sonos._tcp.local."
_DISCOVERY_SETTLE_SECONDS = 3.0
_CONNECT_TIMEOUT = 10.0
_INFO_PROBE_TIMEOUT = 5.0

# Upper bound on how long we'll wait for the Sonos household to
# converge on a new topology after we issue ``modifyGroupMembers``.
# Push events on a healthy LAN arrive in 300ms–2s, but household-wide
# groupings of 12+ speakers routinely take 15–20s to fully converge,
# and aiosonos occasionally swallows push events during heavy topology
# activity. ``_wait_for_group`` polls ``client.groups`` as a fallback
# so a missed event doesn't wedge the wait — this ceiling is just the
# "we really do need to give up" limit for pathological cases.
_TOPOLOGY_SETTLE_TIMEOUT = 30.0

# How often ``_wait_for_group`` rechecks ``client.groups`` while
# waiting for topology to settle. Cheap — reads cached state — so we
# can poll aggressively without generating network traffic.
_TOPOLOGY_POLL_INTERVAL = 0.5

# Spotify URIs as they appear in MusicItem.uri: ``spotify:track:abc123``
# etc. We route these to ``playback.load_content`` with a Spotify
# ``MetadataId`` rather than ``load_stream_url``, because Sonos plays
# Spotify content through the speaker's linked account rather than as
# a plain HTTP stream.
_SPOTIFY_URI_RE = re.compile(
    r"^spotify:(track|album|playlist|artist|episode|show):([A-Za-z0-9]+)$"
)
_SPOTIFY_OPEN_URL_RE = re.compile(
    r"https?://open\.spotify\.com/(track|album|playlist|artist|episode|show)/([A-Za-z0-9]+)"
)

# Sonos publishes this local-API token in every S2 speaker's firmware.
# Not a secret — aiosonos itself uses it. Gates the info endpoint
# against casual abuse, nothing more.
_LOCAL_API_KEY = "123e4567-e89b-12d3-a456-426655440000"
_LOCAL_INFO_URL = "https://{ip}:1443/api/v1/players/local/info"

# Map aiosonos PlayBackState values to our PlaybackState enum.
_PLAYBACK_STATE_MAP: dict[str, PlaybackState] = {
    PlayBackState.PLAYBACK_STATE_PLAYING.value: PlaybackState.PLAYING,
    PlayBackState.PLAYBACK_STATE_PAUSED.value: PlaybackState.PAUSED,
    PlayBackState.PLAYBACK_STATE_IDLE.value: PlaybackState.STOPPED,
    PlayBackState.PLAYBACK_STATE_BUFFERING.value: PlaybackState.TRANSITIONING,
}

# Audio-clip max length per Sonos's own API documentation. Anything
# longer gets truncated. We don't enforce it on the Gilbert side —
# announcements that fit comfortably don't need the ceiling, and
# callers sending longer URLs get a clean Sonos-side error if it
# exceeds.
_AUDIO_CLIP_MAX_SECONDS = 60


@dataclass
class _PlayerMetadata:
    """Per-player info cached from zeroconf + the info endpoint.

    aiosonos's ``SonosPlayer`` is tied to an open WebSocket connection;
    we keep the static identity fields here so the plugin can list /
    look up speakers without touching the live client.
    """

    player_id: str
    household_id: str
    name: str
    ip_address: str
    model: str


class SonosSpeaker(SpeakerBackend):
    """Sonos speaker backend driven by aiosonos (S2 local WebSocket)."""

    backend_name = "sonos"

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Run Sonos zeroconf discovery and report how many "
                    "S2 speakers responded with a valid local-API info "
                    "endpoint."
                ),
            ),
        ]

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict,
    ) -> ConfigActionResult:
        if key == "test_connection":
            return await self._action_test_connection()
        return ConfigActionResult(
            status="error",
            message=f"Unknown action: {key}",
        )

    async def _action_test_connection(self) -> ConfigActionResult:
        count = len(self._player_metadata)
        households = {pm.household_id for pm in self._player_metadata.values()}
        connected = sum(1 for c in self._clients.values() if c is not None)
        if count == 0:
            return ConfigActionResult(
                status="error",
                message=(
                    "No Sonos speakers discovered yet. Zeroconf discovery "
                    "runs in the background on backend start; try again "
                    "in a few seconds, or check that multicast isn't "
                    "blocked on your LAN."
                ),
            )
        return ConfigActionResult(
            status="ok",
            message=(
                f"{count} Sonos speaker(s) discovered across "
                f"{len(households)} household(s); {connected} WebSocket "
                f"connection(s) live."
            ),
        )

    def __init__(self) -> None:
        # One aiosonos client per discovered player. The client owns a
        # persistent WebSocket connection to *its* speaker and dispatches
        # commands through it; group-level commands target the group by
        # id rather than the coordinator player.
        self._clients: dict[str, SonosLocalApiClient] = {}
        self._player_metadata: dict[str, _PlayerMetadata] = {}
        self._zeroconf: AsyncZeroconf | None = None
        self._browser: AsyncServiceBrowser | None = None
        # Background tasks — ``start_listening`` is long-running per
        # client and needs to be cancelled on shutdown.
        self._listen_tasks: dict[str, asyncio.Task[Any]] = {}
        # aiohttp session reused for both zeroconf probes and aiosonos
        # client construction — avoids one-off session churn and
        # (important) lets us pre-install the Sonos self-signed cert
        # bypass once instead of per-probe.
        self._http_session: aiohttp.ClientSession | None = None
        # Lock so zeroconf callbacks don't race against each other.
        self._discovery_lock = asyncio.Lock()
        # IPs we've already brought up (or decided to skip) — zeroconf
        # fires Added/Updated repeatedly as records refresh, and we
        # don't want to re-probe the info endpoint + reconnect every
        # time. The set survives for the lifetime of the backend since
        # a Sonos speaker's IP+identity binding is stable across
        # mDNS refreshes.
        self._known_ips: set[str] = set()
        # Spotify (and other SMAPI) playback goes through the legacy
        # UPnP SOAP AVTransport endpoint on port 1400 — aiosonos's
        # ``loadContent`` path doesn't resolve music-service URIs on
        # current firmware (see ``sonos_smapi`` docstring). Lazy-
        # initialised in ``initialize()`` so tests can swap it.
        self._smapi: SonosSmapiClient | None = None

    # ── Lifecycle ────────────────────────────────────────────────────

    async def initialize(self, config: dict[str, object]) -> None:
        """Start zeroconf discovery and kick off an initial settle wait.

        aiosonos has no LAN-scan helper — we depend on Sonos's own mDNS
        advertisements. Zeroconf fires service-add events as speakers
        respond, and ``_on_service_state_change`` resolves each one and
        creates a client connection. The ``settle`` wait gives the
        initial batch of speakers time to advertise before the caller
        starts making requests; it's not load-bearing (subsequent
        speakers are still picked up asynchronously).
        """
        # Self-signed cert context used for both HTTPS probes and the
        # aiosonos WebSocket. Sonos speakers ship with untrusted certs;
        # verifying them doesn't add security on a LAN-only control
        # plane and would just break every connection. aiosonos itself
        # passes ``ssl=False`` internally, but we use a context for our
        # own httpx probes.
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

        self._http_session = aiohttp.ClientSession()
        self._smapi = SonosSmapiClient()

        self._zeroconf = AsyncZeroconf()
        self._browser = AsyncServiceBrowser(
            self._zeroconf.zeroconf,
            _SONOS_SERVICE_TYPE,
            handlers=[self._on_service_state_change],
        )

        # Wait for the initial wave of advertisements so the first
        # ``list_speakers`` call isn't empty. Callers that need to
        # ensure discovery is complete can poll or await a longer
        # timeout — this is just a best-effort settle.
        await asyncio.sleep(_DISCOVERY_SETTLE_SECONDS)

        logger.info(
            "Sonos backend initialized — %d speaker(s) discovered in %.1fs",
            len(self._player_metadata),
            _DISCOVERY_SETTLE_SECONDS,
        )

    async def close(self) -> None:
        """Tear down all connections + discovery."""
        if self._browser is not None:
            await self._browser.async_cancel()
            self._browser = None

        # Cancel long-running listener tasks before disconnecting —
        # otherwise disconnect races the listener and we log spurious
        # ConnectionClosed errors.
        for task in self._listen_tasks.values():
            task.cancel()
        if self._listen_tasks:
            await asyncio.gather(
                *self._listen_tasks.values(), return_exceptions=True
            )
        self._listen_tasks.clear()

        for client in self._clients.values():
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.debug("Error disconnecting Sonos client", exc_info=True)
        self._clients.clear()

        if self._zeroconf is not None:
            await self._zeroconf.async_close()
            self._zeroconf = None

        if self._smapi is not None:
            await self._smapi.aclose()
            self._smapi = None

        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

        self._player_metadata.clear()

    # ── Discovery ────────────────────────────────────────────────────

    def _on_service_state_change(
        self,
        zeroconf: Any,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """Zeroconf callback — schedule resolution of the changed service.

        Zeroconf delivers events synchronously from its own thread, so
        we schedule an async handler on the main loop rather than doing
        I/O here. Additions and updates both probe the speaker; removals
        drop the cached metadata.
        """
        if state_change == ServiceStateChange.Removed:
            asyncio.create_task(self._handle_service_removed(name))
            return
        if state_change in (
            ServiceStateChange.Added,
            ServiceStateChange.Updated,
        ):
            asyncio.create_task(
                self._handle_service_added(zeroconf, service_type, name)
            )

    async def _handle_service_added(
        self,
        zeroconf: Any,
        service_type: str,
        service_name: str,
    ) -> None:
        """Resolve an mDNS record and bring up a client for the speaker."""
        async with self._discovery_lock:
            info = AsyncServiceInfo(service_type, service_name)
            try:
                resolved = await info.async_request(zeroconf, 3000)
            except Exception:  # noqa: BLE001 - log and drop
                logger.debug(
                    "Zeroconf resolve failed for %s", service_name, exc_info=True
                )
                return
            if not resolved or not info.addresses:
                return

            # Zeroconf returns IPv4 addresses as packed 4-byte strings —
            # convert to dotted-quad strings for the info endpoint + WS.
            ip = ".".join(str(b) for b in info.addresses[0])
            await self._bring_up_speaker(ip)

    async def _handle_service_removed(self, service_name: str) -> None:
        """Clean up state when zeroconf reports a speaker has gone.

        Removal is best-effort — Sonos speakers often advertise
        ephemerally and come back under the same name. We don't tear
        down the client eagerly; the listener task will notice the
        WebSocket closing and we'll reconnect on the next Add event.
        """
        logger.debug("Zeroconf reported service removal: %s", service_name)

    async def _bring_up_speaker(self, ip: str) -> None:
        """Probe the S2 info endpoint, then open an aiosonos client.

        Idempotent by IP: zeroconf re-fires Added/Updated for the same
        speaker as its mDNS records refresh, and we don't want to
        re-probe + reconnect on every firing.
        """
        if ip in self._known_ips:
            return
        self._known_ips.add(ip)
        # Probe /api/v1/players/local/info — this gives us the stable
        # playerId + householdId identifiers that aiosonos expects,
        # plus model/name for UI listings.
        metadata = await self._probe_player(ip)
        if metadata is None:
            return
        if metadata.player_id in self._player_metadata:
            # Already known — idempotent on repeated mDNS events.
            return

        self._player_metadata[metadata.player_id] = metadata

        client = SonosLocalApiClient(ip, self._http_session)
        try:
            await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        except (asyncio.TimeoutError, CannotConnect, ConnectionFailed) as exc:
            logger.warning(
                "Failed to connect to Sonos speaker %s (%s): %s",
                metadata.name,
                ip,
                exc,
            )
            self._player_metadata.pop(metadata.player_id, None)
            return

        self._clients[metadata.player_id] = client

        # aiosonos's ``start_listening`` is typed as accepting an
        # optional ``init_ready: asyncio.Event | None = None`` but then
        # unconditionally calls ``init_ready.set()`` at the end of
        # initial setup — so we MUST pass an Event or it raises
        # ``AttributeError: 'NoneType' object has no attribute 'set'``
        # and the listener task dies before dispatching any events.
        # The Event is useful beyond the bug-workaround anyway: it
        # signals "initial household state is loaded" so a request
        # arriving right after ``_bring_up_speaker`` returns doesn't
        # race against an empty ``client.groups``.
        init_ready = asyncio.Event()

        async def _listen() -> None:
            # ``start_listening`` fetches initial state + keeps the
            # connection alive, dispatching push events to subscribers.
            # Runs until the WebSocket closes or the task is cancelled.
            try:
                await client.start_listening(init_ready)
            except (ConnectionClosed, SonosException):
                logger.debug(
                    "Sonos listener for %s closed", metadata.name, exc_info=True
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - report once and exit
                logger.exception(
                    "Unexpected error in Sonos listener for %s", metadata.name
                )

        task = asyncio.create_task(
            _listen(), name=f"sonos-listen-{metadata.player_id}"
        )
        self._listen_tasks[metadata.player_id] = task

        # Wait (bounded) for initial setup so callers see populated
        # groups/player state when they start querying. If the handshake
        # stalls we still let discovery continue — the speaker just
        # won't be usable until it catches up.
        try:
            await asyncio.wait_for(init_ready.wait(), timeout=_CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(
                "Sonos speaker '%s' (%s) didn't complete initial setup in "
                "%.1fs — marking as degraded",
                metadata.name,
                ip,
                _CONNECT_TIMEOUT,
            )

        logger.info(
            "Connected to Sonos speaker '%s' (%s, %s)",
            metadata.name,
            metadata.model,
            ip,
        )

    async def _probe_player(self, ip: str) -> _PlayerMetadata | None:
        """Hit the S2 info endpoint to extract identity fields."""
        url = _LOCAL_INFO_URL.format(ip=ip)
        headers = {"X-Sonos-Api-Key": _LOCAL_API_KEY}
        try:
            async with httpx.AsyncClient(
                verify=self._ssl_ctx, timeout=_INFO_PROBE_TIMEOUT
            ) as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError:
            logger.debug("S2 info probe failed for %s", ip, exc_info=True)
            return None

        if resp.status_code != 200:
            logger.debug(
                "S2 info probe %s returned HTTP %d", ip, resp.status_code
            )
            return None

        try:
            data = resp.json()
        except ValueError:
            return None

        player_id = str(data.get("playerId") or "")
        household_id = str(data.get("householdId") or "")
        if not player_id or not household_id:
            # Some S2 firmwares omit playerId on the info endpoint and
            # require the caller to discover it via the WebSocket
            # handshake. Fall back to the aiosonos helper which does
            # exactly that.
            try:
                discovery = await get_discovery_info(
                    self._require_http_session(), ip
                )
            except Exception:
                logger.debug(
                    "get_discovery_info fallback failed for %s", ip, exc_info=True
                )
                return None
            player_id = str(discovery.get("playerId", player_id) or "")
            household_id = str(
                discovery.get("householdId", household_id) or ""
            )
            if not player_id or not household_id:
                return None

        return _PlayerMetadata(
            player_id=player_id,
            household_id=household_id,
            name=str(data.get("device", {}).get("name", "") or "Unknown"),
            ip_address=ip,
            model=str(data.get("device", {}).get("model", "") or ""),
        )

    def _require_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None:
            raise RuntimeError("Sonos backend not initialized")
        return self._http_session

    # ── Discovery API ────────────────────────────────────────────────

    async def list_speakers(self) -> list[SpeakerInfo]:
        """Materialize SpeakerInfo for every known speaker.

        Pulls live volume + group membership from the client for each
        player, so results reflect the current state of the system
        (not a stale snapshot taken at discovery time).
        """
        infos: list[SpeakerInfo] = []
        for player_id, meta in self._player_metadata.items():
            client = self._clients.get(player_id)
            volume = 0
            group_id = ""
            group_name = ""
            is_coord = False
            state = PlaybackState.STOPPED
            if client is not None:
                player = client.player
                volume = int(player.volume_level or 0)
                group = player.group
                if group is not None:
                    group_id = group.id
                    group_name = group.name or ""
                    is_coord = player.is_coordinator
                    state = _PLAYBACK_STATE_MAP.get(
                        str(group.playback_state or ""),
                        PlaybackState.STOPPED,
                    )
            infos.append(
                SpeakerInfo(
                    speaker_id=player_id,
                    name=meta.name,
                    ip_address=meta.ip_address,
                    model=meta.model,
                    group_id=group_id,
                    group_name=group_name,
                    is_group_coordinator=is_coord,
                    volume=volume,
                    state=state,
                )
            )
        infos.sort(key=lambda s: s.name.lower())
        return infos

    async def get_speaker(self, speaker_id: str) -> SpeakerInfo | None:
        if speaker_id not in self._player_metadata:
            return None
        # Reuse list_speakers's per-speaker materialization — small
        # enough to be fine, and keeps state-derivation in one place.
        infos = await self.list_speakers()
        return next((i for i in infos if i.speaker_id == speaker_id), None)

    # ── Playback ─────────────────────────────────────────────────────

    async def play_uri(self, request: PlayRequest) -> None:
        """Play an audio URI on the requested speakers.

        Dispatch table:

        - ``request.announce=True`` → ``player.play_audio_clip``. Native
          duck-and-restore on the speaker; ideal for TTS.
        - HTTP(S) URL → ``playback_session.create_session`` +
          ``load_stream_url`` against the authoritative group id returned
          by the preceding topology change. Sonos probes the URL's
          Content-Type and picks the right decoder.
        - ``spotify:…`` URI or ``open.spotify.com`` link →
          ``playback.load_content`` with a Spotify ``MetadataId``.
        """
        logger.info(
            "Sonos play_uri: uri=%s title=%r speaker_ids=%s volume=%s announce=%s",
            request.uri,
            request.title,
            request.speaker_ids,
            request.volume,
            request.announce,
        )
        target_ids = request.speaker_ids or list(self._player_metadata.keys())
        if not target_ids:
            raise RuntimeError("No speakers available")

        # Verify each target is actually connected before attempting
        # to play — otherwise we get misleading KeyErrors. Dedupe while
        # preserving order: callers sometimes pass the same player_id
        # twice (e.g. a speaker resolved via both its real name and an
        # alias), and Sonos rejects ``modifyGroupMembers`` with
        # "Effective set of new group members has repeated player id"
        # if duplicates survive that far.
        seen: set[str] = set()
        live: list[str] = []
        for tid in target_ids:
            if tid in seen or tid not in self._clients:
                continue
            seen.add(tid)
            live.append(tid)
        if not live:
            raise RuntimeError(
                f"None of the requested speakers ({target_ids}) are connected"
            )

        if request.announce:
            await self._play_audio_clip(live, request)
            return

        # The play path reshapes topology (if needed) and immediately
        # issues the playback command against the *authoritative* group
        # id returned by Sonos in the topology-change response —
        # ``modifyGroupMembers`` / ``createGroup`` return a ``GroupInfo``
        # whose ``group.id`` reflects whatever the server actually
        # settled on, coordinator election and all. This sidesteps the
        # cache race that made ``groupCoordinatorChanged`` persistent
        # with the old ``SonosGroup.play_stream_url`` wrapper (which
        # reads ``self.id`` from a potentially-stale cached object).
        #
        # A bounded retry still guards the edge case where another
        # controller (Sonos mobile app, a human walking into the
        # kitchen) reshuffles the household between our modify call and
        # our create_session call — rare, but real.
        backoffs = [0.5, 1.5]
        last_exc: FailedCommand | None = None
        for attempt, backoff in enumerate([0.0, *backoffs]):
            if backoff > 0:
                await asyncio.sleep(backoff)
            try:
                await self._dispatch_play(live, request)
                return
            except FailedCommand as exc:
                if "groupCoordinatorChanged" not in str(exc):
                    raise
                last_exc = exc
                logger.debug(
                    "Sonos groupCoordinatorChanged on dispatch attempt %d "
                    "— reshaping topology and retrying",
                    attempt + 1,
                    exc_info=True,
                )
        assert last_exc is not None
        raise last_exc

    async def _dispatch_play(
        self, live: list[str], request: PlayRequest
    ) -> None:
        """Reshape topology (if needed) and issue the play command.

        Separated from ``play_uri`` so the outer retry can re-run the
        whole dispatch — including a fresh ``_ensure_group`` call —
        rather than just the final Sonos command. That's important
        because on a ``groupCoordinatorChanged`` the authoritative
        group id we captured from the first modify has already been
        invalidated; we need to re-modify and re-capture.
        """
        coord_player_id, group_id = await self._ensure_group(live)
        coord_client = self._clients[coord_player_id]

        # Set volume across the group before loading content so the
        # playback opens at the intended level.
        if request.volume is not None:
            await self._set_group_volume(live, request.volume)

        spotify = _extract_spotify_ref(request.uri)
        if spotify is not None:
            await self._load_spotify_content(
                coord_player_id, spotify, request.title
            )
            return

        # Generic HTTP(S) stream. We bypass ``SonosGroup.play_stream_url``
        # because that wrapper pulls the group id from the cached
        # ``SonosGroup`` object (``self.id``) — and that cache is exactly
        # what goes stale after a topology change. Instead call the
        # low-level playback_session namespace with the authoritative
        # group id we captured from ``_ensure_group``.
        station_metadata: Container = {
            "_objectType": "container",
            "name": request.title or "Gilbert audio",
            "type": "station",
        }
        session = await coord_client.api.playback_session.create_session(
            group_id,
            app_id="com.gilbert.playback",
            app_context="1",
        )
        await coord_client.api.playback_session.load_stream_url(
            session_id=session["sessionId"],
            stream_url=request.uri,
            play_on_completion=True,
            station_metadata=station_metadata,
        )

    async def _load_spotify_content(
        self,
        coord_player_id: str,
        spotify: _SpotifyRef,
        title: str,
    ) -> None:
        """Play Spotify content via SMAPI on the group coordinator.

        Uses the legacy UPnP SOAP AVTransport endpoint on port 1400,
        not aiosonos's ``playback.loadContent`` — the latter returns
        ``ERROR_COMMAND_FAILED: Failed to enqueue track`` for every
        music-service URI on current S2 firmware, because that API
        path doesn't actually resolve music-service objects. See
        ``sonos_smapi`` for the full rationale and references.

        The coordinator's IP (for SOAP) and RINCON id (for the
        ``x-rincon-queue:`` transport pointer) both come from our
        per-player metadata cache. ``coord_player_id`` is the
        authoritative RINCON id returned by ``_ensure_group`` after
        the topology settle — using the cached ``SonosGroup.id``
        would risk pointing at a stale coordinator.
        """
        if self._smapi is None:
            raise RuntimeError("Sonos SMAPI client is not initialized")

        meta = self._player_metadata.get(coord_player_id)
        if meta is None:
            raise RuntimeError(
                f"Cannot play Spotify — no metadata for coordinator "
                f"{coord_player_id!r}"
            )

        logger.info(
            "Sonos SMAPI play: coord=%s(%s) title=%r spotify=%s",
            coord_player_id,
            meta.ip_address,
            title,
            spotify.uri,
        )
        try:
            await self._smapi.play_spotify(
                coord_ip=meta.ip_address,
                coord_rincon_id=coord_player_id,
                kind=spotify.kind,
                spotify_id=spotify.id,
                title=title,
            )
        except SmapiError as exc:
            logger.error(
                "Sonos SMAPI play FAILED: coord=%s spotify=%s error=%s",
                coord_player_id,
                spotify.uri,
                exc,
            )
            raise
        logger.info(
            "Sonos SMAPI play succeeded: coord=%s spotify=%s",
            coord_player_id,
            spotify.uri,
        )

    async def _play_audio_clip(
        self,
        speaker_ids: list[str],
        request: PlayRequest,
    ) -> None:
        """Fire a short overlay clip on each target speaker.

        Sonos's ``audio_clip`` API is single-speaker — the speaker itself
        handles the duck + restore. For multi-speaker announcements we
        just fire the clip on every target in parallel; the sync is
        good enough that listeners don't hear drift on short clips.
        """
        volume = request.volume
        name = request.title or "Gilbert announcement"

        async def _one(pid: str) -> None:
            client = self._clients.get(pid)
            if client is None:
                return
            try:
                await client.player.play_audio_clip(
                    request.uri,
                    volume=volume,
                    name=name,
                )
            except FailedCommand as exc:
                logger.warning(
                    "Audio clip failed on speaker %s: %s",
                    self._name_for(pid),
                    exc,
                )

        results = await asyncio.gather(
            *(_one(pid) for pid in speaker_ids), return_exceptions=True
        )
        failures = [r for r in results if isinstance(r, Exception)]
        if failures and len(failures) == len(results):
            # Every speaker rejected the clip — surface the first error
            # so the caller knows playback didn't happen.
            raise failures[0]

    async def _ensure_group(
        self, target_ids: list[str]
    ) -> tuple[str, str]:
        """Shape topology so ``target_ids`` form one group.

        Returns ``(coordinator_player_id, authoritative_group_id)``
        — both taken straight from Sonos's response to the topology-
        change command (``modifyGroupMembers`` / ``createGroup``).

        The coordinator bit matters: Sonos's local WebSocket API
        requires ``createSession`` to be issued through the
        **coordinator's** WebSocket, not any group member's. When our
        anchor (first target) isn't the coordinator after the
        reshuffle — Sonos elects based on its own heuristics — we
        return the elected coordinator's id so ``_dispatch_play`` can
        route the session command through the right client. This is
        the fix for the persistent ``groupCoordinatorChanged`` we saw
        even after adopting authoritative group ids.

        Dedupes ``target_ids`` defensively before hitting Sonos —
        repeated player ids trigger "Effective set of new group members
        has repeated player id", and callers that feed us from resolved
        speaker-name lists occasionally produce duplicates (e.g. when a
        speaker is addressed by both its device name and an alias).

        Prefers ``modify_group_members`` over ``set_group_members`` in
        the multi-speaker case — Music Assistant's experience is that
        ``setGroupMembers`` triggers more aggressive coordinator
        election, which is exactly the condition we're trying to avoid.
        """
        target_ids = list(dict.fromkeys(target_ids))

        if len(target_ids) == 1:
            pid = target_ids[0]
            client = self._clients[pid]
            current = _current_group_for_player(client, pid)

            # Already solo — the speaker is its own coordinator.
            if current is not None and list(current.player_ids) == [pid]:
                return pid, current.id

            # Solo the speaker: remove it from its current group. The
            # response's ``GroupInfo`` describes the *remnant* group
            # (the one pid left), not the new solo group, so we have
            # to wait for the push event that creates the solo group.
            # A solo group is its own coordinator by definition.
            if current is not None:
                try:
                    await client.api.groups.modify_group_members(
                        current.id,
                        player_ids_to_add=[],
                        player_ids_to_remove=[pid],
                    )
                except FailedCommand:
                    logger.debug(
                        "Sonos modify_group_members (solo) failed for %s",
                        pid,
                        exc_info=True,
                    )
            solo_id = await _wait_for_group(
                client,
                lambda g: g.coordinator_id == pid
                and list(g.player_ids) == [pid],
                timeout=_TOPOLOGY_SETTLE_TIMEOUT,
            )
            return pid, solo_id

        # Multi-speaker. We anchor on the first target for the
        # initial modify call, but we honor whatever coordinator
        # Sonos elects in the response — critically important so
        # we route ``createSession`` through the right WebSocket.
        anchor_id = target_ids[0]
        client = self._clients[anchor_id]
        current = _current_group_for_player(client, anchor_id)
        wanted_set = set(target_ids)

        if current is None:
            info = await client.api.groups.create_group(
                client.household_id,
                player_ids=target_ids,
            )
            group_data = info["group"]
            group_id = group_data["id"]
            coord_id = group_data.get("coordinatorId") or anchor_id
        else:
            current_set = set(current.player_ids)
            if current_set == wanted_set:
                return current.coordinator_id or anchor_id, current.id

            to_add = [pid for pid in target_ids if pid not in current_set]
            to_remove = [
                pid for pid in current.player_ids if pid not in wanted_set
            ]
            info = await client.api.groups.modify_group_members(
                current.id,
                player_ids_to_add=to_add,
                player_ids_to_remove=to_remove,
            )
            group_data = info["group"]
            group_id = group_data["id"]
            coord_id = group_data.get("coordinatorId") or anchor_id

        # Verify all target speakers actually landed in the group
        # before handing control back. Sonos's response confirms the
        # command was accepted, but with many speakers (e.g. household-
        # wide groups) some members can take up to a second or two to
        # fully join, and callers that immediately issue playback
        # commands can start streaming before stragglers are audible.
        #
        # We prefer matching by ``group.id == group_id`` (the
        # authoritative id Sonos just handed us), falling back to
        # matching by coordinator when the cache hasn't seen the new
        # id yet. The predicate requires every target_id to be in the
        # group's player_ids so no speaker is left behind.
        coord_client = self._clients.get(coord_id, client)
        await _wait_for_group(
            coord_client,
            lambda g: (
                (g.id == group_id or g.coordinator_id == coord_id)
                and wanted_set.issubset(set(g.player_ids))
            ),
            timeout=_TOPOLOGY_SETTLE_TIMEOUT,
        )
        return coord_id, group_id

    async def _set_group_volume(
        self, speaker_ids: list[str], volume: int
    ) -> None:
        """Apply the same volume to every speaker in ``speaker_ids``."""
        volume = max(0, min(100, int(volume)))

        async def _one(pid: str) -> None:
            client = self._clients.get(pid)
            if client is None:
                return
            try:
                await client.player.set_volume(volume)
            except FailedCommand:
                logger.debug(
                    "set_volume failed for %s", self._name_for(pid), exc_info=True
                )

        await asyncio.gather(*(_one(pid) for pid in speaker_ids))

    async def stop(self, speaker_ids: list[str] | None = None) -> None:
        """Stop playback on the targets (or everyone, if None)."""
        targets = speaker_ids or list(self._player_metadata.keys())
        seen_groups: set[str] = set()
        for pid in targets:
            client = self._clients.get(pid)
            if client is None:
                continue
            group = client.player.group
            if group is None or group.id in seen_groups:
                continue
            seen_groups.add(group.id)
            try:
                await group.pause()
            except FailedCommand:
                logger.debug(
                    "Pause failed for group %s",
                    group.id,
                    exc_info=True,
                )

    # ── Volume ───────────────────────────────────────────────────────

    async def get_volume(self, speaker_id: str) -> int:
        client = self._clients.get(speaker_id)
        if client is None:
            raise KeyError(f"Unknown speaker: {speaker_id}")
        return int(client.player.volume_level or 0)

    async def set_volume(self, speaker_id: str, volume: int) -> None:
        client = self._clients.get(speaker_id)
        if client is None:
            raise KeyError(f"Unknown speaker: {speaker_id}")
        await client.player.set_volume(max(0, min(100, int(volume))))

    # ── Transport state ──────────────────────────────────────────────

    async def get_playback_state(self, speaker_id: str) -> PlaybackState:
        client = self._clients.get(speaker_id)
        if client is None:
            return PlaybackState.STOPPED
        group = client.player.group
        if group is None:
            return PlaybackState.STOPPED
        return _PLAYBACK_STATE_MAP.get(
            str(group.playback_state or ""),
            PlaybackState.STOPPED,
        )

    async def get_now_playing(self, speaker_id: str) -> NowPlaying:
        """Pull the latest metadata for whatever's playing on the speaker's group."""
        client = self._clients.get(speaker_id)
        state = await self.get_playback_state(speaker_id)
        if client is None:
            return NowPlaying(state=state)

        group = client.player.group
        if group is None:
            return NowPlaying(state=state)

        meta = group.playback_metadata
        if not meta:
            return NowPlaying(state=state)

        # aiosonos's ``MetadataStatus`` is a ``TypedDict`` — at runtime
        # it's a plain dict, so fields MUST be accessed via ``.get(...)``
        # (not ``getattr``, which always returns the default because
        # dicts don't expose keys as attributes). Track/Album/Artist
        # objects inside it are also TypedDicts — same rule.
        current_item = meta.get("currentItem") or {}
        track = current_item.get("track") or {}
        title = str(track.get("name") or "")
        artist = str((track.get("artist") or {}).get("name") or "")
        album = str((track.get("album") or {}).get("name") or "")
        images = track.get("images") or []
        album_art = str((images[0].get("url") if images else "") or "")
        duration_ms = int(track.get("durationMillis") or 0)
        position_ms = int(meta.get("positionMillis") or 0)

        return NowPlaying(
            state=state,
            title=title,
            artist=artist,
            album=album,
            album_art_url=album_art,
            duration_seconds=duration_ms / 1000.0,
            position_seconds=position_ms / 1000.0,
        )

    # ── Grouping ─────────────────────────────────────────────────────

    @property
    def supports_grouping(self) -> bool:
        return True

    async def list_groups(self) -> list[SpeakerGroup]:
        """Return every unique group across every household's clients."""
        seen: dict[str, SpeakerGroup] = {}
        for client in self._clients.values():
            for group in client.groups:
                if group.id in seen:
                    continue
                seen[group.id] = SpeakerGroup(
                    group_id=group.id,
                    name=group.name or "",
                    coordinator_id=group.coordinator_id or "",
                    member_ids=list(group.player_ids),
                )
        return sorted(seen.values(), key=lambda g: g.name.lower())

    async def group_speakers(self, speaker_ids: list[str]) -> SpeakerGroup:
        """Form a group from ``speaker_ids``; returns the resulting group."""
        if not speaker_ids:
            raise ValueError("speaker_ids is empty")
        coord_id, group_id = await self._ensure_group(speaker_ids)
        client = self._clients[coord_id]
        # Prefer the freshly-captured authoritative id to look up the
        # SonosGroup object, falling back to whatever matches the
        # coordinator — the cache may not yet have the new group by
        # id if the push event hasn't landed.
        group = next(
            (g for g in client.groups if g.id == group_id),
            None,
        ) or _current_group_for_player(client, coord_id)
        if group is None:
            return SpeakerGroup(
                group_id=group_id,
                name="",
                coordinator_id=coord_id,
                member_ids=list(speaker_ids),
            )
        return SpeakerGroup(
            group_id=group.id,
            name=group.name or "",
            coordinator_id=group.coordinator_id or coord_id,
            member_ids=list(group.player_ids),
        )

    async def ungroup_speakers(self, speaker_ids: list[str]) -> None:
        """Split each requested speaker into its own solo group."""
        for pid in speaker_ids:
            client = self._clients.get(pid)
            if client is None:
                continue
            try:
                await client.player.leave_group()
            except FailedCommand:
                logger.debug(
                    "leave_group failed for %s", self._name_for(pid), exc_info=True
                )

    # ── Snapshot/restore — no-op under aiosonos ──────────────────────

    async def snapshot(self, speaker_ids: list[str]) -> None:
        """No-op: the aiosonos ``audio_clip`` API self-restores.

        Callers that still invoke ``snapshot``/``restore`` around an
        announcement flow (notably ``SpeakerService.announce``) don't
        need to change — they just become cheap no-ops. The proper
        integration is to set ``PlayRequest.announce=True``, which
        routes to ``player.play_audio_clip`` and handles duck+restore
        natively.
        """

    async def restore(self, speaker_ids: list[str]) -> None:
        """See ``snapshot``."""

    # ── Helpers ──────────────────────────────────────────────────────

    def _name_for(self, player_id: str) -> str:
        meta = self._player_metadata.get(player_id)
        return meta.name if meta else player_id


# ── Group lookup ─────────────────────────────────────────────────────


def _current_group_for_player(
    client: SonosLocalApiClient, player_id: str
) -> Any:
    """Find the fresh group containing ``player_id`` in ``client.groups``.

    We deliberately do NOT use ``client.player.group`` here — that
    attribute is only refreshed when ``SonosPlayer.check_active_group``
    fires, which happens on the player's own data events, not on
    every household-level topology event. ``client.groups`` (i.e.
    ``client._groups``) IS refreshed on every topology push event.

    Matches either a coordinator (``group.coordinator_id == player_id``)
    or a member (``player_id in group.player_ids``). Returns ``None``
    if the player isn't in any group — which happens only briefly
    during topology reshuffles; ``_wait_for_group`` handles the wait.
    """
    for group in client.groups:
        if group.coordinator_id == player_id or player_id in group.player_ids:
            return group
    return None


async def _wait_for_group(
    client: SonosLocalApiClient,
    predicate: Any,
    timeout: float,
    poll_interval: float = _TOPOLOGY_POLL_INTERVAL,
) -> str:
    """Wait (bounded) for a group matching ``predicate`` to appear.

    Adapted from Home Assistant's ``SonosSpeaker.wait_for_groups``
    pattern but with a belt-and-braces poll loop: subscribe to
    groups/player events *and* recheck ``client.groups`` every
    ``poll_interval`` seconds until a predicate returns true. Required
    for operations where Sonos's response doesn't identify the new
    group directly — notably soloing a speaker via
    ``modifyGroupMembers``, whose ``GroupInfo`` response describes the
    *remnant* group rather than the new solo group.

    Why poll as well as subscribe: on large households (12+ speakers)
    aiosonos occasionally swallows push events during the storm of
    updates that accompanies a full-house regroup, so an event-only
    wait can time out even though the group has already formed and
    ``client.groups`` reflects it. The poll is a cheap cache read —
    no network traffic — so it's effectively free insurance.

    Returns the id of the first matching group. Raises ``RuntimeError``
    on timeout (rather than hanging the caller's play command).
    """
    settled = asyncio.Event()
    found: list[str] = []

    def _check() -> bool:
        for group in client.groups:
            if predicate(group):
                if not found:
                    found.append(group.id)
                return True
        return False

    if _check():
        return found[0]

    def _on_event(_event: Any) -> None:
        if _check():
            settled.set()

    async def _poll() -> None:
        while not settled.is_set():
            try:
                await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                return
            if _check():
                settled.set()
                return

    unsub = client.subscribe(
        _on_event,
        event_filter=(
            EventType.GROUP_ADDED,
            EventType.GROUP_UPDATED,
            EventType.GROUP_REMOVED,
            EventType.PLAYER_UPDATED,
        ),
    )
    poll_task = asyncio.create_task(_poll())
    try:
        try:
            await asyncio.wait_for(settled.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            # One final check — an event or poll tick might have
            # landed in the gap between the last callback and the
            # timeout firing.
            if _check():
                return found[0]
            raise RuntimeError(
                f"Timed out after {timeout:.1f}s waiting for Sonos "
                f"topology to settle"
            ) from exc
    finally:
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass
        unsub()
    return found[0]


# ── Spotify URI parsing ──────────────────────────────────────────────


@dataclass
class _SpotifyRef:
    kind: str  # track | album | playlist | artist | episode | show
    id: str
    uri: str  # canonical ``spotify:<kind>:<id>``


# Spotify content-kind strings accepted by Sonos's ``playback.loadContent``.
# Per docs.sonos.com/docs/playback-objects the ``type`` field uses
# lowercase values (``track``, ``playlist``, ``album``, ``artist``,
# ``episode``, ``show``). The aiosonos docstring shows uppercase — that's
# stale; the local API returns a malformed-request error for uppercase.
_SPOTIFY_KIND_TO_LOAD_TYPE: dict[str, str] = {
    "track": "track",
    "album": "album",
    "playlist": "playlist",
    "artist": "artist",
    "episode": "episode",
    "show": "show",
}


def _extract_spotify_ref(uri: str) -> _SpotifyRef | None:
    """Detect a Spotify reference in ``uri``.

    Accepts both the canonical ``spotify:track:…`` scheme and
    ``https://open.spotify.com/track/…`` web URLs. Returns ``None``
    when the URI is neither — caller should treat as a plain HTTP
    stream.
    """
    if not uri:
        return None
    stripped = uri.strip()
    match = _SPOTIFY_URI_RE.match(stripped)
    if match:
        kind, obj_id = match.group(1), match.group(2)
        return _SpotifyRef(kind=kind, id=obj_id, uri=stripped)
    match = _SPOTIFY_OPEN_URL_RE.search(stripped)
    if match:
        kind, obj_id = match.group(1), match.group(2)
        return _SpotifyRef(
            kind=kind, id=obj_id, uri=f"spotify:{kind}:{obj_id}"
        )
    return None
