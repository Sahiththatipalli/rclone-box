"""Browser tool: drives Playwright into the Box Admin Console.

Two entry paths:

* ``capture_admin_session()`` — CLI-only, opens a **headed** browser so a human
  admin can log into Box interactively. Saves the resulting storage state to
  disk. This is a credential — the file is gitignored and readers should treat
  it as such.

* The ``@beta_tool``-decorated functions below — used by the Claude agent at
  runtime, headless, re-using the saved session.

Selectors marked "SELECTOR" are best-effort against the Box Admin Console as
of authoring. Box changes UI regularly. When a selector breaks, the agent will
get an empty extraction and should fall back to ``browser_get_page_text`` /
``browser_screenshot`` and report the drift as a ``note`` finding rather than
silently fabricating results.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anthropic import beta_tool

log = logging.getLogger(__name__)

# Deferred import — Playwright is optional at import-time so unit tests that
# don't touch the browser can still exercise the rest of the package.
try:
    from playwright.sync_api import (
        BrowserContext,
        Page,
        Playwright,
        TimeoutError as PWTimeoutError,
        sync_playwright,
    )

    _PLAYWRIGHT_AVAILABLE = True
except Exception as e:  # pragma: no cover — environmental
    log.debug("playwright not importable: %s", e)
    _PLAYWRIGHT_AVAILABLE = False

    class PWTimeoutError(Exception):  # type: ignore[no-redef]
        pass


DEFAULT_ADMIN_URL = "https://app.box.com/master"
DEFAULT_NAV_TIMEOUT_MS = 30_000
BETWEEN_NAV_DELAY_S = 0.5  # Be polite to Box.
_MAX_TEXT_CHARS = 6_000


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


@dataclass
class BrowserConfig:
    admin_url: str
    storage_state_path: Path
    headless: bool

    @classmethod
    def from_env(cls, headless: bool = True) -> "BrowserConfig":
        return cls(
            admin_url=os.environ.get("BOX_ADMIN_URL", DEFAULT_ADMIN_URL),
            storage_state_path=Path(
                os.environ.get("BOX_STORAGE_STATE", "./storage_state.json")
            ).expanduser(),
            headless=headless,
        )


class BrowserSession:
    """Owns the Playwright objects for the process lifetime."""

    def __init__(self, cfg: BrowserConfig):
        if not _PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "playwright is not installed. Run `pip install -e .` and "
                "`playwright install chromium`."
            )
        self.cfg = cfg
        self._pw: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def start(self) -> None:
        if self._pw is not None:
            return
        if not self.cfg.storage_state_path.exists():
            raise RuntimeError(
                f"No saved Box admin session at {self.cfg.storage_state_path}. "
                "Run `rclone-box-auditor login` first."
            )
        self._pw = sync_playwright().start()
        # Prefer the pre-installed Chromium on the host if PLAYWRIGHT_BROWSERS_PATH is set.
        browser = self._pw.chromium.launch(headless=self.cfg.headless)
        self._context = browser.new_context(
            storage_state=str(self.cfg.storage_state_path),
            viewport={"width": 1440, "height": 900},
        )
        self._context.set_default_navigation_timeout(DEFAULT_NAV_TIMEOUT_MS)
        self._page = self._context.new_page()

    def stop(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        finally:
            if self._pw is not None:
                self._pw.stop()
            self._pw = None
            self._context = None
            self._page = None

    @property
    def page(self) -> Page:
        if self._page is None:
            self.start()
        assert self._page is not None
        return self._page


_session: BrowserSession | None = None


def get_session() -> BrowserSession:
    global _session
    if _session is None:
        _session = BrowserSession(BrowserConfig.from_env(headless=True))
        _session.start()
    return _session


def shutdown_session() -> None:
    global _session
    if _session is not None:
        _session.stop()
        _session = None


# ---------------------------------------------------------------------------
# Interactive login (called by the CLI, not by the agent)
# ---------------------------------------------------------------------------


def capture_admin_session(cfg: BrowserConfig | None = None) -> Path:
    """Open a headed browser, wait for the admin to log in, save session state.

    Blocks on user input at the end — the operator confirms they're signed
    into the Admin Console before we persist the session.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("playwright not installed — see README.")
    cfg = cfg or BrowserConfig.from_env(headless=False)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.goto(cfg.admin_url, wait_until="domcontentloaded")
        print(
            f"\nOpened {cfg.admin_url}. Sign in with your Box admin account.\n"
            "When you can see the Admin Console (users list, dashboard, etc.),\n"
            "come back here and press Enter to save the session.\n"
        )
        try:
            input("Press Enter after signing in> ")
        except EOFError:
            # Non-interactive fallback: give the admin a fixed window.
            print("No TTY detected. Waiting 90 seconds before capturing session.")
            time.sleep(90)
        cfg.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(cfg.storage_state_path))
        context.close()
    # 0600 so a nosy neighbor on a shared host doesn't grab it.
    try:
        os.chmod(cfg.storage_state_path, 0o600)
    except OSError:
        pass
    return cfg.storage_state_path


