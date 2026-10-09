"""URL table for check_url, and the resolver pinning in resolve_and_pin.

The resolver is replaced with a fake, so no test performs a DNS lookup.
"""
from __future__ import annotations

import socket

import pytest

from coworker.policy import urls
from coworker.policy.urls import UrlRefused, check_url, resolve_and_pin


@pytest.mark.parametrize("url", [
    "https://example.com/path?q=1",
    "http://example.com",
    "HTTPS://Example.COM/",
    "https://example.com:8443/a",
    "https://example.com.",              # a trailing dot names the same host
    "https://93.184.216.34/",            # a public IPv4 literal
    "https://[2001:4860:4860::8888]/",   # a public IPv6 literal
    "http://[::ffff:8.8.8.8]/",          # IPv4-mapped public address
])
def test_public_addresses_are_allowed(url):
    assert check_url(url) is None


@pytest.mark.parametrize("url", [
    "ftp://example.com/",
    "file:///C:/Windows/win.ini",
    "javascript:alert(1)",
    "data:text/plain,hello",
    "//example.com/relative",
    "example.com/no-scheme",
    "https://",
    "https://example.com\\@10.0.0.1",    # backslash: browsers and urlsplit disagree
    "https://example.com/\n",            # control character
    "https://example.com:99999/",        # port out of range
    "https://example.com:notaport/",
    "http://[::1",                       # malformed IPv6 literal
    "",
    None,
])
def test_schemes_and_malformed_urls_are_refused(url):
    assert check_url(url) == "url_refused"


@pytest.mark.parametrize("url", [
    "https://user:pass@example.com/",
    "https://user@example.com/",
    "https://@example.com/",
])
def test_userinfo_is_refused(url):
    assert check_url(url) == "url_refused"


@pytest.mark.parametrize("url", [
    "https://localhost:8000/",
    "https://LOCALHOST/",
    "http://api.localhost/",
    "http://localhost./",
    "http://.",
])
def test_localhost_names_are_refused(url):
    assert check_url(url) == "url_refused"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://10.0.0.5/",
    "http://172.16.0.1/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata service
    "http://0.0.0.0/",                            # unspecified
    "http://224.0.0.1/",                          # multicast
    "http://240.0.0.1/",                          # reserved
    "http://100.64.0.1/",                         # carrier-grade NAT, not public
    "http://192.0.2.1/",                          # documentation range
])
def test_non_public_ipv4_literals_are_refused(url):
    assert check_url(url) == "url_refused"


@pytest.mark.parametrize("url", [
    "http://[::1]/",                  # loopback
    "http://[::]/",                   # unspecified
    "http://[fe80::1]/",              # link-local
    "http://[fc00::1]/",              # unique local
    "http://[ff02::1]/",              # multicast
    "http://[::ffff:127.0.0.1]/",     # IPv4-mapped loopback
    "http://[2002:7f00:1::1]/",       # 6to4 wrapping 127.0.0.1
])
def test_non_public_ipv6_literals_are_refused(url):
    assert check_url(url) == "url_refused"


@pytest.mark.parametrize("url", [
    "http://2130706433/",             # 127.0.0.1 written as one decimal number
    "http://127.1/",                  # shortened IPv4
    "http://0x7f000001/",             # hexadecimal IPv4
    "http://example.123/",            # numeric top-level domain
])
def test_numeric_host_spellings_are_refused_without_resolving(url):
    assert check_url(url) == "url_refused"


def test_hostnames_are_judged_by_resolution_not_by_their_text():
    # check_url sees only the text; a name that points at a private address is caught by resolve_and_pin.
    assert check_url("https://intranet.example.com/") is None


def _fake_resolver(monkeypatch, addresses):
    def fake(host, port, *args, **kwargs):
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in addresses]
    monkeypatch.setattr(urls.socket, "getaddrinfo", fake)


def test_resolve_and_pin_returns_public_addresses_once_each(monkeypatch):
    _fake_resolver(monkeypatch, ["93.184.216.34", "93.184.216.34", "2001:4860:4860::8888"])
    assert resolve_and_pin("example.com") == ["93.184.216.34", "2001:4860:4860::8888"]


def test_resolve_and_pin_refuses_when_any_answer_is_private(monkeypatch):
    _fake_resolver(monkeypatch, ["93.184.216.34", "127.0.0.1"])
    with pytest.raises(UrlRefused) as info:
        resolve_and_pin("rebind.example.com")
    assert info.value.code == "url_refused"


def test_resolve_and_pin_refuses_the_metadata_address(monkeypatch):
    _fake_resolver(monkeypatch, ["169.254.169.254"])
    with pytest.raises(UrlRefused):
        resolve_and_pin("metadata.example.com")


def test_resolve_and_pin_refuses_an_ipv6_loopback_answer(monkeypatch):
    _fake_resolver(monkeypatch, ["::1"])
    with pytest.raises(UrlRefused):
        resolve_and_pin("v6.example.com")


def test_resolve_and_pin_refuses_an_empty_answer(monkeypatch):
    _fake_resolver(monkeypatch, [])
    with pytest.raises(UrlRefused):
        resolve_and_pin("empty.example.com")


def test_resolve_and_pin_lets_a_failed_lookup_raise_the_resolver_error(monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror("no such host")
    monkeypatch.setattr(urls.socket, "getaddrinfo", fail)
    with pytest.raises(socket.gaierror):
        resolve_and_pin("missing.example.com")
