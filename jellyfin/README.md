# gilbert-plugin-jellyfin

Jellyfin Media Server backend for Gilbert's `MediaLibraryService`.

## What it provides

- Concrete `MediaLibraryBackend` registered as `"jellyfin"`. All six
  capability flags set to `True`: `now_playing`, `resume`,
  `continue_watching`, `recently_added`, `seek`, `per_user`,
  `next_episode`.
- `link_account` ConfigAction (POST `/Users/AuthenticateByName`,
  persists `access_token`, clears `admin_password` unless
  `keep_password=true`).
- `test_connection` ConfigAction (`GET /System/Info?api_key=…`).

## Configuration keys

(set under `media_library.backends.jellyfin.settings.<key>` in the
entity config UI)

| Key | Type | Sensitive | Default | Notes |
|---|---|---|---|---|
| `server_url` | string | no | "" | e.g. `http://jellyfin.local:8096`. |
| `admin_username` | string | no | "" | Used to bootstrap the device token at link time. |
| `admin_password` | string | yes | "" | Cleared after the link flow unless `keep_password=true`. |
| `keep_password` | boolean | no | False | Retain `admin_password` after link. |
| `device_id` | string | no | (auto) | Stable identifier in `X-Emby-Authorization`. |
| `access_token` | string | yes | "" | Auto-populated by `link_account`. |
| `verify_tls` | boolean | no | True | |
| `request_timeout_seconds` | number | no | 15.0 | |

## Slash commands

None. Tools live on the core `MediaLibraryService` (`/media …`).

## OS-level prerequisites

None. `runtime_dependencies()` returns `[]`.

## Per-user authentication

v1 uses the **admin token + `userId` query/path parameter** for
per-user data. Each per-user query lands on the Jellyfin server's
audit trail as the admin user — accepted v1 limitation tracked in
`docs/specs/OPEN_QUESTIONS.md`. Per-user-token minting (Jellyfin
10.9+ user-scoped api-keys) is v2 work.

When a Gilbert user has no Jellyfin mapping, per-user tools (resume,
continue-watching, recently-added, next_episode) refuse to fall back
to the admin's own user-id — that would leak the admin's history.
The `userId` is what *scopes* the data; without one, the call is
refused upstream.

## Test fixture regeneration

Hand-curated JSON fixtures live under
`tests/fixtures/jellyfin/<kind>.json`. Regenerate from a real Jellyfin
server:

```sh
JELLYFIN_URL=http://jellyfin.local:8096 \
JELLYFIN_TOKEN=admin-token \
JELLYFIN_USER_ID=admin-user-id \
uv run python std-plugins/jellyfin/scripts/capture_jellyfin_fixtures.py
```

The script redacts tokens via a regex pass before writing.

## Notes

- Token at-rest encryption is inherited tech debt across the
  codebase — see `docs/specs/OPEN_QUESTIONS.md`. v1 mandates `0600`
  on `.gilbert/gilbert.db`.
- Jellyfin's username → user-id resolution (`_resolve_user_id`) is
  cached for the service lifetime keyed by the *Jellyfin* username,
  NOT by Gilbert user id. Two Gilbert users mapped to the same
  Jellyfin username share the resolved id by definition — the cache
  cannot leak across Gilbert users.
- `StartPositionTicks` / `SeekPositionTicks` are 100-ns ticks. The
  helpers `_seconds_to_ticks` / `_ticks_to_seconds` handle the
  conversion (1 second == 10_000_000 ticks).
