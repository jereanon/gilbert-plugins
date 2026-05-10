# gilbert-plugin-plex

Plex Media Server backend for Gilbert's `MediaLibraryService`.

## What it provides

- Concrete `MediaLibraryBackend` registered as `"plex"`. Subclassing
  triggers `__init_subclass__` and the aggregator (`MediaLibraryService`)
  picks it up via the registry — no service-side imports required.
- All six capability flags set to `True`: `now_playing`, `resume`,
  `continue_watching`, `recently_added`, `seek`, `per_user`,
  `next_episode`.
- Plex.tv PIN-link flow (`link_account` → `link_account_complete`),
  `choose_server`, and `test_connection` ConfigActions.

## Configuration keys

(set under `media_library.backends.plex.settings.<key>` in the entity
config UI)

| Key | Type | Sensitive | Default | Notes |
|---|---|---|---|---|
| `account_token` | string | yes | "" | Plex.tv X-Plex-Token. Filled by the link flow. |
| `server_machine_id` | string | no | "" | Machine identifier of the chosen server. |
| `server_url` | string | no | "" | Override auto-discovered URL. Empty = let plexapi pick. |
| `verify_tls` | boolean | no | True | Disable for self-signed setups. |
| `request_timeout_seconds` | number | no | 15.0 | Per-request timeout. |
| `default_user_token` | string | yes | "" | Fallback X-Plex-Token for no-mapping calls. |

## Slash commands

None. Tools live on the core `MediaLibraryService` (`/media …`); this
plugin only registers the backend.

## OS-level prerequisites

None. `runtime_dependencies()` returns `[]`.

## Test fixture regeneration

Hand-curated fixtures live under `tests/fixtures/plex/<kind>.xml`.
Regenerate from a real Plex server:

```sh
PLEX_URL=https://your-plex.local:32400 \
PLEX_TOKEN=your-token \
uv run python std-plugins/plex/scripts/capture_plex_fixtures.py
```

The script redacts tokens and server identifiers via a regex pass
before writing. Re-run when plexapi or the Plex API contract changes.

## Notes

- Token at-rest encryption is inherited tech debt across the
  codebase — see the v1 deferral in `docs/specs/OPEN_QUESTIONS.md`.
  Until the at-rest encryption story lands, mandate `0600` on
  `.gilbert/gilbert.db`.
- Per-Home-user token caches live on the backend instance keyed by the
  Plex Home user uuid. A re-link (`account_token` change) atomically
  clears all per-Home-user caches before re-pinning the chosen
  PlexServer.
