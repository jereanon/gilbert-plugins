# Memories

- [Guess That Song Plugin](memory-guess-that-song.md) — multiplayer music guessing game with UI blocks and AI-mediated gameplay
- [UniFi relative-import gotcha](memory-unifi-relative-imports.md) — spec_from_file_location + submodule_search_locations=[] breaks intra-plugin relative imports
- [Plugin pyproject.toml is mandatory](memory-plugin-pyproject.md) — every plugin dir needs one for the uv workspace glob, even with empty deps
- [Sonos Spotify playback — SMAPI SOAP bridge](memory-sonos-spotify-playback.md) — aiosonos loadContent is a dead API path; Gilbert issues AVTransport SOAP calls (AddURIToQueue + Play) with a DIDL SMAPI descriptor
- [Browser plugin](memory-browser-plugin.md) — per-user Playwright contexts, Fernet-sealed credential store, VNC live-login + storage_state merge, screenshots as workspace-reference FileAttachments
