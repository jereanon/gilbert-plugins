# Gilbert Plugins

First-party plugins for the [Gilbert](https://github.com/briandilley/gilbert) AI assistant.

This repository is cloned into `std-plugins/` inside a Gilbert checkout (as a git submodule) and each subdirectory here is loaded automatically at Gilbert startup. Every plugin is **self-contained** — it declares its own Python dependencies in its own `pyproject.toml`, registers its backends or services when loaded, and can be enabled, disabled, and configured entirely from the Gilbert Settings UI without editing any files.

## How to use this repository

You don't normally interact with this repo directly. Gilbert's `gilbert.sh start` runs `git submodule update --init --recursive` if the `std-plugins/` directory is empty, then `uv sync` — which walks every plugin's `pyproject.toml`, installs its third-party deps into Gilbert's shared venv, and leaves the plugin ready to load.

To hack on a plugin:

```bash
cd std-plugins/<plugin-name>
# edit files, run tests from the gilbert repo root
cd ../..
uv run pytest std-plugins/<plugin-name>/tests/ -v
```

To add a new plugin, see the [Adding a Plugin](#adding-a-plugin) section below.

## Available plugins

The table below is an index — jump to each plugin's detail section for configuration, slash commands, and notes.

| Plugin | Provides | Third-party deps | Category |
|---|---|---|---|
| [american-standard](#american-standard) | `ThermostatBackend "american-standard"` | `nexia` | Climate |
| [andon-fm](#andon-fm) | `andon_fm` service (AI-hosted internet radio tuner under `/media/andon-fm`) | — (uses `httpx`) | Media |
| [anthropic](#anthropic) | `AIBackend "anthropic"`, `VisionBackend "anthropic"` | `anthropic` | Intelligence |
| [apple-health](#apple-health) | `HealthBackend "apple-health"` | — (pure stdlib) | Health |
| [arr](#arr) | `radarr` service, `sonarr` service | — (uses `httpx`) | Media |
| [bedrock](#bedrock) | `AIBackend "bedrock"` | `boto3` | Intelligence |
| [browser](#browser) | `browser` service (headless Chrome tools, credential manager, VNC live login) | `playwright`, `cryptography` | Automation |
| [code-conduit](#code-conduit) | `code_conduit` service (`code_send` AI tool, `/code send` slash command) — relay to OpenCode (`opencode serve`); Claude Code planned | — (uses `httpx`) | Developer tools |
| [deepgram](#deepgram) | `StreamingTranscriptionBackend "deepgram"` | — (uses `websockets`) | Speech |
| [deepseek](#deepseek) | `AIBackend "deepseek"` | — (uses `httpx`) | Intelligence |
| [discord-webhook](#discord-webhook) | `PushNotificationBackend "discord-webhook"` | — (uses `httpx`) | Notifications |
| [elevenlabs](#elevenlabs) | `TTSBackend "elevenlabs"`, `BatchTranscriptionBackend "elevenlabs_scribe"`, `StreamingTranscriptionBackend "elevenlabs_scribe_live"` | — (uses `httpx`, `websockets`) | Media / Speech |
| [frigate](#frigate) | `CameraEventBackend "frigate"` | `aiomqtt` | Monitoring |
| [gemini](#gemini) | `AIBackend "gemini"` | — (uses `httpx`) | Intelligence |
| [google](#google) | `AuthBackend "google"`, `UserProviderBackend "google_directory"`, `EmailBackend "gmail"`, `DocumentBackend "google_drive"`, `CalendarBackend "google_calendar"`, `TaskBackend "google_tasks"` | `google-auth`, `google-api-python-client`, `tzdata` | Identity / Communication / Knowledge / Productivity |
| [groq](#groq) | `AIBackend "groq"`, `BatchTranscriptionBackend "groq_whisper"` | — (uses `httpx`) | Intelligence / Speech |
| [guess-that-song](#guess-that-song) | `guess_game` service | — (pure stdlib) | Games |
| [hk-webhook](#hk-webhook) | `HealthBackend "hk-webhook"` | — (pure stdlib) | Health |
| [jellyfin](#jellyfin) | `MediaLibraryBackend "jellyfin"` | — (uses `httpx`) | Media |
| [kokoro](#kokoro) | `TTSBackend "kokoro"` | `kokoro`, `torch`, `av`, `numpy` | Speech |
| [lutron-radiora](#lutron-radiora) | `LightsBackend "lutron-radiora"`, `ShadesBackend "lutron-radiora"` | `pylutron` | Lighting |
| [messaging](#messaging) | `messaging` service (`MessagingService` + `send_text_message` AI tool, `/messages` SPA page) — RCS / MMS / SMS, RCS by default | — (pure stdlib) | Communication |
| [mistral](#mistral) | `AIBackend "mistral"` | — (uses `httpx`) | Intelligence |
| [ngrok](#ngrok) | `TunnelBackend "ngrok"` | `pyngrok` | Infrastructure |
| [ntfy](#ntfy) | `PushNotificationBackend "ntfy"` | — (uses `httpx`) | Notifications |
| [ollama](#ollama) | `AIBackend "ollama"` | — (uses `httpx`) | Intelligence |
| [open-meteo](#open-meteo) | `WeatherBackend "open-meteo"` | — (uses `httpx`) | Intelligence |
| [openai](#openai) | `AIBackend "openai"`, `BatchTranscriptionBackend "openai_whisper"` | — (uses `httpx`) | Intelligence / Speech |
| [openai-compatible](#openai-compatible) | `AIBackend "openai_compatible"` | — (uses `httpx`) | Intelligence |
| [openrouter](#openrouter) | `AIBackend "openrouter"` | — (uses `httpx`) | Intelligence |
| [openwakeword](#openwakeword) | `WakeWordBackend "openwakeword"` | `openwakeword` | Speech |
| [phone](#phone) | `phone_calls` service (`PhoneCallService` + `make_phone_call` AI tool, `/calls` SPA page) | — (pure stdlib) | Telephony |
| [plex](#plex) | `MediaLibraryBackend "plex"` | `plexapi`, `httpx` | Media |
| [porcupine](#porcupine) | `WakeWordBackend "porcupine"` | `pvporcupine` | Speech |
| [pushover](#pushover) | `PushNotificationBackend "pushover"` | — (uses `httpx`) | Notifications |
| [qwen](#qwen) | `AIBackend "qwen"` | — (uses `httpx`) | Intelligence |
| [slack](#slack) | `slack` service (Socket Mode bot) | `slack-bolt` | Communication |
| [sonos](#sonos) | `SpeakerBackend "sonos"`, `MusicBackend "sonos"` | `aiosonos`, `zeroconf` | Media |
| [tavily](#tavily) | `WebSearchBackend "tavily"` | — (uses `httpx`) | Intelligence |
| [telegram](#telegram) | `PushNotificationBackend "telegram"` | — (uses `httpx`) | Notifications |
| [telnyx](#telnyx) | `TelephonyBackend "telnyx"` (voice), `MessagingBackend "telnyx"` (RCS / MMS / SMS) | — (uses `httpx`, `websockets`) | Telephony / Communication |
| [tesseract](#tesseract) | `OCRBackend "tesseract"` | `pytesseract` | Intelligence |
| [unifi](#unifi) | `PresenceBackend "unifi"`, `DoorbellBackend "unifi"` | — (uses `httpx`/`aiohttp`) | Monitoring |
| [voice-agent](#voice-agent) | `voice_agent` service (wake-word-activated voice conversation, `/voice` SPA page) | — (pure stdlib) | Speech |
| [withings](#withings) | `HealthBackend "withings"` | `httpx` | Health |
| [xai](#xai) | `AIBackend "xai"` | — (uses `httpx`) | Intelligence |

---

### american-standard

American Standard / Trane / Nexia / Asair thermostat integration via the [Nexia cloud](https://www.mynexia.com/). Speaks Nexia's HTTPS API through the [`nexia`](https://pypi.org/project/nexia/) async library (the same one Home Assistant uses). Each *zone* on the account is exposed as a Gilbert thermostat — multi-zone HVAC systems show up as one entity per zone with the gateway name as the area.

**Backend registered**
- `ThermostatBackend.backend_name = "american-standard"` — `supports_cooling = True`, `supports_heating = True`, `supports_fan_mode = True`, `supports_humidity = True`. Mode set covers `off`, `heat`, `cool`, `auto`; fan modes are pulled dynamically from each thermostat's reported labels (typically `auto`, `on`, `circulate`).

**Slash commands** — provided by the core `thermostats` service, not by this plugin directly. All thermostat commands live under the `/climate` namespace. With this backend selected:
- `/climate list`, `/climate status <name|area>`
- `/climate mode <name|area> <off|heat|cool|auto>`
- `/climate heat <name|area> <temp>`, `/climate cool <name|area> <temp>`
- `/climate range <name|area> <heat> <cool>` (sets the AUTO-mode comfort band)
- `/climate fan <name|area> <auto|on|circulate>`

Names match either a zone name (e.g. *Upstairs*) or the gateway / thermostat name (e.g. *Main HVAC*, which addresses every zone on that gateway).

**Configure** (Settings → Climate → Thermostats, with the `american-standard` backend selected)
- `username` — Account email used to log in to the Nexia / American Standard / Trane / Asair app.
- `password` *(sensitive)* — Account password.
- `brand` — `nexia` for Nexia / Trane / American Standard accounts; `asair` for Asair-branded accounts. Default `nexia`.

The plugin persists Nexia's per-account device UUID under `.gilbert/plugin-data/american-standard/nexia-state-<username>.json` so reconnecting after a restart doesn't re-register as a new device (which would eventually trip Nexia's account-lockout protection).

**Config action** — `test_connection`: logs in with a fresh, short-lived `aiohttp.ClientSession` and reports the discovered thermostat + zone counts.

**Third-party deps** — `nexia>=2.7.0`.

---

### andon-fm

Tune in to the four AI-hosted internet radio stations from [Andon Labs](https://andonlabs.com/radio): **Thinking Frequencies** (Claude), **OpenAIR** (GPT), **Backlink Broadcast** (Gemini), and **Grok and Roll** (Grok). Each station is a long-running agent autonomously DJing through the day — picking tracks, writing show blocks, posting on X. The plugin hands the Live365 MP3 stream URLs to Gilbert's existing speaker service, so you can listen on Sonos, the host's speakers, or a browser tab. The tuner is a full page under the **Media** nav group; pressing Play opens a dialog that lets you pick which speakers (and the volume) for that play, instead of always falling back to the configured defaults.

**Service registered**
- `andon_fm` — `Configurable` + `ToolProvider` + `WsHandlerProvider`. Resolves `speaker_control` (required), and optionally `scheduler` (for the now-playing scraper) and `event_bus` (for live UI updates).

**Slash commands** (namespace `/radio.*`)
- `/radio.list` — list the four stations with current programming block and listener count.
- `/radio.play <station> [speakers]` — tune in. `<station>` matches name, host (Claude/GPT/Gemini/Grok), substring, or UUID; `[speakers]` defaults to `default_target_speakers` (typically the caller's browser tab).
- `/radio.stop [speakers]` — stop Andon FM playback.
- `/radio.now [station]` — show the current programming block for one station or all four.

**Tuner page** — `UIRoute` at `/media/andon-fm`, slotted under the **Media** nav group as `andon_fm.page`. Renders one card per station with cover art, AI host chip, current block, listener count, and a Play button that opens a speaker-picker dialog (checkbox list of every discovered speaker + the `my browser` magic alias + a volume slider). Block changes stream in live via `andon_fm.now_playing.changed` events — no polling.

**WebSocket RPCs**
- `andon_fm.stations.list` / `andon_fm.now_playing.get` — catalog + cache snapshot.
- `andon_fm.speakers.list` — every discovered speaker (with backend + model + group), prefixed by the `my browser` virtual entry, for the picker dialog.
- `andon_fm.play` / `andon_fm.stop` — wrap the speaker service's play / stop with the station's stream URL.

The plugin is **toggleable** — disabled by default. Enable it under **Settings → Services → "Andon FM"** before the `/media/andon-fm` nav entry, the slash commands, and the WS RPCs come online.

**Configure** (Settings → Media → Andon FM, once enabled)
- `default_target_speakers` — speakers pre-selected in the picker dialog. Default `["my browser"]` (the caller's tab). Multi-select dropdown sourced from the active speaker list. Slash-command callers (`/radio.play <station>` with no speaker) also use this list.
- `default_volume` — default volume in the picker dialog and for slash-command callers. 0-100, default `60`.
- `scraper_enabled` *(restart required)* — fetch each station's current programming block + listener count from `andonlabs.com/radio`. Default `true`. Disable if you only want playback (no metadata).
- `scrape_interval_seconds` *(restart required)* — refresh interval. Default `90`.

**Stations** — bundled in `stations.py`. The four UUIDs / stream URLs are pulled from the public Andon FM web player; edit that file if Andon Labs renumbers them.

**Third-party deps** — none (uses `httpx` from Gilbert core).

---

### anthropic

Claude-powered AI chat and vision backends, speaking the Anthropic Messages API directly over `httpx` (no SDK import for the chat backend; the vision backend lazily imports `anthropic` for its one helper call).

**Backends registered**
- `AIBackend.backend_name = "anthropic"` — tool-use capable, streaming, per-call model override.
- `VisionBackend.backend_name = "anthropic"` — image understanding via Claude's vision API.

**Configure** (Settings → AI and Settings → Vision)
- `enabled` — Initialize this backend at startup (default `true`). Uncheck to hide its settings and stop it being offered in profile dropdowns.
- `api_key` *(sensitive)* — Anthropic API key (`sk-ant-…`).
- `model` — Default Claude model ID used when a request specifies no per-call model (default `claude-sonnet-4-20250514` for chat, `claude-sonnet-4-5-20250929` for vision).
- `enabled_models` — Subset of advertised models that the chat UI and AI profile editor expose for selection. Defaults to every model the backend knows about.
- `max_tokens` — Per-response cap (default `16384`). Sonnet/Opus 4.x comfortably support higher; the AIService recovers from a `max_tokens` cut-off on a text-only response via bounded continuation, but a `tool_use` that gets truncated mid-JSON is unrecoverable, so keep this comfortably above the largest tool input you expect.
- `temperature` — Sampling temperature (chat only).

**Streaming.** The chat backend implements `generate_stream` over SSE — `AIService` forwards each text chunk as a `chat.stream.text_delta` event on the bus, plus `chat.stream.round_complete` after every AI round and `chat.stream.turn_complete` at the end. The WS layer delivers them to the conversation's audience (owner for personal chats, members for shared rooms). The frontend's `TurnBubble` builds a live "thinking card" inside the in-flight turn from those events plus `chat.tool.started` / `chat.tool.completed`, and commits to the authoritative round structure when the `chat.message.send` RPC resolves with the server's `rounds` field. All Anthropic-specific SSE parsing stays inside `anthropic_ai.py`; `capabilities()` reports `streaming=True, attachments_user=True`.

**Config action** — `test_connection`: issues a one-token completion to verify credentials.

---

### apple-health

Push-style ingestion of Apple HealthKit data via an iOS Shortcut. Translates HealthKit identifier names (e.g. `HKQuantityTypeIdentifierStepCount`) to Gilbert's `MetricType` enum via a fixed mapping table; identifiers without a match drop with an INFO log (so adding support for a new metric is a one-line table edit).

**Backend registered**
- `HealthBackend.backend_name = "apple-health"` — `supports_push = True`, `supports_pull = False`. Per spec §4.5 the `extra` whitelist allows exactly two keys: `device` (HKDevice.name) and `source_app` (HKSource.name). Every other key in the payload's `extra` dict is silently stripped before storage.

**Slash commands** — provided by the core `health` service, not by this plugin directly. See the [`/health` slash family](#health-slash-commands).

**Configure** — none. Apple Health is push-only: per-user state lives entirely on the `health_links` row written by the per-user `Generate / rotate webhook URL` button in the account panel.

**Frontend panel** (`account.extensions` slot)
- Failure-mode disclosure (iOS Background App Refresh + lock-state realities) above the install button so users know what they're signing up for.
- "Install our Shortcut" link + SHA-256 hash of the bundled iCloud Shortcut for supply-chain verification (paranoid users compare; the placeholder hash is populated on each Shortcut release).
- Webhook URL display on rotation — raw token shown ONCE; only its SHA-256 hash is persisted.
- Last-delivery indicator so a silently-broken automation is visible.
- Manual setup fallback for users who can't / won't use the prebuilt Shortcut.

**Third-party deps** — none (pure stdlib JSON parsing).

---

### arr

Radarr + Sonarr integration for browsing, searching, and managing your movie and TV library from Gilbert chat. Registered as two services (`radarr`, `sonarr`) so you can run either independently.

**Slash commands** (both services use the same verbs, prefixed `/radarr` or `/sonarr`)
- `list`, `find`, `search`, `details`, `grab`, `add`, `remove`
- `profiles`, `queue`, `recent`, `upcoming`
- `episodes` *(sonarr only)*

**Configure** (Settings → Media → Radarr / Sonarr)
- `url` — Radarr/Sonarr base URL (e.g., `http://radarr.lan:7878`).
- `api_key` *(sensitive)* — instance API key.
- `default_quality_profile` — Quality profile name or ID to use when adding new items.
- `default_root_folder` — Root folder path for new downloads.

**Requires**: nothing on the Gilbert side beyond `httpx`, which is already a core dep.

---

### bedrock

AWS Bedrock chat backend — unlike every other AI plugin this one doesn't speak an OpenAI-compatible API. Bedrock's [Converse API](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html) gives us a unified request shape across Anthropic Claude, Meta Llama, Mistral, and Amazon Nova models, with AWS SigV4 authentication. Useful for installations that already run on AWS and want their model traffic to stay in-VPC / billed through AWS.

**Backend registered** — `AIBackend.backend_name = "bedrock"`: tool-use capable, streaming via `converse_stream`, image-input capable on vision-capable models (Claude, Nova), per-call model override.

**Configure** (Settings → Intelligence → AI, with the `bedrock` backend selected)
- `enabled` — Initialize this backend at startup (default `true`).
- `aws_region` — AWS region for the Bedrock runtime endpoint (default `us-east-1`). Cross-region inference-profile IDs (`us.` / `eu.` prefixed) route automatically within the partition.
- `aws_access_key_id` — Optional. Leave blank to use boto3's default credential chain (env vars, `~/.aws/credentials`, EC2/ECS/Lambda IAM role).
- `aws_secret_access_key` *(sensitive)* — Optional. Paired with the access key.
- `aws_session_token` *(sensitive)* — Optional. For temporary credentials (STS AssumeRole, SSO).
- `model` — Default Bedrock model ID or inference profile ID (default `us.anthropic.claude-sonnet-4-5-20250929-v1:0`). Free-text because the available catalog varies per account and region — paste any model ID from the Bedrock console.
- `enabled_models` — Suggested subset shown in the chat UI and AI profile editor. Ships with common Claude / Llama / Mistral / Nova IDs.
- `max_tokens` — Per-response cap (default `8192`). Sent as `inferenceConfig.maxTokens`.
- `temperature` — Sampling temperature (default `0.7`).

**Streaming.** The backend drives `converse_stream`'s blocking iterator in a background thread and forwards events onto an `asyncio.Queue`. The main coroutine consumes the queue and maps `contentBlockStart` / `contentBlockDelta` / `contentBlockStop` / `messageStop` / `metadata` events to neutral `StreamEvent`s — `TEXT_DELTA`, `TOOL_CALL_START`, `TOOL_CALL_DELTA`, `TOOL_CALL_END`, and finally `MESSAGE_COMPLETE` with the assembled `AIResponse`.

**Attachments.** Vision-capable Bedrock models (Claude, Nova) accept image content blocks with raw bytes (not base64 strings — the plugin decodes). Supported formats: `png`, `jpeg`, `gif`, `webp`. Documents and text attachments become text stubs pointing the model at the workspace tools.

**Config action** — `test_connection`: issues a one-word completion to verify credentials and region.

**Third-party deps**: `boto3` (for AWS SigV4 signing, credential resolution, and the Converse / ConverseStream APIs).

---

### browser

Per-user headless Chrome for AI tools — agents can navigate, scrape text/HTML, click, fill forms, take screenshots that render inline in chat, and (optionally) extract structured JSON via an internal AI sampling call. Includes a per-user encrypted credential manager so the agent can log into sites without the password ever touching an AI prompt, plus a VNC live-login flow for sites whose login flow doesn't fit a CSS-selector form fill.

The plugin is **toggleable** — disabled by default. Enable it under Settings → Services → "Browser plugin" before tools or credentials become active.

**Provides**: a single `browser` service with `ToolProvider` + `WsHandlerProvider` + `Configurable`.

**Tools** (visible to the AI under the active profile):

- Read-only: `browser_navigate`, `browser_get_text`, `browser_get_html`, `browser_screenshot` — `browser_screenshot` returns a workspace-reference `FileAttachment(kind="image")` so the PNG renders inline in the agent's reply.
- Interaction: `browser_click`, `browser_fill`, `browser_press`, `browser_select` — all share the same per-user `Page`, so they serialize automatically.
- Login: `browser_login(credential_id)` — resolves a saved credential server-side and runs the form-fill heuristic. Username/password never appear in tool arguments.
- AI-assisted: `browser_extract(instruction, json_schema?)` — only advertised when the `ai_chat` capability is wired in.

**Architecture**:

- **Browser engine** runs in a Microsoft-maintained `mcr.microsoft.com/playwright:v<X.Y.Z>-jammy` Docker container by default — all OS shared libs are baked in, the host stays clean. One shared container hosts every user's `BrowserContext`. Falls back to host-native Playwright when Docker isn't available. Mode is configurable: `auto` (default) / `docker` / `host`. Resource budget: ~150 MB baseline + ~50-100 MB per active user; default cap of 8 concurrent users → ~750 MB worst-case.
- **Credential store** is keyed strictly by user id — there are no global credentials. WS handlers enforce ownership server-side, so each user only sees and manages their own. Passwords are sealed with a Fernet key auto-generated at `.gilbert/plugin-data/browser/fernet.key` (mode 0600); the `list` endpoint never returns passwords (only the per-id resolution path inside `browser_login` decrypts).
- **Credentials UI** mounts via the generic plugin UI extension framework (see "Plugin UI extensions" in `CLAUDE.md`) into the **per-user Account page** at `/account` → "Browser logins". The plugin declares a `UIPanel(panel_id="browser.credentials", slot="account.extensions", required_role="user")`; the SPA renders it without any core-side knowledge of the plugin.
- **VNC live login**: per-row "Log in interactively" button opens a modal hosting a noVNC iframe pointed at a server-side headed Chromium (under host-native `Xvfb` + `x11vnc` + `websockify`). On close, the headed `storage_state` is merged into the user's persistent headless state.

**Configure** (Settings → Browser):

| Key | Default | Notes |
|---|---|---|
| `mode` | `auto` | `auto` (prefer Docker), `docker` (require), or `host` (force host-native). |
| `docker_image` | (auto) | Override the Docker image. Blank → `mcr.microsoft.com/playwright:v<installed-playwright-version>-jammy`. |
| `idle_timeout_seconds` | 600 | Close per-user contexts after this many idle seconds. |
| `max_concurrent_users` | 8 | Server-wide cap on simultaneous BrowserContexts. |
| `vnc_idle_timeout_seconds` | 900 | Close idle VNC sessions. |
| `vnc_max_concurrent_per_user` | 2 | Per-user VNC cap. |
| `vnc_max_concurrent_total` | 5 | Server-wide VNC cap. |
| `extraction_prompt` | (built-in) | System prompt for `browser_extract`. AI-prompt field. |
| `login_heuristics_prompt` | (built-in) | System prompt for AI-assisted login form detection. AI-prompt field. |

**Third-party deps**: `playwright>=1.45`, `cryptography>=42` (both pulled in automatically by `uv sync`).

**Provisioning**:

```bash
./gilbert.sh doctor --plugin browser            # see what's missing
./gilbert.sh doctor --plugin browser --install  # auto-install where possible
```

The doctor reads `Plugin.runtime_dependencies()` (see `CLAUDE.md`). With Docker available the only check is `docker info`. Without Docker, it falls back to actually launching a headless Chromium on the host and points at `playwright install chromium chromium-headless-shell` plus the OS-libs hint at <https://playwright.dev/python/docs/browsers#install-system-dependencies>. VNC live login additionally needs `xvfb x11vnc websockify` on PATH (apt-get installs).

**RBAC**: All `browser_*` tools default to user level. WS RPCs (`browser.credentials.*`, `browser.vnc.*`) are user-level with per-user ownership enforced inside the handlers. The `/api/browser/vnc/{session_id}/ws` proxy validates session ownership against the calling `UserContext` before bridging to localhost websockify.

---

### code-conduit

Gilbert as a *conduit* between the user (typically over voice via the Mentra glasses or the chat UI) and a coding agent the user runs on their own machine. The plugin never edits code, never reviews diffs, and never makes editorial judgments — it just relays messages verbatim into a session managed by the coding agent (currently [OpenCode](https://github.com/sst/opencode); Claude Code planned for Phase 3) and exposes a tool so the LLM can fire that relay mid-conversation when the user says *"tell Claude to ..."* or *"have OpenCode write tests for ..."*.

**Service registered**
- `CodeConduitService` — capabilities `code_conduit`, `ai_tools`. Wraps a `CodingAgentBackend` (currently only `OpenCodeBackend`) and implements the `CodingConduitProvider` capability protocol so other services can drop messages into the agent without depending on the concrete class.

**Slash commands** (under the `code` namespace)
- `/code send <message> [project=<alias>] [new_session=true|false]` — relay `message` to the active coding agent on the named project (or the operator-configured default). Returns immediately with a `"sent"` acknowledgment; the agent's eventual response is NOT awaited (lands via the Phase-2 inbound channel).

**AI tool**
- `code_send(message, project?, new_session?)` — what the LLM fires when the user says *"tell Claude to ..."* / *"ask the coding agent ..."*. The system prompt makes clear: pass the user's message through verbatim, do not paraphrase. Returns a short voice-friendly confirmation ("Sent to opencode on the gilbert project.").

**Configure** (Settings → Integrations → Code Conduit)
- `enabled` — required, off by default. Service toggle exposed under Settings → Services.
- `backend` *(restart_required)* — `opencode` (the only shipped backend today).
- `default_project_alias` — alias to use when the user doesn't name a project. Must match a key in `project_aliases`.
- `project_aliases` *(multiline)* — newline-separated `alias=/abs/path` pairs. Lets the user say "the gilbert project" instead of pasting a full path. Lines starting with `#` and blanks are ignored.
- `settings.server_url` *(restart_required)* — URL of the user's `opencode serve` daemon. Defaults to `http://127.0.0.1:4096`. For a typical setup (Gilbert on a server, OpenCode on the user's laptop), run `opencode serve --hostname <tailnet-ip>` on the laptop and put the resulting Tailscale URL here.
- `settings.server_password` *(sensitive, restart_required)* — password configured via `OPENCODE_SERVER_PASSWORD` when starting `opencode serve`. HTTP Basic auth with user `opencode`.
- `settings.timeout_seconds` *(restart_required)* — HTTP timeout for OpenCode round-trips. Default 10s; doesn't gate the agent's response (that's fire-and-forget) — only the round-trip to the daemon itself.

**Config action** — `test_connection`: lists one session via the configured backend. Confirms the URL is reachable, the password is correct, and the API is responding without sending a real prompt.

**Operator setup for OpenCode**
1. On the laptop where dev work happens, install OpenCode and start the daemon bound to the Tailscale interface:
   ```
   OPENCODE_SERVER_PASSWORD=<set-a-strong-one> opencode serve --hostname <tailnet-ip>
   ```
2. In Gilbert's Settings → Integrations → Code Conduit, paste `http://<tailnet-ip>:4096` and the password.
3. Click *Test connection*. Once it returns green, the LLM tool `code_send` is live — try *"tell Claude to add error handling to the new endpoint"* via chat or voice.

**Scope notes** — Phase 1 (this release) is outbound only. Inbound notifications (the agent finishes a task, errors, or asks a clarifying question → Gilbert surfaces it via TTS / push) ship in Phase 2 via an SSE consumer + event bus fan-out. Claude Code support (webhook-based inbound, queue-file or `claude -p --resume` outbound) lands in Phase 3.

---

### deepgram

Real-time streaming speech-to-text via the [Deepgram Nova API](https://developers.deepgram.com/). Uses raw `websockets` rather than the `deepgram-sdk` package — fewer deps and the WebSocket protocol is straightforward. Audio is sent as binary frames (PCM16LE, 16 kHz mono by default); an empty binary frame signals end-of-stream.

**Backend registered** — `StreamingTranscriptionBackend.backend_name = "deepgram"`.

**Account setup** — Create an account at https://console.deepgram.com and generate an API key. Free tier includes generous transcription minutes.

**Configure** (Settings → Transcription → Streaming, with the `deepgram` backend selected)
- `api_key` *(sensitive)* — Deepgram API key.
- `model` — Deepgram model ID (default `nova-3`). Choices: `nova-3`, `nova-2`, `enhanced`, `base`.
- `ws_url` — WebSocket URL (default `wss://api.deepgram.com/v1/listen`).

**No third-party Python dependencies** — uses `websockets`, which is already a core Gilbert dep.

---

### deepseek

DeepSeek chat backend, speaking the [OpenAI-compatible DeepSeek API](https://api-docs.deepseek.com/) directly over `httpx`. Runs alongside the other AI backends — pick per-profile in the AI profile editor.

**Backend registered** — `AIBackend.backend_name = "deepseek"`: tool-use capable, streaming, per-call model override.

**Configure** (Settings → Intelligence → AI, with the `deepseek` backend selected)
- `enabled` — Initialize this backend at startup (default `true`).
- `api_key` *(sensitive)* — DeepSeek API key (`sk-…`).
- `base_url` — API base URL (default `https://api.deepseek.com/v1`).
- `model` — Default model ID (default `deepseek-chat`). Choices: `deepseek-chat` (DeepSeek V3), `deepseek-reasoner` (DeepSeek R1).
- `enabled_models` — Subset of advertised models that the chat UI and AI profile editor expose for selection.
- `max_tokens` — Per-response cap (default `8192`).
- `temperature` — Sampling temperature (default `0.7`).

**Streaming.** OpenAI-compatible SSE — `delta.content` → `TEXT_DELTA`, streamed `tool_calls[i].function.arguments` deltas reassembled into complete `ToolCall`s. `capabilities()` reports `streaming=True, attachments_user=True`.

**Attachments.** DeepSeek's current chat models don't accept native image attachments, so every attachment becomes a text stub pointing the model at the workspace tools (`read_workspace_file`, `run_workspace_script`). Text attachments are inlined as `## <name>\n\n<body>`.

**Config action** — `test_connection`: issues a one-word completion to verify credentials.

---

### discord-webhook

Discord channel-webhook delivery for the push-notification fan-out
service. No shared admin secret is required — the secret is each user's
per-route webhook URL (created from the channel's *Edit channel →
Integrations → Create webhook* menu).

**Backend registered** — `PushNotificationBackend.backend_name = "discord-webhook"`.

**Per-user destination fields** (set on `/account/notifications`)
- `webhook_url` *(sensitive)* — full Discord webhook URL. Validated on
  send and on the `test_connection` action against the official
  `discord.com` / `discordapp.com` prefixes — anything else is rejected
  before any HTTP call to prevent SSRF probes.
- `mention` — optional mention prefix (e.g. `@here`, `<@USER_ID>`)
  prepended on URGENT messages only.

**Admin config** (Settings → Notifications → Backend: discord-webhook)
- `timeout` — HTTP timeout in seconds (default 10).
- `username_override` — webhook display name (default `"Gilbert"`).

**Config action** — `test_connection`: pings an arbitrary
`webhook_url` from the action payload with `flags=4096`
(SUPPRESS_NOTIFICATIONS) so members aren't pinged. The same flag is
applied to per-route "Send test" deliveries triggered from the SPA.

**Rate-limit handling** — 429s parse `X-RateLimit-Reset-After` into
`PushDeliveryResult.retry_after_s`; the service uses that value
(capped at 60s) instead of the configured backoff for the next
attempt.

**No third-party Python dependencies** — uses core's `httpx`.

---

### elevenlabs

High-quality text-to-speech via the ElevenLabs API, plus batch and streaming speech-to-text via the ElevenLabs Scribe API. Used by the core `speaker.announce` flow, doorbell greetings, and anything else that calls `TTSBackend.synthesize()`.

**Backends registered**
- `TTSBackend.backend_name = "elevenlabs"` — synthesizes speech from text. Also implements `StreamingTTSCapability` (chunked output via the HTTP `/stream` endpoint) and `BidirectionalTTSCapability` (push-text / read-audio sessions via the stream-input WebSocket).
- `BatchTranscriptionBackend.backend_name = "elevenlabs_scribe"` — one-shot transcription via `POST /v1/speech-to-text`. Supports diarization.
- `StreamingTranscriptionBackend.backend_name = "elevenlabs_scribe_live"` — real-time transcription via the Scribe WebSocket endpoint.

**Configure** (Settings → TTS, when the `elevenlabs` backend is selected)
- `api_key` *(sensitive)* — ElevenLabs API key.
- `voice_id` — Voice ID to synthesize with (copy from the ElevenLabs voice library).
- `model_id` — ElevenLabs model ID (default `eleven_turbo_v2_5`).
- `cache_max_entries` — LRU cache capacity for recently synthesized phrases (default 256).
- `cache_ttl_seconds` — How long a cached clip lives before re-synthesis (default 1800).

**Configure** (Settings → Transcription → Batch, with the `elevenlabs_scribe` backend selected)

The `elevenlabs_scribe` key is **separate** from the TTS backend's key — each backend has its own config block under `transcription.<role>.backends.elevenlabs_scribe.settings.*`.
- `api_key` *(sensitive)* — ElevenLabs API key.
- `model` — Scribe model ID (default `scribe_v1`).
- `base_url` — API base URL (default `https://api.elevenlabs.io`).

**Configure** (Settings → Transcription → Streaming, with the `elevenlabs_scribe_live` backend selected)

The `elevenlabs_scribe_live` key is also **separate** from both the TTS and batch backends.
- `api_key` *(sensitive)* — ElevenLabs API key.
- `model` — Scribe model ID (default `scribe_v1`).
- `ws_url` — WebSocket URL for the Scribe live endpoint (default `wss://api.elevenlabs.io/v1/speech-to-text/stream`).

**Config action** — `test_connection`: requests the available voices list to verify the API key.

**No third-party Python dependencies** — talks directly to the REST API and WebSocket via `httpx` and `websockets` (both already core Gilbert deps).

---

### frigate

[Frigate](https://frigate.video/) NVR object-detection events via MQTT (push), plus snapshot/clip retrieval over HTTP. Subscribes to Frigate's `<prefix>/events` and `<prefix>/available` topics; the camera service consumes the stream, persists rows into the `camera_events` collection (configurable retention), and republishes onto the bus as `camera.event.detected` / `camera.event.ended` / `camera.<label>.detected.<camera>` (glob-friendly, ACTIVE only).

**Backend registered** — `CameraEventBackend.backend_name = "frigate"`. Streaming-style backend (`connect / disconnect / stream_events` on top of the standard `initialize / close`); the camera service owns the reconnect supervisor and re-invokes `connect()` on transport error.

**Slash commands** — provided by the core `cameras` service:
- `/cameras list`, `/cameras clips`, `/cameras seen`, `/cameras count`
- `/cameras mute` (on the greeting service — UIBlock confirm before persisting)

**Configure** (Settings → Monitoring → Cameras, with the `frigate` backend selected)
- `mqtt_host`, `mqtt_port` *(restart)* — Broker hostname / port. Frigate's bundled Mosquitto on `1883` is the most common deploy.
- `mqtt_topic_prefix` *(restart)* — Frigate's `mqtt.topic_prefix` (default `frigate`).
- `mqtt_username`, `mqtt_password` *(sensitive)* — Optional broker credentials.
- `mqtt_client_id` — MQTT client id (default `gilbert-cameras`).
- `mqtt_tls` — Enable TLS for the broker connection.
- `mqtt_tls_ca_cert`, `mqtt_tls_client_cert` *(sensitive)*, `mqtt_tls_client_key` *(sensitive)* — PEM material for self-signed brokers and mTLS.
- `mqtt_tls_insecure` — Skip TLS hostname / cert verification (DISABLES MITM PROTECTION — only for self-signed brokers where you don't want to ship the CA).
- `mqtt_tls_server_hostname` — SNI / cert-CN override.
- `http_base_url` *(restart)* — Frigate web base URL (e.g. `http://frigate.local:5000`). Used for snapshot / clip / camera-config probes.
- `http_auth_mode` — `none` (LAN deploy) or `bearer` (Frigate API keys / proxy).
- `http_token` *(sensitive)* — Bearer token; ignored when `http_auth_mode=none`.
- `verify_ssl` — Verify Frigate's TLS cert (default true).
- `cameras_filter` — Restrict to a subset of cameras the broker reports.

**MQTT broker onboarding hint.** If you don't already have a broker, point this at Frigate's bundled Mosquitto — it's the same broker Frigate publishes its own events to. Frigate's `config.yml` `mqtt:` block configures both ports and credentials; copy them into Gilbert's settings.

**Config action** — `test_connection`: probes Frigate's `/api/version`, attempts a 5-second MQTT connect+subscribe to `<prefix>/+/events`, and warns when the broker reports a Frigate version older than the supported 0.13.0 minimum.

**Single-layer reconnect** — the plugin opens **one** `aiomqtt.Client` per `connect()` call. Any `MqttError` exits the inner client and raises `CameraBackendError`; the camera service catches it, sleeps with exponential backoff (capped at `reconnect_max_seconds`), and calls `connect()` again. The plugin doesn't loop internally so there's only one place backoff semantics live.

**Frigate LWT translation.** When `<prefix>/available` flips to `offline` (Frigate-the-detector down even though the broker is healthy), the plugin signals the consumer which raises `CameraBackendError("frigate offline")`; the service publishes `camera.backend.disconnected` and re-attempts. When the LWT flips back to `online`, the next reconnect succeeds and `camera.backend.connected` fires.

**Defensive payload parsing** — every field read uses `.get()` with a default; `sub_label` accepts string / `[name, score]` list / null / missing forms; missing required fields drop the event with a debug-level log; `false_positive=true` drops the event entirely; invalid JSON payloads are logged at WARNING and dropped (the consumer never crashes on a malformed payload).

**Audio events** flow through transparently — Frigate 0.13+ emits `bark`, `glass_break`, `speech`, etc. on cameras with `audio.enabled=true`. They have `has_snapshot=false` so vision annotation short-circuits naturally; the greeting service announces them when their label is added to `announce_camera_labels`.

**Third-party deps** — `aiomqtt>=2.3.0,<3.0.0` (asyncio-native; v2-only because v1→v2 was a breaking API change and v3 hasn't shipped). HTTP via `httpx` (already a Gilbert core dep).

**SPA contributions** — the plugin owns its UI under `frigate/frontend/`:
- `frigate.cameras_page` — full `/cameras` SPA route declared via `Plugin.ui_routes()`. Per-camera grid, recent-events feed, mute editor.
- `frigate.recent_events` — dashboard card mounted into the `dashboard.bottom` slot via `Plugin.ui_panels()`. Subscribes to `camera.event.detected` for live updates.

Core never imports from `frigate/frontend/`; the `<PluginPanelSlot>` / `<PluginRoutes>` extension points + the per-plugin `panels.ts` side-effect file wire the components in via `panel_id`.

---

### frigate

[Frigate](https://frigate.video/) NVR object-detection events via MQTT (push), plus snapshot/clip retrieval over HTTP. Subscribes to Frigate's `<prefix>/events` and `<prefix>/available` topics; the camera service consumes the stream, persists rows into the `camera_events` collection (configurable retention), and republishes onto the bus as `camera.event.detected` / `camera.event.ended` / `camera.<label>.detected.<camera>` (glob-friendly, ACTIVE only).

**Backend registered** — `CameraEventBackend.backend_name = "frigate"`. Streaming-style backend (`connect / disconnect / stream_events` on top of the standard `initialize / close`); the camera service owns the reconnect supervisor and re-invokes `connect()` on transport error.

**Slash commands** — provided by the core `cameras` service:
- `/cameras list`, `/cameras clips`, `/cameras seen`, `/cameras count`
- `/cameras mute` (on the greeting service — UIBlock confirm before persisting)

**Configure** (Settings → Monitoring → Cameras, with the `frigate` backend selected)
- `mqtt_host`, `mqtt_port` *(restart)* — Broker hostname / port. Frigate's bundled Mosquitto on `1883` is the most common deploy.
- `mqtt_topic_prefix` *(restart)* — Frigate's `mqtt.topic_prefix` (default `frigate`).
- `mqtt_username`, `mqtt_password` *(sensitive)* — Optional broker credentials.
- `mqtt_client_id` — MQTT client id (default `gilbert-cameras`).
- `mqtt_tls` — Enable TLS for the broker connection.
- `mqtt_tls_ca_cert`, `mqtt_tls_client_cert` *(sensitive)*, `mqtt_tls_client_key` *(sensitive)* — PEM material for self-signed brokers and mTLS.
- `mqtt_tls_insecure` — Skip TLS hostname / cert verification (DISABLES MITM PROTECTION — only for self-signed brokers where you don't want to ship the CA).
- `mqtt_tls_server_hostname` — SNI / cert-CN override.
- `http_base_url` *(restart)* — Frigate web base URL (e.g. `http://frigate.local:5000`). Used for snapshot / clip / camera-config probes.
- `http_auth_mode` — `none` (LAN deploy) or `bearer` (Frigate API keys / proxy).
- `http_token` *(sensitive)* — Bearer token; ignored when `http_auth_mode=none`.
- `verify_ssl` — Verify Frigate's TLS cert (default true).
- `cameras_filter` — Restrict to a subset of cameras the broker reports.

**MQTT broker onboarding hint.** If you don't already have a broker, point this at Frigate's bundled Mosquitto — it's the same broker Frigate publishes its own events to. Frigate's `config.yml` `mqtt:` block configures both ports and credentials; copy them into Gilbert's settings.

**Config action** — `test_connection`: probes Frigate's `/api/version`, attempts a 5-second MQTT connect+subscribe to `<prefix>/+/events`, and warns when the broker reports a Frigate version older than the supported 0.13.0 minimum.

**Single-layer reconnect** — the plugin opens **one** `aiomqtt.Client` per `connect()` call. Any `MqttError` exits the inner client and raises `CameraBackendError`; the camera service catches it, sleeps with exponential backoff (capped at `reconnect_max_seconds`), and calls `connect()` again. The plugin doesn't loop internally so there's only one place backoff semantics live.

**Frigate LWT translation.** When `<prefix>/available` flips to `offline` (Frigate-the-detector down even though the broker is healthy), the plugin signals the consumer which raises `CameraBackendError("frigate offline")`; the service publishes `camera.backend.disconnected` and re-attempts. When the LWT flips back to `online`, the next reconnect succeeds and `camera.backend.connected` fires.

**Defensive payload parsing** — every field read uses `.get()` with a default; `sub_label` accepts string / `[name, score]` list / null / missing forms; missing required fields drop the event with a debug-level log; `false_positive=true` drops the event entirely; invalid JSON payloads are logged at WARNING and dropped (the consumer never crashes on a malformed payload).

**Audio events** flow through transparently — Frigate 0.13+ emits `bark`, `glass_break`, `speech`, etc. on cameras with `audio.enabled=true`. They have `has_snapshot=false` so vision annotation short-circuits naturally; the greeting service announces them when their label is added to `announce_camera_labels`.

**Third-party deps** — `aiomqtt>=2.3.0,<3.0.0` (asyncio-native; v2-only because v1→v2 was a breaking API change and v3 hasn't shipped). HTTP via `httpx` (already a Gilbert core dep).

**SPA contributions** — the plugin owns its UI under `frigate/frontend/`:
- `frigate.cameras_page` — full `/cameras` SPA route declared via `Plugin.ui_routes()`. Per-camera grid, recent-events feed, mute editor.
- `frigate.recent_events` — dashboard card mounted into the `dashboard.bottom` slot via `Plugin.ui_panels()`. Subscribes to `camera.event.detected` for live updates.

Core never imports from `frigate/frontend/`; the `<PluginPanelSlot>` / `<PluginRoutes>` extension points + the per-plugin `panels.ts` side-effect file wire the components in via `panel_id`.

---

### frigate

[Frigate](https://frigate.video/) NVR object-detection events via MQTT (push), plus snapshot/clip retrieval over HTTP. Subscribes to Frigate's `<prefix>/events` and `<prefix>/available` topics; the camera service consumes the stream, persists rows into the `camera_events` collection (configurable retention), and republishes onto the bus as `camera.event.detected` / `camera.event.ended` / `camera.<label>.detected.<camera>` (glob-friendly, ACTIVE only).

**Backend registered** — `CameraEventBackend.backend_name = "frigate"`. Streaming-style backend (`connect / disconnect / stream_events` on top of the standard `initialize / close`); the camera service owns the reconnect supervisor and re-invokes `connect()` on transport error.

**Slash commands** — provided by the core `cameras` service:
- `/cameras list`, `/cameras clips`, `/cameras seen`, `/cameras count`
- `/cameras mute` (on the greeting service — UIBlock confirm before persisting)

**Configure** (Settings → Monitoring → Cameras, with the `frigate` backend selected)
- `mqtt_host`, `mqtt_port` *(restart)* — Broker hostname / port. Frigate's bundled Mosquitto on `1883` is the most common deploy.
- `mqtt_topic_prefix` *(restart)* — Frigate's `mqtt.topic_prefix` (default `frigate`).
- `mqtt_username`, `mqtt_password` *(sensitive)* — Optional broker credentials.
- `mqtt_client_id` — MQTT client id (default `gilbert-cameras`).
- `mqtt_tls` — Enable TLS for the broker connection.
- `mqtt_tls_ca_cert`, `mqtt_tls_client_cert` *(sensitive)*, `mqtt_tls_client_key` *(sensitive)* — PEM material for self-signed brokers and mTLS.
- `mqtt_tls_insecure` — Skip TLS hostname / cert verification (DISABLES MITM PROTECTION — only for self-signed brokers where you don't want to ship the CA).
- `mqtt_tls_server_hostname` — SNI / cert-CN override.
- `http_base_url` *(restart)* — Frigate web base URL (e.g. `http://frigate.local:5000`). Used for snapshot / clip / camera-config probes.
- `http_auth_mode` — `none` (LAN deploy) or `bearer` (Frigate API keys / proxy).
- `http_token` *(sensitive)* — Bearer token; ignored when `http_auth_mode=none`.
- `verify_ssl` — Verify Frigate's TLS cert (default true).
- `cameras_filter` — Restrict to a subset of cameras the broker reports.

**MQTT broker onboarding hint.** If you don't already have a broker, point this at Frigate's bundled Mosquitto — it's the same broker Frigate publishes its own events to. Frigate's `config.yml` `mqtt:` block configures both ports and credentials; copy them into Gilbert's settings.

**Config action** — `test_connection`: probes Frigate's `/api/version`, attempts a 5-second MQTT connect+subscribe to `<prefix>/+/events`, and warns when the broker reports a Frigate version older than the supported 0.13.0 minimum.

**Single-layer reconnect** — the plugin opens **one** `aiomqtt.Client` per `connect()` call. Any `MqttError` exits the inner client and raises `CameraBackendError`; the camera service catches it, sleeps with exponential backoff (capped at `reconnect_max_seconds`), and calls `connect()` again. The plugin doesn't loop internally so there's only one place backoff semantics live.

**Frigate LWT translation.** When `<prefix>/available` flips to `offline` (Frigate-the-detector down even though the broker is healthy), the plugin signals the consumer which raises `CameraBackendError("frigate offline")`; the service publishes `camera.backend.disconnected` and re-attempts. When the LWT flips back to `online`, the next reconnect succeeds and `camera.backend.connected` fires.

**Defensive payload parsing** — every field read uses `.get()` with a default; `sub_label` accepts string / `[name, score]` list / null / missing forms; missing required fields drop the event with a debug-level log; `false_positive=true` drops the event entirely; invalid JSON payloads are logged at WARNING and dropped (the consumer never crashes on a malformed payload).

**Audio events** flow through transparently — Frigate 0.13+ emits `bark`, `glass_break`, `speech`, etc. on cameras with `audio.enabled=true`. They have `has_snapshot=false` so vision annotation short-circuits naturally; the greeting service announces them when their label is added to `announce_camera_labels`.

**Third-party deps** — `aiomqtt>=2.3.0,<3.0.0` (asyncio-native; v2-only because v1→v2 was a breaking API change and v3 hasn't shipped). HTTP via `httpx` (already a Gilbert core dep).

**SPA contributions** — the plugin owns its UI under `frigate/frontend/`:
- `frigate.cameras_page` — full `/cameras` SPA route declared via `Plugin.ui_routes()`. Per-camera grid, recent-events feed, mute editor.
- `frigate.recent_events` — dashboard card mounted into the `dashboard.bottom` slot via `Plugin.ui_panels()`. Subscribes to `camera.event.detected` for live updates.

Core never imports from `frigate/frontend/`; the `<PluginPanelSlot>` / `<PluginRoutes>` extension points + the per-plugin `panels.ts` side-effect file wire the components in via `panel_id`.

---

### gemini

Google Gemini chat backend, speaking the [OpenAI-compatible Gemini endpoint](https://ai.google.dev/gemini-api/docs/openai) at `generativelanguage.googleapis.com/v1beta/openai/` directly over `httpx` (no `google-generativeai` SDK). Gemini's pitch is very large context windows (~1M tokens on 2.5 Pro) and native multimodal input.

**Backend registered** — `AIBackend.backend_name = "gemini"`: tool-use capable, streaming, image-input capable on every current model (all 2.5 and 2.0 tiers are natively multimodal), per-call model override.

**Configure** (Settings → Intelligence → AI, with the `gemini` backend selected)
- `enabled` — Initialize this backend at startup (default `true`).
- `api_key` *(sensitive)* — Google AI Studio API key (`AIza…`), generated at https://aistudio.google.com/apikey. Distinct from a Google Cloud Vertex AI key — this plugin uses the AI Studio path, not Vertex.
- `base_url` — API base URL (default `https://generativelanguage.googleapis.com/v1beta/openai`).
- `model` — Default model ID (default `gemini-2.5-flash`). Choices: `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-2.0-flash`, `gemini-1.5-pro`.
- `enabled_models` — Subset exposed to the chat UI and AI profile editor.
- `max_tokens` — Per-response cap (default `8192`).
- `temperature` — Sampling temperature (default `0.7`).

**Streaming.** OpenAI-compatible SSE — `delta.content` → `TEXT_DELTA`, streamed `tool_calls[i].function.arguments` deltas reassembled into complete `ToolCall`s. `capabilities()` reports `streaming=True, attachments_user=True`.

**Attachments.** Every current Gemini model accepts `image_url` content parts with base64 data URLs on the compat endpoint. Document (PDF) attachments become text stubs pointing the model at the workspace tools — PDFs work on Gemini's native API but the OpenAI-compat layer isn't reliable for them yet.

**Config action** — `test_connection`: issues a one-word completion to verify credentials.

---

### google

Bundled Google integration suite. One plugin, six backends — they share credential plumbing (OAuth, shared service accounts, delegated Workspace access), so splitting them would just duplicate boilerplate.

**Backends registered**
- `AuthBackend.backend_name = "google"` — OAuth ID token verification for the login system.
- `UserProviderBackend.backend_name = "google_directory"` — syncs Google Workspace users into Gilbert's user store.
- `EmailBackend.backend_name = "gmail"` — used by the Inbox service for polling, threads, drafts, and sending.
- `DocumentBackend.backend_name = "google_drive"` — Google Drive document sync into the Knowledge service.
- `CalendarBackend.backend_name = "google_calendar"` — Google Calendar v3 events, free/busy, and mutations for the Calendar service.
- `TaskBackend.backend_name = "google_tasks"` — Google Tasks v1 list / create / update / complete / delete for the Tasks service. One Gilbert task list = one Google `tasklist` (bound by `tasklist_id`); polling uses `updatedMin` for delta semantics.

**Configure**

| Setting | Keys |
|---|---|
| Auth (Google OAuth) | `client_id`, `client_secret` *(sensitive)*, `domain` (optional Workspace domain lock) |
| User provider (Workspace directory) | `sa_json` *(sensitive, service-account JSON)*, `delegated_user`, `domain` |
| Inbox (Gmail) | `credential_mode`, `email_address`, OAuth fields (`oauth_client_id`, `oauth_client_secret`, `oauth_redirect_uri`, `oauth_refresh_token`, `oauth_auth_code`), legacy `service_account_json` + `delegated_user` |
| Knowledge (Drive) | `credential_mode`, `folder_id`, OAuth fields, or `service_account_json` for shared / delegated service-account modes |
| Calendar (Google Calendar) | `credential_mode`, `email_address`, OAuth fields, or `service_account_json` for shared / delegated service-account modes |
| Tasks (Google Tasks) | `credential_mode`, `tasklist_id`, OAuth fields, or legacy `service_account_json` + `delegated_user` |

For new personal Google-account setup, use `credential_mode = oauth_bot` and the backend's `connect_google` / `connect_google_complete` actions. Existing Workspace domain-wide-delegation configs continue to work. `shared_service_account` is supported only for Calendar and Drive, where Google sharing can grant the service-account email access to a calendar or folder; Gmail and Tasks do not support that mode.

Each backend exposes a `test_connection` config action that verifies credentials by making a one-off read call or validating the share target for shared service-account setup.

**OAuth scopes required for the Calendar backend** (added to the existing service-account's domain-wide delegation in the Google Workspace admin console):

- `https://www.googleapis.com/auth/calendar`
- `https://www.googleapis.com/auth/calendar.events`

The same service account configured for Gmail can host Calendar — just add the two scopes to its delegated grant. If your service account is locked-scope, create a dedicated one with only the calendar scopes.

**OAuth scope required for the Tasks backend** (added the same way):

- `https://www.googleapis.com/auth/tasks`

The same service account configured for Gmail / Calendar can host Tasks — add this scope to the existing delegated grant. The settings UI's `test_connection` action surfaces "insufficient scope" errors clearly. The `list_tasklists` action enumerates available tasklists so you can pick the right `tasklist_id`.

**At-rest plaintext caveat (Gmail, Calendar, Tasks, Drive — same gap).** Service-account JSON pasted into `backend_config` is `sensitive=True`, which masks it in WS responses and the SPA, but `sensitive` is **not** encryption — the JSON sits in plaintext SQLite at `.gilbert/gilbert.db`. This is a project-wide gap inherited by every backend that holds long-lived secrets. Mitigations:

- Set `.gilbert/gilbert.db` to mode `0600`, owned by the running user (file-permission hardening).
- Scope service-account keys to the minimum users / scopes needed and rotate periodically.
- Track encryption-at-rest as a future cross-cutting feature (open question on the project roadmap).

**Third-party deps**: `google-auth`, `google-api-python-client`, `tzdata` (cross-platform IANA zone data — required for Calendar's `ZoneInfo(...)` lookups; bundled because Alpine/musl ships without `/usr/share/zoneinfo`).

---

### groq

Groq chat backend — runs open-weight models (Llama, Qwen, Mixtral, DeepSeek distills) on Groq's LPU hardware. Main selling point is inference latency: tokens/second is multiples higher than GPU-hosted providers. Speaks the [OpenAI-compatible endpoint](https://console.groq.com/docs/openai) at `api.groq.com/openai/v1` directly over `httpx`. Also provides a batch speech-to-text backend using Groq's Whisper-compatible transcription endpoint.

**Backends registered**
- `AIBackend.backend_name = "groq"`: tool-use capable, streaming, per-call model override.
- `BatchTranscriptionBackend.backend_name = "groq_whisper"`: one-shot transcription via Groq's OpenAI-compatible `/audio/transcriptions` endpoint. Supports `whisper-large-v3`, `whisper-large-v3-turbo`, and `distil-whisper-large-v3-en`.

**Configure** (Settings → Intelligence → AI, with the `groq` backend selected)
- `enabled` — Initialize this backend at startup (default `true`).
- `api_key` *(sensitive)* — Groq API key (`gsk_…`).
- `base_url` — API base URL (default `https://api.groq.com/openai/v1`).
- `model` — Default model ID (default `llama-3.3-70b-versatile`).
- `enabled_models` — Subset of advertised models the chat UI and AI profile editor expose. Defaults to the full list: `llama-3.3-70b-versatile`, `llama-3.1-8b-instant`, `qwen-2.5-32b`, `deepseek-r1-distill-llama-70b`, `gemma2-9b-it`.
- `max_tokens` — Per-response cap (default `8192`).
- `temperature` — Sampling temperature (default `0.7`).

**Configure** (Settings → Transcription → Batch, with the `groq_whisper` backend selected)

The `groq_whisper` API key is **separate** from the sibling `groq` AI backend's key — each backend has its own config block under `transcription.batch.backends.groq_whisper.settings.*`.
- `api_key` *(sensitive)* — Groq API key (`gsk_…`).
- `base_url` — API base URL (default `https://api.groq.com/openai/v1`).
- `model` — Whisper model ID (default `whisper-large-v3`). Choices: `whisper-large-v3`, `whisper-large-v3-turbo`, `distil-whisper-large-v3-en`.

**Streaming.** OpenAI-compatible SSE — `delta.content` → `TEXT_DELTA`, streamed `tool_calls[i].function.arguments` deltas reassembled into complete `ToolCall`s. `capabilities()` reports `streaming=True, attachments_user=True`.

**Attachments.** Groq's hosted chat models don't accept native image attachments, so every attachment becomes a text stub pointing the model at the workspace tools. Text attachments are inlined as `## <name>\n\n<body>`.

**Config action** — `test_connection`: issues a one-word completion to verify credentials.

---

### guess-that-song

Multiplayer music guessing game managed by the AI. The AI picks a track, plays a short clip on the speakers, and players type their guesses in chat. Scoring, round timing, and leaderboards are tracked per-conversation via UI blocks pushed into the chat.

**Service registered** — `guess_game` (requires the `music` and `speaker_control` capabilities — install the `sonos` plugin or another music/speaker backend for this to actually play anything).

**Configure** (Settings → Games → Guess That Song)
- `clip_seconds` — How long each clip plays before guessing opens (default `5.0`).
- `round_time_seconds` — How long players have to guess (default `20.0`).
- `points_correct` — Points awarded per correct guess (default `10`).
- `hint_threshold` — Seconds remaining before a hint drops (default `10.0`).

**No third-party Python dependencies.**

---

### hk-webhook

Generic catch-all health-data webhook backend. Use it from any source — iOS Shortcut, Home Assistant automation, Garmin Connect IQ widget, custom Python script, anything that can POST JSON. Same payload shape as `apple-health` but without the HealthKit-identifier translation step (callers send `MetricType` enum values directly).

**Backend registered**
- `HealthBackend.backend_name = "hk-webhook"` — `supports_push = True`, `supports_pull = False`. Per spec §4.5 the backend declares NO `extra` whitelist; every key in the payload's `extra` dict is silently stripped. The back-channel for caller metadata is `source_event_id`, NOT arbitrary string blobs.

**Payload shape** — accepts three top-level forms (all per-item shapes are identical):

```json
{"metrics": [{"type": "steps", "value": 8431, "unit": "count", "recorded_at": "2026-05-09T07:00:00+00:00"}]}
```

Or a top-level array:

```json
[{"type": "weight", "value": 80.5, "unit": "kg", "recorded_at": "2026-05-09T07:00:00+00:00"}]
```

Or a single object. Unknown metric types drop with an INFO log line; malformed timestamps / values drop with DEBUG so one bad metric doesn't break the whole batch.

**Frontend panel** (`account.extensions` slot)
- Generate / rotate webhook URL button (raw token shown ONCE; only its SHA-256 hash is persisted).
- Copy-paste curl + Python snippets for non-iOS users.

**Third-party deps** — none (pure stdlib JSON parsing).

---

### jellyfin

Jellyfin Media Server backend for the core `MediaLibraryService`.
Talks to Jellyfin's REST API directly via `httpx` (the official
`jellyfin-apiclient-python` is partially synchronous and missing some
endpoints — REST is well documented and stable).

**Backend registered**
- `MediaLibraryBackend.backend_name = "jellyfin"`. All six capability
  flags set to `True`: `now_playing`, `resume`, `continue_watching`,
  `recently_added`, `seek`, `per_user`, `next_episode`.

**Slash commands** — provided by the core `MediaLibraryService`
(`/media …`), not by this plugin.

**Configure** (Settings → Media → Media library, with the `jellyfin`
backend enabled)
- `server_url` — Base URL (e.g. `http://jellyfin.local:8096`).
- `admin_username` — Admin username (used to bootstrap the device
  token; required only at link time).
- `admin_password` *(sensitive)* — Cleared after `link_account`
  unless `keep_password` is true.
- `keep_password` — Default False.
- `device_id` — Auto-generated stable identifier in
  `X-Emby-Authorization`.
- `access_token` *(sensitive)* — Auto-populated by `link_account`.
- `verify_tls` — Default True.
- `request_timeout_seconds` — Default 15.0.

**Config actions** — `link_account` (POST
`/Users/AuthenticateByName`, persists token, clears `admin_password`
unless `keep_password=true`), `test_connection`
(`GET /System/Info?api_key=…`).

**OS-level prerequisites** — none. `runtime_dependencies()` returns
`[]`.

**Notes** — v1 uses **admin token + `userId` query/path parameter**
for per-user data. Each per-user query is logged on the Jellyfin
server's audit trail as the admin user — accepted v1 limitation
(per-user-token minting is v2 work). Username → user-id resolution
caches by the *Jellyfin* username (NOT by Gilbert user id) so two
Gilbert users mapped to the same Jellyfin username share the
resolved id by definition. Token at-rest encryption is inherited
tech debt; v1 mandates `0600` on `.gilbert/gilbert.db`.

---

### kokoro

Local text-to-speech backend powered by the open-weights [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) model. Runs entirely in-process — no cloud API, no HTTP server. Default-disabled because of heavyweight dependencies (PyTorch ~700 MB, ~327 MB model on first use).

**Backend registered**
- `TTSBackend.backend_name = "kokoro"` — synthesizes speech from text using one `kokoro.KPipeline` per language, lazily instantiated on first use. Also implements `StreamingTTSCapability` (chunked output via sentence-split — each sentence is synthesized and encoded separately, so audio starts arriving before the full text finishes).

**Runtime check** — `./gilbert.sh doctor --plugin kokoro` runs a minimal end-to-end synthesis to verify the full stack (torch + kokoro + libgomp + PyAV) is functional. The probe imports `av` and `kokoro`, builds a `KPipeline(lang_code='a')`, and synthesizes one phoneme.

**Configure** (Settings → TTS, when the `kokoro` backend is selected)

| Key             | Type    | Default     | Notes |
|-----------------|---------|-------------|-------|
| `device`        | string  | `cpu`       | `cpu`, `cuda`, `mps`, or `auto`. Restart required. |
| `default_voice` | string  | `af_heart`  | One of the ~54 catalog voices (dropdown). |
| `speed`         | number  | `1.0`       | Default speech rate (0.5 – 2.0). Per-request `speed` on the synthesis call overrides this. |
| `preload`       | boolean | `false`     | Load the default-language pipeline at startup. Adds ~5–10 s to boot but avoids the latency on the first TTS call. Restart required. |

**Voices** — ~54 voices across American English, British English, Japanese, Mandarin, Spanish, French, Hindi, Italian, and Portuguese. The first character of each voice ID encodes the language (`a`=American, `b`=British, `j`=Japanese, `z`=Chinese, `e`=Spanish, `f`=French, `h`=Hindi, `i`=Italian, `p`=Portuguese); the second character is gender (`f`=female, `m`=male). Filter the Settings dropdown by `language`, `region`, or `gender` labels.

**Output formats** — MP3 (libmp3lame), WAV (PCM 16-bit LE), OGG (libvorbis), PCM (raw int16 LE). All output at 44.1 kHz mono, matching the rest of the TTS service.

**OS requirements** — None beyond what `uv sync` installs. PyAV ships its own ffmpeg shared libraries, so no system ffmpeg install is needed. On Linux, torch needs `libgomp1` (usually present); the runtime probe surfaces a clear error if it's missing.

---

### lutron-radiora

Lutron RadioRA 2 / HomeWorks integration. Registers two backends — one for the core `lights` service and one for the core `shades` service — both speaking telnet to the main repeater via [`pylutron`](https://pypi.org/project/pylutron/). Areas, dimmer/switch types, and shade outputs are auto-discovered from the repeater's XML database, so there's no per-room config.

**Backends registered**
- `LightsBackend.backend_name = "lutron-radiora"` — every non-shade output. `supports_dimming = True`; per-light `LightInfo.supports_dimming` reflects pylutron's `Output.is_dimmable` so the lights service skips switch-only loads when the user asks to set brightness.
- `ShadesBackend.backend_name = "lutron-radiora"` — every `SYSTEM_SHADE` / `MOTOR` output. `supports_position = True`, `supports_stop = True`.

**Slash commands** — provided by the core `lights` and `shades` services, not by this plugin directly. With this backend selected:
- `/lights list`, `/lights status <name|area>`, `/lights on <name|area> [pct]`, `/lights off <name|area>`, `/lights toggle <name|area>`, `/lights brightness <name|area> <pct>`
- `/shades list`, `/shades status <name|area>`, `/shades open <name|area>`, `/shades close <name|area>`, `/shades position <name|area> <pct>`, `/shades stop <name|area>`

Names match either the Lutron output name (e.g. *Kitchen Pendants*) or the area / room name (e.g. *Kitchen*, which addresses every output in that area).

**Configure** (Settings → Lighting → Lights / Shades, with the `lutron-radiora` backend selected)
- `host` — Hostname or IP of the RadioRA 2 / HomeWorks main repeater.
- `username` — Telnet username (RadioRA 2 default: `lutron`).
- `password` *(sensitive)* — Telnet password (RadioRA 2 default: `integration`).

Both backends advertise the same connection parameters so the lights and shades pages each have their own copy. Pointing both at the same repeater is fine — the plugin keeps one shared bridge regardless of how many backends are active.

**Config action** — `test_connection`: connects to the repeater and reports the discovered light + shade counts.

**Third-party deps** — `pylutron>=0.4.1`.

---

### messaging

Bidirectional text-message orchestration across three transport tiers — **RCS** (modern, rich text + media + read receipts, default), **MMS** (SMS + binary attachments), and **SMS** (plain-text fallback). Hosts `MessagingService`, the `send_text_message` AI tool, and the `/messages` SPA page. Backend-agnostic — the carrier integration lives in a separate plugin (e.g. [telnyx](#telnyx) contributes `MessagingBackend "telnyx"`).

**Service registered** — `MessagingService`, capability `messaging`. Toggleable (default off). Implements `MessagingProvider`, `ToolProvider`, `WsHandlerProvider`, `Configurable`. Persists messages into the `messages` collection; thread grouping is derived at read time so multi-number setups are first-class.

**Transport tiers** — outbound sends specify a `preferred_type` (defaulting to the service's configured default, `rcs` out of the box). The carrier downgrades to MMS (media + no RCS) or SMS (no media + no RCS) per the recipient's capabilities and reports the actual tier back in `SendResult.actual_type`. That value is stamped onto `Message.type` so the SPA renders a per-message badge (RCS / MMS / SMS) and surfaces unexpected downgrades. Inbound messages carry whatever transport the carrier reports.

**AI tool** — `send_text_message` (`/msg send <to> <body>`): send a text message to a phone number from the configured Gilbert sender. Optional `message_type` argument (`rcs` / `mms` / `sms`) forces a specific tier — useful when the operator knows the recipient can't handle RCS or wants to keep billing predictable. The tool description discourages markdown and formal sign-offs.

**WS RPCs** — `messaging.threads.list`, `messaging.thread.get`, `messaging.send` (accepts optional `preferred_type`). Bus events: `messaging.message_received`, `messaging.message_sent`, `messaging.thread_updated` (all carry `type`).

**Inbound flow** — the carrier plugin receives the carrier-side webhook (e.g. `/api/telnyx/messages/webhook`), parses into a `Message`, and calls back through the deliverer bound to it at startup. `MessagingService` resolves the `our_number` → `owner_user_id` mapping, persists, fires bus events, and (optionally) routes the message through `AIService.chat()` for an auto-reply.

**Configure** (Settings → Messaging → Messaging)
- `enabled` — Toggle the service on. Defaults `false`.
- `backend` — Messaging backend provider (choices come from registered `MessagingBackend`s, default `telnyx`).
- `from_number` — Shared E.164 sender for outbound messages.
- `owner_user_id` — Which Gilbert user owns the shared `from_number`. Inbound messages route to this user's threads. Required for inbound to work; outbound works without it.
- `default_message_type` — Outbound transport preference when the caller doesn't specify one. Defaults `rcs` (modern, rich text + media + read receipts + no segment length cap, end-to-end encrypted with Universal Profile 2.0+). Choices: `rcs` / `mms` / `sms`. Force `sms` to disable RCS globally — useful when your provider prices RCS differently or hasn't rolled it out on your number yet.
- `auto_reply` — When enabled, Gilbert reads inbound messages and replies via the AI service automatically. Defaults `false` — inbound messages still arrive and appear in the SPA, but the user composes replies manually.
- `auto_reply_system_prompt` *(AI prompt, multi-line)* — System prompt for the LLM when auto-replying. Tuned for texting-short tone; lets the LLM stay silent by returning empty text.

**Carrier-specific** — flattened under `settings.<key>` based on the chosen `backend`. For [telnyx](#telnyx): `settings.api_key`, `settings.messaging_profile_id`.

**Third-party deps** — none (pure stdlib; HTTP + WebSocket plumbing lives in the carrier plugin).

---

### mistral

Mistral AI chat backend, speaking the [OpenAI-compatible La Plateforme API](https://docs.mistral.ai/api/) at `api.mistral.ai/v1` directly over `httpx`. Runs the Mistral Large / Medium / Small lineup plus Codestral and the multimodal Pixtral.

**Backend registered** — `AIBackend.backend_name = "mistral"`: tool-use capable, streaming, image-input capable on Pixtral models, per-call model override.

**Configure** (Settings → Intelligence → AI, with the `mistral` backend selected)
- `enabled` — Initialize this backend at startup (default `true`).
- `api_key` *(sensitive)* — Mistral La Plateforme API key.
- `base_url` — API base URL (default `https://api.mistral.ai/v1`).
- `model` — Default model ID (default `mistral-large-latest`). Choices include `mistral-large-latest`, `mistral-medium-latest`, `mistral-small-latest`, `codestral-latest`, `open-mistral-nemo`, `pixtral-large-latest`.
- `enabled_models` — Subset exposed to the chat UI and AI profile editor.
- `max_tokens` — Per-response cap (default `8192`).
- `temperature` — Sampling temperature (default `0.7`).

**Streaming.** OpenAI-compatible SSE — `delta.content` → `TEXT_DELTA`, streamed `tool_calls[i].function.arguments` deltas reassembled into complete `ToolCall`s. `capabilities()` reports `streaming=True, attachments_user=True`.

**Attachments.** Pixtral models accept `image_url` content parts with base64 data URLs (same shape as OpenAI). Non-vision models receive images as text stubs. Document (PDF) attachments become text stubs pointing the model at the workspace tools.

**Config action** — `test_connection`: issues a one-word completion to verify credentials.

---

### ngrok

Tunnel backend that gives Gilbert a public HTTPS URL via [ngrok](https://ngrok.com/) — needed for OAuth callbacks (Google login, Slack Socket Mode) when you're running Gilbert behind NAT without a stable public DNS name.

**Backend registered** — `TunnelBackend.backend_name = "ngrok"`.

**Configure** (Settings → Infrastructure → Tunnel)
- `api_key` *(sensitive)* — ngrok auth token from `dashboard.ngrok.com`.
- `domain` — Optional custom ngrok domain (e.g. `myapp.ngrok.io`). Leave empty to get a random one.

**Config action** — `test_connection`: reports the current public URL if the tunnel is live.

**Third-party deps**: `pyngrok`.

---

### ntfy

Free, simple HTTP-based push delivery via [ntfy.sh](https://ntfy.sh)
(or any self-hosted ntfy server). The recommended starter for the
push-notification fan-out service — no API key, no app-store payment,
just pick an obscure topic and subscribe from the ntfy mobile/desktop
app. The empty-state hero on `/account/notifications` walks new users
through "scan QR → tap test" in well under a minute.

**Backend registered** — `PushNotificationBackend.backend_name = "ntfy"`.

**Per-user destination fields** (set on `/account/notifications`)
- `topic` — ntfy topic (path component). Pick something obscure —
  anyone subscribed to your topic can read your notifications.
- `server` — ntfy server URL. Leave empty to use the admin default.

**Admin config** (Settings → Notifications → Backend: ntfy)
- `default_server` — Default server URL. Defaults to
  `https://ntfy.sh`. Leave at this for the free public server; change
  it only if you self-host.
- `auth_token` *(sensitive)* — Optional Bearer token for protected
  ntfy servers. Empty for the public ntfy.sh.
- `timeout` — HTTP timeout in seconds (default 10).

**Config action** — `test_connection`: sends "Gilbert ntfy
connectivity test" to the topic provided in the action payload.
Refuses to default to a global topic name (no broadcasting to
strangers on ntfy.sh).

**Urgency mapping** — INFO=2, NORMAL=3, URGENT=5 in the `Priority`
header. Source tags map to ntfy emoji (`agent`→robot,
`scheduler`→alarm_clock, etc.). Click-through deep links land in the
`Click` header so a single tap on the notification opens the right
Gilbert page.

**No third-party Python dependencies** — uses core's `httpx`.

---

### ollama

Local Ollama AI backend — chat against any open-weight model you've `ollama pull`ed, running inference on your own machine. Speaks [Ollama's OpenAI-compatible endpoint](https://github.com/ollama/ollama/blob/main/docs/openai.md) at `http://localhost:11434/v1` directly over `httpx`. No API key required for local usage; proxied/remote instances can set one and it flows through as a Bearer token.

**Backend registered** — `AIBackend.backend_name = "ollama"`: tool-use capable (model-dependent), streaming, image-input capable on multimodal tags (`llava`, `llama3.2-vision`, `qwen2.5-vl`), per-call model override.

**Models.** Whatever the user has pulled locally — `ollama pull llama3.3`, `ollama pull qwen2.5-coder:32b`, etc. The `model` field is free-text because the available set depends on local installs. A curated list of common tool-capable tags ships as suggestions in the `enabled_models` dropdown: `llama3.3`, `llama3.2`, `qwen2.5`, `qwen2.5-coder`, `deepseek-r1`, `mistral`, `mistral-nemo`, `phi4`, `gemma3`.

**Configure** (Settings → Intelligence → AI, with the `ollama` backend selected)
- `enabled` — Initialize this backend at startup (default `true`).
- `api_key` *(sensitive, optional)* — Leave blank for local Ollama. Populate only when Ollama sits behind a reverse proxy that gates access.
- `base_url` — Ollama server URL (default `http://localhost:11434/v1`). Point at another host/port if Ollama runs elsewhere on your LAN.
- `model` — Default model tag (default `llama3.3`). Must be a tag you've pulled — Ollama rejects unknown tags.
- `enabled_models` — Suggested subset shown in the chat UI / AI profile editor.
- `max_tokens` — Per-response cap (default `8192`).
- `temperature` — Sampling temperature (default `0.7`).

**Streaming.** OpenAI-compatible SSE — `delta.content` → `TEXT_DELTA`, streamed `tool_calls[i].function.arguments` deltas reassembled into complete `ToolCall`s. `capabilities()` reports `streaming=True, attachments_user=True`.

**Attachments.** Multimodal Ollama models accept `image_url` content parts with base64 data URLs. Text-only models ignore vision parts, so the same payload is safe regardless of which tag is selected.

**Config action** — `test_connection`: issues a one-word completion to verify the server is reachable and the default model tag is installed.

---

### open-meteo

Weather backend powered by [Open-Meteo](https://open-meteo.com/) — a free, no-API-key HTTPS service covering global current conditions, hourly, and daily forecasts. The default backend for the core Weather service. Geocoding for place-name lookups uses Open-Meteo's free Geocoding API. Open-Meteo doesn't issue severe-weather alerts (`capabilities().alerts = False`); the Weather service surfaces `supported=false` from `weather_alerts` so the AI can clearly say "no data" rather than "no alerts." For US severe-weather warnings, install a future `nws` plugin alongside.

**Backend registered** — `WeatherBackend.backend_name = "open-meteo"`: `current=True, hourly=True, daily=True, alerts=False`.

**Configure** (Settings → Intelligence → Weather, with the `open-meteo` backend selected)
- `timeout_seconds` — HTTP timeout in seconds (default `15`). Granular per-phase timeouts inside the client cap connect / read / write / pool independently so a hung handshake doesn't burn the whole budget on connect alone.
- `user_agent` — HTTP `User-Agent` header sent with every request (default `Gilbert/1.0 (https://github.com/briandilley/gilbert)`). Open-Meteo's free-tier docs ask for a useful identifier; keep the contact URL so an operator can reach the project.

**Config action** — `test_connection`: hits the Open-Meteo forecast endpoint for a known coordinate and reports success / failure.

**Powered by Open-Meteo** — please keep the contact-URL `User-Agent` so the upstream operator can reach us. **Commercial use requires a paid Open-Meteo plan / API key**; the free tier permits up to 600 requests/min, 5,000/hour, 10,000/day. Gilbert's per-method cache TTLs (10 min current, 30 min hourly, 1 h daily, 5 min alerts) keep typical home-assistant usage well under these limits.

**No third-party Python dependencies** — talks directly to the REST API via `httpx`.

---

### openai

OpenAI GPT chat backend, speaking the [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) directly over `httpx` (no `openai` SDK dependency). Runs alongside the `anthropic` backend — configure either or both, then pick per-profile in the AI profile editor. Also provides a batch speech-to-text backend via OpenAI's Whisper API.

**Backends registered**
- `AIBackend.backend_name = "openai"`: tool-use capable, streaming, image-input capable on vision models (`gpt-4o`, `gpt-4-turbo`), per-call model override.
- `BatchTranscriptionBackend.backend_name = "openai_whisper"`: one-shot transcription via `/audio/transcriptions`. Supports `whisper-1`, `gpt-4o-transcribe`, and `gpt-4o-mini-transcribe`.

**Configure** (Settings → Intelligence → AI, with the `openai` backend selected)
- `enabled` — Initialize this backend at startup (default `true`). Uncheck to hide its settings and stop it being offered in profile dropdowns.
- `api_key` *(sensitive)* — OpenAI API key (`sk-…`).
- `base_url` — API base URL (default `https://api.openai.com/v1`). Override to point at an OpenAI-compatible proxy (Azure OpenAI, a local gateway, …).
- `organization` — Optional OpenAI organization ID sent as the `OpenAI-Organization` header. Leave blank unless your account belongs to multiple orgs.
- `model` — Default model ID used when a request specifies no per-call model (default `gpt-4o`).
- `enabled_models` — Subset of advertised models that the chat UI and AI profile editor expose for selection. Defaults to every model the backend knows about (`gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo`, `o1`, `o1-mini`, `o3-mini`).
- `max_tokens` — Per-response cap, sent as `max_completion_tokens` so it works for both classic chat models and the `o`-series reasoning models (default `16384`).
- `temperature` — Sampling temperature (default `0.7`). Automatically omitted from requests when the selected model is in the `o`-series, which only accepts the default sampling.

**Configure** (Settings → Transcription → Batch, with the `openai_whisper` backend selected)

The `openai_whisper` API key is **separate** from the sibling `openai` AI backend's key — each backend has its own config block under `transcription.batch.backends.openai_whisper.settings.*`.
- `api_key` *(sensitive)* — OpenAI API key (`sk-…`).
- `base_url` — API base URL (default `https://api.openai.com/v1`). Override for compatible providers.
- `model` — Whisper model ID (default `whisper-1`). Choices: `whisper-1`, `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`.

**Streaming.** The backend implements `generate_stream` over OpenAI's SSE chunks, translating `delta.content` into `TEXT_DELTA` events and assembling incremental `tool_calls[i].function.arguments` deltas back into complete `ToolCall`s at the end of the stream. All OpenAI-specific SSE parsing stays inside `openai_ai.py`; `capabilities()` reports `streaming=True, attachments_user=True`.

**Attachments.** Image attachments are rendered as `image_url` content parts with `data:<mime>;base64,…` URLs, which the vision-capable models (`gpt-4o`, `gpt-4-turbo`) understand natively. Document (PDF) attachments become text stubs pointing the model at the workspace tools (`read_workspace_file`, `run_workspace_script`) — Chat Completions doesn't accept PDFs directly. Text attachments are inlined as `## <name>\n\n<body>`.

**Config action** — `test_connection`: issues a one-word completion to verify credentials.

---

### openai-compatible

Vendor-neutral Chat Completions backend for endpoints that don't have a dedicated Gilbert plugin yet: self-hosted vLLM or LM Studio, corporate OpenAI proxies with custom auth, managed providers that ship an OpenAI-compat endpoint but aren't covered by `groq` / `ollama` / `openrouter` / `xai`. For the providers that *are* covered, prefer those — they ship curated model catalogs and provider-specific defaults. This plugin's job is the long tail.

**Backend registered** — `AIBackend.backend_name = "openai_compatible"`: tool-use capable (when the endpoint supports it), streaming (when the endpoint supports it), image-input capable on vision-aware models, per-call model override.

**Configure** (Settings → Intelligence → AI, with the `openai_compatible` backend selected)
- `enabled` — Initialize this backend at startup (default `true`).
- `base_url` *(required, no default)* — Base URL of the endpoint. Examples: `http://vllm.internal/v1`, `http://localhost:1234/v1` (LM Studio), `https://corp-gateway.example/openai/v1`. Init fails with a clear message if blank — the plugin has no meaningful default.
- `api_key` *(sensitive, optional)* — Bearer token sent as `Authorization: Bearer <key>`. Leave blank for local proxies that don't require auth — the header is omitted entirely.
- `model` — Default model ID. **Free-form** — type whatever the endpoint supports. There is no dropdown because there is no shared catalog. Use the "Refresh models" action below to populate one from the endpoint.
- `max_tokens` — Per-response cap (default `4096` — conservative for local models with small context windows). Sent as plain `max_tokens`.
- `temperature` — Sampling temperature (default `0.7`). Always sent — no o-series special casing.
- `request_headers` *(multiline)* — Extra headers to merge into every request, one per line as `key: value`. Useful for proxies with bespoke auth (`x-api-key`, workspace headers, non-standard bearer prefixes). Blank lines and lines starting with `#` are ignored.
- `supports_tools` *(bool, default `true`)* — Turn off for endpoints (vanilla llama.cpp, some older proxies) that reject requests carrying `tools`. With this off, requests carrying tools raise a clear error instead of silently 4xx'ing.
- `supports_streaming` *(bool, default `true`)* — Turn off for endpoints that choke on `stream: true`. With this off, the stream path routes through a single non-streaming request per round and emits one `MESSAGE_COMPLETE`.

**Config actions**
- `test_connection` — Issues a one-word completion to verify the endpoint and credentials.
- `refresh_models` — `GET /models` and populate the in-memory model list (picked up by `available_models()` — the UI dropdown updates without a restart). On 404, the action reports that `/models` isn't implemented and suggests using the free-form `model` field. The list is in-memory only — re-run after a restart if you want fresh data.

**Attachments.** Image attachments ride as `image_url` content parts with `data:<mime>;base64,…` URLs. Whether the target endpoint actually handles them depends on the model — vision-capable endpoints pick them up, text-only ones ignore them. Document (PDF) attachments become text stubs pointing at the workspace tools; text attachments inline as `## <name>\n\n<body>`.

---

### openwakeword

Local wake-word detection using [openWakeWord](https://github.com/dscripka/openWakeWord) — ONNX-based models running on CPU. No API key, no internet access required. Audio must be 16-bit PCM at 16 kHz mono; the backend buffers incoming chunks into 80 ms windows (1280 samples) for the model.

**Backend registered** — `WakeWordBackend.backend_name = "openwakeword"`.

**Bundled "hey gilbert" model.** The plugin ships a custom-trained `hey_gilbert.onnx` model at `models/hey_gilbert.onnx`. The default `model_paths` config points at it so the backend works out of the box. Callers receive a `WakeEvent` by including `"hey_gilbert"` in their `WakeWordConfig.keywords`. On first use the openwakeword library downloads the feature-extraction models (`melspectrogram.onnx`, `embedding_model.onnx`, `silero_vad.onnx`) into its own cache directory.

**No account needed** — fully local, no API key required.

**Configure** (Settings → Transcription → Wake Word, with the `openwakeword` backend selected)
- `model_paths` — Comma-separated paths to `.onnx` wake-word model files. Defaults to the bundled `hey_gilbert.onnx`. Add additional paths (or replace) to enable other wake-words. Setting this to an empty string falls back to openwakeword's library-bundled pretrained set (`hey_jarvis`, `alexa`, `hey_mycroft`, `hey_rhasspy`, `timer`, `weather`).
- `inference_framework` — `"onnx"` (default, works on Python 3.12+) or `"tflite"` (faster on some hardware, but `tflite-runtime` has no wheels for Python 3.12+ yet).

**Third-party deps** — `openwakeword>=0.6`. The bundled `hey_gilbert.onnx` weighs ~200 KB and is committed alongside the plugin.

---

### openrouter

OpenRouter chat backend — a meta-provider that fronts ~200 models from Anthropic, OpenAI, Google, Meta, Mistral, DeepSeek, xAI, Qwen, and more behind a single API key and a unified [OpenAI-compatible endpoint](https://openrouter.ai/docs). Handy for experimenting across providers without signing up with each one, and for routing a single Gilbert install to different frontier models per profile tier.

**Backend registered** — `AIBackend.backend_name = "openrouter"`: tool-use capable, streaming, image-input capable for vision-capable models, per-call model override.

**Model slugs.** Models are addressed as `provider/model`, e.g. `anthropic/claude-sonnet-4-5`, `openai/gpt-4o`, `google/gemini-2.5-pro`, `meta-llama/llama-3.3-70b-instruct`. The plugin ships with a curated list of popular tool-capable slugs; the `model` field is free-text so users can paste any slug from https://openrouter.ai/models without patching the plugin.

**Configure** (Settings → Intelligence → AI, with the `openrouter` backend selected)
- `enabled` — Initialize this backend at startup (default `true`).
- `api_key` *(sensitive)* — OpenRouter API key (`sk-or-v1-…`).
- `base_url` — API base URL (default `https://openrouter.ai/api/v1`).
- `site_url` — Optional `HTTP-Referer` URL sent to OpenRouter for attribution on their public leaderboard. Blank = anonymous.
- `site_name` — Optional `X-Title` name sent with the same purpose.
- `model` — Default model slug (default `anthropic/claude-sonnet-4-5`).
- `enabled_models` — Subset of the curated slug list exposed to the chat UI and AI profile editor.
- `max_tokens` — Per-response cap (default `8192`).
- `temperature` — Sampling temperature (default `0.7`).

**Streaming.** OpenAI-compatible SSE — `delta.content` → `TEXT_DELTA`, streamed `tool_calls[i].function.arguments` deltas reassembled into complete `ToolCall`s. `capabilities()` reports `streaming=True, attachments_user=True`.

**Attachments.** Vision-capable models on OpenRouter (Claude, GPT-4o, Gemini, Pixtral, Grok Vision, …) accept `image_url` content parts with base64 data URLs. Text-only models ignore vision parts, so the same payload is safe regardless of model choice.

**Config action** — `test_connection`: issues a one-word completion to verify credentials.

---

### phone

Outbound phone calls — Gilbert places PSTN calls on the user's behalf and drives the live conversation through the core `voice_brain` engine (STT → LLM → TTS, barge-in, opening disclosure, transcript). This plugin owns `PhoneCallService` and the `/calls` SPA page; the actual carrier is a separate backend chosen at runtime (`telnyx` ships in std-plugins).

Concurrency cap: at most one active call per user. A second `make_phone_call` while one is live returns a 409-style error — the user must hang up first.

**Service registered** — `PhoneCallService`, capabilities `phone_calls`, `ai_tools`, `ws_handlers`. Requires `entity_storage`, `event_bus`, `ai_chat`, `text_to_speech`, `speech_to_text`, `voice_brain`. Toggleable.

**SPA page** — `/calls` (and deep-links at `/calls/:callId`), gated on the `phone_calls` capability. Two-pane view: recent calls on the left, live transcript + "intervene with a directive" textbox on the right. Subscribes to `phone.call.transcript_delta` / `phone.call.status_changed` for live updates.

**Slash command** — `/call <number> <brief>`, routed through `slash_namespace = "call"`. The AI tool name is `make_phone_call`.

**Configure** (Settings → Phone)

- `backend` — Telephony backend; choices come from `TelephonyBackend.registered_backends()` (e.g. `telnyx`).
- `from_number` — Shared E.164 caller-ID for outbound calls (e.g. `"+13035550100"`). Must be a number you control on the chosen provider.
- `max_call_seconds` — Hard cap on a single call's duration (default 600). Brain hangs up at this many seconds even mid-sentence.
- `opening_disclosure_prompt` — AI prompt for the very first line Gilbert speaks on every outbound call (use `{display_name}` for the user's name). Required by federal AI-call disclosure rules.
- `call_system_prompt` — AI prompt the LLM uses while driving a call. Receives the user's brief + the running transcript per turn. Keep it tight — phone latency is sensitive to long contexts.
- Backend-specific keys (e.g. Telnyx `api_key`, `connection_id`, `public_url`) are flattened into the same Phone section once a backend is chosen — see the `telnyx` plugin entry for those.

**No third-party Python dependencies** — pure stdlib at this layer; the carrier plugin pulls in `httpx` / `websockets`.

---

### plex

Plex Media Server backend for the core `MediaLibraryService`. Wraps
[`plexapi`](https://github.com/pkkid/python-plexapi) for browse / search /
playback dispatch and uses `httpx` directly for the Plex.tv PIN-link
flow and a few endpoints plexapi doesn't expose conveniently.

**Backend registered**
- `MediaLibraryBackend.backend_name = "plex"`. All six capability flags
  set to `True`: `now_playing`, `resume`, `continue_watching`,
  `recently_added`, `seek`, `per_user`, `next_episode`.

**Slash commands** — provided by the core `MediaLibraryService`
(`/media …`), not by this plugin. Tools like `/media search`,
`/media play`, `/media on-deck`, `/media now`, `/media pause` /
`/media resume` / `/media stop` / `/media seek`, `/media recommend` are
registered by the core service when this backend is configured.

**Configure** (Settings → Media → Media library, with the `plex`
backend enabled)
- `account_token` *(sensitive)* — Plex.tv X-Plex-Token. Obtained via
  the Link Account flow.
- `server_machine_id` — Machine identifier of the chosen server.
  Filled by the Choose Server step.
- `server_url` — Override the auto-discovered URL. Empty = let plexapi
  pick from plex.tv resources.
- `verify_tls` — Verify TLS for `https://` URLs (default True).
- `request_timeout_seconds` — Default 15.0.
- `default_user_token` *(sensitive)* — Optional fallback X-Plex-Token
  used for no-mapping calls.

**Config actions** — `link_account` / `link_account_complete` (PIN flow
on plex.tv/link), `choose_server` (lists Plex.tv resources owned by the
linked account), `test_connection` (calls `/identity`).

**OS-level prerequisites** — none. `runtime_dependencies()` returns
`[]`.

**Notes** — token at-rest encryption is inherited tech debt across the
codebase; v1 mandates `0600` on `.gilbert/gilbert.db`. Per-Plex-Home-
user token caches are keyed by the Plex Home user uuid (NOT by Gilbert
user id), with per-user `asyncio.Lock` so two concurrent calls for the
same Home user serialize but two for different Home users do not. A
re-link (`account_token` change) atomically clears all per-Home-user
caches before re-pinning the chosen `PlexServer`.

---

### pushover

[Pushover](https://pushover.net/) push delivery — one-time-payment
mobile apps on iOS / Android. Reliable, no shared topic to leak, but
admins must register a Pushover application once and share its
**app token** across all Gilbert users; each user enters their personal
30-character **user_key** on their route.

**Backend registered** — `PushNotificationBackend.backend_name = "pushover"`.

**Per-user destination fields** (set on `/account/notifications`)
- `user_key` *(sensitive)* — your Pushover user key from
  pushover.net.
- `device` — optional device name to target a single device. Empty
  delivers to every device on the account.

**Admin config** (Settings → Notifications → Backend: pushover)
- `api_token` *(sensitive)* — Pushover application API token.
- `timeout` — HTTP timeout in seconds (default 10).

**Config action** — `test_connection`: calls Pushover's
`/users/validate.json` endpoint with the configured token and a
`user_key` from the action payload, reporting how many devices it
saw.

**Urgency mapping** — INFO→`-1`, NORMAL→`0`, URGENT→`1` (sounds even
on quiet hours; Pushover's emergency priority `2` is reserved for
v1.1 with the outbox so retries are bounded). Click-through deep
links flow as Pushover's `url` + `url_title` fields.

**No third-party Python dependencies** — uses core's `httpx`.

---

### porcupine

Wake-word detection via [Picovoice Porcupine](https://picovoice.ai/platform/porcupine/). Uses the `pvporcupine` SDK — a C-extension with built-in keyword support (e.g., "computer", "hey google", "ok google", "alexa") and support for custom `.ppn` model files. Audio must be 16-bit PCM at 16 kHz mono; the backend buffers incoming chunks into Porcupine's fixed frame size (typically 512 samples).

**Backend registered** — `WakeWordBackend.backend_name = "porcupine"`.

**Account setup** — Get a free access key at https://console.picovoice.ai. Free tier is available for personal use; commercial use requires a paid licence.

**Configure** (Settings → Transcription → Wake Word, with the `porcupine` backend selected)
- `access_key` *(sensitive)* — Picovoice access key from https://console.picovoice.ai.

**Third-party deps** — `pvporcupine>=3.0`.

---

### qwen

Alibaba Qwen chat backend, speaking DashScope's [OpenAI-compatible Chat Completions endpoint](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope) directly over `httpx` (no `dashscope` SDK dependency). Because DashScope accepts OpenAI's request shape, streaming protocol, and tool-calling format verbatim, the backend runs alongside `openai` and `anthropic` with the same capabilities — configure one or several, then pick per-profile in the AI profile editor.

**Backend registered** — `AIBackend.backend_name = "qwen"`: tool-use capable, streaming, image-input capable on vision models (`qwen-vl-max`, `qwen-vl-plus`), per-call model override.

**Configure** (Settings → Intelligence → AI, with the `qwen` backend selected)
- `enabled` — Initialize this backend at startup (default `true`). Uncheck to hide its settings and stop it being offered in profile dropdowns.
- `api_key` *(sensitive)* — DashScope API key (`sk-…`).
- `base_url` — API base URL (default `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`). Switch to `https://dashscope.aliyuncs.com/compatible-mode/v1` for the mainland-China endpoint, or point at a local OpenAI-compatible proxy.
- `model` — Default model ID used when a request specifies no per-call model (default `qwen-plus`).
- `enabled_models` — Subset of advertised models that the chat UI and AI profile editor expose for selection. Defaults to every model the backend knows about (`qwen3-max`, `qwen-max`, `qwen-plus`, `qwen-turbo`, `qwen2.5-72b-instruct`, `qwen2.5-32b-instruct`, `qwen2.5-coder-32b-instruct`, `qwq-32b-preview`, `qwen-vl-max`, `qwen-vl-plus`).
- `max_tokens` — Per-response cap (default `8192`). Sent as the standard OpenAI `max_tokens` field — no `o`-series-style `max_completion_tokens` workaround needed.
- `temperature` — Sampling temperature (default `0.7`).

**Streaming.** The backend implements `generate_stream` over DashScope's SSE chunks, which use the same wire format as OpenAI — `delta.content` becomes `TEXT_DELTA` events, and incremental `tool_calls[i].function.arguments` deltas are reassembled back into complete `ToolCall`s at the end of the stream. `capabilities()` reports `streaming=True, attachments_user=True`.

**Attachments.** Image attachments are rendered as `image_url` content parts with `data:<mime>;base64,…` URLs, which the `qwen-vl-*` models understand natively. Document (PDF) attachments become text stubs pointing the model at the workspace tools (`read_workspace_file`, `run_workspace_script`) — the compatible-mode endpoint doesn't accept PDFs directly. Text attachments are inlined as `## <name>\n\n<body>`.

**Config action** — `test_connection`: issues a one-word completion to verify credentials.

---

### slack

Socket Mode bot that routes Slack DMs and `@Gilbert` mentions to the AI service. Users can chat with Gilbert from Slack with the same tool access, slash commands, and conversation history they have in the web UI. Thread replies where Gilbert is participating are automatically picked up.

**Service registered** — `slack` (requires the `ai_chat` capability, optionally `users` for email-to-user resolution).

**Configure** (Settings → Communication → Slack)
- `bot_token` *(sensitive)* — Slack bot token (`xoxb-…`).
- `app_token` *(sensitive)* — Slack app-level token (`xapp-…`). Required for Socket Mode.
- `ai_profile` — AI profile name routing Slack chat through a specific tier/backend/model (default `standard`).

Slack signing secrets aren't needed — Socket Mode doesn't use HTTP webhooks, so there's nothing for Slack to sign.

**Third-party deps**: `slack-bolt`.

---

### sonos

Sonos speaker control (S2 only) + Spotify-backed music search. Two backends registered by one plugin: speaker control uses the Sonos S2 local WebSocket API via `aiosonos`; music browse/search talks directly to Spotify's Web API via OAuth (playback still routes through the speaker's own linked Spotify account).

**Backends registered**
- `SpeakerBackend.backend_name = "sonos"` — playback, volume, grouping, TTS announcements (via native `audio_clip`), now-playing, Spotify URI handoff, queue repeat-mode (`set_repeat`, `supports_repeat = True`). Requires S2 firmware on every target speaker; run `scripts/check_sonos_s2.py` to verify before enabling.
- `MusicBackend.backend_name = "sonos"` — Spotify search, user library, playlists via Spotify's Web API. Apple Music / Amazon Music / other services are NOT supported — they went away with the SMAPI drop. Capability flags:
  - `supports_queue = True` — exposes the queue trio: `add_to_queue` / `/music queue <title>` (appends via SMAPI `AddURIToQueue`), `play_queue` / `/music play-queue` (re-points AVTransport at the queue + `Play`), and `queue_item` (button-invoked sibling of `play_item`).
  - `supports_stations = True` — exposes `/music station <seed>` backed by Spotify `/v1/recommendations` (track/artist/genre/free-text seeds).
  - `supports_loop = True` — exposes `/music loop [off|track|all]`, which routes to the speaker's repeat-mode.

**Configure** (Settings → Media → Speakers / Music)

*Speaker backend* — no configuration needed beyond enabling it. Discovery happens via zeroconf (`_sonos._tcp.local.`) at startup.

*Music backend* — requires a registered Spotify developer app (one-time; free at https://developer.spotify.com/dashboard):
- `client_id` — Spotify app client ID.
- `client_secret` *(sensitive)* — Spotify app client secret.
- `redirect_uri` — Must match one of the redirect URIs registered on your Spotify app exactly. Default `https://localhost:8000/callback`. Spotify requires HTTPS for named hosts (plain `http://localhost:…` is rejected as "Insecure"). The endpoint doesn't need to actually serve HTTPS — Spotify validates the scheme at authorize time, and Gilbert's manual-paste flow reads the `?code=` out of whatever URL the browser lands on after approval. If your registered URI differs (e.g. custom port, different path), change this field to match.
- `refresh_token` *(sensitive)* — Auto-populated by the Link Spotify flow. Don't edit.
- `spotify_auth_code` — Transient field used by the link flow; auto-cleared once tokens are issued.
- Legacy fields (`preferred_service`, `auth_token`, `auth_key`) retained so existing configs don't fail validation but ignored by the new pipeline.

**Config actions**
- `test_connection` (speaker) — Reports how many S2 speakers responded to zeroconf.
- `test_connection` (music) — Hits Spotify `/me` to verify the linked Spotify app + refresh token.
- `link_spotify` → `link_spotify_complete` — Manual-paste OAuth flow. User clicks Link Spotify, gets an authorize URL, approves on Spotify, pastes the redirect URL into `spotify_auth_code`, saves, clicks Finish Linking. Gilbert exchanges the code for tokens and persists the refresh token.

**Third-party deps**: `aiosonos` (S2 local WebSocket client), `zeroconf` (LAN discovery).

---

### tavily

Web search backend. Used by the Web Search service's `web_search` and `image_search` tools (slash: `/web search …`, `/web images …`). Tavily's API also returns an AI-generated summary of the top results, which Gilbert surfaces as the first "result."

**Backend registered** — `WebSearchBackend.backend_name = "tavily"`.

**Configure** (Settings → Intelligence → Web Search)
- `api_key` *(sensitive)* — Tavily API key.
- `timeout` — HTTP timeout in seconds (default `15`).

**Config action** — `test_connection`: runs a one-result search to verify the API key.

**No third-party Python dependencies** — talks directly to the REST API via `httpx`.

---

### telegram

[Telegram bot](https://core.telegram.org/bots) push delivery for the
push-notification fan-out service. The admin registers a bot with
[@BotFather](https://t.me/BotFather) once and configures the **bot
token**; each user discovers their personal **chat id** through the
backend's `discover_chat_id` action (the SPA renders it as a
clickable-chip wizard).

**Backend registered** — `PushNotificationBackend.backend_name = "telegram"`.

**Per-user destination fields** (set on `/account/notifications`)
- `chat_id` — Telegram chat id (numeric for private chats, prefixed
  `-100…` for channels). Use the *Discover chat id* button on the
  Routes form to look it up automatically.

**Admin config** (Settings → Notifications → Backend: telegram)
- `bot_token` *(sensitive)* — bot token from BotFather.
- `timeout` — HTTP timeout in seconds (default 15).

**Config actions**
- `test_connection` — calls `/getMe` to verify the token, returns the
  bot username.
- `discover_chat_id` — calls `/getUpdates` and returns the recent
  `(chat_id, name, last_text)` triples for the SPA to render as
  pickable chips. The SPA's `ChatIdWizard` component triggers it
  through the standard `config.action.invoke` RPC.

**Webhook-mode rejection.** On `initialize` the backend calls
`/getWebhookInfo`; if a webhook URL is set it logs an ERROR and stays
DISABLED — `getUpdates` and webhooks are mutually exclusive at the
Telegram API level, so a webhook-mode bot would silently fail every
send. Run `/deleteWebhook` on the bot to flip it back to polling
mode.

**Urgency mapping** — INFO sets `disable_notification=true` (silent
notification); NORMAL and URGENT both ping the device. Click-through
deep links render as a single-row inline-keyboard "Open in Gilbert"
button below the message.

**Rate-limit handling** — 429 responses parse `parameters.retry_after`
into `PushDeliveryResult.retry_after_s`; the service uses that value
(capped at 60s) instead of the configured backoff.

**Bot username caching.** After `getMe` succeeds the username is
exposed via `runtime_data["bot_username"]` so the chat-id wizard's
`https://t.me/<bot_username>` deep link renders without a second
roundtrip. The bot **token** is never present in `runtime_data`.

**No third-party Python dependencies** — uses core's `httpx`.

---

### telnyx

Telnyx telephony backend that powers `PhoneCallService` — places outbound PSTN calls and streams bidirectional G.711 mulaw audio over a Telnyx Media Stream WebSocket. The conversation brain (STT, LLM, TTS, barge-in handling) lives in core's `PhoneCallService`; this plugin handles only the carrier side.

Three integration points Telnyx talks to on Gilbert — two for voice, one for SMS:

- `POST /api/telnyx/webhook` — call-control events (`call.initiated`, `call.answered`, `call.hangup`, `call.dtmf.received`, `streaming.failed`).
- `WS /api/telnyx/media` — bidirectional media stream. Telnyx forwards remote-side mulaw audio inbound; Gilbert writes synthesized mulaw outbound through the same socket.
- `POST /api/telnyx/messages/webhook` — messaging events (`message.received`, `message.sent`, `message.finalized`). The plugin parses inbound messages into `Message` records and hands them to `MessagingService`.

All three endpoints must be reachable from Telnyx's network, which means Gilbert needs a publicly-routable HTTPS URL — either an existing reverse-proxy / tunnel (Cloudflared, ngrok, your own ingress) or the ngrok plugin's tunnel service.

**Backends registered** — `TelephonyBackend.backend_name = "telnyx"` (voice) and `MessagingBackend.backend_name = "telnyx"` (RCS / MMS / SMS — ships `type: "RCS"` to `/v2/messages` by default and reads the carrier-picked actual tier off `data.type` in the response; inbound webhook payloads' `type` is normalized to lowercase to match `MessageType`).

**Configure — voice** (Settings → Phone → backend selector + below)

- `api_key` — Telnyx API v2 key (starts with `KEY...`). Found in the Telnyx portal under Account → API Keys. **Sensitive — redacted in the UI.**
- `connection_id` — Telnyx Call Control Connection id. Found under Voice → Call Control Applications. Tells Telnyx which application's webhook URL to use for events from outbound calls.
- `public_url` — Public HTTPS base URL Telnyx can reach this Gilbert instance at (e.g. `https://gilbert.example.com`). Webhooks land at `/api/telnyx/webhook`, the media stream at `wss://.../api/telnyx/media`.

**Configure — messaging** (Settings → Messaging → backend selector + below, with `backend = telnyx`)

- `settings.api_key` — same Telnyx API key as voice. Configured separately so the two products can rotate keys independently. **Sensitive — redacted in the UI.**
- `settings.messaging_profile_id` — Telnyx Messaging Profile id (Messaging → Messaging Profiles in the portal). Configure the profile's `Webhook URL` to `<public-url>/api/telnyx/messages/webhook`.

The corresponding `PhoneCallService` and `MessagingService` settings (caller-ID, max-call-seconds, opening-disclosure prompt, auto-reply behavior, etc.) live alongside the backend selectors in the same Settings categories — see the [phone](#phone) and [messaging](#messaging) sections.

**No third-party Python dependencies** — talks to Telnyx via `httpx` and the Media Stream over the same `websockets` library Deepgram uses.

---

### tesseract

Local OCR backend using [Tesseract](https://tesseract-ocr.github.io/) via `pytesseract`. Runs entirely offline — no network, no API keys. Used by the OCR service for extracting text from images before indexing them in the knowledge base or analyzing them for the vision pipeline.

Requires the Tesseract binary to be installed on the host OS (`apt install tesseract-ocr`, `brew install tesseract`, etc.) — `pytesseract` is just a wrapper.

**Backend registered** — `OCRBackend.backend_name = "tesseract"`.

**Configure** (Settings → Intelligence → OCR)
- `language` — Tesseract language code or pipe-separated list (e.g., `"eng"`, `"eng+fra"`; default `"eng"`).

**Third-party deps**: `pytesseract` (plus the system Tesseract binary).

---

### unifi

Ubiquiti UniFi integration that aggregates signals from multiple UniFi subsystems into a single presence backend, plus a doorbell backend for UniFi Protect camera ring events. Composite design: one plugin registers two distinct backends (`PresenceBackend "unifi"` and `DoorbellBackend "unifi"`), each aggregating whichever UniFi subsystems you have configured.

**Backends registered**
- `PresenceBackend.backend_name = "unifi"` — aggregates UniFi Network WiFi clients, UniFi Protect face detections, and UniFi Access badge events into one presence signal per user.
- `DoorbellBackend.backend_name = "unifi"` — watches UniFi Protect cameras for ring events.

**Configure** (Settings → Monitoring → Presence / Doorbell)

The presence backend has three sub-sections that can each be enabled independently:

| Subsystem | Keys |
|---|---|
| UniFi Network | `unifi_network.host`, `unifi_network.username`, `unifi_network.password` *(sensitive)*, `unifi_network.verify_ssl` |
| UniFi Protect | `unifi_protect.host`, `unifi_protect.username`, `unifi_protect.password` *(sensitive)*, `unifi_protect.verify_ssl` |
| UniFi Access | `unifi_access.host`, `unifi_access.api_token` *(sensitive)*, `unifi_access.verify_ssl` |

The doorbell backend uses a flat config pointing at Protect:
- `host` — UniFi Protect host.
- `username` / `password` *(sensitive)* — Protect credentials.
- `doorbell_names` — Array of camera names to treat as doorbells.

**Config action** — `test_connection`: pings each configured subsystem and reports status.

**No third-party Python dependencies** — all UniFi APIs are spoken via `httpx`/`aiohttp`.

---

### voice-agent

Wake-word-activated voice conversations in the browser. Press "Start Conversation" on the `/voice` page (or trigger the configured wake-word), Gilbert listens through the browser microphone, runs the audio through STT, drives the response through the core `voice_brain` engine, and plays the synthesized speech back through the page's audio output. v1 is turn-taking; barge-in is on the roadmap.

The full Gilbert tool ecosystem is available during a session — knowledge search, MCP tools, agent dispatch, scheduler, … — because the engine runs in `use_full_ai_service` mode and hands the LLM whatever the AI service currently exposes.

**Service registered** — `VoiceAgentService`, capabilities `voice_agent`, `ws_handlers`, `ai_tools`. Requires `voice_brain`, `speech_to_text` (specifically `WakeWordListener`), `ai_chat` (specifically `ConversationMessagePoster`), `entity_storage`, `event_bus`. Toggleable; disabled by default — needs a mic source to actually run a session.

**SPA page** — `/voice`, gated on the `voice_agent` capability so disabling the service hides both the nav entry and the route.

**AI tool exposed only inside a voice session** — `end_conversation` (brain-mediated; not a slash command). The provider's `slash_namespace = "voice"`, kept defensive in case future tools expose user-facing commands.

**No user-facing configuration** beyond the standard service on/off toggle and per-session opening policy (which lives on the engine config, not the service).

**No third-party Python dependencies** — relies entirely on capabilities already resolved through the core service manager.

---

### withings

Withings Public Cloud OAuth pull backend. Syncs sleep, weight, blood pressure, body composition, and heart rate from a connected Withings account every 6 hours by default. Per-user OAuth state lives on the per-user `health_links` row written by the connect flow; global `client_id` / `client_secret` come from the backend's config.

**Backend registered**
- `HealthBackend.backend_name = "withings"` — `supports_pull = True`, `supports_push = False`. Satisfies the `StorageAwareHealthBackend` protocol so `HealthService` injects raw storage + `gilbert.public_base_url` before `initialize`. Per spec §4.5 the `extra` whitelist allows `device_model_id`, `measure_grpid`, and `attrib`.

**Admin precondition**
- `gilbert.public_base_url` MUST be set in core settings before users can connect — Withings's developer dashboard requires a fixed `redirect_uri` registered ahead of time. The required callback shape is `<public_base_url>/api/health/me/oauth/withings/callback`. The backend's `begin_link` returns a `LinkStartResult(status="error", ...)` if `public_base_url` is unset, so users never start an OAuth flow that's guaranteed to fail at the redirect step.

**Configure** (Settings → Personal Data → Health, with the `withings` backend section)
- `client_id` *(sensitive)* — Withings developer-app client ID. Register the app at <https://developer.withings.com/>.
- `client_secret` *(sensitive)* — Withings developer-app client secret.

Per-user `oauth_*` tokens live on `health_links` rows (NOT in `backend_config_params`), and per spec §6.4 they are stored **plaintext in v1** — the SPA panel surfaces a "Tokens stored unencrypted on this Gilbert instance until v2." disclosure so the user can make an informed choice. Webhook tokens (apple-health / hk-webhook) are already hash-at-rest; OAuth refresh tokens cannot be hashed (we have to send them back to Withings on refresh) so they need a different posture. v2 ships Fernet sealed to the OS keychain.

**Token refresh** — automatic on 401 (Withings access tokens last ~3h, refresh tokens are long-lived). The backend retries the request once after a successful refresh; a 401 on the refresh raises `HealthBackendAuthError` so the service surfaces a "reconnect" prompt and (after 5 consecutive auth failures) auto-disables the link row.

**Disconnect revokes upstream** — `WithingsBackend.disconnect(user_id)` overrides the default and calls `POST https://wbsapi.withings.net/v2/oauth2?action=revoke` with the user's access token BEFORE the local `health_links` row is dropped. Revocation failure logs WARN but does NOT block local cleanup — the user's "I disconnected" intent must succeed locally even when Withings is unreachable. Right-to-delete also calls `disconnect` for every linked OAuth backend before the cascade, so revocation happens automatically.

**Frontend panel** (`account.extensions` slot)
- Connect / Disconnect / Sync-now buttons.
- Disabled Connect when `gilbert.public_base_url` is unset, with an explainer pointing the admin at `/system`.
- The "Tokens stored unencrypted on this Gilbert instance until v2." disclosure.

**Third-party deps** — `httpx>=0.27` (already in core; declared explicitly for plugin-isolation correctness).

---

### xai

xAI Grok chat backend, speaking the [OpenAI-compatible xAI API](https://docs.x.ai/docs/api-reference) at `api.x.ai/v1` directly over `httpx`. Runs the Grok 4 / Grok 3 / Grok 2 lineup including the `grok-2-vision-1212` multimodal model.

**Backend registered** — `AIBackend.backend_name = "xai"`: tool-use capable, streaming, image-input capable on `grok-2-vision-1212`, per-call model override.

**Configure** (Settings → Intelligence → AI, with the `xai` backend selected)
- `enabled` — Initialize this backend at startup (default `true`).
- `api_key` *(sensitive)* — xAI API key (`xai-…`).
- `base_url` — API base URL (default `https://api.x.ai/v1`).
- `model` — Default model ID (default `grok-4-0709`). Choices: `grok-4-0709`, `grok-3`, `grok-3-mini`, `grok-2-vision-1212`, `grok-2-1212`.
- `enabled_models` — Subset exposed to the chat UI and AI profile editor.
- `max_tokens` — Per-response cap (default `8192`).
- `temperature` — Sampling temperature (default `0.7`).

**Streaming.** OpenAI-compatible SSE — `delta.content` → `TEXT_DELTA`, streamed `tool_calls[i].function.arguments` deltas reassembled into complete `ToolCall`s. `capabilities()` reports `streaming=True, attachments_user=True`.

**Attachments.** `grok-2-vision-1212` accepts `image_url` content parts with base64 data URLs. Non-vision Grok models ignore image parts, so sending the data URL is safe. Document (PDF) attachments become text stubs pointing the model at the workspace tools.

**Config action** — `test_connection`: issues a one-word completion to verify credentials.

---

## Adding a plugin

Every plugin is a standalone directory. The minimum layout:

```
my-plugin/
    plugin.yaml      # manifest (name, version, provides, requires, depends_on)
    plugin.py        # defines create_plugin() → Plugin instance
    pyproject.toml   # declares the plugin's third-party Python deps
    __init__.py      # empty, makes the directory a package for relative imports
    my_backend.py    # the actual integration code — implements a Gilbert ABC
    tests/
        conftest.py  # registers gilbert_plugin_<name> for pytest
        test_my_backend.py
```

### `plugin.yaml`

```yaml
name: my-plugin
version: "1.0.0"
description: "One-line description that shows up in /plugin list"

provides:
  - my_backend_name

requires: []     # Gilbert capabilities this plugin needs (e.g. ["music", "speaker_control"])
depends_on: []   # Other plugins this plugin depends on
```

### `plugin.py`

For a backend-only plugin, `setup()` just imports the module that defines the backend class — the ABC's `__init_subclass__` hook auto-registers it:

```python
from __future__ import annotations
from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta

class MyPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="my-plugin",
            version="1.0.0",
            description="One-liner",
            provides=["my_backend_name"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import my_backend  # noqa: F401 — triggers backend registration

    async def teardown(self) -> None:
        pass

def create_plugin() -> Plugin:
    return MyPlugin()
```

For a service-registering plugin, create the service instance and call `context.services.register()` — see `slack/plugin.py` or `arr/plugin.py` for examples.

### `pyproject.toml`

Every plugin needs one, even if it has zero third-party deps — Gilbert's `[tool.uv.workspace]` glob expects every workspace member to have a `pyproject.toml`:

```toml
[project]
name = "gilbert-plugin-my-plugin"
version = "1.0.0"
description = "One-liner"
requires-python = ">=3.12"
dependencies = [
    "some-library>=1.2.3",  # drop the list if no third-party deps
]

[tool.uv]
package = false             # virtual workspace member — no wheel is built
```

Gilbert's root `pyproject.toml` adds each plugin as a workspace member under `[tool.uv.sources]` so a plain `uv sync` installs every plugin's deps in one shot.

### `tests/conftest.py`

Pytest needs a little help to treat the plugin directory as the Python package `gilbert_plugin_<name>` so that intra-plugin relative imports work during test collection. Copy `tesseract/tests/conftest.py` as a starting point — it handles the common case of a single-module plugin.

If your plugin has **multiple modules that import each other relatively** (`from .foo import Bar` inside one module), use `unifi/tests/conftest.py` as a template — it has the crucial comment about **not** passing `submodule_search_locations=[]` to `spec_from_file_location`, which would otherwise cause relative imports to resolve to a second copy of the module. The unifi test suite found this the hard way.

### Runtime install flow

A plugin can also be installed at runtime via `/plugin install <github-url>`:

- If the plugin has **no third-party Python deps** (empty `dependencies = []` in its `pyproject.toml`), it hot-loads immediately — no restart needed.
- If it **has deps**, Gilbert persists the install with `needs_restart=True`, returns a message, and waits. Run `/plugin restart` to trigger `gilbert.sh`'s supervisor loop — it re-runs `uv sync` (picking up the new workspace member), then relaunches Gilbert. The boot loader then imports the plugin normally and the restart flag is cleared.

See the main Gilbert `CLAUDE.md` for the full description of the supervisor loop and exit-code convention.

## Running tests

From the Gilbert repo root:

```bash
# Everything
uv run pytest

# A specific plugin
uv run pytest std-plugins/<plugin>/tests/ -v

# Type checking (Gilbert's core + interfaces, which plugins must satisfy)
uv run mypy src/

# Linting (run from gilbert root, --extra dev)
uv run ruff check std-plugins/
```

Gilbert's `pyproject.toml` lists `std-plugins` in `testpaths`, so plugin tests are automatically discovered when you run `uv run pytest` from the Gilbert root.

## Keeping this README accurate

**The table of plugins and every per-plugin section above MUST be updated whenever a plugin is added, removed, renamed, or has its configuration schema change.** This README is the canonical reference for "what plugins exist and how do I configure them" — outdated docs here will mislead users and confuse future Claude sessions. Claude agents working in this repo should treat README drift as a regression and fix it in the same change that modifies a plugin.

## License

MIT
