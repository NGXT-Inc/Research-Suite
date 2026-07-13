"""Safe server-side URL unfurling for feed posts (Feed_PRD.md).

When an agent posts a link, we fetch it server-side and render a static preview
card (title, description, thumbnail) — never a live iframe and never arbitrary
HTML. The whole surface is hostile-input handling, so the module is defensive by
construction:

- **SSRF guard.** Only ``http(s)`` on ports 80/443. The hostname is resolved and
  EVERY resolved address must be a public unicast IP — any private, loopback,
  link-local, reserved, multicast, or unspecified address rejects the URL. This
  blocks the cloud control plane (which performs the fetch) from being turned
  into a proxy onto internal services. Redirects are followed manually, with the
  same validation applied to every hop.
- **Bounded.** Hard timeout, capped redirect count, capped body size, and only
  ``text/html`` is parsed for metadata.
- **Allowlist (advisory).** Common research hosts are labelled ``trusted``; the
  SSRF guard is always enforced regardless, so an unknown host is fetched under
  the same constraints, not blocked. Flip ``enforce_allowlist=True`` to harden.

Connections are pinned to the validated address while HTTP Host and TLS SNI
retain the original hostname. Stdlib-only so the same guard can run on the
stdlib-only daemon.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import urllib.parse
from html.parser import HTMLParser
from typing import Any

_USER_AGENT = "merv-feed-unfurl/1.0"
_DEFAULT_TIMEOUT = 6.0
_MAX_REDIRECTS = 4
_MAX_HTML_BYTES = 1_500_000
_MAX_IMAGE_BYTES = 5_000_000
_ALLOWED_PORTS = {None, 80, 443}

# Advisory: hosts we consider first-class research sources. Not a gate — the SSRF
# guard is what actually protects us — but surfaced as `trusted` on the preview.
ALLOWLIST_SUFFIXES = (
    "arxiv.org",
    "github.com",
    "githubusercontent.com",
    "wandb.ai",
    "huggingface.co",
    "openreview.net",
    "paperswithcode.com",
    "nature.com",
    "ar5iv.org",
    "semanticscholar.org",
)


class UnfurlError(Exception):
    """A link could not be safely unfurled (validation or fetch failed)."""


def _public_addresses(host: str) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return ()
    if not infos:
        return ()
    addresses: list[str] = []
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%", 1)[0])
        except ValueError:
            return ()
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            return ()
        if isinstance(ip, ipaddress.IPv6Address) and (
            ip.ipv4_mapped is not None
            or ip.sixtofour is not None
            or ip.teredo is not None
        ):
            return ()
        if addr not in addresses:
            addresses.append(addr)
    return tuple(addresses)


def _validate_url(url: str) -> tuple[urllib.parse.ParseResult, tuple[str, ...]]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnfurlError("only http and https links can be embedded")
    if not parsed.hostname:
        raise UnfurlError("link has no host")
    if parsed.username is not None or parsed.password is not None:
        raise UnfurlError("links may not contain credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnfurlError("link has an invalid port") from exc
    if port not in _ALLOWED_PORTS:
        raise UnfurlError("only standard web ports (80/443) are allowed")
    addresses = _public_addresses(parsed.hostname)
    if not addresses:
        raise UnfurlError("link resolves to a non-public address")
    return parsed, addresses


def _is_allowlisted(host: str) -> bool:
    host = host.lower()
    return any(host == s or host.endswith("." + s) for s in ALLOWLIST_SUFFIXES)


def _request_pinned(
    parsed: urllib.parse.ParseResult,
    *,
    address: str,
    timeout: float,
    max_bytes: int,
) -> tuple[int, Any, bytes]:
    """GET through the validated address while retaining Host and TLS SNI."""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection_cls = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_cls(parsed.hostname, port=port, timeout=timeout)
    connection._create_connection = (  # type: ignore[method-assign]
        lambda _target, connect_timeout, source_address=None: socket.create_connection(
            (address, port), connect_timeout, source_address
        )
    )
    target = parsed.path or "/"
    if parsed.query:
        target += f"?{parsed.query}"
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if parsed.scheme == "https" else 80
    if parsed.port is not None and port != default_port:
        host = f"{host}:{port}"
    try:
        connection.request(
            "GET",
            target,
            headers={"Host": host, "User-Agent": _USER_AGENT, "Accept": "*/*"},
        )
        response = connection.getresponse()
        return response.status, response.headers, response.read(max_bytes + 1)
    finally:
        connection.close()


def safe_fetch(
    url: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    max_bytes: int = _MAX_HTML_BYTES,
    max_redirects: int = _MAX_REDIRECTS,
) -> tuple[str, str, bytes]:
    """Fetch ``url`` under the SSRF guard, following redirects manually.

    Returns ``(final_url, content_type, body)``. Raises ``UnfurlError`` on any
    validation failure, redirect-limit overflow, transport error, or oversize
    body. Every redirect hop is re-validated.
    """
    current = url
    for _ in range(max_redirects + 1):
        parsed, addresses = _validate_url(current)
        transport_error: Exception | None = None
        for address in addresses:
            try:
                status, headers, body = _request_pinned(
                    parsed, address=address, timeout=timeout, max_bytes=max_bytes
                )
                break
            except (OSError, ValueError, http.client.HTTPException) as exc:
                transport_error = exc
        else:
            raise UnfurlError("could not reach the link") from transport_error
        if status in (301, 302, 303, 307, 308):
            location = headers.get("Location")
            if not location:
                raise UnfurlError("redirect without a target")
            current = urllib.parse.urljoin(current, location)
            continue
        if status >= 400:
            raise UnfurlError(f"link returned HTTP {status}")
        if len(body) > max_bytes:
            raise UnfurlError("linked content is too large to preview")
        return current, headers.get_content_type(), body
    raise UnfurlError("too many redirects")


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        # citation_author legitimately repeats (one tag per author); every other
        # key keeps its first value.
        self.authors: list[str] = []
        self.title: str = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag != "meta":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        key = (a.get("property") or a.get("name") or "").lower()
        content = a.get("content")
        if not key or not content:
            return
        if key == "citation_author":
            self.authors.append(content.strip())
        elif key not in self.meta:
            self.meta[key] = content

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            text = data.strip()
            if text:
                self.title = text


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# Hosts whose pages are papers even when the citation_* meta fails to parse.
_PAPER_HOSTS = ("arxiv.org", "ar5iv.org", "openreview.net")
_MAX_AUTHORS = 10


def _matches(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _classify(host: str, meta: dict[str, str]) -> str:
    """One coarse kind per card: paper | repo | page.

    ``citation_title`` (Highwire meta, emitted by arXiv/OpenReview/Nature/
    Semantic Scholar/…) is the paper signal; GitHub is the one repo host that
    matters to research posts.
    """
    if meta.get("citation_title") or _matches(host, _PAPER_HOSTS):
        return "paper"
    if _matches(host, ("github.com",)):
        return "repo"
    return "page"


def _publication_year(meta: dict[str, str]) -> str:
    for key in ("citation_date", "citation_publication_date", "citation_online_date"):
        m = re.search(r"(?:19|20)\d{2}", meta.get(key, ""))
        if m:
            return m.group(0)
    return ""


def extract_card(final_url: str, content_type: str, body: bytes) -> dict[str, Any]:
    """Build a preview card from an already-fetched response (pure, testable)."""
    host = (urllib.parse.urlparse(final_url).hostname or "").lower()
    trusted = _is_allowlisted(host)
    if content_type != "text/html":
        # A direct (non-HTML) link — surface a minimal card rather than parsing.
        return {
            "url": final_url,
            "title": "",
            "description": "",
            "image_url": "",
            "trusted": trusted,
            "kind": "page",
            "authors": [],
            "year": "",
        }
    parser = _MetaParser()
    try:
        parser.feed(body.decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 - hostile HTML must never crash a post
        raise UnfurlError("could not parse the linked page") from exc
    meta = parser.meta
    title = (
        meta.get("citation_title")
        or meta.get("og:title")
        or meta.get("twitter:title")
        or parser.title
    )
    description = (
        meta.get("og:description")
        or meta.get("twitter:description")
        or meta.get("description")
        or ""
    )
    image = meta.get("og:image") or meta.get("twitter:image") or meta.get("twitter:image:src") or ""
    image_url = urllib.parse.urljoin(final_url, image) if image else ""
    return {
        "url": final_url,
        "title": _clip(title, 140),
        "description": _clip(description, 280),
        "image_url": image_url,
        "trusted": trusted,
        "kind": _classify(host, meta),
        "authors": [_clip(a, 60) for a in parser.authors[:_MAX_AUTHORS]],
        "year": _publication_year(meta),
    }


# A direct arxiv PDF is not HTML (and would blow the HTML fetch cap), so its
# metadata lives on the /abs/ page instead. Old-style ids may contain a slash
# (cond-mat/0703470v2); a trailing ".pdf" is legacy arxiv link style.
_ARXIV_PDF_RE = re.compile(
    r"^https?://(?:www\.|export\.)?arxiv\.org/pdf/([^?#]+?)(?:\.pdf)?/?(?:$|[?#])",
    re.IGNORECASE,
)


def unfurl(url: str) -> dict[str, Any]:
    """Fetch ``url`` and extract a static preview card.

    Returns ``{url, title, description, image_url, trusted, kind, authors,
    year}``. ``image_url`` is the absolute URL of the preview image (caller
    re-hosts it); ``kind`` is paper|repo|page with paper cards carrying the
    citation authors/year when the page exposes them. A direct arxiv PDF link
    is unfurled via its /abs/ page (the PDF itself has no citation meta), with
    the card's ``url`` kept on the PDF so page fragments survive. Raises
    ``UnfurlError`` if the link cannot be safely fetched.
    """
    url = url.strip()
    m = _ARXIV_PDF_RE.match(url)
    if m:
        card = extract_card(*safe_fetch(f"https://arxiv.org/abs/{m.group(1)}"))
        card["url"] = url
        return card
    return extract_card(*safe_fetch(url))


def fetch_preview_image(image_url: str) -> tuple[bytes, str]:
    """Fetch a preview image under the SSRF guard. Returns ``(bytes, content_type)``.

    Raises ``UnfurlError`` if it is not a reasonably-sized image.
    """
    _final, content_type, body = safe_fetch(image_url, max_bytes=_MAX_IMAGE_BYTES)
    if not content_type.startswith("image/"):
        raise UnfurlError("preview image is not an image")
    return body, content_type
