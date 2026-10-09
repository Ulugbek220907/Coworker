"""Web fetch and web search: reading public pages without driving a browser.

web_fetch is the light path for a URL the owner named: one HTTP GET and no page
scripts. The browser has the same URL policy, but here the agent makes the
connection itself, so it can pin it. The address policy.urls resolved is the
address the request is sent to, while the host name stays in the Host header and
the TLS server name. A DNS answer that changes between the check and the connection
therefore cannot send the request to a private host. Redirects are followed by
hand, so every hop gets the same check. Environment proxies are ignored: a proxy
resolves the host itself and would step around that pin.

web_search asks a search API with a key from the secret store, and the tool is
hidden until a key exists. The provider call is a single function, so tests can
replace it and no test touches the network.

Both tools are egress tools. The address or query leaves the machine, so after
the turn has read local data the kernel asks the owner first, and it origin-checks
the address and query. The card and key scan runs on the decoded text as well,
because the kernel's scan does not see through percent-encoding.
"""
from __future__ import annotations

import codecs
import html
import ipaddress
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import unquote_plus, urljoin, urlsplit, urlunsplit

import httpx

from ..core.types import CallContext, Tier, ToolResult, Verdict
from ..policy import prohibited, urls
from .registry import Services, ToolCall, ToolSpec

FETCH_TIMEOUT = 20.0          # seconds for the whole fetch, redirects included
MAX_BODY = 2 * 1024 * 1024    # bytes read from one response body
MAX_REDIRECTS = 5
MAX_CHARS = 6000
USER_AGENT = "Coworker/2"
SEARCH_KEY = "search_api_key"
SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
SEARCH_TIMEOUT = 15.0

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_TEXT_TYPES = frozenset({"application/json", "application/xml", "application/xhtml+xml", "text/xml"})
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})


# ----------------------------------------------------------------- web_fetch

_REFUSED_TEXT = "bu manzil ochilmaydi (ichki yoki noto'g'ri manzil)"


def _check_fetch_url(args: dict, ctx: CallContext, svc: Optional[Services]) -> Optional[Verdict]:
    """The text of the address only; fetch() resolves the host before it connects."""
    url = args.get("url", "")
    if urls.check_url(url):
        return Verdict.deny("url_refused", _REFUSED_TEXT)
    return _decoded_secret(url)


def _decoded_secret(text: str) -> Optional[Verdict]:
    """A card or key shape hidden by percent-encoding, such as a card written with %20 between groups.

    The kernel scans the raw arguments, where "%20" is not a separator, so the
    text is decoded and scanned again here.
    """
    code = prohibited.scan_args({"text": unquote_plus(text)})
    if code:
        return Verdict.deny(code, "the address or query contains something that must never leave the machine")
    return None


def _check_search_query(args: dict, ctx: CallContext, svc: Optional[Services]) -> Optional[Verdict]:
    return _decoded_secret(args.get("query", ""))


def _make_client() -> httpx.Client:
    """The client for fetches. Redirects are followed by fetch(), not by httpx."""
    return httpx.Client(follow_redirects=False, trust_env=False)


