"""Capture Jellyfin REST JSON fixtures for the plugin's test suite.

Reads ``JELLYFIN_URL`` + ``JELLYFIN_TOKEN`` (+ ``JELLYFIN_USER_ID`` for
per-user endpoints) from the environment, hits a few canonical
endpoints, redacts tokens / private identifiers, and writes the result
to ``std-plugins/jellyfin/tests/fixtures/jellyfin/``.

Usage:
    JELLYFIN_URL=http://jellyfin.local:8096 \\
    JELLYFIN_TOKEN=xxx \\
    JELLYFIN_USER_ID=admin-user-id \\
      uv run python std-plugins/jellyfin/scripts/capture_jellyfin_fixtures.py

Re-run after a Jellyfin API contract update.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import httpx

REDACT_PATTERNS = [
    (re.compile(r"\?api_key=[^&\s\"']+"), "?api_key=REDACTED"),
    (
        re.compile(r"\"AccessToken\"\s*:\s*\"[^\"]+\""),
        '"AccessToken": "REDACTED"',
    ),
    (re.compile(r"\"Id\"\s*:\s*\"[a-f0-9-]{20,}\""), '"Id": "REDACTED-ID"'),
]


def main() -> int:
    url = os.environ.get("JELLYFIN_URL")
    token = os.environ.get("JELLYFIN_TOKEN")
    user_id = os.environ.get("JELLYFIN_USER_ID", "")
    if not url or not token:
        print(
            "JELLYFIN_URL and JELLYFIN_TOKEN are required",
            file=sys.stderr,
        )
        return 1

    out_dir = (
        Path(__file__).resolve().parent.parent
        / "tests"
        / "fixtures"
        / "jellyfin"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    headers = {
        "X-Emby-Authorization": (
            f'MediaBrowser Client="GilbertCapture", Device="Gilbert", '
            f'DeviceId="capture", Version="1.0.0", Token="{token}"'
        ),
        "Accept": "application/json",
    }

    endpoints: dict[str, tuple[str, dict[str, str]]] = {
        "movie.json": (
            "/Items?IncludeItemTypes=Movie&Limit=1"
            + (f"&UserId={user_id}" if user_id else ""),
            {},
        ),
        "show.json": (
            "/Items?IncludeItemTypes=Series&Limit=1"
            + (f"&UserId={user_id}" if user_id else ""),
            {},
        ),
        "season.json": (
            "/Items?IncludeItemTypes=Season&Limit=1"
            + (f"&UserId={user_id}" if user_id else ""),
            {},
        ),
        "episode.json": (
            "/Items?IncludeItemTypes=Episode&Limit=1"
            + (f"&UserId={user_id}" if user_id else ""),
            {},
        ),
        "track.json": (
            "/Items?IncludeItemTypes=Audio&Limit=1"
            + (f"&UserId={user_id}" if user_id else ""),
            {},
        ),
        "sessions_list.json": ("/Sessions", {}),
    }

    with httpx.Client(timeout=30.0, verify=False, headers=headers) as client:
        for filename, (path, _) in endpoints.items():
            try:
                resp = client.get(f"{url.rstrip('/')}{path}")
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                print(f"  FAILED {filename}: {exc}", file=sys.stderr)
                continue
            try:
                data = resp.json()
            except json.JSONDecodeError:
                print(f"  FAILED {filename}: bad JSON")
                continue

            # /Items returns {"Items": [...]} — capture the first row
            # by default (single-item-shape fixtures); /Sessions is
            # already a list.
            if (
                isinstance(data, dict)
                and "Items" in data
                and isinstance(data["Items"], list)
                and data["Items"]
            ):
                data = data["Items"][0]

            text = json.dumps(data, indent=2)
            for pat, repl in REDACT_PATTERNS:
                text = pat.sub(repl, text)
            (out_dir / filename).write_text(text)
            print(f"  WROTE  {filename}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
