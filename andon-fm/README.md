# andon-fm

Tune in to the four AI-hosted internet radio stations from
[Andon Labs](https://andonlabs.com/radio):

| Station | Host | Stream |
|---|---|---|
| Thinking Frequencies | Claude | `https://streaming.live365.com/a46431` |
| OpenAIR | GPT | `https://streaming.live365.com/a81044` |
| Backlink Broadcast | Gemini | `https://streaming.live365.com/a13541` |
| Grok and Roll | Grok | `https://streaming.live365.com/a15419` |

The plugin hands the Live365 MP3 stream URLs to Gilbert's existing
speaker service, so you can listen on Sonos, the host's speakers, or a
browser tab — the same dispatch every other audio source goes through.

## What ships

- **`andon_fm` service** — `Configurable` + `ToolProvider` + `WsHandlerProvider`.
- **Slash commands** under `/radio.*`: `list`, `play`, `stop`, `now`.
- **Now-playing scraper** — a scheduler job hits `andonlabs.com/radio`
  every `scrape_interval_seconds` (default 90) and parses each station's
  `currentBlock` (programming block name, description, start time,
  duration) + listener count from the page's embedded JS object. When a
  block transitions, an `andon_fm.now_playing.changed` event publishes
  on the bus so subscribers (e.g. the tuner page) update live.
- **Full-page tuner under Media** — `/media/andon-fm`, contributed as a
  `UIRoute` under the core Media nav group. Four station cards with
  cover art, AI host chip, current block, listener count, and a Play
  button. Pressing Play opens a dialog that lists every discovered
  speaker (plus the `my browser` magic alias) with checkboxes + a
  volume slider — selection is per play, not stored. `default_target_speakers`
  / `default_volume` from settings pre-populate the dialog.

## Slash commands

```
/radio.list                           # list stations + what's on each
/radio.play OpenAIR                   # tune in on the default target
/radio.play "Grok and Roll" kitchen   # play on a specific speaker
/radio.play Claude my-browser         # use a speaker alias / magic name
/radio.stop                           # stop default target
/radio.stop kitchen                   # stop a specific speaker
/radio.now                            # what's on across all stations
/radio.now Backlink                   # one station's current block
```

`<station>` matches the display name, the host model (`Claude`, `GPT`,
`Gemini`, `Grok`), any substring of the name, or the station UUID.

## Configuration

The plugin is **toggleable** — disabled by default. Enable it under
**Settings → Services → "Andon FM"** before the `/media/andon-fm`
nav entry, the slash commands, and the WS RPCs come online.

| Key | Type | Default | Description |
|---|---|---|---|
| `default_target_speakers` | array | `["my browser"]` | Speakers pre-selected in the per-play picker dialog. The magic alias `my browser` routes to the caller's own tab. |
| `default_volume` | int | `60` | Volume 0-100 used as the picker dialog's initial value (and for slash-command callers that omit a volume). |
| `scraper_enabled` | bool | `true` | Refresh now-playing metadata. Disable if you only want playback. Restart required. |
| `scrape_interval_seconds` | int | `90` | How often to poll. Blocks are usually 30-60 minutes long, so low intervals mostly waste requests. Restart required. |

## How it works

The Andon FM web player at `andonlabs.com/radio` embeds each station's
metadata as a JavaScript object literal inside the HTML. We fetch the
page, locate each station's section by its known UUID, and pull
`currentBlock:{…}` out with regex. It's not a documented contract, so
the parser is defensive — any per-station failure leaves that station's
cache untouched and the others keep updating.

The four stream URLs themselves are stable Live365 endpoints — the
same ones the andonlabs.com web player and Andon FM hardware radios
tune in to. We hand them to `SpeakerService.play_on_speakers` as plain
HTTP URIs.

## Files

- `plugin.py` — plugin entry, declares a `/media/andon-fm` UI route
  under the Media nav group.
- `stations.py` — bundled station catalog + lookup helper.
- `scraper.py` — the page-parsing now-playing extractor.
- `andon_fm_service.py` — service, tools, slash commands, scheduler
  wiring, WS handlers (including `andon_fm.speakers.list` for the
  per-play speaker picker).
- `frontend/` — TypeScript page (`AndonFmPage.tsx`) with a per-play
  speaker-picker dialog, plugin-local API hook (`api.ts`), types
  (`types.ts`), and `panels.ts` registration.
- `tests/` — pytest unit tests for the catalog, scraper, and service.

## Credits

All credit for the radio itself goes to
[Andon Labs](https://andonlabs.com/). This plugin is an unofficial
Gilbert integration that consumes the public stream URLs and parses
the public web page.
