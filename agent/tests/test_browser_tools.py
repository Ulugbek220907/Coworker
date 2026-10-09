"""Browser session and browser tools, run against fake pages and fake elements.

No real browser starts: the session talks to objects that behave like Playwright's
Page, ElementHandle and Dialog, and the launch test replaces the Playwright module
with a stub that records the launch options. Name resolution is faked too, so the
URL checks never touch the network.
"""
from __future__ import annotations

import re
import socket
import sys
import types
from pathlib import Path

import pytest

from coworker import browser as flat_browser
from coworker import config
from coworker.browser import BrowserSession
from coworker.core.types import Autonomy, CallContext, Decision, Provenance, Tier
from coworker.tools import browser as tools_browser
from coworker.tools.registry import Services, ToolCall

LISTING_LINE = re.compile(r'\[([0-9a-f]{12})\] (\w+) "([^"]*)"')


# ------------------------------------------------------------------ fakes

class FakeElement:
    """An ElementHandle with the methods the session calls, and nothing more."""

    def __init__(self, tag, path, *, attrs=None, text="", visible=True, enabled=True):
        self.tag = tag
        self.path = path
        self.attrs = dict(attrs or {})
        self.text = text
        self.visible = visible
        self.enabled = enabled
        self.clicked = 0
        self.filled: list[str] = []
        self.pressed: list[str] = []

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def get_attribute(self, name):
        return self.attrs.get(name)

    def inner_text(self):
        return self.text

    def evaluate(self, script, arg=None):
        if script == flat_browser._PATH_JS:
            return self.path
        if "tagName" in script:
            return self.tag
        if script == flat_browser._LABEL_JS:
            return ""
        raise AssertionError("unexpected script evaluated on an element")

    def click(self, timeout=None):
        self.clicked += 1

    def fill(self, text, timeout=None):
        self.filled.append(text)

    def press(self, key):
        self.pressed.append(key)


class FakePage:
    def __init__(self, elements=(), *, url="https://example.com/", title="Example"):
        self.elements = list(elements)
        self.url = url
        self._title = title
        self.closed = False

    def is_closed(self):
        return self.closed

    def title(self):
        return self._title

    def inner_text(self, selector):
        return ""

    def evaluate(self, script, arg=None):
        return ""

    def query_selector_all(self, selector):
        return list(self.elements)

    def query_selector(self, path):
        return next((e for e in self.elements if e.path == path), None)

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self, state, timeout=None):
        pass

    def screenshot(self, path, full_page=False):
        Path(path).write_bytes(b"\x89PNG\r\n")


class FakeDialog:
    def __init__(self, kind, message):
        self.type = kind
        self.message = message
        self.dismissed = False
        self.accepted = False

    def dismiss(self):
        self.dismissed = True

    def accept(self, prompt_text=None):
        self.accepted = True


class FakeRoute:
    def __init__(self, url):
        self.request = types.SimpleNamespace(url=url)
        self.outcome = None

    def continue_(self):
        self.outcome = "continue"

    def abort(self, code):
        self.outcome = ("abort", code)


class FakeContext:
    def __init__(self):
        self.pages: list[FakePage] = []
        self.routes: list[tuple[str, object]] = []
        self.listeners: dict[str, list] = {}

    def set_default_timeout(self, ms):
        self.timeout = ms

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def on(self, event, handler):
        self.listeners.setdefault(event, []).append(handler)

    def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page

    def close(self):
        pass


