"""Tests for LoginRunner."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from gilbert_plugin_browser.credentials import BrowserCredential
from gilbert_plugin_browser.login_runner import LoginRunner


def _page_with_locator_count(counts_by_selector: dict[str, int]):
    """Make a Page where locator(s).count() returns the configured int and
    locator(s) supports .fill / .click / .press."""
    page = MagicMock()
    page.url = "https://x.test/dashboard"
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()

    locators: dict[str, MagicMock] = {}

    def make_locator(selector: str):
        if selector not in locators:
            loc = MagicMock()
            loc.count = AsyncMock(return_value=counts_by_selector.get(selector, 0))
            loc.fill = AsyncMock()
            loc.click = AsyncMock()
            loc.press = AsyncMock()
            locators[selector] = loc
        return locators[selector]

    page.locator = MagicMock(side_effect=make_locator)
    page._locators = locators  # exposed for test assertions
    return page


@pytest.mark.asyncio
async def test_uses_explicit_selectors_when_provided():
    page = _page_with_locator_count({})
    runner = LoginRunner(page)
    cred = BrowserCredential(
        user_id="u",
        site="x",
        label="",
        username="alice",
        password="p",
        login_url="https://x.test/login",
        username_selector="#email",
        password_selector="#password",
        submit_selector="#submit",
    )
    ok, msg = await runner.run(cred)
    assert ok, msg
    page.goto.assert_awaited_with("https://x.test/login", wait_until="load", timeout=30_000)
    page.locator.assert_any_call("#email")
    page.locator.assert_any_call("#password")
    page.locator.assert_any_call("#submit")


@pytest.mark.asyncio
async def test_falls_back_to_heuristic_selectors():
    counts = {
        "input[type=email]": 1,
        "input[type=password]": 1,
        "button[type=submit]": 1,
    }
    page = _page_with_locator_count(counts)
    runner = LoginRunner(page)
    cred = BrowserCredential(
        user_id="u",
        site="x",
        label="",
        username="alice",
        password="p",
        login_url="https://x.test/login",
    )
    ok, _ = await runner.run(cred)
    assert ok
    # Heuristic match was probed.
    page.locator.assert_any_call("input[type=email]")
    page.locator.assert_any_call("input[type=password]")
    page.locator.assert_any_call("button[type=submit]")


@pytest.mark.asyncio
async def test_returns_error_when_no_username_input_found():
    counts = {"input[type=password]": 1}  # password yes, username no
    page = _page_with_locator_count(counts)
    runner = LoginRunner(page)
    cred = BrowserCredential(
        user_id="u",
        site="x",
        label="",
        username="alice",
        password="p",
        login_url="https://x.test/login",
    )
    ok, msg = await runner.run(cred)
    assert not ok
    assert "username" in msg.lower()


@pytest.mark.asyncio
async def test_missing_login_url_fails_fast():
    page = _page_with_locator_count({})
    runner = LoginRunner(page)
    cred = BrowserCredential(
        user_id="u", site="x", label="", username="a", password="p"
    )
    ok, msg = await runner.run(cred)
    assert not ok
    assert "login_url" in msg.lower()


@pytest.mark.asyncio
async def test_falls_back_to_enter_when_no_submit_button():
    counts = {
        "input[type=email]": 1,
        "input[type=password]": 1,
        # No submit button matches.
    }
    page = _page_with_locator_count(counts)
    runner = LoginRunner(page)
    cred = BrowserCredential(
        user_id="u",
        site="x",
        label="",
        username="alice",
        password="p",
        login_url="https://x.test/login",
    )
    ok, _ = await runner.run(cred)
    assert ok
    # The Enter-press fallback kicks in via the password locator.
    page._locators["input[type=password]"].press.assert_awaited_with("Enter")
