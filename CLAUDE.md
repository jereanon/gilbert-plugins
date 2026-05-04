# Gilbert Plugins

First-party plugins for the [Gilbert](https://github.com/briandilley/gilbert) AI assistant. Each subdirectory is a self-contained plugin that Gilbert loads at startup. This repository is cloned into `std-plugins/` inside a Gilbert checkout (as a git submodule) and every plugin's `pyproject.toml` becomes a uv workspace member of the parent Gilbert project.

## Tech Stack

- **Language:** Python 3.12+
- **Framework:** Gilbert plugin system (see main Gilbert repo `CLAUDE.md` for full architecture)
- **Package manager:** uv — plugins are virtual workspace members (`[tool.uv] package = false`) of the parent Gilbert project, so a single `uv sync` from the Gilbert repo root installs every plugin's third-party deps.
- **Testing:** pytest with mocks; tests live under each plugin at `<plugin>/tests/`. Gilbert's root `pyproject.toml` lists `std-plugins` in `testpaths`, so `uv run pytest` from the Gilbert root discovers them automatically.

## Plugin Structure

Every plugin lives in its own directory with these files:

```
my-plugin/
    plugin.yaml       # manifest: name, version, provides, requires, depends_on, config
    plugin.py         # entry point: create_plugin() → Plugin instance
    pyproject.toml    # REQUIRED — declares the plugin's third-party Python deps
    __init__.py       # empty, makes the directory a Python package
    my_backend.py     # the actual integration code
    tests/
        conftest.py       # registers gilbert_plugin_<name> for pytest
        test_my_backend.py
```

### `plugin.yaml`

```yaml
name: my-plugin
version: "1.0.0"
description: "One-liner"

provides:
  - my_backend_name

requires: []
depends_on: []
```

### `plugin.py`

Must expose `create_plugin()` returning a `Plugin` instance. For a backend-only plugin, `setup()` just imports the backend module — the ABC's `__init_subclass__` hook auto-registers it in the backend registry:

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

For a service-registering plugin (rather than a backend), `setup()` creates the service and calls `context.services.register(service_instance)` — see `slack/plugin.py` and `arr/plugin.py` for examples.

### `pyproject.toml`

**Required for every plugin**, even ones with no third-party deps — Gilbert's `[tool.uv.workspace] members = ["std-plugins/*", ...]` glob expects every subdirectory to have a `pyproject.toml`, and errors out if one doesn't.

```toml
[project]
name = "gilbert-plugin-my-plugin"
version = "1.0.0"
description = "One-liner"
requires-python = ">=3.12"
dependencies = [
    "some-library>=1.2.3",
]

[tool.uv]
package = false
```

- `package = false` marks the plugin as a **virtual** workspace member — no wheel is built, the plugin is loaded dynamically via `importlib.util.spec_from_file_location` at runtime. uv still resolves and installs the `dependencies` list into the shared venv.
- If a plugin has no third-party deps beyond what's already in Gilbert's core (`httpx`, `aiohttp`, `pillow`, etc.), leave `dependencies = []` with a comment explaining why — don't omit the key.
- The Gilbert root `pyproject.toml` references each plugin under `[tool.uv.sources]` with `{ workspace = true }` so a plain `uv sync` pulls in every plugin's deps.

### `tests/conftest.py`

Pytest needs to be told how to load the plugin as a Python package so relative imports (`from .client import Foo`) resolve correctly. Copy `tesseract/tests/conftest.py` for the single-module case.

For **multi-module plugins with intra-plugin relative imports** (like `unifi`, where `presence.py` does `from .client import UniFiConnectionError`), copy `unifi/tests/conftest.py` — and **do not** pass `submodule_search_locations=[]` to `spec_from_file_location`. Passing that kwarg marks each module as a package, which causes `from .client import …` inside `presence.py` to resolve to a *second* copy of `client` at `gilbert_plugin_unifi.presence.client` — distinct class objects, `isinstance` checks fail, exception catches miss, and tests mysteriously break. The unifi conftest has a detailed comment about this.

## Key Interfaces

Plugins extend Gilbert by implementing these interfaces from the main repo's `gilbert.interfaces.*`:

- **`Plugin`** (`gilbert.interfaces.plugin`) — `metadata()`, `setup(context)`, `teardown()`. Optional hooks: `runtime_dependencies()` (declare non-pip OS deps for `gilbert doctor`) and `ui_panels()` (contribute SPA components into named slots without core knowing about the plugin).
- **`Service`** (`gilbert.interfaces.service`) — `service_info()`, `start(resolver)`, `stop()`. Implement this when your plugin adds a discoverable service (radio, slack bot, game, etc.).
- **Backend ABCs** — `AIBackend`, `TTSBackend`, `SpeakerBackend`, `MusicBackend`, `AuthBackend`, `EmailBackend`, `DocumentBackend`, `VisionBackend`, `OCRBackend`, `TunnelBackend`, `WebSearchBackend`, `PresenceBackend`, `DoorbellBackend`, `UserProviderBackend`. Set `backend_name = "…"` on the subclass and the ABC's `__init_subclass__` registers it automatically.
- **`ToolProvider`** protocol (`gilbert.interfaces.tools`) — `tool_provider_name`, `get_tools()`, `execute_tool()`. Implement alongside `Service` to expose AI tools and slash commands.
- **`ToolOutput`** / **`UIBlock`** (`gilbert.interfaces.ui`) — return interactive forms from tools (inputs, selects, sliders, buttons).
- **`Configurable`** (`gilbert.interfaces.configuration`) — `config_namespace`, `config_category`, `config_params()`, `on_config_changed()`. Plus `ConfigActionProvider` for "Test connection" / "Link account" buttons on the Settings page.

## Plugin Registration and Context

In `setup()`, plugins receive a `PluginContext` with:

- `context.services` — `ServiceManager` instance. Call `.register(service)` to add a discoverable service.
- `context.config` — Initial resolved plugin config (`plugin.yaml` defaults merged with user overrides at load time). **Do not read this for runtime config** — use the `ConfigurationReader` capability instead, which reflects live changes made via the Settings UI.
- `context.data_dir` — Persistent data directory for the plugin, auto-created under `.gilbert/plugin-data/<plugin-name>/`.
- `context.storage` — Optional namespaced `StorageBackend` already scoped to `gilbert.plugin.<plugin-name>` — entity collections you create through it are automatically namespaced and won't collide with other plugins or core.

## Plugin frontend (TypeScript / React)

Plugins that ship SPA components keep them inside their own directory:

```
my-plugin/
    plugin.py
    plugin.yaml
    pyproject.toml
    frontend/                 # everything UI lives here
        types.ts              # plugin-local TS types
        api.ts                # plugin-local hooks (e.g. useFooApi)
        FooPanel.tsx          # the React component(s)
        panels.ts             # side-effect: registerPanel('foo.bar', FooPanel)
        styles.css            # plugin-scoped styles, if any
```

Core's Vite build picks every `<plugin>/frontend/panels.ts` (and `.tsx`) up automatically via an `import.meta.glob` in `frontend/src/plugins/index.ts` — adding a plugin's UI is purely additive, no edits to core. Plugin TypeScript can import core helpers via the `@/` alias (`@/components/ui/button`, `@/hooks/useWebSocket`, etc.); core never imports from the plugin.

To surface a panel:

1. In Python: declare a `UIPanel` from `Plugin.ui_panels()`:

   ```python
   from gilbert.interfaces.plugin import UIPanel

   def ui_panels(self) -> list[UIPanel]:
       return [
           UIPanel(
               panel_id="myplugin.thing",
               slot="account.extensions",   # or "settings.<category>"
               label="My thing",
               required_role="user",         # or "admin"
           ),
       ]
   ```

2. In TypeScript: register the React component under the same `panel_id`:

   ```ts
   // <plugin>/frontend/panels.ts
   import { registerPanel } from "@/lib/plugin-panels";
   import { ThingPanel } from "./ThingPanel";
   registerPanel("myplugin.thing", ThingPanel);
   ```

The frontend's `<PluginPanelSlot slot="…">` mounts every registered panel for that slot, filtered by `required_role`. Panels with no registered React component are silently skipped (e.g. when the plugin is loaded backend-only without its frontend bundle).

WS RPCs unique to the plugin should live in a per-plugin hook (`<plugin>/frontend/api.ts` exporting `useFooApi`) using the underlying `rpc()` from `useWebSocket`. Don't bolt them onto core's `useWsApi`.

## Runtime dependencies (non-pip)

Plugins that need binaries or system libraries beyond what `pyproject.toml` can install (Chromium, tesseract, ffmpeg, system fonts, …) declare them via `Plugin.runtime_dependencies()`:

```python
from gilbert.interfaces.plugin import RuntimeDependency

def runtime_dependencies(self) -> list[RuntimeDependency]:
    return [
        RuntimeDependency(
            name="ffmpeg",
            description="Required for the audio-mux feature.",
            check_cmd="ffmpeg -version",
            install_hint="apt: 'sudo apt-get install ffmpeg'; brew: 'brew install ffmpeg'",
            auto_install_cmd="",   # leave blank for sudo-required installs
        ),
    ]
```

Users run `./gilbert.sh doctor` (or `./gilbert.sh doctor --plugin <name>`) which iterates every plugin's deps, runs each `check_cmd` via `/bin/sh -c`, and prints PASS/FAIL with the install hint on failure. `--install` runs each failing check's `auto_install_cmd` for plugins that opted in (reserve auto-install for unattended-safe paths like Playwright's per-user browser cache; sudo apt-get installs should stay manual).

The check should ideally exercise the dep, not just probe its file path — Playwright's previous `executable_path` probe passed even when launching headless failed because the headless-shell binary or OS libs were missing. Doing a real `chromium.launch(headless=True)` catches both.

## Configuration

Plugin config defaults go in `plugin.yaml` under `config:`. Users override via the main Settings UI or `.gilbert/config.yaml` under `plugins.<plugin-name>`. Plugins that need runtime-tunable config should implement `Configurable` and read via the `ConfigurationReader` capability:

```python
from gilbert.interfaces.configuration import ConfigurationReader

async def start(self, resolver):
    config_svc = resolver.get_capability("configuration")
    if isinstance(config_svc, ConfigurationReader):
        section = config_svc.get_section("my-plugin")
        api_key = section.get("api_key", "")
```

Never `isinstance(config_svc, ConfigurationService)` — that imports a concrete class from `core/services/` and violates the layer rules. Always use the `ConfigurationReader` protocol.

## Development Guidelines

- **Follow main Gilbert repo conventions.** Interface-first, type hints everywhere, async I/O, no blocking calls in service lifecycle methods.
- **Depend on capabilities, not concrete services.** Use `resolver.require_capability("music")` (returns the abstract `Service`) and `isinstance` checks against `@runtime_checkable Protocol` classes from `gilbert.interfaces.*`. Never `from gilbert.core.services.X import Y` — that reaches across the layer boundary and defeats the plugin isolation.
- **Use the backend registry, never direct imports.** Look up backend classes by name from `Backend.registered_backends()`. The import side-effect in your `plugin.py` is what triggers registration.
- **Return `ToolOutput` for interactive tools.** Use UI blocks for rich interaction.
- **Write tests alongside the plugin.** Tests go in `<plugin>/tests/test_<feature>.py`, collected automatically from the Gilbert root via `testpaths`.
- **Use relative imports within the plugin.** `from .game import GameState` — the plugin loader handles package setup.
- **`slash_namespace` on Service-providing plugins.** If your plugin exposes slash commands, set `slash_namespace = "..."` as a class attribute on the `Service` subclass to pick a short, user-friendly prefix. Without it, the auto-detected namespace is the directory name, which can be long.

## Commands

```bash
# Run all tests (from the Gilbert repo root — picks up std-plugins/*/tests via pyproject testpaths)
uv run pytest

# Run a specific plugin's tests
uv run pytest std-plugins/arr/tests/ -v

# Type checking (from Gilbert repo root — runs against core + interfaces)
uv run mypy src/

# Linting
uv run ruff check std-plugins/
```

## Keeping the README Plugin List Current

**The plugin table and per-plugin sections in `README.md` MUST stay in sync with the actual plugins in this repo.** This is non-negotiable — the README is the canonical reference for "what plugins exist, what they provide, and how to configure them." Future Claude sessions and human users read it to decide which plugins to enable.

Treat README drift the same way you treat stale memories: a regression to be fixed immediately, not deferred. Specifically:

- **Adding a plugin** → add a row to the table AND a full detail section under `## Available plugins` with: what it provides (exact backend names), third-party deps (with version floors), main config keys, slash commands if any, and any OS-level prerequisites.
- **Removing a plugin** → remove its row and detail section, and grep the README for any cross-references that might now point to nothing.
- **Renaming a plugin** → fix the section heading, the table row, the link anchor, and any inline references.
- **Changing a plugin's configuration schema** → update the "Configure" subsection with the new keys, types, sensitive flags, and defaults.
- **Changing what a plugin provides (new backend, new service)** → update the table's "Provides" column and the detail section.

Before committing a change that touches any `plugin.yaml`, `plugin.py`, or `config_params()` method, re-read the corresponding README section and confirm it's still accurate. A commit that changes plugin behavior without updating the README is incomplete.

## Agent Memory System

Claude AI agents working in this repo use a file-based memory system at `.claude/memory/` to retain knowledge about plugins, their internals, and their gotchas across conversations — the same system Gilbert's main repo uses.

### How it works

1. **Index file:** `.claude/memory/MEMORIES.md` is a flat index of all memories, one per line, each a markdown link to a detailed memory file. The index is the only file Claude loads by default.
2. **Memory files:** `.claude/memory/memory-<slug>.md` — detailed notes on a specific plugin, integration gotcha, or design decision.
3. **Loading on demand:** When working on a task, check the index. If a relevant memory exists, read the full file. Always mention in the terminal when you're loading a memory (e.g., "Loading memory: guess-that-song").

### Keeping memories current

**This is not optional.** Memories are how future Claude sessions understand this repo. Treat them like documentation that matters.

- **Create** a memory after designing a non-trivial new plugin or making a significant architectural decision (e.g. "unifi intra-plugin relative imports broke on `submodule_search_locations=[]`").
- **Update** a memory when the plugin changes in a way that makes the memory stale — new fields, renamed classes, changed behavior, new gotchas.
- **Remove** a memory when the plugin is deleted or replaced. Stale memories are worse than no memories.
- **Before every commit in this repo**, review any memories touched by the change. Update stale memories, delete obsolete ones, create new ones for anything significant.
- After learning something **non-obvious** about a third-party API, a test harness quirk, or a packaging edge case, capture it.

### Memory file format

```markdown
# <Title>

## Summary
One or two sentences describing what this is.

## Details
Detailed information — interfaces used, configuration, how it connects to the
rest of the system, design decisions, gotchas, test harness surprises, etc.

## Related
- Links to related memory files or source paths
```

### Index format (`.claude/memory/MEMORIES.md`)

```markdown
# Memories

- [Guess That Song Plugin](memory-guess-that-song.md) — multiplayer music guessing game, UI blocks, AI-mediated
- [UniFi Relative Import Gotcha](memory-unifi-relative-imports.md) — spec_from_file_location + submodule_search_locations=[] breaks intra-plugin relative imports
```

### Rules

- Keep the index concise — one line per memory, under 120 characters.
- Memory file names use `memory-<slug>.md` with kebab-case slugs.
- Don't dump entire source files into memories. Capture the *knowledge* — what it is, why it exists, how it fits together, what surprised you.
- Always keep `MEMORIES.md` in sync when creating, renaming, or deleting memory files.

## Privacy

**Never put private or personal information in tracked files.** This includes plugin source code, `plugin.yaml` examples, README text, and `.claude/memory/` files. API keys, personal email addresses, voice IDs, device names that identify people — none of that goes into commits. If you need an example in a doc, use obvious placeholders (`sk-ant-…`, `xoxb-…`, `example@example.com`).

## Existing Plugins

See **[README.md](README.md)** for the full plugin inventory with configuration, slash commands, and per-plugin notes. The README is the canonical source — this `CLAUDE.md` documents *how plugins work*, the README documents *what plugins exist*.