class FakeChromium:
    def __init__(self, context):
        self.context = context
        self.calls: list[dict] = []
        self.fail_channel = False

    def launch_persistent_context(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_channel and "channel" in kwargs:
            raise RuntimeError("no real Chrome installed")
        return self.context


@pytest.fixture
def home(tmp_path, monkeypatch):
    """COWORKER_HOME for the test: config_dir() is pointed here."""
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setattr(config, "config_dir", lambda: folder)
    return folder


@pytest.fixture
def session(monkeypatch):
    s = BrowserSession(headless=True, request_guard=lambda url: None)
    monkeypatch.setattr(tools_browser, "_session_obj", s)
    return s


@pytest.fixture
def launcher(monkeypatch):
    """A stand-in for the Playwright module, recording what the session launches."""
    chromium = FakeChromium(FakeContext())
    stub = types.SimpleNamespace(chromium=chromium, stop=lambda: None)
    api = types.ModuleType("playwright.sync_api")
    api.sync_playwright = lambda: types.SimpleNamespace(start=lambda: stub)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
    return chromium


@pytest.fixture
def fake_dns(monkeypatch):
    table = {
        "example.com": ["93.184.216.34"],
        "intranet.example": ["10.1.2.3"],
        "mixed.example": ["93.184.216.34", "127.0.0.1"],
    }

    def getaddrinfo(host, port, *args, **kwargs):
        if host not in table:
            raise socket.gaierror(-2, "Name or service not known")
        out = []
        for ip in table[host]:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            sockaddr = (ip, 0, 0, 0) if family == socket.AF_INET6 else (ip, 0)
            out.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return out

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return table


def ctx(**overrides) -> CallContext:
    base = dict(turn_id="t1", actor="owner", chat_id=1, autonomy=Autonomy.ASK_FOR_WRITES,
                grants=frozenset({"browser", "web"}), generation=0, provenance=Provenance.OWNER)
    base.update(overrides)
    return CallContext(**base)


def call(name, args, context=None) -> ToolCall:
    return ToolCall(name=name, args=args, ctx=context or ctx(), svc=Services())


def spec(name):
    return next(s for s in tools_browser.SPECS if s.name == name)


def attach(session, page):
    session._page = page


def refs(listing: str) -> dict[str, str]:
    """Label -> ref for every line of a listing: ``[ref] tag "label"``."""
    return {label: ref for ref, _tag, label in LISTING_LINE.findall(listing)}


def listing_refs(session, page) -> dict[str, str]:
    attach(session, page)
    return refs(session.read_page()["elements"])


# ----------------------------------------------------------- specs and source

def test_specs_declare_the_catalogue_tiers_and_gov_classes():
    got = {s.name: (s.tier, s.gov_class) for s in tools_browser.SPECS}
    assert got == {
        "web_open": (Tier.READ, "BROWSER"),
        "web_read": (Tier.READ, "BROWSER"),
        "web_click": (Tier.LOCAL_WRITE, "BROWSER"),
        "web_type": (Tier.LOCAL_WRITE, "BROWSER"),
        "web_screenshot": (Tier.READ, "VISION"),
    }
    assert all(s.family == "browser" and s.untrusted for s in tools_browser.SPECS)


def test_no_automation_suppression_flag_and_no_headless_global():
    for path in (flat_browser.__file__, tools_browser.__file__):
        assert "AutomationControlled" not in Path(path).read_text(encoding="utf-8")
    assert not hasattr(flat_browser, "HEADLESS")


# --------------------------------------------------------------- session setup

def test_profile_lives_under_coworker_home_and_downloads_are_off(home, session, launcher):
    session.read_page()
    kwargs = launcher.calls[0]
    profile = Path(kwargs["user_data_dir"])
    assert profile.parent == home and profile.name == "browser-profile"
    assert kwargs["accept_downloads"] is False
    assert kwargs["headless"] is True
    assert kwargs["channel"] == "chrome"
    assert not any("AutomationControlled" in arg for arg in kwargs["args"])


def test_bundled_chromium_is_the_fallback_without_a_real_chrome(home, session, launcher):
    launcher.fail_channel = True
    session.read_page()
    assert len(launcher.calls) == 2
    assert "channel" not in launcher.calls[1]
    assert launcher.calls[1]["accept_downloads"] is False


def test_guard_aborts_requests_the_url_guard_refuses(session):
    guard = BrowserSession(headless=True,
                           request_guard=lambda url: "url_refused" if "10.0.0.1" in url else None)
    allowed, refused = FakeRoute("https://example.com/a.js"), FakeRoute("http://10.0.0.1/x")
    guard._guard_route(allowed)
    guard._guard_route(refused)
    assert allowed.outcome == "continue"
    assert refused.outcome == ("abort", "blockedbyclient")


def test_a_guard_that_raises_refuses_the_request(session):
    def broken(url):
        raise RuntimeError("resolver down")

    guard = BrowserSession(headless=True, request_guard=broken)
    route = FakeRoute("https://example.com/")
    guard._guard_route(route)
    assert route.outcome == ("abort", "blockedbyclient")


def test_worker_timeout_is_reported_as_timeout(session, monkeypatch):
    def slow(fn, timeout=None):
        raise TimeoutError("slow")

    monkeypatch.setattr(session._worker, "call", slow)
    assert session.read_page()["code"] == "timeout"


def test_missing_playwright_is_reported_as_not_configured(session, monkeypatch):
    monkeypatch.setattr(flat_browser, "available", lambda: False)
    out = session.read_page()
    assert out["code"] == "not_configured" and "playwright" in out["error"]


# ----------------------------------------------------------------- listing

def test_listing_refs_are_fingerprints_that_stay_the_same_on_a_re_read(session):
    save = FakeElement("button", "html > body > button:nth-of-type(1)", attrs={"aria-label": "Save"})
    page = FakePage([save])
    attach(session, page)
    first = refs(session.read_page()["elements"])
    second = refs(session.read_page()["elements"])
    assert first == second
    ref = first["Save"]
    assert len(ref) == 12
    assert session.lookup(ref).path == save.path
    assert session.lookup(ref).label == "Save"


def test_password_value_never_reaches_the_listing(session):
    field = FakeElement("input", "html > body > input:nth-of-type(1)",
                        attrs={"type": "password", "value": "hunter2"})
    attach(session, FakePage([field]))
    listing = session.read_page()["elements"]
    assert "hunter2" not in listing
    assert "(password maydoni)" in listing


# --------------------------------------------------------------- click

def test_click_runs_when_the_node_still_reads_the_same_label(session):
    save = FakeElement("button", "html > body > button:nth-of-type(1)", attrs={"aria-label": "Save"})
    ref = listing_refs(session, FakePage([save]))["Save"]
    out = session.click(ref)
    assert out["ok"] is True and out["clicked"] == "Save"
    assert save.clicked == 1


def test_click_refused_when_the_label_changed_since_the_listing(session):
    save = FakeElement("button", "html > body > button:nth-of-type(1)", attrs={"aria-label": "Save"})
    ref = listing_refs(session, FakePage([save]))["Save"]
    save.attrs["aria-label"] = "Delete account"        # re-rendered with another label
    out = session.click(ref)
    assert out["code"] == "element_gone"
    assert save.clicked == 0


def test_click_refused_when_the_node_moved_off_its_path(session):
    save = FakeElement("button", "html > body > button:nth-of-type(1)", attrs={"aria-label": "Save"})
    ref = listing_refs(session, FakePage([save]))["Save"]
    save.path = "html > body > div:nth-of-type(2) > button:nth-of-type(1)"
    out = session.click(ref)
    assert out["code"] == "element_gone"
    assert save.clicked == 0


def test_click_with_an_unknown_ref_is_refused(session):
    attach(session, FakePage([]))
    assert session.click("0" * 12)["code"] == "element_gone"


def test_click_tool_reports_a_refused_click_as_untrusted_failure(session):
    save = FakeElement("button", "html > body > button:nth-of-type(1)", attrs={"aria-label": "Save"})
    ref = listing_refs(session, FakePage([save]))["Save"]
    save.attrs["aria-label"] = "Pay now"
    result = tools_browser._click(call("web_click", {"ref": ref}))
    assert result.ok is False and result.code == "element_gone" and result.untrusted is True


# --------------------------------------------------------------- typing

def test_password_field_is_never_typed_into(session):
    field = FakeElement("input", "html > body > input:nth-of-type(1)",
                        attrs={"type": "password", "aria-label": "Parol"})
    ref = listing_refs(session, FakePage([field]))["Parol"]
    out = session.type_text(ref, "abc")
    assert out["code"] == "password_field"
    assert field.filled == []


def test_autocomplete_password_hint_counts_as_a_password_field(session):
    field = FakeElement("input", "html > body > input:nth-of-type(1)",
                        attrs={"type": "text", "aria-label": "Kod", "autocomplete": "current-password"})
    ref = listing_refs(session, FakePage([field]))["Kod"]
    assert session.type_text(ref, "abc")["code"] == "password_field"
    assert field.filled == []


def test_type_into_a_plain_field_fills_and_submits(session):
    field = FakeElement("input", "html > body > input:nth-of-type(1)", attrs={"aria-label": "Qidiruv"})
    ref = listing_refs(session, FakePage([field]))["Qidiruv"]
    out = session.type_text(ref, "kitob", submit=True)
    assert out["ok"] is True and out["typed_into"] == "Qidiruv"
    assert field.filled == ["kitob"] and field.pressed == ["Enter"]


def test_type_arg_check_refuses_a_password_field_before_any_confirm(session):
    field = FakeElement("input", "html > body > input:nth-of-type(1)",
                        attrs={"type": "password", "aria-label": "Parol"})
    ref = listing_refs(session, FakePage([field]))["Parol"]
    verdict = spec("web_type").arg_checks[0]({"ref": ref, "text": "x"}, ctx(), None)
    assert verdict.decision == Decision.DENY and verdict.code == "password_field"


def test_submit_adds_a_confirm_with_a_summary_that_names_the_text(session):
    field = FakeElement("input", "html > body > input:nth-of-type(1)", attrs={"aria-label": "Qidiruv"})
    ref = listing_refs(session, FakePage([field]))["Qidiruv"]
    check = spec("web_type").arg_checks[1]
    assert check({"ref": ref, "text": "kitob"}, ctx(), None) is None
    verdict = check({"ref": ref, "text": "kitob", "submit": True}, ctx(), None)
    assert verdict.decision == Decision.CONFIRM
    assert verdict.code == "submit_outbound"
    assert "Enter" in verdict.summary and "kitob" in verdict.summary


# ------------------------------------------------------ label classification

@pytest.mark.parametrize("label, decision, code", [
    ("Buy now", Decision.DENY, "prohibited_tier"),
    ("Pay", Decision.DENY, "prohibited_tier"),
    ("Купить", Decision.DENY, "prohibited_tier"),
    ("Sign in", Decision.CONFIRM, "label_credential"),
    ("Delete draft", Decision.CONFIRM, "label_destructive"),
    ("O\u2019chirish", Decision.CONFIRM, "label_destructive"),
    ("Send", Decision.CONFIRM, "label_outbound"),
    ("Restart computer", Decision.CONFIRM, "label_system_change"),
    ("Open menu", None, None),
])
def test_click_arg_check_classifies_the_label(session, label, decision, code):
    el = FakeElement("button", "html > body > button:nth-of-type(1)", attrs={"aria-label": label})
    ref = listing_refs(session, FakePage([el]))[label]
    verdict = spec("web_click").arg_checks[0]({"ref": ref}, ctx(), None)
    if decision is None:
        assert verdict is None
        return
    assert verdict.decision == decision
    assert verdict.code == code
    if decision == Decision.CONFIRM:
        assert verdict.summary.startswith(f"Sahifada «{label}»")


def test_click_arg_check_refuses_an_unknown_ref(session):
    attach(session, FakePage([]))
    verdict = spec("web_click").arg_checks[0]({"ref": "f" * 12}, ctx(), None)
    assert verdict.decision == Decision.DENY and verdict.code == "element_gone"


def test_relax_lets_only_plain_labels_through_on_an_owner_ask_for_writes_turn(session):
    plain = FakeElement("button", "html > body > button:nth-of-type(1)", attrs={"aria-label": "Open menu"})
    risky = FakeElement("button", "html > body > button:nth-of-type(2)", attrs={"aria-label": "Delete draft"})
    found = listing_refs(session, FakePage([plain, risky]))
    relax = spec("web_click").relax
    assert relax({"ref": found["Open menu"]}, ctx(autonomy=Autonomy.ASK_FOR_WRITES)) is True
    assert relax({"ref": found["Open menu"]}, ctx(autonomy=Autonomy.ASK_ALWAYS)) is False
    assert relax({"ref": found["Open menu"]}, ctx(autonomy=Autonomy.AUTONOMOUS_READONLY)) is False
    assert relax({"ref": found["Delete draft"]}, ctx(autonomy=Autonomy.ASK_FOR_WRITES)) is False
    assert relax({"ref": "e" * 12}, ctx(autonomy=Autonomy.ASK_FOR_WRITES)) is False


def test_click_summary_names_the_control(session):
    el = FakeElement("button", "html > body > button:nth-of-type(1)", attrs={"aria-label": "Delete draft"})
    ref = listing_refs(session, FakePage([el]))["Delete draft"]
    assert "Delete draft" in spec("web_click").summary({"ref": ref})


# -------------------------------------------------------- dialogs and downloads

def test_a_dialog_is_reported_then_dismissed_never_accepted(session):
    attach(session, FakePage([]))
    dialog = FakeDialog("confirm", "Really delete everything?")
    session._on_dialog(dialog)
    assert dialog.dismissed is True and dialog.accepted is False
    out = session.read_page()
    assert out["dialogs"] == [{"type": "confirm", "message": "Really delete everything?"}]
    assert "dialogs_note" in out
    assert "dialogs" not in session.read_page()


# ----------------------------------------------------------------- open

class _NoBrowser:
    def __init__(self):
        self.opened: list[str] = []

    def open_url(self, url):
        self.opened.append(url)
        return {"url": url, "title": "", "elements": "(interaktiv element topilmadi)"}


URL_TABLE = [
    ("https://example.com/", True),
    ("http://example.com/path?q=1", True),
    ("file:///C:/Windows/win.ini", False),
    ("ftp://example.com/", False),
    ("javascript:alert(1)", False),
    ("https://user:secret@example.com/", False),
    ("http://localhost:8080/", False),
    ("http://127.0.0.1/", False),
    ("http://10.0.0.5/", False),
    ("http://169.254.169.254/latest/meta-data/", False),
    ("http://[::1]/", False),
    ("https://intranet.example/", False),   # a public-looking name that points at 10.1.2.3
    ("https://mixed.example/", False),      # one private answer among public ones
    ("https://nowhere.invalid/", False),    # does not resolve at all
]


@pytest.mark.parametrize("url, allowed", URL_TABLE)
def test_web_open_url_table_through_the_handler(session, fake_dns, monkeypatch, url, allowed):
    opener = _NoBrowser()
    monkeypatch.setattr(session, "open_url", opener.open_url)
    result = tools_browser._open(call("web_open", {"url": url}))
    assert result.ok is allowed
    if allowed:
        assert opener.opened == [url]
    else:
        assert result.code == "url_refused"
        assert opener.opened == []


@pytest.mark.parametrize("url", [
    "file:///C:/Windows/win.ini", "https://user:secret@example.com/", "http://localhost:8080/",
    "http://127.0.0.1/", "http://10.0.0.5/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
])
def test_web_open_arg_check_refuses_literal_bad_addresses(url):
    verdict = spec("web_open").arg_checks[0]({"url": url}, ctx(), None)
    assert verdict.decision == Decision.DENY and verdict.code == "url_refused"


def test_web_open_arg_check_passes_a_public_literal_address():
    assert spec("web_open").arg_checks[0]({"url": "https://example.com/"}, ctx(), None) is None


def test_open_reports_the_login_hint_for_a_login_wall(session):
    attach(session, FakePage([], title="Sign in to continue"))
    out = session.open_url("https://example.com/")
    assert "login_hint" in out


# ------------------------------------------------------------- read / screenshot

def test_web_read_returns_text_and_elements_as_untrusted(session):
    attach(session, FakePage([FakeElement("a", "html > body > a:nth-of-type(1)", attrs={"aria-label": "Home"})]))
    result = tools_browser._read(call("web_read", {}))
    assert result.ok is True and result.untrusted is True
    assert "text" in result.data and "[" in result.data["elements"]


def test_screenshot_is_sent_as_a_file_from_the_scratch_folder(session, home):
    attach(session, FakePage([]))
    result = tools_browser._shot(call("web_screenshot", {}))
    assert result.ok is True and result.untrusted is True
    assert len(result.sendable) == 1
    shot = Path(result.sendable[0])
    assert shot.parent == home / "generated"
    assert shot.read_bytes().startswith(b"\x89PNG")
    assert "path" not in result.data


# ------------------------------------------- egress, card text and schema caps

def test_web_open_declares_egress_and_its_url_as_a_sensitive_argument() -> None:
    open_spec = spec("web_open")
    assert open_spec.egress is True
    assert open_spec.sensitive_args == ("url",)


def test_web_type_text_is_capped_at_what_a_card_can_show() -> None:
    from coworker.tools.desktop import CARD_TEXT
    from coworker.tools.registry import validate_args

    params = spec("web_type").parameters
    assert validate_args(params, {"ref": "abc123", "text": "x" * CARD_TEXT}) is None
    error = validate_args(params, {"ref": "abc123", "text": "x" * (CARD_TEXT + 1)})
    assert error is not None and "too long" in error


def test_web_type_card_shows_the_whole_text_before_the_form_is_sent(monkeypatch) -> None:
    text = "Ulugbek Karimov, 2026-10-09 deposit slip number 4471"
    session = BrowserSession(headless=True, request_guard=lambda url: None)
    session._fingerprints = {"abc123": flat_browser.Fingerprint("abc123", "input", "input", "Message", False)}
    monkeypatch.setattr(tools_browser, "_session_obj", session)
    summary = tools_browser._type_summary({"ref": "abc123", "text": text, "submit": True})
    assert text in summary
    assert "Enter" in summary
