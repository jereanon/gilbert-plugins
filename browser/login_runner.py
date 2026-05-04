"""Best-effort form auto-fill for browser_login.

Given a credential row and a Playwright Page, navigate to the login
URL, locate username + password fields by configured selector or by
heuristic, fill them, click submit, and wait for navigation. Returns
``(success: bool, message: str)``.
"""

from __future__ import annotations

import logging
from typing import Any

from .credentials import BrowserCredential

logger = logging.getLogger(__name__)


_USERNAME_HEURISTICS = [
    "input[autocomplete=username]",
    "input[type=email]",
    "input[name*=email i]",
    "input[name*=user i]",
    "input[id*=email i]",
    "input[id*=user i]",
]
_PASSWORD_HEURISTICS = [
    "input[type=password]",
    "input[autocomplete=current-password]",
    "input[name*=password i]",
    "input[id*=password i]",
]
_SUBMIT_HEURISTICS = [
    "button[type=submit]",
    "input[type=submit]",
    "button[name*=login i]",
    "button:has-text('Sign in')",
    "button:has-text('Log in')",
    "button:has-text('Login')",
]


class LoginRunner:
    def __init__(self, page: Any) -> None:
        self._page = page

    async def run(self, cred: BrowserCredential) -> tuple[bool, str]:
        if not cred.login_url:
            return False, "credential is missing a login_url"

        try:
            await self._page.goto(cred.login_url, wait_until="load", timeout=30_000)
        except Exception as exc:
            return False, f"failed to load login URL: {exc}"

        username_sel = cred.username_selector or await self._first_match(
            _USERNAME_HEURISTICS
        )
        if not username_sel:
            return (
                False,
                "no username input detected; configure username_selector "
                "or use VNC live login for this site",
            )

        password_sel = cred.password_selector or await self._first_match(
            _PASSWORD_HEURISTICS
        )
        if not password_sel:
            return (
                False,
                "no password input detected; configure password_selector "
                "or use VNC live login for this site",
            )

        try:
            await self._page.locator(username_sel).fill(cred.username, timeout=10_000)
            await self._page.locator(password_sel).fill(cred.password, timeout=10_000)
        except Exception as exc:
            return False, f"failed to fill credentials: {exc}"

        submit_sel = cred.submit_selector or await self._first_match(
            _SUBMIT_HEURISTICS
        )
        try:
            if submit_sel:
                await self._page.locator(submit_sel).click(timeout=10_000)
            else:
                # No obvious submit button — press Enter in the password
                # field, which most login forms accept.
                await self._page.locator(password_sel).press("Enter")
        except Exception as exc:
            return False, f"failed to submit form: {exc}"

        try:
            await self._page.wait_for_load_state("load", timeout=15_000)
        except Exception:
            # Many sites navigate via XHR + history.pushState rather than
            # full reload. Don't fail the login on that — the cookies are
            # already in the BrowserContext if the request succeeded.
            pass

        return True, f"login attempted; current url: {self._page.url}"

    async def _first_match(self, selectors: list[str]) -> str:
        """Return the first selector with at least one matching element."""
        for sel in selectors:
            try:
                count = await self._page.locator(sel).count()
            except Exception:
                continue
            if count > 0:
                return sel
        return ""
