"""Browser control through Playwright.

The desktop layer could technically drive a browser through UI Automation,
but it would be reading a rendered picture of a page that already knows its
own structure. Playwright hands over the DOM directly - real link targets,
real form fields, real disabled states - so this is a separate, better path
rather than a special case of `uia`.

Session model: a persistent profile of our own, under the app's config
directory. The user signs into whatever they need once, in a visible window,
and those sessions survive restarts. Their everyday Chrome profile is left
alone - attaching to it would lock it while Chrome runs and would hand the
agent every account they have open, which is far more than any of this needs.

Everything a page says is untrusted. A web page is written by someone else,
so its text is data and never instruction - the same rule documents already
live under, and it matters more here because a page can be authored to be
read by exactly this kind of agent.
"""
from __future__ import annotations

import logging
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("browser")

NAV_TIMEOUT = 30_000
CALL_TIMEOUT = 75.0
MAX_ELEMENTS = 60
MAX_TEXT = 6000

# Same principle as the desktop layer: confirm what cannot be undone, let
# ordinary navigation flow. Buying something is the case that matters here.
DANGEROUS = re.compile(
    r"\b("
    r"buy|purchase|order|pay|checkout|subscribe|confirm|place\s*order|"
    r"delete|remove|unsubscribe|cancel\s*(account|subscription)|"
    r"send|submit|post|publish|sign\s*out|log\s*out|"
    r"куп|оплат|заказ|подтверд|удал|отправ|подписа|оформить|"
    r"sotib|tola|buyurtma|tasdiq|ochir|yubor"
    r")", re.IGNORECASE,
)

INTERACTIVE_SELECTOR = (
    "a[href], button, input:not([type=hidden]), textarea, select, "
    "[role=button], [role=link], [role=textbox], [role=checkbox], [role=tab], "
    "[onclick], [contenteditable=true]"
)


def available() -> bool:
    import importlib.util
    return bool(importlib.util.find_spec("playwright"))


# Kept for config compatibility; the browser now has one mode - its own
# persistent Chrome profile. "Attach to your live Chrome" was removed because
# Chrome 136+ blocks remote debugging on the default profile, so it could not
# work on a current Chrome, and lifting the user's cookie/login files into a
# copy is not something this should do.
MODE = "profile"
CDP_PORT = 9222
CHROME_USER_DATA = ""
CHROME_PROFILE = "Default"


def status() -> str:
    if not available():
        return "o'rnatilmagan — pip install playwright && playwright install chromium"
    return "tayyor"


# ------------------------------------------------------------- worker thread

class _Worker:
    """Playwright's sync API refuses to run inside an asyncio loop, so it gets
    its own thread - and keeping the browser pinned to one thread is required
    anyway."""

    def __init__(self) -> None:
        self._jobs: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, daemon=True, name="browser")
            self._thread.start()

    def _loop(self) -> None:
        while True:
            fn, box = self._jobs.get()
            try:
                box["result"] = fn()
            except Exception as exc:
                box["error"] = exc
            finally:
                box["done"].set()

    def call(self, fn: Callable[[], Any], timeout: float = CALL_TIMEOUT) -> Any:
        self._ensure()
        box: dict[str, Any] = {"done": threading.Event()}
        self._jobs.put((fn, box))
        if not box["done"].wait(timeout):
            raise TimeoutError("Brauzer javob bermadi")
        if "error" in box:
            raise box["error"]
        return box.get("result")


_worker = _Worker()


# ---------------------------------------------------------------- session

@dataclass
class _State:
    playwright: Any = None
    browser: Any = None       # the connected Browser in CDP mode
    context: Any = None
    page: Any = None
    channel: str = ""
    mode: str = ""
    cdp_proc: Any = None      # the Chrome we launched, if any
    restarted: bool = False   # did we just restart the user's Chrome
    elements: list[dict] = field(default_factory=list)


_state = _State()


