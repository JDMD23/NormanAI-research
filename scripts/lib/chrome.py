"""Read pages from JD's logged-in Chrome, via AppleScript.

Mirrors NormanAI-crm-core's `linkedin_aggregate/chrome_transport.py` — same
mechanism, same lease discipline — because a second, different way of driving
the same browser is how you get two jobs fighting over one window.

Scope is deliberately narrow: this module opens a URL and returns the page's
visible text. It does not parse. Parsing per-site DOM is what rots; the text
goes to Grok for extraction instead, so a site redesign costs nothing.

Mac + Chrome only. On any other host this raises ChromeUnavailable and the
caller skips the lane rather than failing the run.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

from lib.browser_coordination import (
    BrowserLeaseUnavailable,
    SharedBrowserLease,
)

MAX_CHARS = 400_000


class ChromeUnavailable(RuntimeError):
    """No Mac, no Chrome, or the lease is held — skip the lane, don't fail."""


class ChromeBlocked(RuntimeError):
    """Logged out, captcha, or an auth wall. Stop; don't push through."""


class PageNotReady(RuntimeError):
    """Rendered thin. Retryable — never treat as 'found nothing'."""


@contextmanager
def lease() -> Iterator[None]:
    """Hold Chrome exclusively so we never fight crm-core's lanes for it."""
    try:
        with SharedBrowserLease():
            yield
    except BrowserLeaseUnavailable as exc:
        raise ChromeUnavailable(
            "another browser job holds the Chrome lease — try again after it finishes"
        ) from exc


def _osascript(script: str, timeout: int = 30) -> str:
    try:
        proc = subprocess.run(
            ["osascript"], input=script, text=True, capture_output=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise ChromeUnavailable("osascript not found — this lane needs a Mac") from exc
    except subprocess.TimeoutExpired as exc:
        raise PageNotReady("Chrome did not respond in time") from exc
    if proc.returncode:
        raise ChromeUnavailable(f"Chrome not reachable: {proc.stderr.strip()}")
    return proc.stdout.strip()


def run_osascript(script: str, timeout: int = 60) -> str:
    """Run one AppleScript through the guarded Chrome transport.

    The strict saved-list reader uses this public boundary instead of its own
    subprocess implementation, so all Research Chrome work has one failure
    vocabulary and remains straightforward to fake in offline tests.
    """
    return _osascript(script, timeout=timeout)


def applescript_string_literal(value: str) -> str:
    """Return a safely quoted AppleScript string literal."""
    return json.dumps(value)


def ensure_chrome_running() -> None:
    """Ensure Chrome has a usable window without navigating anywhere."""
    run_osascript(
        'tell application "Google Chrome"\n'
        '  if (count of windows) = 0 then make new window\n'
        'end tell'
    )


def _js(expr: str) -> str:
    """Collapse a JS payload to one line for AppleScript.

    Strips `//` comments FIRST. sales-nav §10.10: a single `//` comment
    survived collapsing and commented out the rest of the payload, including
    the closing braces — every scan died on a syntax error for a day.
    """
    without_comments = re.sub(r"//[^\n]*", "", expr)
    return " ".join(without_comments.split())


_READ = _js(
    """
JSON.stringify((() => {
  const body = document.body ? document.body.innerText : '';
  return {
    title: document.title || '',
    url: window.location.href || '',
    body: body.slice(0, %d),
    length: body.length
  };
})())
"""
    % MAX_CHARS
)

# Phrases that mean "you are not logged in", not "there is nothing here".
_AUTH_WALL = (
    "sign in to continue",
    "log in to continue",
    "please log in",
    "create a free account to",
    "verify you are human",
    "complete the security check",
    "enable javascript and cookies",
)


def open_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise ValueError(f"https only, got: {url}")
    script = (
        'tell application "Google Chrome"\n'
        "activate\n"
        "if (count of windows) = 0 then make new window\n"
        f"set URL of active tab of window 1 to {json.dumps(url)}\n"
        "end tell\n"
    )
    _osascript(script)


def read_page() -> dict:
    script = (
        'tell application "Google Chrome"\n'
        f"execute active tab of window 1 javascript {json.dumps(_READ)}\n"
        "end tell\n"
    )
    raw = _osascript(script)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PageNotReady(f"unreadable page payload: {raw[:200]}") from exc
    if not isinstance(payload, dict):
        raise PageNotReady("page payload was not an object")
    return payload


def fetch(url: str, *, settle: float = 3.0, attempts: int = 3, min_chars: int = 500) -> dict:
    """Open a URL and return {title, url, body}. Retries thin renders.

    A short body is treated as not-yet-rendered rather than as an empty result.
    sales-nav §10.1 is the cautionary tale: reading the DOM before it settled
    produced confident empty answers that overwrote good data.
    """
    open_url(url)
    last: Exception | None = None
    for attempt in range(attempts):
        time.sleep(settle * (attempt + 1))
        try:
            page = read_page()
        except PageNotReady as exc:
            last = exc
            continue

        body = str(page.get("body") or "")
        low = body[:4000].lower()
        if any(phrase in low for phrase in _AUTH_WALL):
            raise ChromeBlocked(f"auth wall or captcha at {url} — log in and re-run")
        if len(body.strip()) >= min_chars:
            return {
                "title": str(page.get("title") or ""),
                "url": str(page.get("url") or url),
                "body": body,
                "truncated": int(page.get("length") or 0) > MAX_CHARS,
            }
        last = PageNotReady(f"only {len(body.strip())} chars from {url}")

    raise last or PageNotReady(f"no usable content from {url}")
