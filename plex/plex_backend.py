"""PlexBackend — concrete ``MediaLibraryBackend`` for Plex Media Server.

Wraps ``plexapi`` for browsing / search / playback dispatch and uses
``httpx`` directly for the Plex.tv PIN-link flow and a few endpoints
plexapi doesn't surface conveniently.

Per spec §8 and ``memory-multi-user-isolation.md`` and the per-Plex-Home-
user lock pattern in §8.5: token + ``PlexServer`` caches are keyed by
the Plex Home user uuid (NOT by Gilbert user id), with a per-user
``asyncio.Lock`` so two concurrent calls for the same Home user
serialize but two for different Home users do not.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

# plexapi is intentionally imported lazily inside methods that need it
# so tests can replace ``plexapi.PlexServer`` / ``plexapi.MyPlexAccount``
# via ``unittest.mock`` without forcing the import at module load.
from plexapi.exceptions import NotFound, PlexApiException, Unauthorized

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.media_library import (
    ContinueWatchingEntry,
    MediaClient,
    MediaClientNotFoundError,
    MediaItem,
    MediaKind,
    MediaLibraryBackend,
    MediaLibraryUnavailableError,
    MediaPlaybackState,
    MediaPlayCommand,
    MediaSearchFilters,
    MediaSession,
    RecentlyAddedEntry,
)
from gilbert.interfaces.plugin import RuntimeDependency
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)


# Plex.tv endpoints used by the PIN linking flow.
_PLEX_TV_PINS = "https://plex.tv/api/v2/pins"
_PLEX_TV_RESOURCES = "https://plex.tv/api/v2/resources"


def _utc_seconds_from_addedat(value: Any) -> float:
    """Plex returns ``addedAt`` as either a unix timestamp (Plex Server
    local epoch) or a ``datetime`` from plexapi's helpers. Normalize to
    UTC unix seconds at the mapping boundary so consumers don't see
    server-local times leak through.
    """
    if value is None:
        return 0.0
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # plexapi returns naive datetimes in the *server's* local tz
            # in some places; treat naive as UTC since the underlying
            # Plex epoch is already UTC seconds-since-epoch from the
            # server's clock. This matches the ``addedAt`` integer path.
            return value.replace(tzinfo=UTC).timestamp()
        return value.astimezone(UTC).timestamp()
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _kind_from_plex(obj: Any) -> MediaKind:
    """Map plexapi's ``type`` attribute to ``MediaKind``."""
    type_str = str(getattr(obj, "type", "") or "").lower()
    return {
        "movie": MediaKind.MOVIE,
        "show": MediaKind.SHOW,
        "season": MediaKind.SEASON,
        "episode": MediaKind.EPISODE,
        "artist": MediaKind.MUSIC_ARTIST,
        "album": MediaKind.MUSIC_ALBUM,
        "track": MediaKind.MUSIC_TRACK,
        "musicvideo": MediaKind.MUSIC_VIDEO,
        "photo": MediaKind.PHOTO,
    }.get(type_str, MediaKind.UNKNOWN)


def _attr(obj: Any, name: str, default: Any = "") -> Any:
    """Safe ``getattr`` with default."""
    val = getattr(obj, name, default)
    return val if val is not None else default


