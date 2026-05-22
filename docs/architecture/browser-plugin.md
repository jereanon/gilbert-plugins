# Browser Plugin

## Summary
Per-user headless Chrome via Playwright running in a Docker container by default (host stays clean — no apt-get install gymnastics). Owns one `BrowserContext` per user inside the shared container, exposes navigate/get_text/get_html/click/fill/press/select/screenshot/login/extract tools, an encrypted-at-rest per-user credential store, and a VNC live-login flow for sites whose forms don't fit a CSS-selector fill. Toggleable in Settings → Services (off by default).

## Details

### Mode (auto / docker / host)
- `BrowserService.start()` reads the `mode` ConfigParam and calls `_maybe_start_container()`. `auto` (default) prefers Docker via `BrowserContainer.is_available()` (probes `docker info`, not just `docker --version`), falls back to host-native Playwright. `docker` requires Docker (still falls back if the container fails to start, with a warning). `host` skips Docker entirely.
- `BrowserContainer` runs `mcr.microsoft.com/playwright:v<X.Y.Z>-jammy`; the tag is auto-pinned to the installed Python `playwright` package version detected at runtime via `importlib.metadata.version("playwright")`. Override via the `docker_image` ConfigParam.
- The container runs `npx -y --package=playwright@<X.Y.Z> playwright run-server --host 0.0.0.0 --port <PORT>`; `ContextPool` connects via `chromium.connect("ws://127.0.0.1:<PORT>/")`.
- Storage state stays HOST-side — the WS protocol carries `storage_state` and screenshot bytes between the Python client and the container. No volume mounts.
- One container, many BrowserContexts — same per-user-context model as the host-native code.

### Per-user concurrency model
- One `BrowserContext` per `user_id` in `ContextPool` (`std-plugins/browser/context_pool.py`). Storage state lives at `<plugin_data>/users/<user_id>/state.json`.
- One `Page` per user, lazily created on first tool call, kept on `BrowserService._pages`.
- Per-user `asyncio.Lock` in `_page_locks` serializes concurrent tool calls in the same AI turn — Playwright Pages are not safe to mutate concurrently. Cross-user calls fan out freely.
- Idle reaper closes contexts whose `last_used` is older than `idle_timeout_seconds` (default 600). On close it flushes `storage_state` to disk first.

### Credential store (`credentials.py`)
- Entity collection: `browser_credentials` in the plugin's namespaced storage (`gilbert.plugin.browser`). **Strictly per-user — no global credentials**.
- Encryption: per-installation Fernet key at `<plugin_data>/fernet.key` (mode 0600), generated on first start. Loss of the key makes credentials unrecoverable — back it up alongside `.gilbert/`.
- `list_for_user` strips the password column. Only the per-id `get` resolution path inside `browser_login` decrypts the password — it never round-trips to the UI.
- Ownership is enforced in `get`/`delete`: a `PermissionError` is raised when the caller's `user_id` doesn't match the row's `user_id`.
- The credentials UI mounts on the **per-user Account page** (`/account`), not on admin Settings — surfaced via the generic `Plugin.ui_panels()` extension framework.

### Tool dispatch (`browser_service.py`)
- `_user_id` and `_conversation_id` are read from injected magic keys on the `arguments` dict (AIService injects them at `core/services/ai.py`). Tools must NOT declare them as `ToolParameter`s — those would leak into the model's tool schema.
- `browser_screenshot` uses the `WorkspaceProvider` capability (`get_output_dir` + `register_file`), then constructs a workspace-reference `FileAttachment(kind="image")` so the PNG renders inline in chat.
- `browser_extract` is gated behind the `ai_chat` capability — `get_tools()` only advertises it when an AI sampling service is present, so installs without an AI backend hide the tool instead of returning errors.

### VNC live login (`vnc.py`)
- Stays HOST-NATIVE — independent of the headless container. Spawns Xvfb (`:N`) + x11vnc + websockify + headed Chromium on the host, with caps per-user (default 2) and server-wide (default 5). Requires `xvfb`, `x11vnc`, `websockify` on PATH.
- `BrowserService._ws_vnc_stop` calls `ContextPool.merge_storage_state` after the headed session ends, merging cookies + localStorage origins into the user's persistent headless state file.
- The web-layer route `/api/browser/vnc/{session_id}/ws` (in `src/gilbert/web/routes/browser.py`) calls `BrowserService.get_vnc_websockify_port(session_id, user_id)` to authorize the WebSocket upgrade before opening a localhost TCP bridge.

### Frontend (lives in `std-plugins/browser/frontend/`)
- All TS / TSX is inside the plugin directory. Core SPA imports nothing browser-specific.
- `panels.ts` registers `BrowserCredentialsPanel` under `panel_id="browser.credentials"` for slot `account.extensions`. Backend declares the same `panel_id` via `BrowserPlugin.ui_panels()`. The frontend's `<PluginPanelSlot slot="account.extensions" />` mounts whatever's registered.
- `api.ts` exports `useBrowserApi()` — plugin-local WS RPC bindings using `rpc()` from `useWebSocket`. Core's `useWsApi` has no browser entries.
- WS RPC payloads go directly on the frame (matching `agent.*` convention), not nested under `payload`.

### Tests
- `time.monotonic` patching is a TRAP for the `_reap_once` test in `vnc.py` — patching globally breaks asyncio's event-loop clock. The test backdates `session.last_used` directly instead.
- LoginRunner test fixture must cache locator factory results by selector so `page._locators[sel].press.assert_awaited_with("Enter")` works after the runner resolves the same selector multiple times.
- BrowserContainer tests use subprocess fakes — `asyncio.create_subprocess_exec` is monkeypatched, no real Docker invoked. The readiness probe uses `asyncio.open_connection`, also monkeypatched.

### Provisioning
- `./gilbert.sh doctor --plugin browser [--install]` runs the plugin's declared `runtime_dependencies()` checks. With Docker available the only check is `docker info`. Without Docker, falls back to actually launching a headless Chromium on the host (catches missing binaries AND missing OS shared libs in one check). VNC live login adds `Xvfb` / `x11vnc` / `websockify` checks.
- The doctor's `--install` runs `auto_install_cmd` for the binary fetch (`playwright install chromium chromium-headless-shell`); apt-get installs and Docker installs stay manual (sudo).

## Related
- Spec: `docs/superpowers/specs/2026-05-04-browser-plugin-verification.md`
- Plan: `docs/superpowers/plans/2026-05-04-browser-plugin.md`
- WorkspaceProvider protocol: `src/gilbert/interfaces/workspace.py`
- AISamplingProvider protocol: `src/gilbert/interfaces/ai.py`
- Plugin UI extension framework: `frontend/src/lib/plugin-panels.ts`, `frontend/src/components/PluginPanelSlot.tsx`, `frontend/src/plugins/index.ts`
