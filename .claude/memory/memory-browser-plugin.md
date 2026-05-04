# Browser Plugin

## Summary
Per-user headless Chrome via Playwright. Owns a `BrowserContext` per user (storage_state persisted on disk), exposes navigate/get_text/get_html/click/fill/press/select/screenshot/login/extract tools, an encrypted-at-rest credential store, and a VNC live-login flow for sites whose forms don't fit a CSS-selector fill.

## Details

### Per-user concurrency model
- One `BrowserContext` per `user_id` in `ContextPool` (`std-plugins/browser/context_pool.py`). Storage state lives at `<plugin_data>/users/<user_id>/state.json`.
- One `Page` per user, lazily created on first tool call, kept on `BrowserService._pages`.
- Per-user `asyncio.Lock` in `_page_locks` serializes concurrent tool calls in the same AI turn — Playwright Pages are not safe to mutate concurrently. Cross-user calls fan out freely.
- Idle reaper closes contexts whose `last_used` is older than `idle_timeout_seconds` (default 600). On close it flushes `storage_state` to disk first.

### Credential store (`credentials.py`)
- Entity collection: `browser_credentials` in the plugin's namespaced storage (`gilbert.plugin.browser`).
- Encryption: per-installation Fernet key at `<plugin_data>/fernet.key` (mode 0600), generated on first start. Loss of the key makes credentials unrecoverable — back it up alongside `.gilbert/`.
- `list_for_user` strips the password column. Only the per-id `get` resolution path inside `browser_login` decrypts the password — it never round-trips to the UI.
- Ownership is enforced in `get`/`delete`: a `PermissionError` is raised when the caller's `user_id` doesn't match the row's `user_id`.

### Tool dispatch (`browser_service.py`)
- `_user_id` and `_conversation_id` are read from injected magic keys on the `arguments` dict (AIService injects them at `core/services/ai.py:3328-3334`). Tools must NOT declare them as `ToolParameter`s — those would leak into the model's tool schema.
- `browser_screenshot` uses the `WorkspaceProvider` capability (`get_output_dir` + `register_file`) per the verification doc finding 0.1, then constructs a workspace-reference `FileAttachment(kind="image")` so the PNG renders inline in chat.
- `browser_extract` is gated behind the `ai_chat` capability — `get_tools()` only advertises it when an AI sampling service is present, so installs without an AI backend hide the tool instead of returning errors.

### VNC live login (`vnc.py`)
- `VncSessionManager.start_session` spawns Xvfb (`:N`) + x11vnc + websockify + headed Chromium, with caps per-user (default 2) and server-wide (default 5).
- `BrowserService._ws_vnc_stop` calls `ContextPool.merge_storage_state` after the headed session ends, merging cookies + localStorage origins into the user's persistent headless state file.
- The web-layer route `/api/browser/vnc/{session_id}/ws` (in `src/gilbert/web/routes/browser.py`) calls `BrowserService.get_vnc_websockify_port(session_id, user_id)` to authorize the WebSocket upgrade before opening a localhost TCP bridge.

### Tests
- `time.monotonic` patching is a TRAP for the `_reap_once` test in `vnc.py` — patching globally breaks asyncio's event-loop clock. The test backdates `session.last_used` directly instead.
- LoginRunner test fixture must cache locator factory results by selector so `page._locators[sel].press.assert_awaited_with("Enter")` works after the runner resolves the same selector multiple times.

### Frontend
- `BrowserCredentialsPanel` lives at `frontend/src/components/settings/BrowserCredentialsPanel.tsx`. SettingsPage special-cases the `Browser` category to render it after the regular ConfigSection list.
- WS RPC payloads go directly on the frame (matching `agent.*` convention), not nested under `payload`.

### Host requirements
- After `uv sync`, run `uv run playwright install chromium` to download the browser binary. Without that the service start logs a warning and tools return errors on first call (no startup crash).
- Linux headless deps: `libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2`. `uv run playwright install-deps chromium` installs them on apt-based distros.
- VNC live-login extras: `xvfb`, `x11vnc`, `websockify`. Standard apt/dnf packages.

## Related
- Spec: `docs/superpowers/specs/2026-05-04-browser-plugin-verification.md` (the seven Phase-0 findings that decided the design)
- Plan: `docs/superpowers/plans/2026-05-04-browser-plugin.md`
- WorkspaceProvider protocol: `src/gilbert/interfaces/workspace.py`
- AISamplingProvider protocol: `src/gilbert/interfaces/ai.py`
