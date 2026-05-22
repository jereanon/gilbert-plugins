"""Scraper / page-parser tests.

The Andon FM page format is brittle, so we test against a captured
sample that mirrors the real shape (concatenated JS object literals
for each station, with currentBlock + stats + tweets nested in).
"""

from __future__ import annotations

from gilbert_plugin_andon_fm.scraper import parse_page
from gilbert_plugin_andon_fm.stations import BUNDLED_STATIONS

# Synthesized after a real fetch of https://andonlabs.com/radio — same
# shape, two stations, all the fields the parser cares about.
SAMPLE_PAGE = """
prefix junk... ,{id:"6b53fc38-ed57-4738-80d6-f9fddf981054",name:"Thinking Frequencies",subtitle:"",stationId:"6b53fc38-ed57-4738-80d6-f9fddf981054",streamUrl:"https://streaming.live365.com/a46431",imageUrl:"https://example/img.png",balance:32.8,twitterUsername:"andon_thinking",twitterAccountId:"abc",stats:{currentListeners:21,totalListeners:3359,avgDurationSeconds:678},contentStats:{categoryBreakdown:[]},
recentTweets:[{id:"111",content:"Spinning some chill jazz tonight",posted_at:"2026-05-19T01:00:00Z",author:{username:"andon_thinking"}},{id:"112",content:"Up next: ambient hour",posted_at:"2026-05-19T01:30:00Z",author:{username:"andon_thinking"}}],
currentBlock:{name:"The Late Night Lounge",description:"A cool, steady groove.",imageUrl:null,startedAt:"2026-05-19T02:15:28Z",durationMinutes:60}},
{id:"aab4d149-92fa-4386-9c1e-d938ecb66ee3",name:"Backlink Broadcast",subtitle:"",stationId:"aab4d149-92fa-4386-9c1e-d938ecb66ee3",streamUrl:"https://streaming.live365.com/a13541",imageUrl:"https://example/back.png",stats:{currentListeners:5,totalListeners:200,avgDurationSeconds:100},
currentBlock:{name:"Morning Mix",description:"Wake up tunes.",imageUrl:null,startedAt:"2026-05-19T08:00:00Z",durationMinutes:30}},
suffix junk
"""


def test_parse_page_extracts_both_stations() -> None:
    out = parse_page(SAMPLE_PAGE, BUNDLED_STATIONS)
    assert "6b53fc38-ed57-4738-80d6-f9fddf981054" in out
    assert "aab4d149-92fa-4386-9c1e-d938ecb66ee3" in out
    # Only the two stations in the sample show up — the other catalog
    # stations are silently absent.
    assert len(out) == 2


def test_parse_page_pulls_block_fields() -> None:
    out = parse_page(SAMPLE_PAGE, BUNDLED_STATIONS)
    tf = out["6b53fc38-ed57-4738-80d6-f9fddf981054"]
    assert tf.block.name == "The Late Night Lounge"
    assert tf.block.description.startswith("A cool, steady groove")
    assert tf.block.started_at == "2026-05-19T02:15:28Z"
    assert tf.block.duration_minutes == 60
    assert tf.listeners == 21
    # Two tweets in the sample
    assert len(tf.tweets) == 2
    assert tf.tweets[0]["content"].startswith("Spinning")


def test_parse_page_empty_or_garbage_returns_empty() -> None:
    assert parse_page("", BUNDLED_STATIONS) == {}
    assert parse_page("<html>nothing useful</html>", BUNDLED_STATIONS) == {}


def test_parse_page_skips_stations_with_missing_block() -> None:
    page = """
    {id:"6b53fc38-ed57-4738-80d6-f9fddf981054",name:"Thinking Frequencies",
    stationId:"6b53fc38-ed57-4738-80d6-f9fddf981054",streamUrl:"https://x",
    stats:{currentListeners:9}}
    """
    out = parse_page(page, BUNDLED_STATIONS)
    snap = out["6b53fc38-ed57-4738-80d6-f9fddf981054"]
    assert snap.listeners == 9
    assert snap.block.name == ""  # missing currentBlock → empty CurrentBlock
    assert snap.block.duration_minutes == 0