# ---------------------------------------------------------------------------
# Tools exposed to Claude
# ---------------------------------------------------------------------------


@beta_tool
def browser_open_admin_console() -> dict[str, Any]:
    """Navigate to the Box Admin Console using the saved admin session.

    Returns the resulting URL and page title so the agent can confirm we
    landed on an authenticated admin page (not a login redirect).
    """
    try:
        s = get_session()
        page = s.page
        page.goto(s.cfg.admin_url, wait_until="domcontentloaded")
        time.sleep(BETWEEN_NAV_DELAY_S)
        return {
            "url": page.url,
            "title": page.title(),
            "signed_in": "login" not in page.url.lower(),
        }
    except Exception as e:
        return {"error": True, "op": "open_admin_console", "message": str(e)}


@beta_tool
def browser_list_users(search: str = "", limit: int = 50) -> dict[str, Any]:
    """List users from the Box Admin Console → Users tab.

    Best-effort scrape of the user list. If Box has changed the UI and the
    extraction returns 0 users, call ``browser_get_page_text`` to inspect the
    current DOM and record a ``note`` finding about the drift.

    Args:
        search: Optional substring to search for (email or name).
        limit: Max users to return (default 50, hard cap 500).

    Returns:
        ``{url, users: [{name, email, id?}], truncated}``.
    """
    limit = max(1, min(int(limit), 500))
    try:
        s = get_session()
        page = s.page
        users_url = s.cfg.admin_url.rstrip("/") + "/users"
        page.goto(users_url, wait_until="domcontentloaded")
        time.sleep(BETWEEN_NAV_DELAY_S)

        # SELECTOR: Box Admin Console user rows. Update if UI changes.
        # Strategy: look for anchors whose href contains "/users/" (user detail),
        # capture the visible text nearest them.
        anchors = page.query_selector_all('a[href*="/master/users/"]')
        users: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for a in anchors:
            try:
                href = a.get_attribute("href") or ""
                m = re.search(r"/users/(\d+)", href)
                uid = m.group(1) if m else ""
                if uid in seen_ids:
                    continue
                text = (a.inner_text() or "").strip()
                if not text:
                    continue
                # Row text is often "Name\nemail@domain". Split on newline.
                parts = [p.strip() for p in text.splitlines() if p.strip()]
                name = parts[0] if parts else ""
                email = next((p for p in parts if "@" in p), "")
                if search:
                    hay = f"{name} {email}".lower()
                    if search.lower() not in hay:
                        continue
                users.append({"name": name, "email": email, "id": uid})
                seen_ids.add(uid)
                if len(users) >= limit:
                    break
            except Exception as inner:
                log.debug("skipping user row: %s", inner)

        return {
            "url": page.url,
            "users": users,
            "truncated": len(users) >= limit,
            "warning": (
                "No users extracted — Admin Console selectors may have drifted."
                if not users
                else None
            ),
        }
    except PWTimeoutError as e:
        return {"error": True, "op": "list_users", "message": f"navigation timeout: {e}"}
    except Exception as e:
        return {"error": True, "op": "list_users", "message": str(e)}


