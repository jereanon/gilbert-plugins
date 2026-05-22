# Sonos Spotify Playback — SMAPI SOAP Bridge

## Summary
Spotify playback on Sonos does NOT go through aiosonos's
`playback.loadContent` API — that path is broken on current S2
firmware for music-service URIs. Instead, Gilbert's Sonos plugin
issues legacy UPnP SOAP calls against `http://<coord_ip>:1400/MediaRenderer/AVTransport/Control`
to enqueue + play Spotify content via the SMAPI service descriptor.
The aiosonos library is still used for everything else: discovery,
grouping, volume, audio-clip announcements, HTTP(S) stream playback.

## Details

### Why aiosonos.loadContent doesn't work

We tried the "obvious" path first:

```python
request = {
    "type": "track",
    "id": {"serviceId": "9", "objectId": "spotify:track:<ID>"},
    "playbackAction": "PLAY",
}
await client.api.playback.load_content(group_id, request)
```

On S16 firmware 94.1-75110 this reliably returned
`FailedCommand: ERROR_COMMAND_FAILED — Failed to enqueue track`.
After deep investigation:

- Music Assistant (primary consumer of aiosonos) doesn't use
  `loadContent` at all. It proxies Spotify audio through its own
  HTTP server with librespot, uses `play_stream_url` with
  `service.id = "mass"`.
- A Sonos developer confirmed on their own community forum: the
  Cloud Control API "is pretty lame… you cannot queue [music
  services'] items, you can only play Favorites and a few other
  limited things."
- aiosonos itself has zero Spotify-specific code — the
  `LoadContentRequest` docstring example is aspirational/stale.
- Legacy `/status/accounts` HTTP endpoint is empty on current
  firmware, so we can't even discover the `accountId` the
  `loadContent` docstring suggests passing.

Conclusion: loadContent-with-serviceId=9 is a dead API path; there is
no local WebSocket API for playing arbitrary Spotify tracks on current
firmware.

### The working path — SMAPI via SOAP

The UPnP SOAP endpoints on port 1400 are still alive in every current
Sonos firmware's device description (confirmed via
`http://<ip>:1400/xml/device_description.xml`). The SMAPI flow SoCo's
`sharelink` plugin uses still works and is what `sonos_smapi.py`
implements — without importing SoCo.

Request shape for a Spotify track:

```
EnqueuedURI:         spotify%3atrack%3a<ID>        (no scheme prefix)
EnqueuedURIMetaData: <DIDL-Lite>…
                       <item id="00032020spotify%3atrack%3a<ID>" …>
                         <dc:title>…</dc:title>
                         <upnp:class>object.item.audioItem.musicTrack</upnp:class>
                         <desc id="cdudn" …>SA_RINCON{sn}_X_#Svc{sn}-0-Token</desc>
                       </item>
                     </DIDL-Lite>
```

The `<desc>` descriptor routes the URI to Sonos's Spotify SMAPI
service — `sn` is the SMAPI service number (2311 world / 3079 US),
NOT the `serviceId=9` used by the cloud Control API. Different
households are registered against different numbers; the client
tries 2311 first and falls back to 3079 on failure.

Containers (playlist, album, show) use an `x-rincon-cpcontainer:`
prefix + kind-specific item_id key + `<upnp:class>` container
variant. Tracks have no scheme prefix — Sonos infers the service
from the DIDL descriptor alone.

### SOAP sequence per fire

1. `RemoveAllTracksFromQueue(InstanceID=0)` — start clean
2. `AddURIToQueue(EnqueuedURI, EnqueuedURIMetaData, DesiredFirstTrackNumberEnqueued=0, EnqueueAsNext=0)` — returns `FirstTrackNumberEnqueued`
3. `SetAVTransportURI(CurrentURI="x-rincon-queue:<coord_rincon_id>#0")` — point the transport at the coordinator's queue
4. `Seek(Unit="TRACK_NR", Target=1)` — jump to the track we just enqueued
5. `Play(Speed=1)`

All five target the group coordinator's IP (not any speaker IP) —
AVTransport is a coordinator-only service. `coord_rincon_id` and
`coord_ip` both come from the existing aiosonos-driven
`_ensure_group` flow via `SonosSpeaker._player_metadata`.

### Layout

- `std-plugins/sonos/sonos_smapi.py` — standalone module.
  - `build_spotify_enqueue(kind, spotify_id, title, service_number)` —
    pure function that returns `(enqueue_uri, didl_metadata)`.
  - `SonosSmapiClient` — async httpx wrapper with one public method,
    `play_spotify(coord_ip, coord_rincon_id, kind, spotify_id, title)`.
    Owns an internal `httpx.AsyncClient` unless one is injected.
  - `SmapiError` — raised on HTTP non-200 or SOAP fault; surfaces UPnP
    `errorCode` + `errorDescription` when present.
- `std-plugins/sonos/sonos_speaker.py::_load_spotify_content` —
  thin wrapper that looks up the coordinator's IP via
  `self._player_metadata[coord_player_id].ip_address`, then delegates
  to `self._smapi.play_spotify(...)`.
- `SonosSpeaker.__init__` creates `self._smapi` lazily;
  `initialize()` constructs it; `close()` calls `aclose()`.

### Gotchas

- The SOAP endpoint is HTTP (port 1400), not HTTPS (port 1443). No
  SSL context needed; a plain `httpx.AsyncClient` works.
- `RemoveAllTracksFromQueue` is only valid on the group coordinator.
  If you call it against a follower the call succeeds but nothing
  happens — tracks stay on the coordinator's queue. Same for
  `AddURIToQueue`. Always target `coord_player_id`'s IP.
- `x-rincon-queue:<id>#0` uses the bare RINCON id (e.g.
  `RINCON_542A1BE1908601400`) — do NOT prefix with `uuid:` even
  though the UDN in device_description does.
- Titles go inside `<dc:title>…</dc:title>` and are XML-escaped for
  `&`, `<`, `>`. Apostrophes don't need escaping in element text per
  the XML spec; `xml.sax.saxutils.escape` leaves them alone by
  default, which is correct here.

## Related
- `std-plugins/sonos/sonos_smapi.py` — SOAP + DIDL implementation
- `std-plugins/sonos/sonos_speaker.py::_load_spotify_content` — call site
- `std-plugins/sonos/tests/test_sonos_smapi.py` — DIDL shape + happy path + fallback coverage
- `std-plugins/sonos/tests/test_sonos_speaker.py::test_spotify_uri_uses_smapi_bridge` — integration at the backend level
- SoCo's `soco/plugins/sharelink.py` — reference implementation for the DIDL shape (we do not import SoCo; this is the shape we copy)
- Sonos community forum, "How to queue a Spotify track through API" — Sonos staff confirmation that the Cloud API can't do this