def _profile_dir():
    from .config import config_dir

    d = config_dir() / "browser-profile"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _clear_stale_lock() -> None:
    """Remove a leftover Chrome singleton lock in the agent's own profile.

    If the agent crashed with its browser open, a SingletonLock stays behind
    and the next launch_persistent_context fails with "profile in use" - which
    would leave the browser permanently broken until the folder is cleaned by
    hand. On Windows these are ordinary files/junctions we own, safe to delete;
    Chrome recreates them.
    """
    import os

    d = _profile_dir()
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            target = d / name
            if target.exists() or target.is_symlink():
                target.unlink()
        except OSError:
            pass


# Visible by default: the user signs in through this window, and seeing what
# is being done on their behalf is a feature, not overhead. Settings can
# flip it for a machine nobody is sitting at.
HEADLESS = False

# Prefer the real Chrome install over Playwright's bundled Chromium. The
# bundled build is "Chrome for Testing" - it renders and networks slightly
# differently, has no extensions, and here it could not reach eclass.uz at
# all. channel="chrome" drives the actual Google Chrome on the machine, so
# pages behave the way the user expects. Falls back to bundled Chromium when
# no real Chrome is installed.
CHANNEL = "chrome"


def _ensure_page(headless: bool | None = None):
    """One browser, one page, reused across calls.

    Always the agent's OWN Chrome profile, driven by Playwright through the
    real Chrome binary. This is the only reliable option: Chrome 136+ refuses
    remote debugging on the user's default profile (a security fix), so the old
    "attach to your live Chrome" mode could not work on a current Chrome and
    is gone. The user signs into a site once in this window and the session
    persists here forever - their normal Chrome is never touched or closed.
    """
    if _state.page is not None and not _state.page.is_closed():
        return _state.page

    from playwright.sync_api import sync_playwright

    if _state.playwright is None:
        _state.playwright = sync_playwright().start()

    headless = HEADLESS if headless is None else headless
    common = dict(
        user_data_dir=str(_profile_dir()),
        headless=headless,
        viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled",
              "--no-first-run", "--no-default-browser-check"],
    )
    _clear_stale_lock()
    try:
        _state.context = _state.playwright.chromium.launch_persistent_context(
            channel=CHANNEL, **common
        )
        _state.channel = CHANNEL
    except Exception as exc:
        # No real Chrome, or it is briefly locked - fall back to bundled Chromium.
        log.info("channel %r unavailable (%s); using bundled Chromium", CHANNEL, exc)
        _clear_stale_lock()
        _state.context = _state.playwright.chromium.launch_persistent_context(**common)
        _state.channel = "chromium"

    _state.mode = "profile"
    _state.context.set_default_timeout(NAV_TIMEOUT)
    _state.page = _state.context.pages[0] if _state.context.pages else _state.context.new_page()
    return _state.page


def close() -> dict:
    def job():
        try:
            if _state.context is not None:
                _state.context.close()
        except Exception:
            pass
        try:
            if _state.playwright is not None:
                _state.playwright.stop()
        except Exception:
            pass
        _state.playwright = _state.browser = _state.context = _state.page = None
        _state.mode = ""
        _state.elements = []
        return {"ok": True}

    try:
        return _worker.call(job, timeout=30)
    except Exception as exc:
        return {"error": str(exc)[:150]}


# ------------------------------------------------------------------ actions

def open_url(url: str) -> dict:
    if not available():
        return {"error": status()}
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")

    def job():
        page = _ensure_page()
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        page.wait_for_timeout(700)          # let the usual client-side render settle
        out = _describe(page)
        if _needs_login(page):
            out["login_hint"] = (
                "Bu sayt hali kirilmagan. Brauzer oynasi ochiq — bir marta "
                "kiring (login), keyin sessiya saqlanadi va qayta so'ramaydi."
            )
        return out

    return _run(job)


_LOGIN_SIGNS = ("log in", "sign in", "войти", "kirish", "log into",
                "authorization required", "please sign in")


def _needs_login(page) -> bool:
    """Rough guess that a page is showing a login wall, so the agent can tell
    the user to sign in once in the visible window rather than looping."""
    try:
        title = (page.title() or "").lower()
        if any(s in title for s in ("log in", "sign in", "login")):
            return True
        body = (page.inner_text("body") or "")[:600].lower()
        return sum(s in body for s in _LOGIN_SIGNS) >= 1 and len(body) < 400
    except Exception:
        return False


