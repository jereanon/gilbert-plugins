"""JellyfinBackend — concrete ``MediaLibraryBackend`` for Jellyfin.

Talks to Jellyfin's REST API directly via ``httpx``. The official
``jellyfin-apiclient-python`` is partially synchronous and missing
some endpoints we need (Sessions remote control), so we hand-roll
the bits we need.

v1 design (per spec §9.5): admin token + ``userId`` query/path
parameter for per-user data. Each per-user query is logged on the
Jellyfin server's audit trail as the admin user — accepted v1
limitation, tracked as v2 work in OPEN_QUESTIONS.

Service-lifetime cache for username → user-id resolution keyed by the
*Jellyfin* username (NOT by Gilbert user id) so two Gilbert users
mapped to the same Jellyfin username share the resolved id correctly.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.media_library import (
    ContinueWatchingEntry,
    MediaClient,
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


# Jellyfin uses 100-ns "ticks" everywhere a time / position lands —
# 1 second == 10_000_000 ticks. Documented in spec §9.5.
_TICKS_PER_SECOND = 10_000_000


def _seconds_to_ticks(seconds: float) -> int:
    return int(seconds * _TICKS_PER_SECOND)


def _ticks_to_seconds(ticks: float | int | None) -> float:
    if ticks is None:
        return 0.0
    try:
        return float(ticks) / _TICKS_PER_SECOND
    except (TypeError, ValueError):
        return 0.0


def _kind_from_jellyfin_type(t: str) -> MediaKind:
    return {
        "Movie": MediaKind.MOVIE,
        "Series": MediaKind.SHOW,
        "Season": MediaKind.SEASON,
        "Episode": MediaKind.EPISODE,
        "MusicArtist": MediaKind.MUSIC_ARTIST,
        "MusicAlbum": MediaKind.MUSIC_ALBUM,
        "Audio": MediaKind.MUSIC_TRACK,
        "MusicVideo": MediaKind.MUSIC_VIDEO,
        "Photo": MediaKind.PHOTO,
    }.get(t, MediaKind.UNKNOWN)


def _utc_seconds_from_iso(value: Any) -> float:
    """Convert Jellyfin ``DateCreated`` etc. (ISO 8601 with offset) to
    UTC unix seconds at the mapping boundary.
    """
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value)
    # Jellyfin sometimes returns 'Z' suffix; sometimes a trailing
    # microsecond stretch beyond fromisoformat's tolerance. Normalize.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Trim sub-second precision past 6 digits (Jellyfin can emit 7).
    if "." in s and "+" in s:
        head, tail = s.split(".", 1)
        frac, off = tail.split("+", 1)
        s = f"{head}.{frac[:6]}+{off}"
    elif "." in s and "-" in s and s.rfind("-") > s.find("."):
        head, tail = s.split(".", 1)
        idx = tail.rfind("-")
        s = f"{head}.{tail[:idx][:6]}{tail[idx:]}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).timestamp()


def _jellyfin_to_media_item(
    data: dict[str, Any],
    *,
    server_url: str,
    server_id: str,
) -> MediaItem:
    """Pure mapping helper — Jellyfin item JSON → ``MediaItem``."""
    kind = _kind_from_jellyfin_type(str(data.get("Type") or ""))
    item_id = str(data.get("Id") or "")
    title = str(data.get("Name") or "")
    sort_title = str(data.get("SortName") or title)
    year = data.get("ProductionYear")
    runtime_ticks = data.get("RunTimeTicks") or 0
    duration_seconds = _ticks_to_seconds(runtime_ticks)
    summary = str(data.get("Overview") or "")
    rating_raw = data.get("CommunityRating")
    try:
        rating: float | None = (
            float(rating_raw) if rating_raw is not None else None
        )
    except (TypeError, ValueError):
        rating = None
    content_rating = str(data.get("OfficialRating") or "")
    studios = data.get("Studios") or []
    studio = ""
    if studios and isinstance(studios, list):
        first = studios[0]
        if isinstance(first, dict):
            studio = str(first.get("Name") or "")
    genres_raw = data.get("Genres") or []
    genres = tuple(str(g) for g in genres_raw if isinstance(g, str))
    actors: tuple[str, ...] = ()
    directors: tuple[str, ...] = ()
    people = data.get("People") or []
    if isinstance(people, list):
        actor_list: list[str] = []
        director_list: list[str] = []
        for p in people:
            if not isinstance(p, dict):
                continue
            pname = str(p.get("Name") or "")
            ptype = str(p.get("Type") or "")
            if not pname:
                continue
            if ptype == "Actor":
                actor_list.append(pname)
            elif ptype == "Director":
                director_list.append(pname)
        actors = tuple(actor_list)
        directors = tuple(director_list)

    image_tags = data.get("ImageTags") or {}
    poster_url = ""
    if isinstance(image_tags, dict) and image_tags.get("Primary"):
        poster_url = (
            f"{server_url.rstrip('/')}/Items/{item_id}/Images/Primary"
            f"?tag={image_tags['Primary']}&maxHeight=480"
        )
    backdrop_url = ""
    backdrop_tags = data.get("BackdropImageTags") or []
    if (
        isinstance(backdrop_tags, list)
        and backdrop_tags
        and isinstance(backdrop_tags[0], str)
    ):
        backdrop_url = (
            f"{server_url.rstrip('/')}/Items/{item_id}/Images/Backdrop/0"
            f"?tag={backdrop_tags[0]}"
        )

    parent_id = str(data.get("ParentId") or data.get("SeasonId") or "")
    parent_title = str(data.get("SeasonName") or "")
    grandparent_id = str(data.get("SeriesId") or "")
    grandparent_title = str(data.get("SeriesName") or "")
    season_number = data.get("ParentIndexNumber")
    episode_number = data.get("IndexNumber")

    user_data = data.get("UserData") or {}
    if not isinstance(user_data, dict):
        user_data = {}
    view_count = int(user_data.get("PlayCount", 0) or 0)
    is_watched = bool(user_data.get("Played", False))
    view_offset_ticks = user_data.get("PlaybackPositionTicks", 0) or 0
    view_offset_seconds = _ticks_to_seconds(view_offset_ticks)
    last_played = user_data.get("LastPlayedDate")
    last_viewed_at = _utc_seconds_from_iso(last_played) if last_played else 0.0

    added_at = _utc_seconds_from_iso(data.get("DateCreated"))

    library_section = ""
    # Some endpoints surface ParentLogoItemId / collection name; the
    # most reliable hint is the LibraryName field on Items/Latest.
    if "LibraryName" in data:
        library_section = str(data.get("LibraryName") or "")
    elif "CollectionType" in data:
        library_section = str(data.get("CollectionType") or "")

    return MediaItem(
        id=item_id,
        backend_name="jellyfin",
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


def _session_to_media_session(
    data: dict[str, Any], *, server_url: str, server_id: str
) -> MediaSession | None:
    """Map a /Sessions row to MediaSession; returns None if no
    NowPlayingItem.
    """
    npi = data.get("NowPlayingItem")
    if not npi or not isinstance(npi, dict):
        return None
    item = _jellyfin_to_media_item(
        npi, server_url=server_url, server_id=server_id
    )
    play_state = data.get("PlayState") or {}
    if not isinstance(play_state, dict):
        play_state = {}
    is_paused = bool(play_state.get("IsPaused", False))
    state = MediaPlaybackState.PAUSED if is_paused else MediaPlaybackState.PLAYING
    position_ticks = play_state.get("PositionTicks", 0) or 0
    position_seconds = _ticks_to_seconds(position_ticks)
    duration_seconds = item.duration_seconds

    client = MediaClient(
        client_id=str(data.get("Id") or ""),
        backend_name="jellyfin",
        server_id=server_id,
        name=str(data.get("DeviceName") or ""),
        device=str(data.get("Client") or ""),
        platform=str(data.get("ApplicationVersion") or ""),
        address=str(data.get("RemoteEndPoint") or ""),
        is_online=True,
        supports_remote_control=bool(
            data.get("SupportsRemoteControl", False)
        ),
        supports_seek=bool(
            (data.get("SupportedCommands") or [])
            and "SeekTo"
            in {str(c) for c in data.get("SupportedCommands") or []}
        ),
        last_seen_at=time.time(),
    )

    return MediaSession(
        session_id=str(data.get("Id") or ""),
        backend_name="jellyfin",
        client=client,
        item=item,
        state=state,
        position_seconds=position_seconds,
        duration_seconds=duration_seconds,
        backend_user_name=str(data.get("UserName") or ""),
        is_transcoding=bool(
            (data.get("TranscodingInfo") or {}).get("IsVideoDirect", True) is False
        ),
    )


def _session_to_client(
    data: dict[str, Any], *, server_id: str
) -> MediaClient:
    supported_cmds = {str(c) for c in data.get("SupportedCommands") or []}
    return MediaClient(
        client_id=str(data.get("Id") or ""),
        backend_name="jellyfin",
        server_id=server_id,
        name=str(data.get("DeviceName") or ""),
        device=str(data.get("Client") or ""),
        platform=str(data.get("ApplicationVersion") or ""),
        address=str(data.get("RemoteEndPoint") or ""),
        is_online=True,
        supports_remote_control=bool(
            data.get("SupportsRemoteControl", False)
        ),
        supports_seek="SeekTo" in supported_cmds,
        last_seen_at=time.time(),
    )


# ── Backend ────────────────────────────────────────────────────────


class JellyfinBackend(MediaLibraryBackend):
    backend_name = "jellyfin"
    supports_now_playing = True
    supports_resume = True
    supports_continue_watching = True
    supports_recently_added = True
    supports_seek = True
    supports_per_user = True
    supports_next_episode = True

    def __init__(self) -> None:
        self._server_url: str = ""
        self._access_token: str = ""
        self._device_id: str = uuid.uuid4().hex
        self._verify_tls: bool = True
        self._timeout: float = 15.0
        self._http: httpx.AsyncClient | None = None
        self._server_id: str = ""

        # service-lifetime cache: jellyfin_username → jellyfin user id.
        self._user_id_cache: dict[str, str] = {}

    @classmethod
    def runtime_dependencies(cls) -> list[RuntimeDependency]:
        return []

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="server_url",
                type=ToolParameterType.STRING,
                description=(
                    "Jellyfin server base URL "
                    "(e.g. http://jellyfin.local:8096)."
                ),
                default="",
                restart_required=True,
            ),
            ConfigParam(
                key="admin_username",
                type=ToolParameterType.STRING,
                description=(
                    "Admin username (used to bootstrap the device "
                    "token; required only at link time)."
                ),
                default="",
                restart_required=True,
            ),
            ConfigParam(
                key="admin_password",
                type=ToolParameterType.STRING,
                description=(
                    "Admin password — only used to obtain the access "
                    "token; cleared after link unless keep_password "
                    "is true."
                ),
                default="",
                sensitive=True,
                restart_required=True,
            ),
            ConfigParam(
                key="keep_password",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "If True, retain admin_password after the link "
                    "flow (advanced; default False)."
                ),
                default=False,
            ),
            ConfigParam(
                key="device_id",
                type=ToolParameterType.STRING,
                description=(
                    "Stable device identifier used in "
                    "X-Emby-Authorization. Auto-generated if empty."
                ),
                default="",
            ),
            ConfigParam(
                key="access_token",
                type=ToolParameterType.STRING,
                description=(
                    "Auto-populated by link_account. Admin's API "
                    "access token."
                ),
                default="",
                sensitive=True,
                restart_required=True,
            ),
            ConfigParam(
                key="verify_tls",
                type=ToolParameterType.BOOLEAN,
                description="Verify TLS for https URLs.",
                default=True,
            ),
            ConfigParam(
                key="request_timeout_seconds",
                type=ToolParameterType.NUMBER,
                description="Request timeout (seconds).",
                default=15.0,
            ),
        ]

    # ── BackendActionProvider ──────────────────────────────────────

    def backend_actions(self) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="link_account",
                label="Link Jellyfin account",
                description=(
                    "Authenticate with admin credentials; persist the "
                    "access token, then clear admin_password unless "
                    "keep_password is set."
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
            return await self._link_account(payload)
        if key == "test_connection":
            return await self._test_connection(payload)
        return ConfigActionResult(
            status="error",
            message=f"Unknown Jellyfin action '{key}'",
        )

    def _emby_auth_header(self, token: str = "") -> str:
        device_id = self._device_id
        parts = [
            'MediaBrowser Client="Gilbert"',
            'Device="Gilbert"',
            f'DeviceId="{device_id}"',
            'Version="1.0.0"',
        ]
        if token:
            parts.append(f'Token="{token}"')
        return ", ".join(parts)

    async def _link_account(
        self, payload: dict[str, Any]
    ) -> ConfigActionResult:
        if not self._server_url:
            return ConfigActionResult(
                status="error",
                message="server_url is required.",
            )
        async with httpx.AsyncClient(
            timeout=self._timeout, verify=self._verify_tls
        ) as client:
            try:
                resp = await client.post(
                    f"{self._server_url.rstrip('/')}/Users/AuthenticateByName",
                    headers={
                        "X-Emby-Authorization": self._emby_auth_header(),
                        "Content-Type": "application/json",
                    },
                    json={
                        "Username": payload.get("Username")
                        or payload.get("username")
                        or "",
                        "Pw": payload.get("Pw")
                        or payload.get("password")
                        or "",
                    },
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                return ConfigActionResult(
                    status="error",
                    message=f"Jellyfin authentication failed: {exc}",
                )
        body = resp.json()
        token = body.get("AccessToken", "")
        if not token:
            return ConfigActionResult(
                status="error",
                message="Authentication succeeded but no token returned.",
            )
        keep_password = bool(payload.get("keep_password", False))
        persist = {"access_token": token}
        if not keep_password:
            persist["admin_password"] = ""
        return ConfigActionResult(
            status="ok",
            message="Jellyfin account linked.",
            data={"persist": persist},
        )

    async def _test_connection(
        self, payload: dict[str, Any]
    ) -> ConfigActionResult:
        if not self._server_url or not self._access_token:
            return ConfigActionResult(
                status="error",
                message="Configure server_url + access_token first.",
            )
        async with httpx.AsyncClient(
            timeout=self._timeout, verify=self._verify_tls
        ) as client:
            try:
                resp = await client.get(
                    f"{self._server_url.rstrip('/')}/System/Info",
                    params={"api_key": self._access_token},
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                return ConfigActionResult(
                    status="error",
                    message=f"Jellyfin /System/Info failed: {exc}",
                )
        body = resp.json()
        return ConfigActionResult(
            status="ok",
            message=(
                f"Jellyfin {body.get('Version', '?')} reachable "
                f"({body.get('ServerName', '')})."
            ),
            data={"server_id": body.get("Id", "")},
        )

    # ── Lifecycle ──────────────────────────────────────────────────

    async def initialize(self, config: dict[str, object]) -> None:
        prev_token = self._access_token
        self._server_url = str(config.get("server_url") or "")
        self._access_token = str(config.get("access_token") or "")
        device_id = str(config.get("device_id") or "")
        if device_id:
            self._device_id = device_id
        self._verify_tls = bool(config.get("verify_tls", True))
        try:
            self._timeout = float(
                config.get("request_timeout_seconds") or 15.0
            )
        except (TypeError, ValueError):
            self._timeout = 15.0

        # Token rotation → drop username→user-id cache (the user pool
        # may also have changed).
        if prev_token and prev_token != self._access_token:
            self._user_id_cache.clear()

        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
        if not self._server_url:
            self._http = None
            return
        self._http = httpx.AsyncClient(
            base_url=self._server_url.rstrip("/"),
            timeout=self._timeout,
            verify=self._verify_tls,
            headers={
                "X-Emby-Authorization": self._emby_auth_header(
                    self._access_token
                ),
            }
            if self._access_token
            else {},
        )

    async def close(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                logger.debug("Jellyfin http close raised", exc_info=True)
            self._http = None

    # ── HTTP helper ────────────────────────────────────────────────

    def _require_http(self) -> httpx.AsyncClient:
        if self._http is None or not self._access_token:
            raise MediaLibraryUnavailableError("Jellyfin not configured")
        return self._http

    async def _get_json(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        client = self._require_http()
        try:
            resp = await client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise MediaLibraryUnavailableError(
                f"Jellyfin server unreachable: {exc}"
            ) from exc
        if resp.status_code in (401, 403):
            raise MediaLibraryUnavailableError("Jellyfin token revoked")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 500:
            raise MediaLibraryUnavailableError(
                f"Jellyfin returned {resp.status_code}"
            )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise MediaLibraryUnavailableError(str(exc)) from exc
        return resp.json()

    async def _post(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        client = self._require_http()
        try:
            resp = await client.post(path, params=params, json=json)
        except httpx.HTTPError as exc:
            raise MediaLibraryUnavailableError(
                f"Jellyfin server unreachable: {exc}"
            ) from exc
        if resp.status_code in (401, 403):
            raise MediaLibraryUnavailableError("Jellyfin token revoked")
        if resp.status_code >= 500:
            raise MediaLibraryUnavailableError(
                f"Jellyfin returned {resp.status_code}"
            )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise MediaLibraryUnavailableError(str(exc)) from exc
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json() if "application/json" in resp.headers.get(
            "content-type", ""
        ) else None

    async def _resolve_user_id(self, username: str) -> str:
        if not username:
            return ""
        cached = self._user_id_cache.get(username)
        if cached:
            return cached
        users = await self._get_json("/Users")
        if not isinstance(users, list):
            return ""
        for u in users:
            if not isinstance(u, dict):
                continue
            if u.get("Name") == username:
                uid = str(u.get("Id") or "")
                if uid:
                    self._user_id_cache[username] = uid
                return uid
        return ""

    # ── Library queries ───────────────────────────────────────────

    _KIND_TO_INCLUDE = {
        MediaKind.MOVIE: "Movie",
        MediaKind.SHOW: "Series",
        MediaKind.SEASON: "Season",
        MediaKind.EPISODE: "Episode",
        MediaKind.MUSIC_ARTIST: "MusicArtist",
        MediaKind.MUSIC_ALBUM: "MusicAlbum",
        MediaKind.MUSIC_TRACK: "Audio",
    }

    async def search(
        self,
        query: str,
        *,
        filters: MediaSearchFilters | None = None,
        backend_user_id: str = "",
    ) -> list[MediaItem]:
        if not backend_user_id:
            # Without a per-user mapping we still query against an
            # admin-token search but without scoping; spec §9.5 says
            # this is allowed (shared-account deployments).
            params: dict[str, Any] = {
                "searchTerm": query,
                "Recursive": True,
                "Limit": filters.limit if filters else 30,
            }
        else:
            params = {
                "searchTerm": query,
                "Recursive": True,
                "Limit": filters.limit if filters else 30,
            }
        if filters and filters.kinds:
            includes = [
                self._KIND_TO_INCLUDE[k]
                for k in filters.kinds
                if k in self._KIND_TO_INCLUDE
            ]
            if includes:
                params["IncludeItemTypes"] = ",".join(includes)
        if filters and filters.year_from is not None:
            params["MinYear"] = filters.year_from
        if filters and filters.year_to is not None:
            params["MaxYear"] = filters.year_to
        if filters and filters.genre:
            params["Genres"] = filters.genre
        if filters and filters.unwatched_only:
            params["IsPlayed"] = False

        path = (
            f"/Users/{backend_user_id}/Items"
            if backend_user_id
            else "/Items"
        )
        data = await self._get_json(path, params=params)
        items_data = (data or {}).get("Items") if isinstance(data, dict) else []
        return [
            _jellyfin_to_media_item(
                d,
                server_url=self._server_url,
                server_id=self._server_id,
            )
            for d in items_data or []
            if isinstance(d, dict)
        ]

    async def get_item(
        self, item_id: str, backend_user_id: str = ""
    ) -> MediaItem | None:
        path = (
            f"/Users/{backend_user_id}/Items/{item_id}"
            if backend_user_id
            else f"/Items/{item_id}"
        )
        data = await self._get_json(path)
        if data is None or not isinstance(data, dict):
            return None
        return _jellyfin_to_media_item(
            data,
            server_url=self._server_url,
            server_id=self._server_id,
        )

    async def list_libraries(self, backend_user_id: str = "") -> list[str]:
        if not backend_user_id:
            data = await self._get_json("/Library/MediaFolders")
            items = (data or {}).get("Items") if isinstance(data, dict) else []
        else:
            data = await self._get_json(
                f"/Users/{backend_user_id}/Views"
            )
            items = (data or {}).get("Items") if isinstance(data, dict) else []
        return [
            str(d.get("Name", ""))
            for d in items or []
            if isinstance(d, dict) and d.get("Name")
        ]

    async def list_backend_users(self) -> list[dict[str, str]]:
        data = await self._get_json("/Users")
        if not isinstance(data, list):
            return []
        out: list[dict[str, str]] = []
        for u in data:
            if not isinstance(u, dict):
                continue
            out.append(
                {
                    "id": str(u.get("Id") or ""),
                    "username": str(u.get("Name") or ""),
                    "display_name": str(u.get("Name") or ""),
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
        if not backend_user_id:
            raise MediaLibraryUnavailableError(
                "Jellyfin recently_added requires a per-user mapping"
            )
        params: dict[str, Any] = {"Limit": limit}
        if kind is not None and kind in self._KIND_TO_INCLUDE:
            params["IncludeItemTypes"] = self._KIND_TO_INCLUDE[kind]
        data = await self._get_json(
            f"/Users/{backend_user_id}/Items/Latest",
            params=params,
        )
        if not isinstance(data, list):
            return []
        out: list[RecentlyAddedEntry] = []
        for d in data:
            if not isinstance(d, dict):
                continue
            item = _jellyfin_to_media_item(
                d,
                server_url=self._server_url,
                server_id=self._server_id,
            )
            out.append(
                RecentlyAddedEntry(item=item, added_at=item.added_at)
            )
        return out

    async def continue_watching(
        self,
        *,
        backend_user_id: str = "",
        limit: int = 10,
    ) -> list[ContinueWatchingEntry]:
        if not backend_user_id:
            raise MediaLibraryUnavailableError(
                "Jellyfin continue_watching requires a per-user mapping"
            )
        data = await self._get_json(
            f"/Users/{backend_user_id}/Items/Resume",
            params={"Limit": limit},
        )
        items = (data or {}).get("Items") if isinstance(data, dict) else []
        out: list[ContinueWatchingEntry] = []
        for d in items or []:
            if not isinstance(d, dict):
                continue
            item = _jellyfin_to_media_item(
                d,
                server_url=self._server_url,
                server_id=self._server_id,
            )
            next_up = (
                item.kind == MediaKind.EPISODE
                and item.view_offset_seconds == 0
            )
            out.append(ContinueWatchingEntry(item=item, next_up=next_up))
        return out

    async def next_episode(
        self,
        show_id: str,
        *,
        backend_user_id: str = "",
    ) -> MediaItem | None:
        if not backend_user_id:
            raise MediaLibraryUnavailableError(
                "Jellyfin next_episode requires a per-user mapping"
            )
        next_up = await self._get_json(
            f"/Shows/{show_id}/NextUp",
            params={"UserId": backend_user_id},
        )
        items = (next_up or {}).get("Items") if isinstance(next_up, dict) else []
        if items and isinstance(items[0], dict):
            return _jellyfin_to_media_item(
                items[0],
                server_url=self._server_url,
                server_id=self._server_id,
            )
        # Fallback: lowest unwatched (season, episode).
        eps_data = await self._get_json(
            f"/Shows/{show_id}/Episodes",
            params={
                "UserId": backend_user_id,
                "IsPlayed": False,
                "SortBy": "ParentIndexNumber,IndexNumber",
                "Limit": 1,
            },
        )
        eps = (eps_data or {}).get("Items") if isinstance(eps_data, dict) else []
        if eps and isinstance(eps[0], dict):
            return _jellyfin_to_media_item(
                eps[0],
                server_url=self._server_url,
                server_id=self._server_id,
            )
        return None

    # ── Clients & sessions ────────────────────────────────────────

    async def list_clients(self) -> list[MediaClient]:
        data = await self._get_json(
            "/Sessions", params={"ActiveWithinSeconds": 600}
        )
        if not isinstance(data, list):
            return []
        out: list[MediaClient] = []
        for d in data:
            if not isinstance(d, dict):
                continue
            out.append(
                _session_to_client(d, server_id=self._server_id)
            )
        return out

    async def now_playing(self) -> list[MediaSession]:
        data = await self._get_json("/Sessions")
        if not isinstance(data, list):
            return []
        sessions: list[MediaSession] = []
        for d in data:
            if not isinstance(d, dict):
                continue
            session = _session_to_media_session(
                d,
                server_url=self._server_url,
                server_id=self._server_id,
            )
            if session is not None:
                sessions.append(session)
        return sessions

    # ── Playback ──────────────────────────────────────────────────

    async def play(
        self,
        command: MediaPlayCommand,
        *,
        backend_user_id: str = "",
    ) -> None:
        params = {
            "ItemIds": command.item.id,
            "PlayCommand": "PlayNow",
        }
        if command.offset_seconds:
            params["StartPositionTicks"] = _seconds_to_ticks(
                command.offset_seconds
            )
        await self._post(
            f"/Sessions/{command.client.client_id}/Playing",
            params=params,
        )

    async def pause(self, client_id: str) -> None:
        await self._post(f"/Sessions/{client_id}/Playing/Pause")

    async def resume(self, client_id: str) -> None:
        await self._post(f"/Sessions/{client_id}/Playing/Unpause")

    async def stop(self, client_id: str) -> None:
        await self._post(f"/Sessions/{client_id}/Playing/Stop")

    async def seek(self, client_id: str, position_seconds: float) -> None:
        await self._post(
            f"/Sessions/{client_id}/Playing/Seek",
            params={
                "SeekPositionTicks": _seconds_to_ticks(position_seconds)
            },
        )


__all__ = [
    "JellyfinBackend",
    "_jellyfin_to_media_item",
    "_kind_from_jellyfin_type",
    "_seconds_to_ticks",
    "_session_to_client",
    "_session_to_media_session",
    "_ticks_to_seconds",
    "_utc_seconds_from_iso",
]
