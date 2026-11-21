"""Resolve live-browser address-bar text into an address or a web search."""

from __future__ import annotations

import pytest

from wisp.coding.browser_address import ResolvedBrowserAddress, resolve_browser_address
from wisp.coding.browser_control import BrowserControlError
from wisp.config.settings import BrowserSearchEngine


@pytest.mark.parametrize(
    ("text", "url"),
    [
        ("example.com", "https://example.com"),
        ("  example.com/docs?page=2#top  ", "https://example.com/docs?page=2#top"),
        ("localhost", "http://localhost"),
        ("LocalHost:3000/app", "http://LocalHost:3000/app"),
        ("127.0.0.1:8080", "http://127.0.0.1:8080"),
        ("127.0.0.2", "http://127.0.0.2"),
        ("[::1]:8080/", "http://[::1]:8080/"),
        ("[2001:db8::1]", "https://[2001:db8::1]"),
        ("10.0.0.1", "https://10.0.0.1"),
        ("example.com:8443", "https://example.com:8443"),
        ("devbox:8080", "https://devbox:8080"),
        ("x.com/r?u=https://y.com", "https://x.com/r?u=https://y.com"),
        ("http://example.com", "http://example.com"),
        ("https://localhost:3000", "https://localhost:3000"),
        ("HTTPS://Example.com/Path", "https://Example.com/Path"),
        ("Http://example.com", "http://example.com"),
    ],
)
def test_addresses(text: str, url: str) -> None:
    assert resolve_browser_address(text, "google") == ResolvedBrowserAddress(url, is_search=False)


@pytest.mark.parametrize(
    ("text", "query"),
    [
        ("rust", "rust"),
        ("rust traits", "rust+traits"),
        ("see https://x.com", "see+https%3A%2F%2Fx.com"),
        ("a&b#c+d", "a%26b%23c%2Bd"),
        ("café au lait", "caf%C3%A9+au+lait"),
        ("foo:bar", "foo%3Abar"),
        ("localhost:dev", "localhost%3Adev"),
        (":8080", "%3A8080"),
        ("::1", "%3A%3A1"),
        ("devbox", "devbox"),
        # "@" after the first word is not in an authority, so it may be searched.
        ("email me@example.com", "email+me%40example.com"),
        ("what is user@host", "what+is+user%40host"),
    ],
)
def test_searches(text: str, query: str) -> None:
    assert resolve_browser_address(text, "google") == ResolvedBrowserAddress(
        f"https://www.google.com/search?q={query}", is_search=True
    )


@pytest.mark.parametrize(
    ("engine", "url"),
    [
        ("google", "https://www.google.com/search?q=wisp+agent"),
        ("duckduckgo", "https://duckduckgo.com/?q=wisp+agent"),
        ("bing", "https://www.bing.com/search?q=wisp+agent"),
    ],
)
def test_engine_urls(engine: BrowserSearchEngine, url: str) -> None:
    assert resolve_browser_address("wisp agent", engine).url == url


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "Enter an address or search text"),
        ("   ", "Enter an address or search text"),
        ("ftp://example.com", "not ftp:"),
        ("file:///etc/passwd", "not file:"),
        ("javascript://alert(1)", "not javascript:"),
        ("FILE:///etc/passwd", "not file:"),
        # Credentials are rejected, never sent to a search engine.
        ("user:pass@host", "without embedded credentials"),
        ("user:pass@example.com/path", "without embedded credentials"),
        ("https://user:pass@example.com", "without embedded credentials"),
        ("user:pass@host/path with spaces", "HTTP or HTTPS URL"),
        ("//user:pass@host/path", "without embedded credentials"),
        ("localhost:65536", "HTTP or HTTPS URL"),
        ("http://", "HTTP or HTTPS URL"),
        ("https://exa mple.com", "HTTP or HTTPS URL"),
        ("[::1", "HTTP or HTTPS URL"),
        ("example.com\x00", "control characters"),
        ("two\twords", "control characters"),
        ("rust\x85traits", "control characters"),
        pytest.param("x" * 2049, "longer than 2,048 characters", id="long-input"),
        # Exactly at the input limit, so it is searched; the search URL is too long.
        pytest.param("x" * 2048, "search is too long", id="limit-input"),
        # Within the input limit, but percent-encoding triples each "&".
        pytest.param("& " * 600, "search is too long", id="long-search"),
    ],
)
def test_rejected(text: str, message: str) -> None:
    with pytest.raises(BrowserControlError, match=message):
        resolve_browser_address(text, "google")


def test_length_limits_are_inclusive() -> None:
    """A resolved address or search URL may be exactly 2,048 characters long."""
    host = "a" * (2048 - len("https://") - len(".com"))
    assert resolve_browser_address(f"{host}.com", "bing").url == f"https://{host}.com"
    with pytest.raises(BrowserControlError, match="longer than 2,048 characters"):
        resolve_browser_address(f"{host}a.com", "bing")
    prefix = "https://www.google.com/search?q="
    query = "q" * (2048 - len(prefix))
    assert resolve_browser_address(query, "google").url == prefix + query
    with pytest.raises(BrowserControlError, match="search is too long"):
        resolve_browser_address(query + "q", "google")
