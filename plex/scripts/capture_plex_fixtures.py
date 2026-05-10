"""Capture Plex API XML fixtures for the plugin's test suite.

Reads ``PLEX_URL`` + ``PLEX_TOKEN`` from the environment, hits a few
canonical endpoints, redacts tokens / private identifiers, and writes
the result to ``std-plugins/plex/tests/fixtures/plex/``.

Usage:
    PLEX_URL=https://plex.local:32400 PLEX_TOKEN=xxx \\
      uv run python std-plugins/plex/scripts/capture_plex_fixtures.py

Re-run after a plexapi or Plex API contract update.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import httpx

REDACT_PATTERNS = [
    (re.compile(r"X-Plex-Token=[^&\s\"']+"), "X-Plex-Token=REDACTED"),
    (re.compile(r"machineIdentifier=\"[^\"]+\""), "machineIdentifier=\"REDACTED\""),
    (re.compile(r"address=\"[^\"]+\""), "address=\"REDACTED\""),
]

ENDPOINTS = {
    "movie.xml": "/library/sections/1/all?type=1&limit=1",
    "show.xml": "/library/sections/2/all?type=2&limit=1",
    "season.xml": "/library/sections/2/all?type=3&limit=1",
    "episode.xml": "/library/sections/2/all?type=4&limit=1",
    "artist.xml": "/library/sections/3/all?type=8&limit=1",
    "album.xml": "/library/sections/3/all?type=9&limit=1",
    "track.xml": "/library/sections/3/all?type=10&limit=1",
    "sessions.xml": "/status/sessions",
}


def main() -> int:
    url = os.environ.get("PLEX_URL")
    token = os.environ.get("PLEX_TOKEN")
    if not url or not token:
        print("PLEX_URL and PLEX_TOKEN are required", file=sys.stderr)
        return 1

    out_dir = (
        Path(__file__).resolve().parent.parent
        / "tests"
        / "fixtures"
        / "plex"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=30.0, verify=False) as client:
        for filename, path in ENDPOINTS.items():
            try:
                resp = client.get(
                    f"{url.rstrip('/')}{path}",
                    headers={"X-Plex-Token": token},
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                print(f"  FAILED {filename}: {exc}", file=sys.stderr)
                continue
            body = resp.text
            for pat, repl in REDACT_PATTERNS:
                body = pat.sub(repl, body)
            (out_dir / filename).write_text(body)
            print(f"  WROTE  {filename}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
