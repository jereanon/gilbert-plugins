"""UniFi Access integration — badge reader events and entry/exit tracking."""

import logging
import time
from dataclasses import dataclass
from typing import Any

from .client import UniFiClient

logger = logging.getLogger(__name__)

# Event type keywords that indicate entry vs exit
_ENTRY_KEYWORDS = ("unlock", "entry", "granted", "open")
_EXIT_KEYWORDS = ("lock", "exit", "close")
# Substrings in event_type that mark an event as a doorbell/intercom press
# (vs. a routine badge unlock). Intercom devices like UA-G2 Pro emit these.
_DOORBELL_KEYWORDS = ("doorbell", "ring", "intercom", "call")


@dataclass(frozen=True)
class AccessDoor:
    """A UniFi Access door / reader device."""

    device_id: str
    name: str
    model: str
    online: bool


@dataclass(frozen=True)
class BadgeEvent:
    """A badge reader event."""

    event_id: str
    person_name: str
    direction: str  # "in" or "out"
    door_name: str
    timestamp: int  # epoch ms
    event_type: str = ""


class UniFiAccess:
    """Queries UniFi Access for badge reader events."""

    def __init__(self, client: UniFiClient) -> None:
        self._client = client

    async def list_doors(self) -> list[AccessDoor]:
        """List all UniFi Access door readers / hubs."""
        data = await self._client.get("/proxy/access/api/v2/devices")
        if data is None:
            return []

        raw_devices: list[dict[str, Any]] = (
            data.get("data", [])
            if isinstance(data, dict)
            else (data if isinstance(data, list) else [])
        )

        doors: list[AccessDoor] = []
        for d in raw_devices:
            if not isinstance(d, dict):
                continue
            name = d.get("name") or d.get("alias") or d.get("display_name") or ""
            if not name:
                continue
            doors.append(
                AccessDoor(
                    device_id=str(d.get("id") or d.get("_id") or ""),
                    name=str(name),
                    model=str(d.get("type") or d.get("model") or ""),
                    online=bool(d.get("is_online", d.get("online", True))),
                )
            )
        if doors:
            logger.debug(
                "Access doors: %d (%s)",
                len(doors),
                ", ".join(d.name for d in doors),
            )
        return doors

    async def get_doorbell_events(self, lookback_seconds: int = 30) -> list[BadgeEvent]:
        """Return recent doorbell-press / intercom-ring events from Access devices.

        Filtered to event types that look like a press (vs. routine badge
        unlocks), so this is safe to poll frequently.
        """
        lookback_hours = max(1, (lookback_seconds // 3600) + 1)
        events = await self.get_badge_events(lookback_hours=lookback_hours)
        cutoff_ms = int(time.time() * 1000) - (lookback_seconds * 1000)
        return [e for e in events if e.timestamp >= cutoff_ms and _is_doorbell_event(e)]

    async def get_badge_events(self, lookback_hours: int = 24) -> list[BadgeEvent]:
        """Get badge events within the lookback window."""
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - (lookback_hours * 3600 * 1000)

        data = await self._client.get(
            "/proxy/access/api/v2/device/logs",
            params={"start": start_ms, "end": now_ms},
        )
        if data is None:
            return []

        raw_events: list[dict[str, Any]] = (
            data.get("data", [])
            if isinstance(data, dict)
            else (data if isinstance(data, list) else [])
        )

        events: list[BadgeEvent] = []
        for e in raw_events:
            event_type = str(e.get("event_type", e.get("type", "")))
            person_name = self._extract_person_name(e)
            # Doorbell-press events from intercoms have no badge holder, so
            # don't drop them when person_name is empty.
            if not person_name and not _looks_like_doorbell(event_type):
                continue

            direction = _classify_direction(event_type)
            door_name = e.get("door_name", e.get("device_name", ""))
            timestamp = e.get("timestamp", e.get("time", 0))

            # Normalize timestamp to epoch ms
            if isinstance(timestamp, (int, float)):
                if timestamp < 1e12:  # seconds, not ms
                    timestamp = int(timestamp * 1000)

            events.append(
                BadgeEvent(
                    event_id=e.get("id", e.get("_id", "")),
                    person_name=person_name,
                    direction=direction,
                    door_name=str(door_name),
                    timestamp=int(timestamp),
                    event_type=event_type,
                )
            )

        # Sort by timestamp descending (most recent first)
        events.sort(key=lambda ev: ev.timestamp, reverse=True)

        if events:
            logger.debug("Badge events: %d in lookback window", len(events))

        return events

    async def get_currently_badged_in(self, lookback_hours: int = 24) -> list[BadgeEvent]:
        """Get people whose most recent event is an entry (badge in).

        Returns the most recent "in" event per person, excluding those
        whose most recent event is "out".
        """
        events = await self.get_badge_events(lookback_hours=lookback_hours)

        # Most recent event per person
        latest_per_person: dict[str, BadgeEvent] = {}
        for event in events:
            name_lower = event.person_name.lower()
            if name_lower not in latest_per_person:
                latest_per_person[name_lower] = event

        # Filter to those whose latest event is "in"
        return [event for event in latest_per_person.values() if event.direction == "in"]

    @staticmethod
    def _extract_person_name(event: dict[str, Any]) -> str:
        """Extract the person's name from a badge event.

        Checks multiple fields since the API structure varies.
        """
        # Try common fields
        for field in ("full_name", "actor_name", "holder_name", "person_name"):
            name = event.get(field)
            if name and isinstance(name, str):
                return name.strip()

        # Try nested actor object
        actor = event.get("actor", {})
        if isinstance(actor, dict):
            name = actor.get("name", actor.get("display_name", ""))
            if name and isinstance(name, str):
                return name.strip()

        # Try credential holder
        holder = event.get("credential_holder", {})
        if isinstance(holder, dict):
            first = holder.get("first_name", "")
            last = holder.get("last_name", "")
            if first or last:
                return f"{first} {last}".strip()

        return ""


def _classify_direction(event_type: str) -> str:
    """Classify a badge event as entry ("in") or exit ("out")."""
    lower = event_type.lower()
    if any(kw in lower for kw in _ENTRY_KEYWORDS):
        return "in"
    if any(kw in lower for kw in _EXIT_KEYWORDS):
        return "out"
    # Default to "in" for unclassified events (someone interacted with a reader)
    return "in"


def _looks_like_doorbell(event_type: str) -> bool:
    """True if the event_type names a doorbell-press / intercom-call event."""
    lower = event_type.lower()
    return any(kw in lower for kw in _DOORBELL_KEYWORDS)


def _is_doorbell_event(event: BadgeEvent) -> bool:
    return _looks_like_doorbell(event.event_type)
