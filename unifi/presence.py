"""UniFi composite presence backend — aggregates Network, Protect, and Access signals."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.presence import (
    PresenceBackend,
    PresenceObservation,
    PresenceState,
    UserPresence,
)
from gilbert.interfaces.tools import ToolParameterType

from .access import UniFiAccess
from .client import (
    UniFiAPIError,
    UniFiAuthError,
    UniFiClient,
    UniFiConnectionError,
)
from .name_resolver import NameResolver
from .network import UniFiNetwork
from .protect import UniFiProtect

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _BadgeSignal:
    direction: str  # "in" or "out"
    since: str


@dataclass(frozen=True)
class _FaceSignal:
    since: str


@dataclass(frozen=True)
class _WifiSignal:
    since: str
    device_name: str


class UniFiPresenceBackend(PresenceBackend):
    """Composite presence backend combining UniFi Network, Protect, and Access.

    Signal aggregation priority:
    1. Badge IN  → PRESENT  (authoritative physical access)
    2. Badge OUT → AWAY     (explicit departure)
    3. Face seen → PRESENT  (high-confidence visual ID)
    4. WiFi phone connected → PRESENT (only phones count, not laptops/IoT)
    5. No signals → AWAY
    """

    backend_name = "unifi"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="unifi_network.host",
                type=ToolParameterType.STRING,
                description="UniFi Network controller URL (e.g., https://192.168.1.1).",
                default="",
                restart_required=True,
            ),
            ConfigParam(
                key="unifi_network.username",
                type=ToolParameterType.STRING,
                description="UniFi Network username.",
                default="",
                restart_required=True,
            ),
            ConfigParam(
                key="unifi_network.password",
                type=ToolParameterType.STRING,
                description="UniFi Network password.",
                default="",
                restart_required=True,
                sensitive=True,
            ),
            ConfigParam(
                key="unifi_network.verify_ssl",
                type=ToolParameterType.BOOLEAN,
                description="Verify SSL certificates for Network controller.",
                default=False,
            ),
            ConfigParam(
                key="unifi_protect.host",
                type=ToolParameterType.STRING,
                description="UniFi Protect controller URL (e.g., https://192.168.1.1).",
                default="",
                restart_required=True,
            ),
            ConfigParam(
                key="unifi_protect.username",
                type=ToolParameterType.STRING,
                description="UniFi Protect username.",
                default="",
                restart_required=True,
            ),
            ConfigParam(
                key="unifi_protect.password",
                type=ToolParameterType.STRING,
                description="UniFi Protect password.",
                default="",
                restart_required=True,
                sensitive=True,
            ),
            ConfigParam(
                key="unifi_protect.verify_ssl",
                type=ToolParameterType.BOOLEAN,
                description="Verify SSL certificates for Protect controller.",
                default=False,
            ),
            ConfigParam(
                key="face_lookback_minutes",
                type=ToolParameterType.INTEGER,
                description="Minutes to look back for face detection events.",
                default=30,
            ),
            ConfigParam(
                key="badge_lookback_hours",
                type=ToolParameterType.INTEGER,
                description="Hours to look back for badge/access events.",
                default=24,
            ),
        ]

    # Stable backend identifiers reported on PresenceObservation rows.
    # The presence service composes these with the raw "thing" id when
    # building the mapping pivot key, so renaming a value here breaks
    # already-saved mappings — change deliberately.
    _BACKEND_ACCESS: str = "unifi:access"
    _BACKEND_PROTECT: str = "unifi:protect"
    _BACKEND_NETWORK: str = "unifi:network"

    def __init__(self) -> None:
        self._clients: dict[str, UniFiClient] = {}
        self._network: UniFiNetwork | None = None
        self._protect: UniFiProtect | None = None
        self._access: UniFiAccess | None = None
        self._device_person_map: dict[str, str] = {}
        self._face_lookback_minutes: int = 30
        self._badge_lookback_hours: int = 24
        self._name_resolver: NameResolver = NameResolver()
        # Per-source admin-asserted thing→user mappings. Populated by
        # the presence service via ``apply_thing_mappings`` whenever
        # the mapping UI changes. Keys are the per-backend ``thing_id``
        # (the raw name as observed); values are user_ids. Only
        # non-empty user_ids are stored — clearing a mapping just
        # removes the entry so the fuzzy resolver takes over again.
        self._thing_overrides: dict[str, dict[str, str]] = {
            self._BACKEND_ACCESS: {},
            self._BACKEND_PROTECT: {},
            self._BACKEND_NETWORK: {},
        }

    async def initialize(self, config: dict[str, object]) -> None:
        dpm = config.get("device_person_map", {}) or {}
        self._device_person_map = dict(dpm) if isinstance(dpm, dict) else {}
        self._face_lookback_minutes = int(str(config.get("face_lookback_minutes", 30) or 30))
        self._badge_lookback_hours = int(str(config.get("badge_lookback_hours", 24) or 24))
        za = config.get("zone_aliases", {}) or {}
        zone_aliases: dict[str, list[str]] = dict(za) if isinstance(za, dict) else {}

        # Initialize network controller
        net_cfg = config.get("unifi_network", {})
        if isinstance(net_cfg, dict) and net_cfg.get("host"):
            client = await self._get_or_create_client(net_cfg)
            if client:
                self._network = UniFiNetwork(client, self._device_person_map)
                logger.info("UniFi Network initialized (%s)", net_cfg["host"])

        # Initialize protect/access controller (may be same or different host)
        prot_cfg = config.get("unifi_protect", {})
        if isinstance(prot_cfg, dict) and prot_cfg.get("host"):
            client = await self._get_or_create_client(prot_cfg)
            if client:
                self._protect = UniFiProtect(client, zone_aliases)
                self._access = UniFiAccess(client)
                logger.info("UniFi Protect/Access initialized (%s)", prot_cfg["host"])

        # Load user list for name resolution
        user_service = config.get("_user_service")
        if user_service is not None:
            await self._name_resolver.load_users(user_service)

        active = []
        if self._network:
            active.append("network")
        if self._protect:
            active.append("protect")
        if self._access:
            active.append("access")
        logger.info("UniFi presence backend ready — subsystems: %s", ", ".join(active) or "none")

    async def _get_or_create_client(self, cfg: dict[str, Any]) -> UniFiClient | None:
        """Get an existing client for the host or create a new one."""
        host = cfg["host"]
        if host in self._clients:
            return self._clients[host]

        username = cfg.get("username", "")
        password = cfg.get("password", "")
        if not username or not password:
            logger.warning("No credentials configured for UniFi host %s", host)
            return None

        verify_ssl = cfg.get("verify_ssl", False)
        client = UniFiClient(
            host=host,
            username=str(username),
            password=str(password),
            verify_ssl=bool(verify_ssl),
        )

        try:
            await client.login()
        except (UniFiAuthError, UniFiConnectionError) as e:
            logger.warning("Failed to connect to UniFi at %s: %s", host, e)
            await client.close()
            return None

        self._clients[host] = client
        return client

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
        self._network = None
        self._protect = None
        self._access = None

    # --- Backend actions ---

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Ping the configured UniFi controller(s) to verify "
                    "credentials and reachability."
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
        """Probe each initialized UniFi subsystem with a cheap real query.

        Deliberately avoids calling ``client.login()`` directly — the
        working runtime path is "make an API call, UniFiClient
        auto-logs-in on first request / on 401". Testing that same path
        matches what polling actually does, so if presence is working
        we'll see it working here too.
        """
        subsystems_ok: list[str] = []
        errors: list[str] = []

        if self._network is None and self._protect is None and self._access is None:
            return ConfigActionResult(
                status="error",
                message=(
                    "No UniFi subsystems are initialized. Check that host "
                    "and credentials are set under Network/Protect, save, "
                    "and restart the presence service."
                ),
            )

        if self._network is not None:
            try:
                clients = await self._network.get_connected_clients()
                subsystems_ok.append(f"network ({len(clients)} clients)")
            except (UniFiAuthError, UniFiConnectionError, UniFiAPIError) as exc:
                errors.append(f"network: {exc}")
            except Exception as exc:
                errors.append(f"network: {exc}")

        if self._protect is not None:
            try:
                cameras = await self._protect.list_cameras()
                subsystems_ok.append(f"protect ({len(cameras)} cameras)")
            except (UniFiAuthError, UniFiConnectionError, UniFiAPIError) as exc:
                errors.append(f"protect: {exc}")
            except Exception as exc:
                errors.append(f"protect: {exc}")

        if self._access is not None:
            try:
                # Small lookback window so the query is as cheap as possible
                events = await self._access.get_badge_events(lookback_hours=1)
                subsystems_ok.append(f"access ({len(events)} recent events)")
            except (UniFiAuthError, UniFiConnectionError, UniFiAPIError) as exc:
                errors.append(f"access: {exc}")
            except Exception as exc:
                errors.append(f"access: {exc}")

        if not subsystems_ok and errors:
            return ConfigActionResult(
                status="error",
                message="; ".join(errors),
            )
        if errors:
            return ConfigActionResult(
                status="error",
                message=(
                    "Partial success — " + ", ".join(subsystems_ok) + " OK; " + "; ".join(errors)
                ),
            )
        return ConfigActionResult(
            status="ok",
            message="Connected: " + ", ".join(subsystems_ok),
        )

    # --- PresenceBackend implementation ---

    async def get_presence(self, user_id: str) -> UserPresence:
        all_presence = await self.get_all_presence()
        for p in all_presence:
            if p.user_id.lower() == user_id.lower():
                return p
        return UserPresence(
            user_id=user_id,
            state=PresenceState.UNKNOWN,
            source="unifi",
        )

    async def get_all_presence(self) -> list[UserPresence]:
        # Query all subsystems in parallel
        badge_signals, wifi_signals, face_signals = await asyncio.gather(
            self._get_badge_signals(),
            self._get_wifi_signals(),
            self._get_face_signals(),
        )

        # Resolve all raw names to user IDs and collect signals per resolved user.
        # Multiple raw names may resolve to the same user (e.g., "Brian" and "Brian Dilley").
        user_signals: dict[str, dict[str, Any]] = {}  # user_id → {badge, face, wifi}

        def _try_resolve(source: str, raw_name: str) -> str | None:
            """Resolve a raw name to a user_id, preferring admin-asserted
            overrides from the mapping UI before falling back to the
            fuzzy ``NameResolver``. Returns None if neither succeeds."""
            uid = self._thing_overrides.get(source, {}).get(raw_name)
            if not uid:
                resolved = self._name_resolver.resolve(raw_name)
                uid = resolved.user_id if resolved else None
            if uid:
                if uid not in user_signals:
                    user_signals[uid] = {}
                return uid
            logger.debug("Could not resolve '%s' to a known user — skipping", raw_name)
            return None

        for raw_name, signal in badge_signals.items():
            uid = _try_resolve(self._BACKEND_ACCESS, raw_name)
            if uid is not None and "badge" not in user_signals[uid]:
                user_signals[uid]["badge"] = signal

        for raw_name, signal in face_signals.items():
            uid = _try_resolve(self._BACKEND_PROTECT, raw_name)
            if uid is not None and "face" not in user_signals[uid]:
                user_signals[uid]["face"] = signal

        for raw_name, signal in wifi_signals.items():
            uid = _try_resolve(self._BACKEND_NETWORK, raw_name)
            if uid is not None and "wifi" not in user_signals[uid]:
                user_signals[uid]["wifi"] = signal

        # Apply priority cascade for each resolved user
        results: list[UserPresence] = []
        for uid, signals in user_signals.items():
            badge = signals.get("badge")
            face = signals.get("face")
            wifi = signals.get("wifi")

            if badge and badge.direction == "in":
                state = PresenceState.PRESENT
                source = "unifi:access"
                since = badge.since
            elif badge and badge.direction == "out":
                state = PresenceState.AWAY
                source = "unifi:access"
                since = badge.since
            elif face:
                state = PresenceState.PRESENT
                source = "unifi:protect"
                since = face.since
            elif wifi:
                state = PresenceState.PRESENT
                source = "unifi:network"
                since = wifi.since
            else:
                state = PresenceState.AWAY
                source = "unifi"
                since = ""

            results.append(
                UserPresence(
                    user_id=uid,
                    state=state,
                    since=since,
                    source=source,
                )
            )

        return results

    async def list_tracked_users(self) -> list[str]:
        all_presence = await self.get_all_presence()
        return [p.user_id for p in all_presence]

    async def get_observations(self) -> list[PresenceObservation]:
        """Emit one observation per raw name each subsystem has seen.

        Used by the presence service's mapping UI to surface every
        identifiable thing the backend has detected, regardless of
        whether a user mapping exists for it yet. Pulls from the same
        signal-gathering helpers ``get_all_presence`` uses, so an
        unmapped face / badge / wifi name shows up here on the next
        poll after first sighting.
        """
        badge_signals, wifi_signals, face_signals = await asyncio.gather(
            self._get_badge_signals(),
            self._get_wifi_signals(),
            self._get_face_signals(),
            return_exceptions=False,
        )

        observations: list[PresenceObservation] = []

        for raw_name, signal in badge_signals.items():
            observations.append(
                PresenceObservation(
                    backend=self._BACKEND_ACCESS,
                    thing_id=raw_name,
                    label=raw_name,
                    kind="badge",
                    first_seen=signal.since,
                    last_seen=signal.since,
                )
            )

        for raw_name, signal in face_signals.items():
            observations.append(
                PresenceObservation(
                    backend=self._BACKEND_PROTECT,
                    thing_id=raw_name,
                    label=raw_name,
                    kind="face",
                    first_seen=signal.since,
                    last_seen=signal.since,
                )
            )

        for raw_name, signal in wifi_signals.items():
            observations.append(
                PresenceObservation(
                    backend=self._BACKEND_NETWORK,
                    thing_id=raw_name,
                    label=signal.device_name or raw_name,
                    kind="wifi",
                    first_seen=signal.since,
                    last_seen=signal.since,
                )
            )

        return observations

    async def apply_thing_mappings(self, mappings: dict[str, str]) -> None:
        """Adopt admin-edited mappings from the presence service.

        Keys are ``"{backend}:{thing_id}"`` strings (e.g. ``"unifi:protect:Brian D"``);
        the colon delimiter inside the backend name means we have to be
        careful splitting — the first two components are the backend,
        the rest is the thing_id. Empty user_id removes the override so
        the fuzzy NameResolver takes over again on the next poll.
        """
        new_overrides: dict[str, dict[str, str]] = {
            self._BACKEND_ACCESS: {},
            self._BACKEND_PROTECT: {},
            self._BACKEND_NETWORK: {},
        }
        for key, user_id in mappings.items():
            if not user_id:
                continue
            # Backend names have the form "unifi:<subsystem>"; the
            # thing_id is whatever follows after the second colon.
            parts = key.split(":", 2)
            if len(parts) < 3:
                continue
            backend = f"{parts[0]}:{parts[1]}"
            thing_id = parts[2]
            if backend in new_overrides:
                new_overrides[backend][thing_id] = user_id
        self._thing_overrides = new_overrides
        # Bust the fuzzy-resolver's cache: a thing that used to fall
        # through to fuzzy may now be authoritatively mapped (or vice
        # versa), and the cached ResolvedUser would lock in the old
        # answer until the cache TTL'd naturally.
        self._name_resolver._cache.clear()

    # --- Signal gathering (each isolated for error tolerance) ---

    async def _get_badge_signals(self) -> dict[str, _BadgeSignal]:
        if self._access is None:
            return {}
        try:
            badged_in = await self._access.get_badge_events(
                lookback_hours=self._badge_lookback_hours,
            )
            # Get most recent event per person
            latest: dict[str, _BadgeSignal] = {}
            for event in badged_in:
                name = event.person_name
                if name.lower() not in {n.lower() for n in latest}:
                    since = _epoch_ms_to_iso(event.timestamp)
                    latest[name] = _BadgeSignal(direction=event.direction, since=since)
            return latest
        except (UniFiConnectionError, UniFiAuthError, UniFiAPIError) as e:
            logger.warning("UniFi Access unavailable: %s", e)
            return {}

    async def _get_face_signals(self) -> dict[str, _FaceSignal]:
        if self._protect is None:
            return {}
        try:
            faces = await self._protect.get_face_detections(
                lookback_minutes=self._face_lookback_minutes,
            )
            # Most recent face detection per person
            result: dict[str, _FaceSignal] = {}
            for f in faces:
                name = f.person_name
                if name.lower() not in {n.lower() for n in result}:
                    since = _epoch_ms_to_iso(f.timestamp)
                    result[name] = _FaceSignal(since=since)
            return result
        except (UniFiConnectionError, UniFiAuthError, UniFiAPIError) as e:
            logger.warning("UniFi Protect unavailable: %s", e)
            return {}

    async def _get_wifi_signals(self) -> dict[str, _WifiSignal]:
        if self._network is None:
            return {}
        try:
            people = await self._network.get_people_on_network()
            result: dict[str, _WifiSignal] = {}
            for person, clients in people.items():
                if clients:
                    # Use the most recent last_seen
                    best = max(clients, key=lambda c: c.last_seen)
                    result[person] = _WifiSignal(
                        since=best.last_seen,
                        device_name=best.device_name or best.hostname,
                    )
            return result
        except (UniFiConnectionError, UniFiAuthError, UniFiAPIError) as e:
            logger.warning("UniFi Network unavailable: %s", e)
            return {}


def _epoch_ms_to_iso(epoch_ms: int) -> str:
    """Convert epoch milliseconds to ISO 8601 string."""
    if not epoch_ms:
        return ""
    try:
        dt = datetime.fromtimestamp(epoch_ms / 1000.0, tz=UTC)
        return dt.isoformat()
    except (ValueError, OSError):
        return ""
