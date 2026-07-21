"""Dependency-free proxy error taxonomy shared by routing and transport."""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlsplit


class UpstreamError(Exception):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "cloud_unreachable",
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.details = details or {}


def brain_not_running_message(control_url: Optional[str]) -> str:
    return (
        "Merv brain server is not running"
        + (f" at {control_url}" if control_url else "")
        + ". Start it with:\n"
        "    merv-http\n"
        "If it is on another port, set MERV_CONTROL_URL to the brain URL."
    )


def is_loopback_url(url: Optional[str]) -> bool:
    if not url:
        return False
    return (urlsplit(url).hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
