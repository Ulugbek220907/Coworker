"""Browser control through Playwright, in this process, on one pinned thread.

The desktop layer could technically drive a browser through UI Automation,
but it would be reading a rendered picture of a page that already knows its
own structure. Playwright hands over the DOM directly - real link targets,
real form fields, real disabled states - so this is a separate, better path
rather than a special case of `uia`.

Session model: a persistent profile of our own, under the app's config
directory (COWORKER_HOME once config_dir honours it). The user signs into
whatever they need once, in a visible window, and those sessions survive
restarts. Their everyday Chrome profile is left alone - attaching to it would
lock it while Chrome runs and would hand the agent every account they have
open, which is far more than any of this needs.

Everything a page says is untrusted. A web page is written by someone else,
so its text is data and never instruction - the same rule documents already
live under, and it matters more here because a page can be authored to be
read by exactly this kind of agent.

Element references are fingerprints, not positions. A listing gives each
control a ref computed from its stable DOM path and its label. Before an
action the path is resolved again and the label is read from that same node;
if either changed, the action is refused and the page has to be read again.
A positional index would silently point at another control after any
re-render.

Every request the page makes goes through the URL guard the tool layer passes
in, not only the first address, because a public page can redirect to a
private one. The guard checks the text of an address and resolves it, but Chrome
resolves the name again for the connection, and a name can answer differently the
second time (DNS rebinding). So every connection the browser makes is carried by
PinnedProxy, a SOCKS5 proxy on this machine: Chrome gives it the host name, the
proxy resolves it once, refuses unless every address is public, and connects to
one of those same addresses. Service workers are not allowed to run, so their
requests cannot bypass the guard either.

Dialogs are never accepted or dismissed silently: each one is recorded, sent
back with the next result, and dismissed, since dismissing approves nothing.
Downloads are switched off. The browser does not try to disguise that it is
automated; getting past bot detection is not something this agent does.

Isolation is deferred: Playwright runs in this process, so a renderer crash or
a hung driver can take the agent down with it. Running the browser in a child
process belongs to the governor's child model and is not built yet; until then
the BROWSER pool bounds how many calls run at once.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import queue
import re
import socket
import socketserver
import struct
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .policy import urls

log = logging.getLogger("browser")

NAV_TIMEOUT = 30_000
CALL_TIMEOUT = 75.0
MAX_ELEMENTS = 60
MAX_TEXT = 6000
MAX_DIALOGS = 10
CHANNEL = "chrome"
VIEWPORT = {"width": 1280, "height": 900}
ABORT_ERROR = "blockedbyclient"
CONNECT_TIMEOUT = 15.0
IDLE_TIMEOUT = 300.0
_PUMP_CHUNK = 65536

INTERACTIVE_SELECTOR = (
    "a[href], button, input:not([type=hidden]), textarea, select, "
    "[role=button], [role=link], [role=textbox], [role=checkbox], [role=tab], "
    "[onclick], [contenteditable=true]"
)


def available() -> bool:
    import importlib.util
    return bool(importlib.util.find_spec("playwright"))


def status() -> str:
    if not available():
        return "o'rnatilmagan — pip install playwright && playwright install chromium"
    return "tayyor"


# ------------------------------------------------------------- worker thread

class _Worker:
    """Playwright's sync API refuses to run inside an asyncio loop and must stay
    on the thread that started it, so each session owns one thread for its
    whole life. The governor's BROWSER pool already serialises callers; this
    only pins the thread."""

    def __init__(self, name: str) -> None:
        self._jobs: queue.Queue = queue.Queue()
        self._name = name
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, daemon=True, name=self._name)
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


# ------------------------------------------------------------ pinned network

SOCKS_VERSION = 5
_SOCKS_OK = 0x00
_SOCKS_NOT_ALLOWED = 0x02      # the destination is not public, or not a web address
_SOCKS_UNREACHABLE = 0x04      # the name did not resolve
_SOCKS_REFUSED = 0x05          # no address accepted the connection
_SOCKS_COMMAND = 0x07          # only CONNECT is offered
_SOCKS_ADDRESS = 0x08          # the address type is not supported


class _Reply(Exception):
    """A request the proxy refuses. ``code`` is the SOCKS5 reply the browser receives."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class PinnedProxy:
    """A SOCKS5 proxy on 127.0.0.1 that carries every connection the browser makes.

    Chrome hands a SOCKS proxy a host name, not an address, so Chrome does not
    resolve the name itself. This proxy resolves it once, refuses unless every
    address is public, and connects to one of those same addresses. A second,
    later lookup by the browser would be a second answer, and that is the gap DNS
    rebinding uses; with this proxy there is no such lookup. Every request is
    covered, including those of service workers and WebSockets.

    ``resolve`` returns the public addresses of a host or raises ``urls.UrlRefused``;
    the default is ``urls.resolve_and_pin``, the same check web_fetch uses.
    """

    def __init__(self, resolve: Callable[[str], list[str]] = urls.resolve_and_pin) -> None:
        self._resolve = resolve
        self._server: Optional[socketserver.ThreadingTCPServer] = None

    def start(self) -> int:
        """Listen on a free local port and return it. A running proxy keeps its port."""
        if self._server is None:
            proxy = self

            class Handler(socketserver.BaseRequestHandler):
                def handle(self) -> None:
                    proxy._serve(self.request)

            class Server(socketserver.ThreadingTCPServer):
                daemon_threads = True

            self._server = Server(("127.0.0.1", 0), Handler)
            threading.Thread(target=self._server.serve_forever, name="browser-proxy", daemon=True).start()
        return int(self._server.server_address[1])

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()

    def _serve(self, sock: socket.socket) -> None:
        sock.settimeout(CONNECT_TIMEOUT)
        upstream: Optional[socket.socket] = None
        try:
            host, port = _read_request(sock)
            upstream = self._connect(host, port)
            sock.sendall(_reply(_SOCKS_OK))
            _relay(sock, upstream)
        except _Reply as refusal:
            _send_quietly(sock, _reply(refusal.code))
        except (OSError, ValueError):
            pass                              # a client that broke off; nothing to report
        finally:
            if upstream is not None:
                upstream.close()

    def _connect(self, host: str, port: int) -> socket.socket:
        refused = False
        for address in self._pin(host, port):
            try:
                return socket.create_connection((address, port), timeout=CONNECT_TIMEOUT)
            except ConnectionRefusedError:
                refused = True
            except OSError:
                continue
        raise _Reply(_SOCKS_REFUSED if refused else _SOCKS_UNREACHABLE)

    def _pin(self, host: str, port: int) -> list[str]:
        """The addresses to connect to, every one of them public. Raises _Reply otherwise."""
        if not 0 < port < 65536:
            raise _Reply(_SOCKS_NOT_ALLOWED)
        literal = _ip_literal(host)
        if literal is not None:
            if urls.check_url(f"http://{_authority(literal)}:{port}/") is not None:
                raise _Reply(_SOCKS_NOT_ALLOWED)
            return [str(literal)]
        if urls.check_url(f"http://{host}:{port}/") is not None:
            raise _Reply(_SOCKS_NOT_ALLOWED)
        try:
            return list(self._resolve(host))
        except urls.UrlRefused:
            raise _Reply(_SOCKS_NOT_ALLOWED)
        except OSError:
            raise _Reply(_SOCKS_UNREACHABLE)