def read_page() -> dict:
    if not available():
        return {"error": status()}

    def job():
        page = _ensure_page()
        return _describe(page, with_text=True)

    return _run(job)


def elements() -> dict:
    if not available():
        return {"error": status()}

    def job():
        page = _ensure_page()
        return {"url": page.url, "elements": _collect(page)}

    return _run(job)


def click(ref: int) -> dict:
    def job():
        page = _ensure_page()
        target = _by_ref(ref)
        if target is None:
            return {"error": f"[{ref}] topilmadi. Avval sahifani qayta o'qing."}
        handles = page.query_selector_all(INTERACTIVE_SELECTOR)
        if target["index"] >= len(handles):
            return {"error": "Sahifa o'zgargan — qayta o'qing."}
        before = _fingerprint(page)
        handles[target["index"]].click(timeout=15_000)
        page.wait_for_timeout(900)
        return {"ok": True, "clicked": target["label"],
                **_effect(page, before), **_describe(page)}

    return _run(job)


def type_text(ref: int, text: str, submit: bool = False) -> dict:
    def job():
        page = _ensure_page()
        target = _by_ref(ref)
        if target is None:
            return {"error": f"[{ref}] topilmadi. Avval sahifani qayta o'qing."}
        handles = page.query_selector_all(INTERACTIVE_SELECTOR)
        if target["index"] >= len(handles):
            return {"error": "Sahifa o'zgargan — qayta o'qing."}
        field = handles[target["index"]]
        before = _fingerprint(page)
        field.fill(text, timeout=15_000)
        result: dict[str, Any] = {"ok": True, "typed_into": target["label"]}
        if submit:
            field.press("Enter")
            page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
            page.wait_for_timeout(1200)
            result.update(_effect(page, before))
        result.update(_describe(page))
        return result

    return _run(job)


def _fingerprint(page) -> tuple[str, str]:
    try:
        return page.url, (page.title() or "")
    except Exception:
        return "", ""


def _effect(page, before: tuple[str, str]) -> dict:
    """Did that actually do anything?

    A form filled into a decoy field reports a perfectly successful fill and
    then nothing happens - the agent would tell the user it had searched when
    it had not. Comparing the page before and after turns a silent no-op into
    something the model can notice and retry.
    """
    after = _fingerprint(page)
    if after == before:
        return {
            "changed": False,
            "warning": (
                "Sahifa o'zgarmadi — bu element kerakli tugma/maydon "
                "bo'lmasligi mumkin. Boshqa raqamni sinab ko'ring."
            ),
        }
    return {"changed": True}


def go_back() -> dict:
    def job():
        page = _ensure_page()
        page.go_back(wait_until="domcontentloaded")
        page.wait_for_timeout(600)
        return _describe(page)

    return _run(job)


def screenshot() -> dict:
    """A picture for the PERSON to look at on their phone - the model never
    sees it. Cheaper and more honest than pretending to read pixels."""
    def job():
        from .office import _work_dir

        page = _ensure_page()
        target = _work_dir() / "page.png"
        page.screenshot(path=str(target), full_page=False)
        return {"ok": True, "path": str(target), "url": page.url}

    return _run(job)


def _run(job) -> dict:
    if not available():
        return {"error": status()}
    try:
        return _worker.call(job)
    except TimeoutError:
        return {"error": "Brauzer javob bermadi (sahifa og'ir bo'lishi mumkin)."}
    except Exception as exc:
        return {"error": f"Brauzer xatosi: {str(exc).splitlines()[0][:180]}"}


# ------------------------------------------------------------------ helpers

def _describe(page, with_text: bool = False) -> dict:
    out: dict[str, Any] = {
        "url": page.url,
        "title": (page.title() or "")[:120],
        "elements": _collect(page),
    }
    if with_text:
        out["text"] = _page_text(page)
    return out


