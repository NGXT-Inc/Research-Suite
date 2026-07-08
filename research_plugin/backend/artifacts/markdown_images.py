"""Markdown image-link helpers shared by lints and data-plane readers."""

from __future__ import annotations

import re


MARKDOWN_FIGURE_MAX_BYTES = 5_000_000

# Gated markdown roles whose relative image links are captured as submitted
# figures at resource.register (association) time. Single source of truth shared by the
# data-plane reader (reads the figure bytes) and the control plane (pins them
# into the blob store + report_figures index).
MARKDOWN_FIGURE_ROLES = frozenset({"plan", "report", "reflection_doc", "synthesis_doc"})

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
