"""Turn text typed for the live browser into an address to open or a web search."""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote_plus, urlsplit

from wisp.coding.browser_control import BrowserControlError
from wisp.coding.browser_live import validate_browser_url
from wisp.config.settings import BrowserSearchEngine
from wisp.events import MAX_BROWSER_URL_CHARS

_SEARCH_URLS: dict[BrowserSearchEngine, str] = {
    "google": "https://www.google.com/search?q=",
    "duckduckgo": "https://duckduckgo.com/?q=",
    "bing": "https://www.bing.com/search?q=",
}
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*(?=://)")
_AUTHORITY_END = re.compile(r"[/?#]")
_LEADING_AUTHORITY_END = re.compile(r"[\s/?#]")
_TOO_LONG = f"The address is longer than {MAX_BROWSER_URL_CHARS:,} characters"


@dataclass(frozen=True, slots=True)
class ResolvedBrowserAddress:
    """Where the live browser should go for one piece of typed text.

    Attributes:
        url (str): HTTP or HTTPS URL that passed ``validate_browser_url``.
        is_search (bool): True when ``url`` is a search for the typed text.
    """

    url: str
    is_search: bool


def resolve_browser_address(text: str, engine: BrowserSearchEngine) -> ResolvedBrowserAddress:
    """Decide whether typed text is an address or a search, as a browser's address bar does.

    Text starting with ``scheme://`` is an address and must use HTTP or HTTPS. Text
    whose leading authority (everything before the first whitespace, ``/``, ``?`` or
    ``#``, ignoring a leading ``//``) contains ``@`` is treated as an address and so
    rejected, never searched: it may carry credentials such as ``user:password@host``.
    Other text containing whitespace is a search. A single word is an address when
    its host is ``localhost``, contains a dot, is a bracketed IPv6 literal, or carries
    an all-digit port; anything else is a search. Addresses without a scheme get
    ``http://`` for ``localhost`` and loopback IPs and ``https://`` otherwise.

    Args:
        text (str): What the user typed; surrounding whitespace is ignored.
        engine (BrowserSearchEngine): Search engine used when the text is not an address.

    Returns:
        ResolvedBrowserAddress: The URL to open and whether it is a search.

    Examples:
        >>> resolve_browser_address("localhost:3000", "google").url
        'http://localhost:3000'
        >>> resolve_browser_address("rust traits", "duckduckgo").url
        'https://duckduckgo.com/?q=rust+traits'

    Raises:
        BrowserControlError: The text is empty or contains control characters, names a
            scheme other than HTTP or HTTPS, is an invalid address, or it or the URL it
            resolves to is longer than 2,048 characters.
    """
    text = text.strip()
    if not text:
        raise BrowserControlError("Enter an address or search text")
    if len(text) > MAX_BROWSER_URL_CHARS:
        raise BrowserControlError(_TOO_LONG)
    if any(unicodedata.category(ch) == "Cc" for ch in text):
        raise BrowserControlError("The address contains control characters")
    if scheme := _SCHEME.match(text):
        name = scheme.group().lower()
        if name not in {"http", "https"}:
            raise BrowserControlError(f"Only HTTP and HTTPS addresses can be opened, not {name}:")
        return _address(name + text[scheme.end() :])
    if _has_userinfo(text):
        # Possible credentials must never reach a search engine; as an address they
        # are rejected by validate_browser_url instead.
        return _address(f"https://{text.removeprefix('//')}")
    if any(ch.isspace() for ch in text) or not _looks_like_address(text):
        return _search(text, engine)
    return _address(f"{_default_scheme(text)}://{text}")


def _address(url: str) -> ResolvedBrowserAddress:
    # An added scheme can push a long address past the limit every event accepts.
    if len(url) > MAX_BROWSER_URL_CHARS:
        raise BrowserControlError(_TOO_LONG)
    return ResolvedBrowserAddress(url=validate_browser_url(url), is_search=False)


def _search(text: str, engine: BrowserSearchEngine) -> ResolvedBrowserAddress:
    url = _SEARCH_URLS[engine] + quote_plus(text)
    if len(url) > MAX_BROWSER_URL_CHARS:
        raise BrowserControlError("The search is too long; shorten it and try again")
    return ResolvedBrowserAddress(url=url, is_search=True)


def _has_userinfo(text: str) -> bool:
    return "@" in _LEADING_AUTHORITY_END.split(text.removeprefix("//"), maxsplit=1)[0]


def _looks_like_address(text: str) -> bool:
    authority = _AUTHORITY_END.split(text, maxsplit=1)[0]
    if authority.startswith("["):
        return True
    host, separator, port = authority.partition(":")
    if separator and not (port.isascii() and port.isdigit()):
        return False
    return host.lower() == "localhost" or "." in host or (bool(separator) and bool(host))


def _default_scheme(text: str) -> str:
    try:
        host = urlsplit(f"//{text}").hostname
    except ValueError:
        return "https"
    if host is None:
        return "https"
    if host == "localhost":
        return "http"
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        return "https"
    return "http" if loopback else "https"