def _plex_to_media_item(
    obj: Any, *, server_id: str = ""
) -> MediaItem:
    """Convert a plexapi object (Movie / Show / Episode / ...) into a
    ``MediaItem``.

    Pure function — no I/O, no plexapi-specific imports beyond what's
    needed to read attribute names. Works against real plexapi objects
    AND mock objects with the same attribute surface, which is how the
    tests run against recorded XML fixtures.
    """
    kind = _kind_from_plex(obj)

    rating_key = str(_attr(obj, "ratingKey", ""))
    title = str(_attr(obj, "title", ""))
    sort_title = str(_attr(obj, "titleSort", "") or title)
    year = _attr(obj, "year", None)
    duration_ms = _attr(obj, "duration", 0)
    duration_seconds = (
        float(duration_ms) / 1000.0 if duration_ms else 0.0
    )
    summary = str(_attr(obj, "summary", "") or "")
    rating_raw = _attr(obj, "rating", None)
    try:
        rating: float | None = (
            float(rating_raw) if rating_raw is not None else None
        )
    except (TypeError, ValueError):
        rating = None
    content_rating = str(_attr(obj, "contentRating", "") or "")
    studio = str(_attr(obj, "studio", "") or "")

    def _names(attr_name: str) -> tuple[str, ...]:
        items = _attr(obj, attr_name, []) or []
        out: list[str] = []
        for entry in items:
            tag = getattr(entry, "tag", None)
            if isinstance(tag, str) and tag:
                out.append(tag)
        return tuple(out)

    genres = _names("genres")
    actors = _names("roles")
    directors = _names("directors")

    poster_url = str(_attr(obj, "thumbUrl", "") or "")
    backdrop_url = str(_attr(obj, "artUrl", "") or "")

    parent_id = str(_attr(obj, "parentRatingKey", "") or "")
    parent_title = str(_attr(obj, "parentTitle", "") or "")
    grandparent_id = str(_attr(obj, "grandparentRatingKey", "") or "")
    grandparent_title = str(_attr(obj, "grandparentTitle", "") or "")
    season_number = _attr(obj, "seasonNumber", None)
    episode_number = _attr(obj, "index", None)
    if kind != MediaKind.EPISODE:
        episode_number = None  # only meaningful for EPISODE

    library_section = ""
    section = getattr(obj, "section", None)
    if callable(section):
        try:
            section_obj = section()
            library_section = str(getattr(section_obj, "title", "") or "")
        except Exception:
            library_section = ""
    elif hasattr(obj, "librarySectionTitle"):
        library_section = str(_attr(obj, "librarySectionTitle", "") or "")

    added_at = _utc_seconds_from_addedat(_attr(obj, "addedAt", None))
    last_viewed_at = _utc_seconds_from_addedat(
        _attr(obj, "lastViewedAt", None)
    )
    view_count = int(_attr(obj, "viewCount", 0) or 0)
    view_offset_ms = int(_attr(obj, "viewOffset", 0) or 0)
    view_offset_seconds = float(view_offset_ms) / 1000.0
    is_watched = view_count > 0

    return MediaItem(
        id=rating_key,
        backend_name="plex",
        server_id=server_id,
        title=title,
        kind=kind,
        sort_title=sort_title,
        year=int(year) if year is not None else None,
        duration_seconds=duration_seconds,
        summary=summary,
        rating=rating,
        content_rating=content_rating,
        studio=studio,
        genres=genres,
        actors=actors,
        directors=directors,
        poster_url=poster_url,
        backdrop_url=backdrop_url,
        parent_id=parent_id,
        parent_title=parent_title,
        grandparent_id=grandparent_id,
        grandparent_title=grandparent_title,
        season_number=int(season_number) if season_number is not None else None,
        episode_number=(
            int(episode_number) if episode_number is not None else None
        ),
        library_section=library_section,
        added_at=added_at,
        last_viewed_at=last_viewed_at,
        view_count=view_count,
        view_offset_seconds=view_offset_seconds,
        is_watched=is_watched,
    )


def _plex_session_to_media_session(
    obj: Any, *, server_id: str
) -> MediaSession:
    """Map a plexapi ``session`` (an item-like object with extra
    Player / User attributes) to ``MediaSession``.
    """
    item = _plex_to_media_item(obj, server_id=server_id)
    player = getattr(obj, "player", None) or getattr(obj, "Player", None)
    user = getattr(obj, "user", None) or getattr(obj, "User", None)

    client_id = ""
    client_name = "Unknown"
    device = ""
    platform = ""
    if player is not None:
        client_id = str(_attr(player, "machineIdentifier", "") or "")
        client_name = str(_attr(player, "title", "") or "Unknown")
        device = str(_attr(player, "device", "") or "")
        platform = str(_attr(player, "platform", "") or "")
        state_str = str(_attr(player, "state", "") or "").lower()
    else:
        state_str = ""

    state = {
        "playing": MediaPlaybackState.PLAYING,
        "paused": MediaPlaybackState.PAUSED,
        "buffering": MediaPlaybackState.BUFFERING,
        "stopped": MediaPlaybackState.STOPPED,
    }.get(state_str, MediaPlaybackState.PLAYING)

    backend_user_name = ""
    if user is not None:
        backend_user_name = str(_attr(user, "title", "") or "")

    transcode = getattr(obj, "transcodeSessions", None) or []
    is_transcoding = bool(transcode)
    quality_label = ""
    media_list = getattr(obj, "media", None) or []
    if media_list:
        first_media = media_list[0]
        bitrate = _attr(first_media, "bitrate", "")
        resolution = _attr(first_media, "videoResolution", "")
        if resolution or bitrate:
            quality_label = f"{resolution} @ {bitrate}".strip(" @")

    client = MediaClient(
        client_id=client_id,
        backend_name="plex",
        server_id=server_id,
        name=client_name,
        device=device,
        platform=platform,
        is_online=True,
        supports_seek=True,
        supports_remote_control=True,
        last_seen_at=time.time(),
    )

    session_id = str(_attr(obj, "sessionKey", "") or item.id)
    return MediaSession(
        session_id=session_id,
        backend_name="plex",
        client=client,
        item=item,
        state=state,
        position_seconds=item.view_offset_seconds,
        duration_seconds=item.duration_seconds,
        backend_user_name=backend_user_name,
        is_transcoding=is_transcoding,
        quality_label=quality_label,
    )