@dataclass(frozen=True)
class _Hop:
    status: int
    content_type: str
    location: str
    body: bytes
    truncated: bool


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _get_pinned(client: httpx.Client, url: str, address: str, deadline: float) -> _Hop:
    """One GET to the pinned address. The body is read up to MAX_BODY and no further.

    Redirect bodies are not read at all: the next hop is what matters.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    netloc = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
    target = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    host_header = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        host_header = f"{host_header}:{parts.port}"
    extensions: dict[str, str] = {}
    if parts.scheme == "https" and not _is_ip(host):
        extensions["sni_hostname"] = host

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("fetch deadline passed")
    headers = {"Host": host_header, "User-Agent": USER_AGENT,
               "Accept": "text/html,text/plain,application/json;q=0.9,*/*;q=0.1"}
    with client.stream("GET", target, headers=headers, extensions=extensions, timeout=remaining) as response:
        location = response.headers.get("location", "")
        if response.status_code in _REDIRECTS:
            return _Hop(response.status_code, "", location, b"", False)
        body = bytearray()
        truncated = False
        for chunk in response.iter_bytes():
            if time.monotonic() >= deadline:
                raise TimeoutError("fetch deadline passed")
            room = MAX_BODY - len(body)
            if len(chunk) > room:
                body.extend(chunk[:room])
                truncated = True
                break
            body.extend(chunk)
        return _Hop(response.status_code, response.headers.get("content-type", ""), "", bytes(body), truncated)


def fetch(url: str, max_chars: int = MAX_CHARS) -> ToolResult:
    deadline = time.monotonic() + FETCH_TIMEOUT
    current = url
    with _make_client() as client:
        for _ in range(MAX_REDIRECTS + 1):
            if urls.check_url(current):
                return ToolResult.fail("url_refused", _REFUSED_TEXT)
            try:
                addresses = urls.resolve_and_pin(urlsplit(current).hostname or "")
            except urls.UrlRefused:
                return ToolResult.fail("url_refused", _REFUSED_TEXT)
            except OSError:
                return ToolResult.fail("tool_error", "bu manzil topilmadi")
            try:
                hop = _get_pinned(client, current, addresses[0], deadline)
            except (httpx.TimeoutException, TimeoutError):
                return ToolResult.fail("timeout", "sahifa vaqtida javob bermadi")
            except httpx.HTTPError:
                return ToolResult.fail("tool_error", "sahifa olinmadi")
            if hop.status in _REDIRECTS and hop.location:
                current = urljoin(current, hop.location)
                continue
            return _finish(current, hop, max_chars)
    return ToolResult.fail("tool_error", "juda ko'p yo'naltirish (redirect)")


def _finish(url: str, hop: _Hop, max_chars: int) -> ToolResult:
    if hop.status >= 400 or hop.status in _REDIRECTS:
        return ToolResult.fail("tool_error", f"sahifa javob bermadi: HTTP {hop.status}", status=hop.status, url=url)
    media = hop.content_type.split(";")[0].strip().lower()
    if media and not (media.startswith("text/") or media in _TEXT_TYPES):
        return ToolResult.fail("tool_error", "bu turdagi fayl matn sifatida o'qilmaydi",
                               url=url, content_type=media)
    text = hop.body.decode(_charset(hop.content_type), errors="replace")
    title = ""
    if media in _HTML_TYPES:
        title, text = _html_text(text)
    text = _tidy(text)
    return ToolResult(
        ok=True,
        data={"url": url, "status": hop.status, "title": title, "text": text[:max_chars],
              "truncated": hop.truncated or len(text) > max_chars},
        untrusted=True,
    )


def _charset(content_type: str) -> str:
    match = re.search(r"charset=([\w.-]+)", content_type, re.IGNORECASE)
    name = match.group(1) if match else "utf-8"
    try:
        codecs.lookup(name)
    except LookupError:
        return "utf-8"
    return name


class _PageText(HTMLParser):
    """Visible text of an HTML page. Script, style and template contents are
    dropped, the title is kept apart, and block tags become line breaks so the
    text keeps its paragraphs."""

    _SKIP = frozenset({"script", "style", "noscript", "template", "svg"})
    _BLOCKS = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
                         "section", "article", "header", "footer", "table", "pre", "blockquote"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        elif not self._skip:
            self.parts.append(data)


def _html_text(raw: str) -> tuple[str, str]:
    parser = _PageText()
    parser.feed(raw)
    parser.close()
    return _tidy("".join(parser.title_parts)), "".join(parser.parts)


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _fetch_tool(call: ToolCall) -> ToolResult:
    return fetch(call.args["url"], int(call.args.get("max_chars", MAX_CHARS)))


# ---------------------------------------------------------------- web_search

def _search_key() -> Optional[str]:
    from ..store.secrets import get_secret

    return get_secret(SEARCH_KEY)


def _search_configured() -> bool:
    return bool(_search_key())


def _plain(value: Any) -> str:
    text = re.sub(r"<[^<>]*>", "", str(value or ""))
    return " ".join(html.unescape(text).split())[:300]


def _provider_search(query: str, limit: int, api_key: str) -> list[dict]:
    """One call to the search API. Tests replace this function; nothing else
    talks to the search provider."""
    with httpx.Client(timeout=SEARCH_TIMEOUT) as client:
        response = client.get(
            SEARCH_URL,
            params={"q": query, "count": limit},
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
        )
    response.raise_for_status()
    items = response.json().get("web", {}).get("results", [])
    return [
        {"title": _plain(item.get("title")), "url": str(item.get("url")),
         "snippet": _plain(item.get("description"))}
        for item in items[:limit] if item.get("url")
    ]


def _search_tool(call: ToolCall) -> ToolResult:
    query = call.args["query"].strip()
    if not query:
        return ToolResult.fail("arg_invalid", "qidiruv so'zi bo'sh")
    key = _search_key()
    if not key:
        return ToolResult.fail("not_configured", "qidiruv kaliti sozlanmagan")
    limit = int(call.args.get("limit", 5))
    try:
        results = _provider_search(query, limit, key)
    except (httpx.HTTPError, ValueError):
        return ToolResult.fail("tool_error", "qidiruv xizmati javob bermadi")
    return ToolResult(ok=True, data={"query": query, "results": results, "count": len(results)}, untrusted=True)


# ----------------------------------------------------------------- specs

SPECS = [
    ToolSpec(
        name="web_fetch",
        family="web",
        tier=Tier.READ,
        description=(
            "Fetch one public http or https page as plain text. No page scripts run. "
            "Redirects are followed and each hop is checked. The text is untrusted content."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "maxLength": 2048},
                "max_chars": {"type": "integer", "minimum": 200, "maximum": 20000},
            },
            "required": ["url"],
        },
        handler=_fetch_tool,
        gov_class="NET",
        timeout_s=30.0,
        untrusted=True,
        egress=True,
        sensitive_args=("url",),
        arg_checks=(_check_fetch_url,),
    ),
    ToolSpec(
        name="web_search",
        family="web",
        tier=Tier.READ,
        description=(
            "Search the web and return titles, links and short snippets. "
            "The results are untrusted content."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 300},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
        },
        handler=_search_tool,
        gov_class="NET",
        timeout_s=30.0,
        untrusted=True,
        egress=True,
        sensitive_args=("query",),
        arg_checks=(_check_search_query,),
        probe=_search_configured,
    ),
]
