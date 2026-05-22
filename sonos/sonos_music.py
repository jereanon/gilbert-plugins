"""Sonos music backend — Spotify Web API + aiosonos playback.

Replaces the previous SoCo+SMAPI implementation. The Sonos local
WebSocket API doesn't expose browse/search to third-party apps — even
the Sonos mobile app talks to Spotify directly for library views, then
tells speakers what to play. We follow the same architecture:

- **Browse / search**: talk to Spotify's Web API directly using an
  OAuth token issued to Gilbert's registered Spotify app. Token refresh
  happens server-side automatically; users don't see it after the
  one-time link flow.
- **Playback**: hand the resolved Spotify URI (``spotify:track:…``) to
  the speaker backend, which uses the speaker's own linked Spotify
  account to stream. The two links (Gilbert↔Spotify for search,
  Sonos↔Spotify for playback) are independent — both need to exist
  but neither cares about the other's identity.

The link flow is manual-paste to keep it web-endpoint-free: the user
clicks Link Spotify, gets an authorize URL, approves access in
Spotify, copies the resulting ``?code=…`` out of the redirected URL's
query string, pastes it into the Spotify Auth Code field, saves, and
clicks Finish Linking. Less slick than a redirect loop but works
without registering new HTTP routes for the plugin to own.

Legacy config fields (``preferred_service``, ``auth_token``,
``auth_key``) are retained so upgrades don't lose data, but they're
no longer consulted — the plugin's entire search / resolve pipeline
now goes through Spotify's Web API.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.music import (
    LinkedMusicServiceLister,
    MusicBackend,
    MusicItem,
    MusicItemKind,
    MusicSearchUnavailableError,
    Playable,
)
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)

# ── Spotify Web API constants ────────────────────────────────────────

_SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
_SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SPOTIFY_API_BASE = "https://api.spotify.com/v1"

# Scopes Gilbert requests at link time. Covers search (no scope needed),
# user library (for "my liked songs"), and user-owned playlists (for
# "my playlists"). ``user-read-private`` gets the user's display name +
# country for UX niceties. Intentionally omit ``*-modify-*`` scopes —
# we only read, speakers do the playing through their own link.
_DEFAULT_SCOPES = (
    "user-library-read user-library-modify playlist-read-private "
    "playlist-read-collaborative user-read-private"
)

# Query-kind → Spotify search type param. Artist/playlist/album/track
# are native; station maps onto playlist (closest analogue — curated
# editorial playlists are how Spotify replaced "radio stations" for
# its free tier).
_KIND_TO_SPOTIFY_TYPE: dict[MusicItemKind, str] = {
    MusicItemKind.TRACK: "track",
    MusicItemKind.ALBUM: "album",
    MusicItemKind.ARTIST: "artist",
    MusicItemKind.PLAYLIST: "playlist",
    MusicItemKind.STATION: "playlist",
}

# Refresh tokens ~1h before they expire so concurrent requests don't
# race against expiry. Spotify tokens live 3600s; we rotate at 3300.
_TOKEN_REFRESH_MARGIN = 300

# Spotify rate-limits; a 10s timeout on search is plenty.
_SPOTIFY_HTTP_TIMEOUT = 10.0

# Regex for extracting the ``code`` parameter from either a raw code
# string or a full redirect URL the user pasted in after authorizing.
_AUTH_CODE_RE = re.compile(r"[?&]code=([^&\s]+)")


class _SpotifyClient:
    """Thin Spotify Web API wrapper with OAuth refresh.

    One instance per ``SonosMusic`` backend. Holds the access + refresh
    tokens, refreshes automatically before expiry, and exposes just the
    endpoints the music backend actually uses. Not reused across
    plugins — Spotify app creds are tenant-specific.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str = "",
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: str = ""
        self._access_token_expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = None

    async def _require_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=_SPOTIFY_HTTP_TIMEOUT)
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    @property
    def has_refresh_token(self) -> bool:
        return bool(self._refresh_token)

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    def authorize_url(self, redirect_uri: str, state: str, scope: str) -> str:
        """Build the ``accounts.spotify.com/authorize`` URL the user opens."""
        params = {
            "client_id": self._client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": scope,
        }
        return f"{_SPOTIFY_AUTH_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> None:
        """Exchange an authorization code for access + refresh tokens."""
        http = await self._require_http()
        resp = await http.post(
            _SPOTIFY_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        self._access_token = str(payload.get("access_token") or "")
        self._refresh_token = str(
            payload.get("refresh_token") or self._refresh_token
        )
        expires_in = int(payload.get("expires_in") or 3600)
        self._access_token_expires_at = time.monotonic() + expires_in

    async def _ensure_access_token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        async with self._lock:
            if (
                self._access_token
                and time.monotonic()
                < self._access_token_expires_at - _TOKEN_REFRESH_MARGIN
            ):
                return self._access_token
            if not self._refresh_token:
                raise MusicSearchUnavailableError(
                    "Spotify isn't linked yet. Run Settings → Media → Music "
                    "→ Link Spotify to authorize Gilbert."
                )
            http = await self._require_http()
            resp = await http.post(
                _SPOTIFY_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            self._access_token = str(payload.get("access_token") or "")
            # Spotify may rotate the refresh token — always honour the
            # new one if present (missing = keep existing).
            new_refresh = str(payload.get("refresh_token") or "")
            if new_refresh:
                self._refresh_token = new_refresh
            expires_in = int(payload.get("expires_in") or 3600)
            self._access_token_expires_at = time.monotonic() + expires_in
            return self._access_token

    async def _authed_get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        token = await self._ensure_access_token()
        http = await self._require_http()
        resp = await http.get(
            f"{_SPOTIFY_API_BASE}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 401:
            # Access token revoked / expired early — force a refresh
            # and retry once.
            self._access_token = ""
            self._access_token_expires_at = 0
            token = await self._ensure_access_token()
            resp = await http.get(
                f"{_SPOTIFY_API_BASE}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        resp.raise_for_status()
        return resp.json()

    async def search(
        self,
        query: str,
        spotify_type: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Raw Spotify search. Returns the array under ``<type>s.items``."""
        data = await self._authed_get(
            "/search",
            params={"q": query, "type": spotify_type, "limit": limit},
        )
        key = f"{spotify_type}s"
        bucket = data.get(key, {})
        items = bucket.get("items") or []
        return [i for i in items if isinstance(i, dict)]

    async def my_playlists(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the user's playlists (owned + followed)."""
        data = await self._authed_get(
            "/me/playlists", params={"limit": limit}
        )
        items = data.get("items") or []
        return [i for i in items if isinstance(i, dict)]

    async def my_liked_tracks(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the user's Liked Songs (saved tracks)."""
        data = await self._authed_get(
            "/me/tracks", params={"limit": limit}
        )
        items = data.get("items") or []
        # ``/me/tracks`` wraps each track in ``{added_at, track: {...}}``.
        return [i["track"] for i in items if isinstance(i, dict) and i.get("track")]

    async def recommendations(
        self,
        seed_tracks: list[str] | None = None,
        seed_artists: list[str] | None = None,
        seed_genres: list[str] | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Spotify's ``/recommendations`` — returns track dicts seeded
        by up to 5 combined ids/genres.

        Note: Spotify deprecated this endpoint for *new* applications
        in late 2024 but kept it live for apps that already had access.
        If your Spotify app was registered after the cutoff and the
        request 404s, the station tool surfaces the error to the user.
        """
        params: dict[str, Any] = {"limit": limit}
        if seed_tracks:
            params["seed_tracks"] = ",".join(seed_tracks[:5])
        if seed_artists:
            params["seed_artists"] = ",".join(seed_artists[:5])
        if seed_genres:
            params["seed_genres"] = ",".join(seed_genres[:5])
        if not (seed_tracks or seed_artists or seed_genres):
            raise ValueError("recommendations() requires at least one seed")
        data = await self._authed_get("/recommendations", params=params)
        items = data.get("tracks") or []
        return [t for t in items if isinstance(t, dict)]

    async def available_genre_seeds(self) -> list[str]:
        """Return Spotify's list of valid genre seeds for ``/recommendations``."""
        data = await self._authed_get(
            "/recommendations/available-genre-seeds"
        )
        seeds = data.get("genres") or []
        return [str(g) for g in seeds if isinstance(g, str)]


# ── Spotify ↔ MusicItem mappers ──────────────────────────────────────


def _spotify_track_to_music_item(track: dict[str, Any]) -> MusicItem:
    album = track.get("album") or {}
    artists = track.get("artists") or []
    artist_name = ""
    if artists and isinstance(artists[0], dict):
        artist_name = str(artists[0].get("name") or "")
    images = album.get("images") or []
    art = (
        str(images[0].get("url") or "")
        if images and isinstance(images[0], dict)
        else ""
    )
    duration_ms = int(track.get("duration_ms") or 0)
    return MusicItem(
        id=str(track.get("id") or ""),
        title=str(track.get("name") or ""),
        kind=MusicItemKind.TRACK,
        subtitle=artist_name,
        uri=str(track.get("uri") or ""),
        album_art_url=art,
        duration_seconds=duration_ms / 1000.0,
        service="Spotify",
    )


def _spotify_album_to_music_item(album: dict[str, Any]) -> MusicItem:
    artists = album.get("artists") or []
    artist_name = ""
    if artists and isinstance(artists[0], dict):
        artist_name = str(artists[0].get("name") or "")
    images = album.get("images") or []
    art = (
        str(images[0].get("url") or "")
        if images and isinstance(images[0], dict)
        else ""
    )
    return MusicItem(
        id=str(album.get("id") or ""),
        title=str(album.get("name") or ""),
        kind=MusicItemKind.ALBUM,
        subtitle=artist_name,
        uri=str(album.get("uri") or ""),
        album_art_url=art,
        service="Spotify",
    )


def _spotify_artist_to_music_item(artist: dict[str, Any]) -> MusicItem:
    images = artist.get("images") or []
    art = (
        str(images[0].get("url") or "")
        if images and isinstance(images[0], dict)
        else ""
    )
    return MusicItem(
        id=str(artist.get("id") or ""),
        title=str(artist.get("name") or ""),
        kind=MusicItemKind.ARTIST,
        uri=str(artist.get("uri") or ""),
        album_art_url=art,
        service="Spotify",
    )


def _spotify_playlist_to_music_item(pl: dict[str, Any]) -> MusicItem:
    owner = pl.get("owner") or {}
    images = pl.get("images") or []
    art = (
        str(images[0].get("url") or "")
        if images and isinstance(images[0], dict)
        else ""
    )
    return MusicItem(
        id=str(pl.get("id") or ""),
        title=str(pl.get("name") or ""),
        kind=MusicItemKind.PLAYLIST,
        subtitle=str(owner.get("display_name") or ""),
        uri=str(pl.get("uri") or ""),
        album_art_url=art,
        service="Spotify",
    )


def _extract_auth_code(pasted: str) -> str:
    """Pull the ``code`` out of whatever the user pasted.

    Accepts either a bare code or a full redirect URL with ``?code=…``.
    Returns an empty string if nothing recognisable is found."""
    pasted = (pasted or "").strip()
    if not pasted:
        return ""
    # If it looks like a URL, parse it properly.
    if pasted.startswith(("http://", "https://")):
        parsed = urlparse(pasted)
        qs = parse_qs(parsed.query)
        code = qs.get("code", [""])[0]
        if code:
            return code
    # Otherwise try the regex (covers partial URL pastes too).
    match = _AUTH_CODE_RE.search(pasted)
    if match:
        return match.group(1)
    # Plain code — Spotify codes are URL-safe alphanumeric, usually
    # 200+ chars. Pass through if it doesn't look like JSON or an
    # obvious mistake.
    if len(pasted) >= 20 and "=" not in pasted and " " not in pasted:
        return pasted
    return ""


# ── Backend ─────────────────────────────────────────────────────────


class SonosMusic(MusicBackend, LinkedMusicServiceLister):
    """Music backend backed by Spotify's Web API.

    Named ``sonos`` so the existing music-backend selector doesn't
    break on upgrade — even though the browse/search side now talks to
    Spotify directly (not through Sonos). The speaker backend is still
    Sonos, and that's where the name comes from.
    """

    backend_name = "sonos"
    supports_queue = True
    supports_stations = True
    supports_loop = True

    @classmethod
    def compatible_speaker_backends(cls) -> frozenset[str]:
        return frozenset({"sonos"})

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="client_id",
                type=ToolParameterType.STRING,
                description=(
                    "Spotify Web API client ID from "
                    "https://developer.spotify.com/dashboard — register "
                    "an app, copy its client ID here."
                ),
                default="",
            ),
            ConfigParam(
                key="client_secret",
                type=ToolParameterType.STRING,
                description="Spotify Web API client secret (paired with client_id).",
                default="",
                sensitive=True,
            ),
            ConfigParam(
                key="redirect_uri",
                type=ToolParameterType.STRING,
                description=(
                    "OAuth redirect URI. Must match one of the redirect "
                    "URIs registered on your Spotify app EXACTLY (case- "
                    "and trailing-slash sensitive). Default "
                    "``http://127.0.0.1:8000/callback`` is empirically "
                    "confirmed against Spotify's current policy — "
                    "``localhost`` by hostname is rejected with "
                    "'Insecure' even on HTTPS; only numeric loopback "
                    "(127.0.0.1 / [::1]) or a real HTTPS URL on a valid "
                    "domain works. If you see 'Not matching "
                    "configuration', your app doesn't have this exact "
                    "URL registered — add it in the Spotify developer "
                    "dashboard. The endpoint doesn't need to actually "
                    "respond — after authorizing, your browser will hit "
                    "an error page with the ``?code=…`` in the URL bar; "
                    "paste that URL (or just the code) into Spotify "
                    "Auth Code below."
                ),
                default="https://127.0.0.1:8000/callback",
            ),
            ConfigParam(
                key="refresh_token",
                type=ToolParameterType.STRING,
                description=(
                    "Spotify refresh token — auto-populated by Link "
                    "Spotify → Finish Linking. Don't edit by hand."
                ),
                default="",
                sensitive=True,
            ),
            ConfigParam(
                key="spotify_auth_code",
                type=ToolParameterType.STRING,
                description=(
                    "Paste the full redirect URL (or just the ``code`` "
                    "query parameter) you landed on after authorizing "
                    "in Spotify. Used only during the link flow — "
                    "auto-cleared once tokens are issued."
                ),
                default="",
            ),
            # ── Legacy fields retained for backward compatibility ──
            # These were used by the old SMAPI-based implementation.
            # They have no effect on the Spotify Web API pipeline but
            # we keep them on the schema so existing configs don't
            # fail validation on upgrade. The old token was
            # speaker-bound and isn't transferable to the Web API; a
            # re-link via the new flow is required.
            ConfigParam(
                key="preferred_service",
                type=ToolParameterType.STRING,
                description=(
                    "Legacy: which linked Sonos music service to search "
                    "against. Ignored in the Spotify Web API pipeline — "
                    "left here so existing configs don't fail validation."
                ),
                default="Spotify",
            ),
            ConfigParam(
                key="auth_token",
                type=ToolParameterType.STRING,
                description=(
                    "Legacy SMAPI token. Unused by the Spotify Web API "
                    "pipeline (incompatible format). Retained so the "
                    "settings row preserves the old value across upgrade."
                ),
                default="",
                sensitive=True,
            ),
            ConfigParam(
                key="auth_key",
                type=ToolParameterType.STRING,
                description="Legacy SMAPI token key — see ``auth_token``.",
                default="",
                sensitive=True,
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="link_spotify",
                label="Link Spotify",
                description=(
                    "Start the Spotify Web API OAuth flow. Returns a "
                    "URL — open it, authorize Gilbert, copy the URL "
                    "you get redirected to into Spotify Auth Code, "
                    "save settings, then click Finish Linking."
                ),
            ),
            ConfigAction(
                key="link_spotify_complete",
                label="Finish Linking",
                description=(
                    "Exchange the authorization code (from Spotify Auth "
                    "Code) for a refresh token, and save it. Auto-clears "
                    "the auth code once tokens are issued."
                ),
            ),
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Hit Spotify's /me endpoint to verify the refresh "
                    "token is valid and the current Spotify app config "
                    "works."
                ),
            ),
        ]

    def __init__(self) -> None:
        self._client_id: str = ""
        self._client_secret: str = ""
        self._redirect_uri: str = "https://127.0.0.1:8000/callback"
        self._refresh_token: str = ""
        self._spotify: _SpotifyClient | None = None
        # Pending-link state — held in memory between link_spotify and
        # link_spotify_complete. Persists a nonce for CSRF validation of
        # the ``state`` round-trip.
        self._pending_state: str = ""
        # Cached copy of the ``spotify_auth_code`` settings field. Gets
        # refreshed every time ``initialize`` runs, which (per
        # MusicService.on_config_changed) happens after each settings
        # save — so when the user pastes the code + saves, the next
        # click on Finish Linking sees the latest value. Stored
        # separately because ConfigAction invocations don't carry
        # config values in their payload; without this the
        # link_spotify_complete handler would have nowhere to pull the
        # code from.
        self._auth_code_cache: str = ""

    # ── Lifecycle ────────────────────────────────────────────────────

    async def initialize(self, config: dict[str, object]) -> None:
        self._client_id = str(config.get("client_id") or "")
        self._client_secret = str(config.get("client_secret") or "")
        self._redirect_uri = str(
            config.get("redirect_uri") or "https://127.0.0.1:8000/callback"
        )
        self._refresh_token = str(config.get("refresh_token") or "")
        self._auth_code_cache = str(config.get("spotify_auth_code") or "")

        if self._client_id and self._client_secret:
            self._spotify = _SpotifyClient(
                self._client_id,
                self._client_secret,
                self._refresh_token,
            )
        else:
            self._spotify = None

        logger.info(
            "Sonos music backend initialized (spotify_configured=%s, linked=%s)",
            bool(self._client_id and self._client_secret),
            bool(self._refresh_token),
        )

    async def close(self) -> None:
        if self._spotify is not None:
            await self._spotify.close()
            self._spotify = None

    # ── Service listing ──────────────────────────────────────────────

    def list_linked_services(self) -> list[str]:
        """Report Spotify as linked whenever a refresh token is present.

        The old implementation scanned linked Sonos services. In the
        new pipeline the only relevant "linked service" is Gilbert's
        Spotify OAuth — if it's present, Spotify is available. If it
        isn't, no services are available.
        """
        if self._refresh_token:
            return ["Spotify"]
        return []

    # ── Browse ───────────────────────────────────────────────────────

    async def list_favorites(self) -> list[MusicItem]:
        """Return the user's Spotify Liked Songs.

        Analogous to the old "Sonos favorites" concept but scoped to
        Spotify specifically. Liked tracks is the closest Spotify
        primitive to the Sonos favorites list — both are curated,
        user-specific collections with one-click access."""
        client = self._require_client()
        tracks = await client.my_liked_tracks(limit=50)
        return [_spotify_track_to_music_item(t) for t in tracks]

    async def list_playlists(self) -> list[MusicItem]:
        client = self._require_client()
        playlists = await client.my_playlists(limit=50)
        return [_spotify_playlist_to_music_item(p) for p in playlists]

    # ── Search ───────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        *,
        kind: MusicItemKind = MusicItemKind.TRACK,
        limit: int = 10,
    ) -> list[MusicItem]:
        spotify_type = _KIND_TO_SPOTIFY_TYPE.get(kind)
        if spotify_type is None:
            raise ValueError(f"Unsupported search kind: {kind}")

        client = self._require_client()
        try:
            raw = await client.search(query, spotify_type, limit)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise MusicSearchUnavailableError(
                    "Spotify auth token rejected. An admin needs to re-run "
                    "Settings → Media → Music → Link Spotify."
                ) from exc
            raise

        if kind == MusicItemKind.TRACK:
            return [_spotify_track_to_music_item(r) for r in raw]
        if kind == MusicItemKind.ALBUM:
            return [_spotify_album_to_music_item(r) for r in raw]
        if kind == MusicItemKind.ARTIST:
            return [_spotify_artist_to_music_item(r) for r in raw]
        # PLAYLIST or STATION (which we also route through playlists)
        return [_spotify_playlist_to_music_item(r) for r in raw]

    # ── Playback resolution ──────────────────────────────────────────

    async def resolve_playable(self, item: MusicItem) -> Playable:
        """Return the Spotify URI as a Playable.

        The speaker backend turns the URI into a Sonos LoadContent
        call against the household's linked Spotify account — no
        metadata envelope needed. We just hand back the URI and let
        the speaker layer do the speaker-specific stuff."""
        if item.uri:
            return Playable(uri=item.uri, didl_meta="", title=item.title)
        # Reconstruct from id + kind when uri is missing (shouldn't
        # happen in practice for our mappers, but guards against
        # items synthesized by other callers).
        if item.id:
            kind_str = item.kind.value if item.kind else "track"
            return Playable(
                uri=f"spotify:{kind_str}:{item.id}",
                didl_meta="",
                title=item.title,
            )
        raise ValueError(f"MusicItem has no uri and no id: {item.title}")

    # ── Stations ─────────────────────────────────────────────────────

    async def start_station(
        self,
        seed: MusicItem | str,
        limit: int = 30,
    ) -> list[MusicItem]:
        """Resolve ``seed`` into a list of station tracks via Spotify's
        ``/recommendations`` endpoint.

        Seed dispatch:

        - ``MusicItem`` of kind TRACK / ARTIST → use its id directly as
          ``seed_tracks`` / ``seed_artists``.
        - ``MusicItem`` of any other kind → fall through to the
          string path on the item title.
        - ``str`` → first try to match Spotify's available genre seeds
          (case-insensitive). If no genre match, search Spotify for an
          artist with the seed as its name; if found, use as
          ``seed_artists``. As a last resort search for a track and use
          ``seed_tracks``. Each step uses an existing Spotify endpoint
          we already wire elsewhere — no new auth scopes needed.
        """
        client = self._require_client()

        seed_tracks: list[str] = []
        seed_artists: list[str] = []
        seed_genres: list[str] = []
        resolved_label = ""

        if isinstance(seed, MusicItem):
            if seed.kind == MusicItemKind.TRACK and seed.id:
                seed_tracks.append(seed.id)
                resolved_label = f"track:{seed.title}"
            elif seed.kind == MusicItemKind.ARTIST and seed.id:
                seed_artists.append(seed.id)
                resolved_label = f"artist:{seed.title}"
            else:
                seed = seed.title

        if isinstance(seed, str):
            text = seed.strip()
            if not text:
                raise ValueError("station seed is empty")

            try:
                genres = await client.available_genre_seeds()
            except httpx.HTTPStatusError:
                genres = []
            text_low = text.lower()
            matched_genre = next(
                (g for g in genres if g.lower() == text_low),
                None,
            )
            if matched_genre is not None:
                seed_genres.append(matched_genre)
                resolved_label = f"genre:{matched_genre}"
            else:
                # Try artist match before track — "play a station based
                # on Wilco" is overwhelmingly artist-shaped.
                artist_hits = await client.search(text, "artist", 1)
                if artist_hits:
                    seed_artists.append(str(artist_hits[0].get("id") or ""))
                    resolved_label = f"artist:{artist_hits[0].get('name', text)}"
                else:
                    track_hits = await client.search(text, "track", 1)
                    if track_hits:
                        seed_tracks.append(str(track_hits[0].get("id") or ""))
                        resolved_label = f"track:{track_hits[0].get('name', text)}"

        if not (seed_tracks or seed_artists or seed_genres):
            raise MusicSearchUnavailableError(
                f"Couldn't resolve station seed {seed!r} to a Spotify track, artist, or genre"
            )

        try:
            tracks = await client.recommendations(
                seed_tracks=seed_tracks or None,
                seed_artists=seed_artists or None,
                seed_genres=seed_genres or None,
                limit=limit,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 401:
                raise MusicSearchUnavailableError(
                    "Spotify auth token rejected. Re-link Spotify in Settings → Media → Music."
                ) from exc
            if status == 404:
                # Spotify deprecated /recommendations for new apps in
                # late 2024. Apps registered after the cutoff get a 404.
                raise MusicSearchUnavailableError(
                    "Spotify's recommendations API isn't available for this app — "
                    "stations require a Spotify app with legacy access to "
                    "/recommendations."
                ) from exc
            raise

        logger.info(
            "Spotify station seeded by %s returned %d tracks",
            resolved_label,
            len(tracks),
        )
        return [_spotify_track_to_music_item(t) for t in tracks]

    # ── Config actions ───────────────────────────────────────────────

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        if key == "link_spotify":
            return await self._action_link_start()
        if key == "link_spotify_complete":
            return await self._action_link_complete(payload)
        if key == "test_connection":
            return await self._action_test_connection()
        return ConfigActionResult(
            status="error",
            message=f"Unknown action: {key}",
        )

    async def _action_link_start(self) -> ConfigActionResult:
        if not self._client_id or not self._client_secret:
            return ConfigActionResult(
                status="error",
                message=(
                    "Set Client ID and Client Secret first, then click Save "
                    "before starting the link flow."
                ),
            )
        client = self._spotify
        if client is None:
            client = _SpotifyClient(self._client_id, self._client_secret)
            self._spotify = client

        self._pending_state = secrets.token_urlsafe(16)
        url = client.authorize_url(
            redirect_uri=self._redirect_uri,
            state=self._pending_state,
            scope=_DEFAULT_SCOPES,
        )
        # Log both the redirect_uri and the full authorize URL so
        # "redirect_uri: Insecure" / "Not matching configuration"
        # rejections can be diagnosed against what Spotify actually
        # received — without this it's a guessing game between stale
        # config, unreloaded code, and a mismatched registered URI.
        logger.info(
            "Spotify link flow starting — redirect_uri=%s authorize_url=%s",
            self._redirect_uri,
            url,
        )
        return ConfigActionResult(
            status="pending",
            message=(
                "1) Open the URL and authorize Gilbert on Spotify. "
                "2) Your browser will be redirected to the redirect URI "
                "— which probably shows an error page. That's fine. "
                "3) Copy the *entire* URL from the browser's address bar "
                "(or just the ``?code=…`` part) into Spotify Auth Code "
                "below. "
                "4) Click SAVE (important — without saving, the paste "
                "field doesn't reach the backend). "
                "5) Click Finish Linking."
            ),
            open_url=url,
            followup_action="link_spotify_complete",
        )

    async def _action_link_complete(
        self,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        logger.info(
            "Spotify link_complete invoked: payload_keys=%s "
            "auth_code_cache_len=%d refresh_token_present=%s",
            list(payload.keys()),
            len(self._auth_code_cache or ""),
            bool(self._refresh_token),
        )
        if self._spotify is None:
            return ConfigActionResult(
                status="error",
                message=(
                    "No link flow in progress. Click Link Spotify to start."
                ),
            )
        # The auth code comes from the ``spotify_auth_code`` settings
        # field after the user saved it. ``MusicService.on_config_changed``
        # re-runs ``initialize`` on save, which refreshes our cached
        # copy. ConfigAction payloads from the UI are empty (they only
        # carry the action ``key`` / ``backend``), so we can't get the
        # code from there. Also fall back to ``payload`` for any future
        # caller that *does* include the value inline.
        raw = self._auth_code_cache or str(
            payload.get("settings.spotify_auth_code")
            or payload.get("spotify_auth_code")
            or ""
        )
        code = _extract_auth_code(raw)
        if not code:
            return ConfigActionResult(
                status="error",
                message=(
                    "No authorization code found in Spotify Auth Code. "
                    "Did you click SAVE first? The paste field has to be "
                    "saved before clicking Finish Linking — otherwise "
                    "the code never reaches the backend. Paste the "
                    "redirect URL (or just the ``?code=…`` part) into "
                    "Spotify Auth Code, click Save, *then* click Finish "
                    "Linking."
                ),
            )
        try:
            await self._spotify.exchange_code(code, self._redirect_uri)
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:300]
            return ConfigActionResult(
                status="error",
                message=(
                    f"Spotify rejected the auth code ({exc.response.status_code}): "
                    f"{body}. Make sure the redirect URI matches one registered "
                    f"in your Spotify app."
                ),
            )
        except Exception as exc:  # noqa: BLE001 - surface to user
            logger.exception("Spotify exchange_code failed")
            return ConfigActionResult(
                status="error",
                message=f"Couldn't finish linking: {exc}",
            )

        self._refresh_token = self._spotify.refresh_token
        # Persist the new refresh_token + clear the transient auth_code.
        # ``persist`` is the Gilbert config side-channel the UI uses to
        # drop values into unsaved form state; the user then clicks
        # Save to commit them.
        return ConfigActionResult(
            status="ok",
            message=(
                "Spotify linked. Click Save to store the refresh token."
            ),
            data={
                "persist": {
                    "settings.refresh_token": self._refresh_token,
                    "settings.spotify_auth_code": "",
                },
            },
        )

    async def _action_test_connection(self) -> ConfigActionResult:
        if self._spotify is None:
            return ConfigActionResult(
                status="error",
                message=(
                    "Spotify Client ID / Secret not set. Configure them "
                    "first and save."
                ),
            )
        try:
            me = await self._spotify._authed_get("/me")  # noqa: SLF001
        except MusicSearchUnavailableError as exc:
            return ConfigActionResult(status="error", message=str(exc))
        except httpx.HTTPStatusError as exc:
            return ConfigActionResult(
                status="error",
                message=(
                    f"Spotify /me returned {exc.response.status_code}: "
                    f"{exc.response.text[:200]}"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - surface to user
            return ConfigActionResult(
                status="error",
                message=f"Spotify request failed: {exc}",
            )
        name = me.get("display_name") or me.get("id") or "?"
        return ConfigActionResult(
            status="ok",
            message=(
                f"Connected to Spotify as {name}. Linked Spotify account "
                f"is healthy."
            ),
        )

    # ── Helpers ──────────────────────────────────────────────────────

    def _require_client(self) -> _SpotifyClient:
        if self._spotify is None:
            raise MusicSearchUnavailableError(
                "Spotify isn't configured yet. Go to Settings → Media → "
                "Music and enter Client ID + Client Secret, then run "
                "Link Spotify."
            )
        return self._spotify