@beta_tool
def browser_open_user_content(user_id: str) -> dict[str, Any]:
    """Open a specific user's Content Manager view as an admin.

    Args:
        user_id: The Box user id from ``browser_list_users``.

    Returns landing URL, title, and a hint at how many top-level items are
    visible. To actually list them, call ``browser_list_current_folder``.
    """
    if not user_id or not user_id.isdigit():
        return {"error": True, "op": "open_user_content", "message": "numeric user_id required."}
    try:
        s = get_session()
        page = s.page
        # SELECTOR / URL: Box's admin user detail page.
        url = f"{s.cfg.admin_url.rstrip('/')}/users/{user_id}"
        page.goto(url, wait_until="domcontentloaded")
        time.sleep(BETWEEN_NAV_DELAY_S)
        return {"url": page.url, "title": page.title()}
    except Exception as e:
        return {"error": True, "op": "open_user_content", "message": str(e)}


@beta_tool
def browser_list_current_folder() -> dict[str, Any]:
    """Extract the folder listing visible on the current Admin Console page.

    Works on Content Manager and user-content views. Returns folder + file
    names as best-effort — if empty, the page structure changed and you should
    dump the raw text with ``browser_get_page_text``.
    """
    try:
        s = get_session()
        page = s.page
        # SELECTOR: try both grid and list layouts.
        items: list[dict[str, Any]] = []
        # Folders and files usually render as rows with data-testid or role=row.
        row_selectors = [
            '[data-testid*="item-row"]',
            '[role="row"]',
            "tr.item-row",
        ]
        rows = []
        for sel in row_selectors:
            rows = page.query_selector_all(sel)
            if rows:
                break
        for r in rows[:500]:
            try:
                text = (r.inner_text() or "").strip()
                if not text:
                    continue
                # Row text tends to be "Name\nType\nSize\nModified".
                parts = [p.strip() for p in text.splitlines() if p.strip()]
                name = parts[0] if parts else ""
                # Heuristic: folders don't have a size in bytes/KB/MB.
                is_folder = not any(
                    re.search(r"\b\d[\d,.]*\s*(B|KB|MB|GB|TB)\b", p, re.I) for p in parts
                )
                items.append({"name": name, "kind": "folder" if is_folder else "file"})
            except Exception:
                continue
        return {
            "url": page.url,
            "items": items,
            "count": len(items),
            "warning": (
                "No rows extracted — Admin Console selectors may have drifted."
                if not items
                else None
            ),
        }
    except Exception as e:
        return {"error": True, "op": "list_current_folder", "message": str(e)}


@beta_tool
def browser_get_page_text() -> dict[str, Any]:
    """Return the current page's URL, title, and visible text (truncated).

    Fallback for when a selector-based tool returned 0 rows. Never used to
    invent findings — use it to record what the DOM actually contains so a
    human can update selectors.
    """
    try:
        s = get_session()
        page = s.page
        body = page.query_selector("body")
        text = (body.inner_text() if body else "") or ""
        return {
            "url": page.url,
            "title": page.title(),
            "text": text[:_MAX_TEXT_CHARS],
            "truncated": len(text) > _MAX_TEXT_CHARS,
        }
    except Exception as e:
        return {"error": True, "op": "get_page_text", "message": str(e)}


@beta_tool
def browser_screenshot(label: str) -> dict[str, Any]:
    """Save a PNG screenshot of the current page as evidence.

    Args:
        label: Short slug used in the filename. Sanitized.

    Returns the on-disk path so you can cite it in a finding's ``evidence``.
    """
    # Deferred import to keep tools/report.py’s side effects out of tests.
    from rclone_box_auditor.tools.report import current_run

    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", label or "shot").strip("_")[:60] or "shot"
    run = current_run()
    shots_dir = run.run_dir / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    path = shots_dir / f"{stamp}-{safe}.png"
    try:
        s = get_session()
        s.page.screenshot(path=str(path), full_page=True)
        return {"saved": True, "path": str(path)}
    except Exception as e:
        return {"error": True, "op": "screenshot", "message": str(e)}


BROWSER_TOOLS = [
    browser_open_admin_console,
    browser_list_users,
    browser_open_user_content,
    browser_list_current_folder,
    browser_get_page_text,
    browser_screenshot,
]