def _read_request(sock: socket.socket) -> tuple[str, int]:
    """The destination of a SOCKS5 CONNECT request. Only the no-authentication method is offered."""
    version, count = _recv_exact(sock, 2)
    methods = _recv_exact(sock, count)
    if version != SOCKS_VERSION or 0x00 not in methods:
        sock.sendall(bytes([SOCKS_VERSION, 0xFF]))
        raise ValueError("no acceptable authentication method")
    sock.sendall(bytes([SOCKS_VERSION, 0x00]))
    version, command, _reserved, kind = _recv_exact(sock, 4)
    if version != SOCKS_VERSION:
        raise ValueError("unexpected SOCKS version in the request")
    if kind == 0x01:
        host = socket.inet_ntoa(_recv_exact(sock, 4))
    elif kind == 0x04:
        host = str(ipaddress.IPv6Address(_recv_exact(sock, 16)))
    elif kind == 0x03:
        host = _recv_exact(sock, _recv_exact(sock, 1)[0]).decode("ascii")
    else:
        raise _Reply(_SOCKS_ADDRESS)
    (port,) = struct.unpack("!H", _recv_exact(sock, 2))
    if command != 0x01:
        raise _Reply(_SOCKS_COMMAND)
    return host, port


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ValueError("the client closed the connection early")
        data += chunk
    return bytes(data)


