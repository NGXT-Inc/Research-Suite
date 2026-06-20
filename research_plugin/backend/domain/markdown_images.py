"""Markdown image-link helpers shared by lints and data-plane readers."""

from __future__ import annotations

import re


MARKDOWN_FIGURE_MAX_BYTES = 5_000_000

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_IMAGE_LINK_RE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")


def markdown_image_links(markdown_text: str) -> list[str]:
    """Relative markdown image links, in order."""
    links: list[str] = []
    for target in markdown_image_targets(markdown_text):
        if target.startswith(("http://", "https://", "data:", "/")):
            continue
        links.append(target)
    return links


def markdown_image_targets(markdown_text: str) -> list[str]:
    """All markdown image targets, including external and absolute links."""
    stripped = _HTML_COMMENT_RE.sub("", markdown_text)
    return [match.group(1) for match in _IMAGE_LINK_RE.finditer(stripped)]
