"""Shared feed embed limits and CSP wrapping."""

from __future__ import annotations

import re

MAX_FEED_EMBED_BYTES = 524_288

# Defense-in-depth CSP baked into the stored document itself. The UI also
# serves it under an iframe `sandbox` attribute and a response-header CSP
# (see transport/feed_http.py); this meta tag covers any other consumer.
_EMBED_CSP_CONTENT = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; media-src data:"
)
_EMBED_CSP_META = f'<meta http-equiv="Content-Security-Policy" content="{_EMBED_CSP_CONTENT}">'

_HEAD_SNIFF_PREFIX = 4096

# Tag-boundary matches so `<header>`/`<html-ish>` text never counts as the
# document's <head>/<html> element.
_HEAD_TAG_RE = re.compile(r"<head[\s>]")
_HTML_TAG_RE = re.compile(r"<html[\s>]")


def _looks_like_html(data: bytes) -> bool:
    """True for a full document, or any fragment that starts with a tag.

    Reject binaries outright (anything not opening with ``<``); a bare
    fragment like ``<div>...</div>`` is legitimate embed content and gets
    wrapped into a full document by ``wrap_embed_html``.
    """
    head = data[:_HEAD_SNIFF_PREFIX].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<!doctype html", b"<html")):
        return True
    if b"<html" in head:
        return True
    return head.startswith(b"<")


def sniff_html_type(data: bytes) -> str | None:
    """Best-effort content-type check: must look like an HTML document."""
    return "text/html" if _looks_like_html(data) else None


def wrap_embed_html(data: bytes) -> str:
    """Return ``data`` decoded and with a CSP meta tag as the first <head> child.

    A full document gets the tag injected right after its opening <head>; a
    bare fragment is wrapped in a minimal document skeleton first.
    """
    text = data.decode("utf-8", errors="replace")
    lower = text.lower()
    head_match = _HEAD_TAG_RE.search(lower)
    if head_match is not None:
        close_idx = text.find(">", head_match.start())
        if close_idx != -1:
            insert_at = close_idx + 1
            return text[:insert_at] + _EMBED_CSP_META + text[insert_at:]
    html_match = _HTML_TAG_RE.search(lower)
    if html_match is not None:
        # Has an <html> but no <head> — insert one right after the tag.
        close_idx = text.find(">", html_match.start())
        if close_idx != -1:
            insert_at = close_idx + 1
            return (
                text[:insert_at]
                + f"<head>{_EMBED_CSP_META}</head>"
                + text[insert_at:]
            )
    return (
        "<!doctype html><html><head>"
        f"{_EMBED_CSP_META}"
        f"</head><body>{text}</body></html>"
    )