def _reply(code: int) -> bytes:
    """A SOCKS5 reply. The bound address is never meaningful here, so it is zero."""
    return bytes([SOCKS_VERSION, code, 0x00, 0x01]) + bytes(6)


def _send_quietly(sock: socket.socket, data: bytes) -> None:
    try:
        sock.sendall(data)
    except OSError:
        pass


def _ip_literal(host: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _authority(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    return f"[{address}]" if address.version == 6 else str(address)


def _relay(client: socket.socket, upstream: socket.socket) -> None:
    """Copy bytes both ways until both directions are finished. A half-close is passed on."""
    client.settimeout(IDLE_TIMEOUT)
    upstream.settimeout(IDLE_TIMEOUT)
    back = threading.Thread(target=_pump, args=(upstream, client), name="browser-proxy-pump", daemon=True)
    back.start()
    _pump(client, upstream)
    back.join()


def _pump(source: socket.socket, target: socket.socket) -> None:
    try:
        while True:
            data = source.recv(_PUMP_CHUNK)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    finally:
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _as_http(url: str) -> str:
    """A WebSocket address as the http address the URL guard judges: ws becomes http, wss https."""
    return re.sub(r"^ws(s?)://", lambda m: f"http{m.group(1).lower()}://", url, flags=re.IGNORECASE)


# ------------------------------------------------------------- fingerprints

@dataclass(frozen=True)
class Fingerprint:
    """One control as the owner was shown it, and how to find it again.

    ``path`` is a CSS path from the nearest id (or the root) down to the node.
    ``label`` is read the way the listing read it. ``secret`` marks
    password-like inputs: the agent never types into them and their value is
    never read into a label.
    """

    ref: str
    path: str
    tag: str
    label: str
    secret: bool


def _ref_for(path: str, label: str) -> str:
    """Stable for the same control on the same page: a re-read gives the same ref."""
    return hashlib.sha256(f"{path}\n{label}".encode("utf-8")).hexdigest()[:12]


def _gone() -> dict:
    return {"error": "Sahifa o'zgargan yoki element topilmadi — sahifani qayta o'qing.",
            "code": "element_gone"}


def _password_refused() -> dict:
    return {"error": "Parol maydoniga matn yozish mumkin emas.", "code": "password_field"}


# ----------------------------------------------------------------- session

class BrowserSession:
    """One Chrome profile, one page, one pinned thread, and the fingerprints of
    the last listing.

    Options live on the instance (headless mode, the URL guard), not in module
    globals. Each public action returns a dict: the result, or ``error`` and
    ``code``.
    """

    def __init__(
        self,
        *,
        headless: bool,
        request_guard: Callable[[str], Optional[str]],
        resolve: Callable[[str], list[str]] = urls.resolve_and_pin,
    ) -> None:
        self.headless = headless
        self._guard = request_guard
        self._proxy = PinnedProxy(resolve)
        self._worker = _Worker("browser")
        self._lock = threading.Lock()
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._fingerprints: dict[str, Fingerprint] = {}
        self._dialogs: list[dict[str, str]] = []

    def lookup(self, ref: Any) -> Optional[Fingerprint]:
        """What the last listing showed for this ref. Policy checks use it before
        an action runs, so the label they classify is the label the owner saw."""
        with self._lock:
            return self._fingerprints.get(str(ref))

    # ------------------------------------------------------------- actions

    def open_url(self, url: str) -> dict:
        def job() -> dict:
            page = self._ensure_page()
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            page.wait_for_timeout(700)          # let the usual client-side render settle
            out = self._describe(page)
            if _needs_login(page):
                out["login_hint"] = (
                    "Bu sayt hali kirilmagan. Brauzer oynasi ochiq — bir marta "
                    "kiring (login), keyin sessiya saqlanadi va qayta so'ramaydi."
                )
            return out

        return self._run(job)

    def read_page(self) -> dict:
        return self._run(lambda: self._describe(self._ensure_page(), with_text=True))

    def click(self, ref: str) -> dict:
        def job() -> dict:
            page = self._ensure_page()
            found = self._resolve(page, ref)
            if found is None:
                return _gone()
            fp, node = found
            before = _page_state(page)
            node.click(timeout=15_000)
            page.wait_for_timeout(900)
            return {"ok": True, "clicked": fp.label, **_effect(page, before), **self._describe(page)}

        return self._run(job)

    def type_text(self, ref: str, text: str, submit: bool = False) -> dict:
        def job() -> dict:
            page = self._ensure_page()
            found = self._resolve(page, ref)
            if found is None:
                return _gone()
            fp, field = found
            if fp.secret or _is_secret(field, fp.tag):
                return _password_refused()
            before = _page_state(page)
            field.fill(text, timeout=15_000)
            result: dict[str, Any] = {"ok": True, "typed_into": fp.label}
            if submit:
                field.press("Enter")
                page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
                page.wait_for_timeout(1200)
                result.update(_effect(page, before))
            result.update(self._describe(page))
            return result

        return self._run(job)

    def screenshot(self) -> dict:
        """A picture for the PERSON to look at on their phone - the model never
        sees it. Cheaper and more honest than pretending to read pixels."""
        def job() -> dict:
            from .office import _work_dir

            page = self._ensure_page()
            target = _work_dir() / f"page-{uuid.uuid4().hex[:8]}.png"
            page.screenshot(path=str(target), full_page=False)
            return {"ok": True, "path": str(target), "url": page.url}

        return self._run(job)

    def close(self) -> dict:
        def job() -> dict:
            try:
                if self._context is not None:
                    self._context.close()
            except Exception:
                pass
            try:
                if self._playwright is not None:
                    self._playwright.stop()
            except Exception:
                pass
            self._proxy.stop()
            self._playwright = self._context = self._page = None
            with self._lock:
                self._fingerprints = {}
            return {"ok": True}

        try:
            return self._worker.call(job, timeout=30)
        except Exception as exc:
            return {"error": str(exc)[:150], "code": "tool_error"}

    # ------------------------------------------------------------ internals

    def _run(self, job: Callable[[], dict]) -> dict:
        if not available():
            return {"error": status(), "code": "not_configured"}
        try:
            return self._worker.call(job)
        except TimeoutError:
            return {"error": "Brauzer javob bermadi (sahifa og'ir bo'lishi mumkin).", "code": "timeout"}
        except Exception as exc:
            first = (str(exc).splitlines() or ["noma'lum xato"])[0]
            return {"error": f"Brauzer xatosi: {first[:180]}", "code": "tool_error"}

    def _ensure_page(self) -> Any:
        """One browser, one page, reused across calls. Always the agent's OWN
        profile: Chrome 136+ refuses remote debugging on the default profile, so
        attaching to the everyday browser is not an option anyway."""
        if self._page is not None and not self._page.is_closed():
            return self._page

        from playwright.sync_api import sync_playwright

        if self._playwright is None:
            self._playwright = sync_playwright().start()
        profile = _profile_dir()
        common: dict[str, Any] = dict(
            user_data_dir=str(profile),
            headless=self.headless,
            viewport=dict(VIEWPORT),
            accept_downloads=False,
            args=["--no-first-run", "--no-default-browser-check"],
            # Every connection leaves through the pinned proxy, so Chrome never resolves a host itself.
            proxy={"server": f"socks5://127.0.0.1:{self._proxy.start()}"},
            # A service worker fetches outside the page's requests; none is allowed to run.
            service_workers="block",
        )
        _clear_stale_lock(profile)
        try:
            self._context = self._playwright.chromium.launch_persistent_context(channel=CHANNEL, **common)
        except Exception as exc:
            # No real Chrome, or it is briefly locked - fall back to bundled Chromium.
            log.info("channel %r unavailable (%s); using bundled Chromium", CHANNEL, exc)
            _clear_stale_lock(profile)
            self._context = self._playwright.chromium.launch_persistent_context(**common)
        self._context.set_default_timeout(NAV_TIMEOUT)
        self._context.route("**/*", self._guard_route)
        if hasattr(self._context, "route_web_socket"):      # Playwright 1.48 and later
            self._context.route_web_socket("**/*", self._guard_socket)
        self._context.on("page", self._watch)
        for page in self._context.pages:
            self._watch(page)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    def _guard_route(self, route: Any) -> None:
        """Refuse a request the URL guard rejects, before Chrome sends it. The URL
        itself is not logged: it can carry a token in its query."""
        try:
            reason = self._guard(route.request.url)
        except Exception:
            reason = "guard failed"
        if reason is None:
            route.continue_()
            return
        log.info("browser request refused by the URL guard")
        route.abort(ABORT_ERROR)

    def _guard_socket(self, ws: Any) -> None:
        """A WebSocket is judged as the request it is: a refused address is closed, the rest connect."""
        try:
            reason = self._guard(_as_http(ws.url))
        except Exception:
            reason = "guard failed"
        if reason is None:
            ws.connect_to_server()
            return
        log.info("browser socket refused by the URL guard")
        ws.close()

    def _watch(self, page: Any) -> None:
        page.on("dialog", self._on_dialog)

    def _on_dialog(self, dialog: Any) -> None:
        """A dialog nobody asked for is reported, then dismissed. Dismissing is
        the answer that approves nothing: it cancels a confirm, leaves a prompt
        empty and keeps a page from being left. Accepting would be an approval the
        owner never gave, so it never happens. The report is what keeps this from
        being Playwright's silent default."""
        with self._lock:
            if len(self._dialogs) < MAX_DIALOGS:
                self._dialogs.append({"type": dialog.type, "message": (dialog.message or "")[:200]})
        try:
            dialog.dismiss()
        except Exception:
            log.debug("dialog dismiss failed", exc_info=True)

    def _take_dialogs(self) -> dict:
        with self._lock:
            taken, self._dialogs = self._dialogs, []
        if not taken:
            return {}
        return {"dialogs": taken,
                "dialogs_note": "Sahifada kutilmagan oyna chiqdi. U yopildi, tasdiqlanmadi."}

    def _resolve(self, page: Any, ref: str) -> Optional[tuple[Fingerprint, Any]]:
        """The live node for a ref, if it is still at its path and still reads the
        same label. Anything else is refused: the page changed since the owner saw it."""
        fp = self.lookup(ref)
        if fp is None:
            return None
        node = page.query_selector(fp.path)
        if node is None or _label_of(node, fp.tag, fp.secret) != fp.label:
            return None
        return fp, node

    def _describe(self, page: Any, with_text: bool = False) -> dict:
        out: dict[str, Any] = {
            "url": page.url,
            "title": (page.title() or "")[:120],
            "elements": self._collect(page),
        }
        if with_text:
            out["text"] = _page_text(page)
        out.update(self._take_dialogs())
        return out

    def _collect(self, page: Any) -> str:
        """Interactive elements as a numbered list of fingerprints. The listing
        replaces the previous fingerprints, so a ref is only valid for the page
        as it was last shown."""
        try:
            handles = page.query_selector_all(INTERACTIVE_SELECTOR)
        except Exception:
            return "(sahifa o'qilmadi)"

        lines: list[str] = []
        seen: set[tuple] = set()
        found: dict[str, Fingerprint] = {}
        for handle in handles:
            if len(lines) >= MAX_ELEMENTS:
                lines.append(f"... (yana elementlar bor, {MAX_ELEMENTS} tasi ko'rsatildi)")
                break
            try:
                if not handle.is_visible():
                    continue
                tag = handle.evaluate("e => e.tagName.toLowerCase()")
                secret = _is_secret(handle, tag)
                label = _label_of(handle, tag, secret)
                if not label:
                    continue
                key = (tag, label.lower())
                if key in seen:
                    continue
                path = handle.evaluate(_PATH_JS)
                if not path:
                    continue
                seen.add(key)
                fp = Fingerprint(_ref_for(path, label), path, tag, label, secret)
                found[fp.ref] = fp
                extra = "" if handle.is_enabled() else " (o'chiq)"
                lines.append(f'[{fp.ref}] {tag} "{label}"{extra}')
            except Exception:
                continue
        with self._lock:
            self._fingerprints = found
        return "\n".join(lines) if lines else "(interaktiv element topilmadi)"


# ------------------------------------------------------------------ helpers

def _profile_dir() -> Path:
    from .config import config_dir

    d = config_dir() / "browser-profile"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _clear_stale_lock(profile: Path) -> None:
    """Remove a leftover Chrome singleton lock in the agent's own profile.

    If the agent crashed with its browser open, a SingletonLock stays behind
    and the next launch fails with "profile in use" - which would leave the
    browser permanently broken until the folder is cleaned by hand. These are
    ordinary files we own, safe to delete; Chrome recreates them.
    """
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        target = profile / name
        try:
            if target.exists() or target.is_symlink():
                target.unlink()
        except OSError:
            pass


def _page_state(page: Any) -> tuple[str, str]:
    try:
        return page.url, (page.title() or "")
    except Exception:
        return "", ""


def _effect(page: Any, before: tuple[str, str]) -> dict:
    """Did that actually do anything?

    A form filled into a decoy field reports a perfectly successful fill and
    then nothing happens - the agent would tell the user it had searched when
    it had not. Comparing the page before and after turns a silent no-op into
    something the model can notice and retry.
    """
    if _page_state(page) == before:
        return {
            "changed": False,
            "warning": (
                "Sahifa o'zgarmadi — bu element kerakli tugma/maydon "
                "bo'lmasligi mumkin. Boshqa elementni sinab ko'ring."
            ),
        }
    return {"changed": True}


_LOGIN_SIGNS = ("log in", "sign in", "войти", "kirish", "log into",
                "authorization required", "please sign in")


def _needs_login(page: Any) -> bool:
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


def _is_secret(handle: Any, tag: str) -> bool:
    if tag != "input":
        return False
    kind = (handle.get_attribute("type") or "").lower()
    hint = (handle.get_attribute("autocomplete") or "").lower()
    return kind == "password" or "password" in hint


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

# A CSS path from the nearest id (or the root) to the node. The path is checked
# in the page before it is used: a path that does not lead back to this very
# node is no fingerprint, so the control is left out of the listing.
_PATH_JS = """
(el) => {
  const segs = [];
  let node = el;
  while (node && node.nodeType === 1) {
    if (node.id && document.querySelectorAll('#' + CSS.escape(node.id)).length === 1) {
      segs.unshift('#' + CSS.escape(node.id));
      break;
    }
    if (node === document.documentElement) {
      segs.unshift('html');
      break;
    }
    let idx = 1;
    for (let s = node.previousElementSibling; s; s = s.previousElementSibling) {
      if (s.tagName === node.tagName) idx++;
    }
    segs.unshift(node.tagName.toLowerCase() + ':nth-of-type(' + idx + ')');
    node = node.parentElement;
  }
  const path = segs.join(' > ');
  return path && document.querySelector(path) === el ? path : '';
}
"""


def _label_of(handle: Any, tag: str, secret: bool = False) -> str:
    getters = [
        lambda: handle.get_attribute("aria-label"),
        lambda: handle.evaluate(_LABEL_JS),
        lambda: handle.inner_text(),
        lambda: handle.get_attribute("placeholder"),
        lambda: handle.get_attribute("title"),
        lambda: None if secret else handle.get_attribute("value"),
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


def _page_text(page: Any) -> str:
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