def _plex_player_to_media_client(
    obj: Any, *, server_id: str
) -> MediaClient:
    return MediaClient(
        client_id=str(_attr(obj, "machineIdentifier", "") or ""),
        backend_name="plex",
        server_id=server_id,
        name=str(_attr(obj, "name", "") or _attr(obj, "title", "") or ""),
        device=str(_attr(obj, "device", "") or _attr(obj, "product", "") or ""),
        platform=str(_attr(obj, "platform", "") or ""),
        address=str(_attr(obj, "address", "") or ""),
        is_online=True,
        supports_remote_control=True,
        supports_seek=True,
        last_seen_at=time.time(),
    )


# ── Backend ───────────────────────────────────────────────────────


class PlexBackend(MediaLibraryBackend):
    backend_name = "plex"
    supports_now_playing = True
    supports_resume = True
    supports_continue_watching = True
    supports_recently_added = True
    supports_seek = True
    supports_per_user = True
    supports_next_episode = True

    def __init__(self) -> None:
        self._account_token: str = ""
        self._server_url: str = ""
        self._machine_id: str = ""
        self._verify_tls: bool = True
        self._timeout: float = 15.0
        self._default_user_token: str = ""
        self._device_id: str = uuid.uuid4().hex

        self._account: Any | None = None
        self._server: Any | None = None
        self._http: httpx.AsyncClient | None = None

        # Per-Home-user caches keyed by Plex Home user uuid (NOT Gilbert
        # user id — see spec §6.3 cache layering).
        self._user_tokens: dict[str, str] = {}
        self._user_servers: dict[str, Any] = {}
        self._user_locks_dict_lock = asyncio.Lock()
        self._user_locks: dict[str, asyncio.Lock] = {}

    # ── Backend metadata / config ───────────────────────────────────

    @classmethod
    def runtime_dependencies(cls) -> list[RuntimeDependency]:
        return []

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="account_token",
                type=ToolParameterType.STRING,
                description=(
                    "Plex.tv account token (X-Plex-Token). Obtained "
                    "via the Link Account flow."
                ),
                default="",
                sensitive=True,
                restart_required=True,
            ),
            ConfigParam(
                key="server_machine_id",
                type=ToolParameterType.STRING,
                description=(
                    "Machine identifier of the chosen server. Filled "
                    "by the Choose Server step."
                ),
                default="",
                restart_required=True,
            ),
            ConfigParam(
                key="server_url",
                type=ToolParameterType.STRING,
                description=(
                    "Override the auto-discovered Plex URL. Empty = "
                    "let plexapi pick."
                ),
                default="",
            ),
            ConfigParam(
                key="verify_tls",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Verify TLS for https Plex URLs. Some self-signed "
                    "setups require False."
                ),
                default=True,
            ),
            ConfigParam(
                key="request_timeout_seconds",
                type=ToolParameterType.NUMBER,
                description="Request timeout (seconds).",
                default=15.0,
            ),
            ConfigParam(
                key="default_user_token",
                type=ToolParameterType.STRING,
                description=(
                    "Optional fallback X-Plex-Token used for "
                    "no-mapping calls. Defaults to account_token."
                ),
                default="",
                sensitive=True,
            ),
        ]

    # ── ConfigActionProvider (also a BackendActionProvider) ─────────

    def backend_actions(self) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="link_account",
                label="Link Plex account",
                description=(
                    "Generate a Plex.tv PIN; finish at plex.tv/link, "
                    "then click Continue."
                ),
                required_role="admin",
            ),
            ConfigAction(
                key="link_account_complete",
                label="Continue",
                description=(
                    "Finish the PIN flow once the user has approved on "
                    "plex.tv/link."
                ),
                hidden=True,
                required_role="admin",
            ),
            ConfigAction(
                key="choose_server",
                label="Choose server",
                description=(
                    "List Plex.tv resources owned by this account; pick "
                    "one to populate server_machine_id + server_url."
                ),
                required_role="admin",
            ),
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description="Verify the server is reachable.",
                required_role="admin",
            ),
        ]

    async def invoke_backend_action(
        self, key: str, payload: dict[str, Any]
    ) -> ConfigActionResult:
        if key == "link_account":
            return await self._link_account_start(payload)
        if key == "link_account_complete":
            return await self._link_account_complete(payload)
        if key == "choose_server":
            return await self._choose_server(payload)
        if key == "test_connection":
            return await self._test_connection(payload)
        return ConfigActionResult(
            status="error",
            message=f"Unknown Plex action '{key}'",
        )

    async def _link_account_start(
        self, payload: dict[str, Any]
    ) -> ConfigActionResult:
        client_id = self._device_id
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(
                    _PLEX_TV_PINS,
                    headers={
                        "Accept": "application/json",
                        "X-Plex-Client-Identifier": client_id,
                        "X-Plex-Product": "Gilbert",
                        "X-Plex-Version": "1.0.0",
                    },
                    params={"strong": "true"},
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                return ConfigActionResult(
                    status="error",
                    message=f"Plex.tv PIN request failed: {exc}",
                )
        data = resp.json()
        pin_id = data.get("id")
        code = data.get("code")
        return ConfigActionResult(
            status="pending",
            message=f"Visit plex.tv/link and enter code: {code}",
            open_url="https://plex.tv/link",
            followup_action="link_account_complete",
            data={"pin_id": pin_id, "code": code, "client_id": client_id},
        )

    async def _link_account_complete(
        self, payload: dict[str, Any]
    ) -> ConfigActionResult:
        pin_id = payload.get("pin_id")
        client_id = str(payload.get("client_id") or self._device_id)
        if not pin_id:
            return ConfigActionResult(
                status="error",
                message="link_account_complete requires {pin_id}",
            )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(
                    f"{_PLEX_TV_PINS}/{pin_id}",
                    headers={
                        "Accept": "application/json",
                        "X-Plex-Client-Identifier": client_id,
                    },
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                return ConfigActionResult(
                    status="error",
                    message=f"Could not fetch PIN status: {exc}",
                )
        data = resp.json()
        token = data.get("authToken")
        if not token:
            return ConfigActionResult(
                status="pending",
                message=(
                    "Not yet authorized. Approve at plex.tv/link, "
                    "then click Continue again."
                ),
                followup_action="link_account_complete",
                data={"pin_id": pin_id, "client_id": client_id},
            )
        return ConfigActionResult(
            status="ok",
            message="Plex account linked.",
            data={"persist": {"account_token": token}},
        )

    async def _choose_server(
        self, payload: dict[str, Any]
    ) -> ConfigActionResult:
        if not self._account_token:
            return ConfigActionResult(
                status="error",
                message="Link your Plex account first.",
            )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(
                    _PLEX_TV_RESOURCES,
                    headers={
                        "Accept": "application/json",
                        "X-Plex-Token": self._account_token,
                        "X-Plex-Client-Identifier": self._device_id,
                    },
                    params={"includeHttps": "1", "includeRelay": "1"},
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                return ConfigActionResult(
                    status="error",
                    message=f"Plex.tv resources fetch failed: {exc}",
                )
        resources = resp.json() or []
        servers: list[dict[str, str]] = []
        for r in resources:
            provides = str(r.get("provides", ""))
            if "server" not in provides:
                continue
            servers.append(
                {
                    "id": str(r.get("clientIdentifier", "")),
                    "name": str(r.get("name", "")),
                    "url": str(r.get("publicAddress", "")),
                }
            )
        return ConfigActionResult(
            status="ok",
            message=f"Found {len(servers)} server(s).",
            data={"servers": servers},
        )

    async def _test_connection(
        self, payload: dict[str, Any]
    ) -> ConfigActionResult:
        if not self._server_url or not self._account_token:
            return ConfigActionResult(
                status="error",
                message="Configure server_url + account_token first.",
            )
        async with httpx.AsyncClient(
            timeout=self._timeout, verify=self._verify_tls
        ) as client:
            try:
                resp = await client.get(
                    f"{self._server_url.rstrip('/')}/identity",
                    headers={"X-Plex-Token": self._account_token},
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                return ConfigActionResult(
                    status="error",
                    message=f"Test connection failed: {exc}",
                )
        return ConfigActionResult(
            status="ok",
            message=f"Reached {self._server_url} successfully.",
        )

    # ── Lifecycle ───────────────────────────────────────────────────

    async def initialize(self, config: dict[str, object]) -> None:
        prev_token = self._account_token
        self._account_token = str(config.get("account_token") or "")
        self._server_url = str(config.get("server_url") or "")
        self._machine_id = str(config.get("server_machine_id") or "")
        self._verify_tls = bool(config.get("verify_tls", True))
        try:
            self._timeout = float(
                config.get("request_timeout_seconds") or 15.0
            )
        except (TypeError, ValueError):
            self._timeout = 15.0
        self._default_user_token = str(
            config.get("default_user_token") or ""
        )

        # Token rotation → atomically clear all per-Home-user caches.
        if prev_token and prev_token != self._account_token:
            self._user_tokens.clear()
            self._user_servers.clear()
            self._user_locks.clear()

        if not self._account_token:
            self._account = None
            self._server = None
            return

        # Build account / server lazily — wrap in to_thread because
        # plexapi __init__ does sync HTTP.
        try:
            from plexapi.myplex import MyPlexAccount
            from plexapi.server import PlexServer

            self._account = await asyncio.to_thread(
                MyPlexAccount, token=self._account_token
            )
            if self._server_url:
                self._server = await asyncio.to_thread(
                    PlexServer,
                    self._server_url,
                    token=self._account_token,
                )
            elif self._machine_id and self._account is not None:
                # Resolve via plex.tv resource list.
                self._server = await asyncio.to_thread(
                    self._account.resource(self._machine_id).connect
                )
        except Unauthorized as exc:
            raise MediaLibraryUnavailableError(
                "Plex token revoked"
            ) from exc
        except PlexApiException as exc:
            raise MediaLibraryUnavailableError(str(exc)) from exc

        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
        self._http = httpx.AsyncClient(
            timeout=self._timeout, verify=self._verify_tls
        )

    async def close(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                logger.debug("Plex http close raised", exc_info=True)
            self._http = None
        # plexapi has no explicit close.

    # ── Per-user lock + server cache ────────────────────────────────

    async def _get_user_lock(self, backend_user_id: str) -> asyncio.Lock:
        async with self._user_locks_dict_lock:
            lock = self._user_locks.get(backend_user_id)
            if lock is None:
                lock = asyncio.Lock()
                self._user_locks[backend_user_id] = lock
            return lock

    async def _get_user_server(self, backend_user_id: str) -> Any:
        if not backend_user_id:
            if self._server is None:
                raise MediaLibraryUnavailableError("Plex not configured")
            return self._server
        lock = await self._get_user_lock(backend_user_id)
        async with lock:
            if backend_user_id in self._user_servers:
                return self._user_servers[backend_user_id]
            if self._account is None or self._server is None:
                raise MediaLibraryUnavailableError("Plex not configured")
            try:
                # plexapi: account.user(<id>).get_token(machine_id)
                token = await asyncio.to_thread(
                    self._account.user(backend_user_id).get_token,
                    self._server.machineIdentifier,
                )
            except Unauthorized as exc:
                raise MediaLibraryUnavailableError(
                    "Plex token revoked"
                ) from exc
            except (NotFound, PlexApiException) as exc:
                raise MediaLibraryUnavailableError(str(exc)) from exc
            self._user_tokens[backend_user_id] = token
            from plexapi.server import PlexServer

            try:
                server = await asyncio.to_thread(
                    PlexServer,
                    self._server.url,
                    token=token,
                )
            except (Unauthorized, PlexApiException) as exc:
                raise MediaLibraryUnavailableError(str(exc)) from exc
            self._user_servers[backend_user_id] = server
            return server

    def _evict_user(self, backend_user_id: str) -> None:
        self._user_tokens.pop(backend_user_id, None)
        self._user_servers.pop(backend_user_id, None)

    async def _server_id(self, backend_user_id: str = "") -> str:
        # Prefer the running server's machineIdentifier; fall back to
        # the configured value.
        if self._server is not None:
            try:
                return str(getattr(self._server, "machineIdentifier", ""))
            except Exception:
                pass
        return self._machine_id

    # ── Library queries ────────────────────────────────────────────

    async def search(
        self,
        query: str,
        *,
        filters: MediaSearchFilters | None = None,
        backend_user_id: str = "",
    ) -> list[MediaItem]:
        try:
            server = await self._get_user_server(backend_user_id)
        except MediaLibraryUnavailableError:
            raise
        kwargs: dict[str, Any] = {}
        if filters:
            mediatypes = []
            for k in filters.kinds:
                mt = {
                    MediaKind.MOVIE: "movie",
                    MediaKind.SHOW: "show",
                    MediaKind.SEASON: "season",
                    MediaKind.EPISODE: "episode",
                    MediaKind.MUSIC_ARTIST: "artist",
                    MediaKind.MUSIC_ALBUM: "album",
                    MediaKind.MUSIC_TRACK: "track",
                }.get(k)
                if mt:
                    mediatypes.append(mt)
            if mediatypes:
                kwargs["mediatype"] = mediatypes[0]
            if filters.limit:
                kwargs["limit"] = filters.limit
        try:
            results = await asyncio.to_thread(
                lambda: list(server.search(query, **kwargs))
            )
        except Unauthorized as exc:
            self._evict_user(backend_user_id)
            raise MediaLibraryUnavailableError(
                "Plex token revoked"
            ) from exc
        except PlexApiException as exc:
            raise MediaLibraryUnavailableError(str(exc)) from exc
        sid = await self._server_id(backend_user_id)
        items = [_plex_to_media_item(r, server_id=sid) for r in results]
        if filters and filters.year_from is not None:
            items = [i for i in items if i.year and i.year >= filters.year_from]
        if filters and filters.year_to is not None:
            items = [i for i in items if i.year and i.year <= filters.year_to]
        if filters and filters.unwatched_only:
            items = [i for i in items if not i.is_watched]
        if filters and filters.library_section:
            items = [
                i for i in items if i.library_section == filters.library_section
            ]
        if filters and filters.limit:
            items = items[: filters.limit]
        return items

    async def get_item(
        self, item_id: str, backend_user_id: str = ""
    ) -> MediaItem | None:
        try:
            server = await self._get_user_server(backend_user_id)
        except MediaLibraryUnavailableError:
            raise
        try:
            obj = await asyncio.to_thread(server.fetchItem, int(item_id))
        except (NotFound, ValueError):
            return None
        except Unauthorized as exc:
            self._evict_user(backend_user_id)
            raise MediaLibraryUnavailableError(
                "Plex token revoked"
            ) from exc
        except PlexApiException as exc:
            raise MediaLibraryUnavailableError(str(exc)) from exc
        return _plex_to_media_item(obj, server_id=await self._server_id())

    async def list_libraries(self, backend_user_id: str = "") -> list[str]:
        try:
            server = await self._get_user_server(backend_user_id)
        except MediaLibraryUnavailableError:
            raise
        try:
            sections = await asyncio.to_thread(
                lambda: list(server.library.sections())
            )
        except (Unauthorized, PlexApiException) as exc:
            raise MediaLibraryUnavailableError(str(exc)) from exc
        return [str(s.title) for s in sections]

    async def list_backend_users(self) -> list[dict[str, str]]:
        if self._account is None:
            raise MediaLibraryUnavailableError("Plex not configured")
        try:
            users = await asyncio.to_thread(self._account.users)
        except (Unauthorized, PlexApiException) as exc:
            raise MediaLibraryUnavailableError(str(exc)) from exc
        out: list[dict[str, str]] = []
        # Owner first (admin token holder).
        try:
            me = await asyncio.to_thread(lambda: self._account)
            out.append(
                {
                    "id": str(getattr(me, "uuid", "") or "owner"),
                    "username": str(
                        getattr(me, "username", "") or "owner"
                    ),
                    "display_name": str(
                        getattr(me, "title", "") or "Owner"
                    ),
                }
            )
        except Exception:
            pass
        for u in users or []:
            out.append(
                {
                    "id": str(getattr(u, "id", "") or getattr(u, "uuid", "")),
                    "username": str(getattr(u, "username", "") or ""),
                    "display_name": str(getattr(u, "title", "") or ""),
                }
            )
        return out

    async def recently_added(
        self,
        *,
        kind: MediaKind | None = None,
        limit: int = 10,
        library_section: str = "",
        backend_user_id: str = "",
    ) -> list[RecentlyAddedEntry]:
        try:
            server = await self._get_user_server(backend_user_id)
        except MediaLibraryUnavailableError:
            raise
        try:
            results = await asyncio.to_thread(
                lambda: list(server.library.recentlyAdded())
            )
        except Unauthorized as exc:
            self._evict_user(backend_user_id)
            raise MediaLibraryUnavailableError(
                "Plex token revoked"
            ) from exc
        except PlexApiException as exc:
            raise MediaLibraryUnavailableError(str(exc)) from exc
        sid = await self._server_id()
        items = [_plex_to_media_item(r, server_id=sid) for r in results]
        if kind is not None:
            items = [i for i in items if i.kind == kind]
        if library_section:
            items = [
                i for i in items if i.library_section == library_section
            ]
        items.sort(key=lambda i: -i.added_at)
        return [
            RecentlyAddedEntry(item=i, added_at=i.added_at)
            for i in items[:limit]
        ]

    async def continue_watching(
        self,
        *,
        backend_user_id: str = "",
        limit: int = 10,
    ) -> list[ContinueWatchingEntry]:
        try:
            server = await self._get_user_server(backend_user_id)
        except MediaLibraryUnavailableError:
            raise
        try:
            results = await asyncio.to_thread(
                lambda: list(server.library.onDeck())
            )
        except Unauthorized as exc:
            self._evict_user(backend_user_id)
            raise MediaLibraryUnavailableError(
                "Plex token revoked"
            ) from exc
        except PlexApiException as exc:
            raise MediaLibraryUnavailableError(str(exc)) from exc
        sid = await self._server_id()
        out: list[ContinueWatchingEntry] = []
        for r in results[:limit]:
            item = _plex_to_media_item(r, server_id=sid)
            next_up = item.view_offset_seconds == 0 and not item.is_watched
            out.append(ContinueWatchingEntry(item=item, next_up=next_up))
        return out

    async def next_episode(
        self,
        show_id: str,
        *,
        backend_user_id: str = "",
    ) -> MediaItem | None:
        try:
            server = await self._get_user_server(backend_user_id)
        except MediaLibraryUnavailableError:
            raise
        try:
            show = await asyncio.to_thread(server.fetchItem, int(show_id))
            on_deck = await asyncio.to_thread(
                lambda: getattr(show, "onDeck", lambda: None)()
            )
        except NotFound:
            return None
        except Unauthorized as exc:
            self._evict_user(backend_user_id)
            raise MediaLibraryUnavailableError(
                "Plex token revoked"
            ) from exc
        except PlexApiException as exc:
            raise MediaLibraryUnavailableError(str(exc)) from exc

        if on_deck is not None:
            return _plex_to_media_item(
                on_deck, server_id=await self._server_id()
            )
        # Fallback: lowest unwatched (season, episode)
        try:
            episodes = await asyncio.to_thread(
                lambda: list(show.episodes())
            )
        except (Unauthorized, PlexApiException):
            return None
        unwatched = [
            ep for ep in episodes if int(getattr(ep, "viewCount", 0) or 0) == 0
        ]
        if not unwatched:
            return None
        unwatched.sort(
            key=lambda ep: (
                int(getattr(ep, "seasonNumber", 0) or 0),
                int(getattr(ep, "index", 0) or 0),
            )
        )
        return _plex_to_media_item(
            unwatched[0], server_id=await self._server_id()
        )

    # ── Clients & sessions ──────────────────────────────────────────

    async def list_clients(self) -> list[MediaClient]:
        if self._server is None:
            raise MediaLibraryUnavailableError("Plex not configured")
        try:
            account_devices = (
                await asyncio.to_thread(self._account.devices)
                if self._account is not None
                else []
            )
            server_clients = await asyncio.to_thread(self._server.clients)
        except Unauthorized as exc:
            raise MediaLibraryUnavailableError(
                "Plex token revoked"
            ) from exc
        except PlexApiException as exc:
            raise MediaLibraryUnavailableError(str(exc)) from exc
        sid = await self._server_id()
        out: list[MediaClient] = []
        seen: set[str] = set()
        for d in account_devices or []:
            provides = str(getattr(d, "provides", "") or "")
            if "player" not in provides:
                continue
            cid = str(getattr(d, "clientIdentifier", "") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            out.append(
                MediaClient(
                    client_id=cid,
                    backend_name="plex",
                    server_id=sid,
                    name=str(
                        getattr(d, "name", "") or getattr(d, "title", "")
                    ),
                    device=str(getattr(d, "device", "") or ""),
                    platform=str(getattr(d, "platform", "") or ""),
                    is_online=True,
                    supports_remote_control=True,
                    supports_seek=True,
                    last_seen_at=time.time(),
                )
            )
        for c in server_clients or []:
            cid = str(getattr(c, "machineIdentifier", "") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            out.append(
                _plex_player_to_media_client(c, server_id=sid)
            )
        return out

    async def now_playing(self) -> list[MediaSession]:
        if self._server is None:
            raise MediaLibraryUnavailableError("Plex not configured")
        try:
            sessions = await asyncio.to_thread(self._server.sessions)
        except Unauthorized as exc:
            raise MediaLibraryUnavailableError(
                "Plex token revoked"
            ) from exc
        except PlexApiException as exc:
            raise MediaLibraryUnavailableError(str(exc)) from exc
        sid = await self._server_id()
        return [
            _plex_session_to_media_session(s, server_id=sid)
            for s in sessions or []
        ]

    # ── Playback ────────────────────────────────────────────────────

    async def play(
        self,
        command: MediaPlayCommand,
        *,
        backend_user_id: str = "",
    ) -> None:
        if self._server is None:
            raise MediaLibraryUnavailableError("Plex not configured")
        try:
            target_client_obj = await asyncio.to_thread(
                lambda: self._server.client(command.client.client_id)
                if hasattr(self._server, "client")
                else None
            )
        except (NotFound, PlexApiException):
            target_client_obj = None
        try:
            item_obj = await asyncio.to_thread(
                self._server.fetchItem, int(command.item.id)
            )
        except (NotFound, ValueError) as exc:
            raise MediaLibraryUnavailableError(
                f"Item {command.item.id} not found"
            ) from exc
        except Unauthorized as exc:
            raise MediaLibraryUnavailableError(
                "Plex token revoked"
            ) from exc

        offset_ms = int(command.offset_seconds * 1000)
        try:
            if target_client_obj is not None and hasattr(
                target_client_obj, "playMedia"
            ):
                await asyncio.to_thread(
                    target_client_obj.playMedia, item_obj, offset=offset_ms
                )
                return
        except (NotFound, PlexApiException) as exc:
            logger.warning(
                "Plex companion play failed, falling back: %s", exc
            )

        # Fallback: legacy /clients/<id>/playMedia.
        if self._http is None:
            raise MediaLibraryUnavailableError(
                "Plex http client unavailable"
            )
        params = {
            "key": getattr(item_obj, "key", ""),
            "machineIdentifier": getattr(self._server, "machineIdentifier", ""),
            "address": getattr(self._server, "address", ""),
            "port": getattr(self._server, "port", ""),
            "offset": offset_ms,
        }
        url = f"{self._server_url.rstrip('/')}/clients/{command.client.client_id}/playMedia"
        try:
            resp = await self._http.post(
                url,
                params=params,
                headers={"X-Plex-Token": self._account_token},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise MediaLibraryUnavailableError(
                f"Plex playback dispatch failed: {exc}"
            ) from exc

    async def _control_client(
        self, client_id: str, action: str
    ) -> None:
        if self._server is None:
            raise MediaLibraryUnavailableError("Plex not configured")
        try:
            target = await asyncio.to_thread(
                lambda: self._server.client(client_id)
            )
        except NotFound as exc:
            raise MediaClientNotFoundError(
                f"Plex client {client_id} not found"
            ) from exc
        except PlexApiException as exc:
            raise MediaLibraryUnavailableError(str(exc)) from exc
        try:
            await asyncio.to_thread(getattr(target, action))
        except (Unauthorized, PlexApiException) as exc:
            raise MediaLibraryUnavailableError(str(exc)) from exc

    async def pause(self, client_id: str) -> None:
        await self._control_client(client_id, "pause")

    async def resume(self, client_id: str) -> None:
        await self._control_client(client_id, "play")

    async def stop(self, client_id: str) -> None:
        await self._control_client(client_id, "stop")

    async def seek(self, client_id: str, position_seconds: float) -> None:
        if self._server is None:
            raise MediaLibraryUnavailableError("Plex not configured")
        try:
            target = await asyncio.to_thread(
                lambda: self._server.client(client_id)
            )
        except NotFound as exc:
            raise MediaClientNotFoundError(
                f"Plex client {client_id} not found"
            ) from exc
        try:
            await asyncio.to_thread(
                target.seekTo, int(position_seconds * 1000)
            )
        except (Unauthorized, PlexApiException) as exc:
            raise MediaLibraryUnavailableError(str(exc)) from exc


__all__ = [
    "PlexBackend",
    "_kind_from_plex",
    "_plex_to_media_item",
    "_plex_session_to_media_session",
    "_plex_player_to_media_client",
    "_utc_seconds_from_addedat",
]