def _collect(page) -> str:
    """Interactive elements as a numbered list, mirroring the desktop layer so
    the model only has to learn one pattern."""
    try:
        handles = page.query_selector_all(INTERACTIVE_SELECTOR)
    except Exception:
        return "(sahifa o'qilmadi)"

    lines: list[str] = []
    seen: set[tuple] = set()
    _state.elements = []
    for index, handle in enumerate(handles):
        if len(lines) >= MAX_ELEMENTS:
            lines.append(f"... (yana elementlar bor, {MAX_ELEMENTS} tasi ko'rsatildi)")
            break
        try:
            if not handle.is_visible():
                continue
            tag = handle.evaluate("e => e.tagName.toLowerCase()")
            label = _label_of(handle, tag)
            if not label:
                continue
            key = (tag, label.lower())
            if key in seen:
                continue
            seen.add(key)
            ref = len(_state.elements) + 1
            _state.elements.append({"ref": ref, "index": index, "label": label, "tag": tag})
            extra = "" if handle.is_enabled() else " (o'chiq)"
            lines.append(f'[{ref}] {tag} "{label}"{extra}')
        except Exception:
            continue
    return "\n".join(lines) if lines else "(interaktiv element topilmadi)"


# Resolves the label a person would read: the <label> pointing at this field,
# aria-labelledby, then the usual attributes. Without this a search box comes
# back as "(maydon)" and the model has to guess which blank field to use.
_LABEL_JS = """
(el) => {
  const txt = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  if (el.id) {
    const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
    if (lab && txt(lab.innerText)) return txt(lab.innerText);
  }
  const closest = el.closest('label');
  if (closest && txt(closest.innerText)) return txt(closest.innerText);
  const by = el.getAttribute('aria-labelledby');
  if (by) {
    const parts = by.split(/\\s+/).map(id => document.getElementById(id))
                    .filter(Boolean).map(n => txt(n.innerText));
    if (parts.join(' ').trim()) return txt(parts.join(' '));
  }
  return '';
}
"""


def _label_of(handle, tag: str) -> str:
    getters = [
        lambda: handle.get_attribute("aria-label"),
        lambda: handle.evaluate(_LABEL_JS),
        lambda: handle.inner_text(),
        lambda: handle.get_attribute("placeholder"),
        lambda: handle.get_attribute("title"),
        lambda: handle.get_attribute("value"),
        lambda: handle.get_attribute("name"),
    ]
    for getter in getters:
        try:
            text = re.sub(r"\s+", " ", (getter() or "").strip())
        except Exception:
            continue
        if text:
            return text[:60]

    if tag in ("input", "textarea"):
        # An unlabelled field is still usable if its purpose is stated.
        try:
            kind = handle.get_attribute("type") or "text"
        except Exception:
            kind = "text"
        return f"({kind} maydoni)"
    return ""


# Strip the furniture before reading. Taking `main`'s innerText verbatim gave
# a Wikipedia article that opened with "172 languages / Article / Talk /
# Edit / Tools" - navigation the reader did not ask for, charged as tokens.
_EXTRACT_JS = """
() => {
  const CHROME = 'nav, header, footer, aside, script, style, noscript,' +
    '[role=navigation], [role=banner], [role=complementary],' +
    '.navbox, .sidebar, .vector-menu, .mw-editsection, .reflist, .mw-jump-link';
  const clean = (el) => {
    const copy = el.cloneNode(true);
    copy.querySelectorAll(CHROME).forEach(n => n.remove());
    return (copy.innerText || '').trim();
  };
  // A fixed selector order is fragile: on Wikipedia the first
  // .mw-parser-output match is a nested coordinates box, which yielded 83
  // characters for a long article. Score the candidates and keep the fullest.
  const seen = new Set();
  let best = '';
  for (const sel of ['article', '[role=main]', 'main', '.mw-parser-output',
                     '#content', '#main', 'body']) {
    for (const el of document.querySelectorAll(sel)) {
      if (seen.has(el)) continue;
      seen.add(el);
      const text = clean(el);
      if (text.length > best.length) best = text;
    }
    if (best.length > 2000) break;   // good enough, stop paying for more
  }
  return best;
}
"""


def _page_text(page) -> str:
    try:
        text = page.evaluate(_EXTRACT_JS) or ""
    except Exception:
        try:
            text = page.inner_text("body")
        except Exception:
            return ""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:MAX_TEXT]


def _by_ref(ref: int) -> dict | None:
    return next((e for e in _state.elements if e["ref"] == int(ref)), None)


def is_dangerous(label: str) -> bool:
    return bool(DANGEROUS.search(label or ""))
