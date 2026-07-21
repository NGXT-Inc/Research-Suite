"""Small stdlib JSON-over-HTTP transport for the hosted/local brain."""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any, Callable, Optional
from urllib import error as urllib_error
from urllib.request import Request

from .errors import UpstreamError, brain_not_running_message, is_loopback_url


class StdlibHttpClient:
    def __init__(
        self,
        *,
        control_url: Optional[str],
        timeout_seconds: float,
        headers: Callable[[bool], dict[str, str]],
        opener: Callable[..., Any],
    ) -> None:
        self.control_url = control_url
        self.timeout_seconds = timeout_seconds
        self._headers = headers
        self._opener = opener

    def get(self, *, url: str, is_cloud: bool) -> dict[str, Any]:
        request = Request(url, method="GET", headers=self._headers(is_cloud))
        return self.send(request=request, timeout=self.timeout_seconds)

    def post(
        self,
        *,
        url: str,
        payload: dict[str, Any],
        is_cloud: bool,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=self._headers(is_cloud),
        )
        return self.send(request=request, timeout=timeout or self.timeout_seconds)

    def send(self, *, request: Request, timeout: float) -> dict[str, Any]:
        try:
            with self._opener(request, timeout=timeout) as response:
                body_bytes = response.read()
        except urllib_error.HTTPError as exc:
            raise self.error_from_http(exc) from exc
        except urllib_error.URLError as exc:
            if is_loopback_url(self.control_url):
                raise UpstreamError(
                    brain_not_running_message(self.control_url),
                    error_code="brain_not_running",
                    details={"reason": str(exc.reason)},
                ) from exc
            raise UpstreamError(
                f"control plane unreachable: {exc.reason}",
                error_code="cloud_unreachable",
                details={"reason": str(exc.reason)},
            ) from exc
        try:
            return json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise UpstreamError(
                "upstream returned non-JSON response",
                error_code="daemon_bad_response",
                details={"body": body_bytes[:512].decode("utf-8", errors="replace")},
            ) from exc

    @staticmethod
    def error_from_http(exc: urllib_error.HTTPError) -> UpstreamError:
        raw = b""
        with suppress(Exception):
            raw = exc.read() or b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        message = body.get("detail") or exc.reason or "upstream returned HTTP error"
        error_code = body.get("error_code") or "upstream_http_error"
        details = {
            key: value
            for key, value in body.items()
            if key not in {"detail", "error_code"}
        }
        details.setdefault("status", exc.code)
        return UpstreamError(str(message), error_code=str(error_code), details=details)
