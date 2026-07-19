"""Shared stdlib JSON transport for VM provider clients."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ...sandbox_backend import BackendUnavailableError


def request_json(
    *,
    provider: str,
    method: str,
    base_url: str,
    path: str,
    body: dict[str, Any] | None,
    headers: dict[str, str],
    timeout: float,
    require_object: bool = False,
    report_http_status: bool = True,
) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(f"{base_url}{path}", data=data, method=method, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BackendUnavailableError(
            f"{provider} API {method} {path} failed with HTTP {exc.code}: {detail}",
            status=exc.code if report_http_status else None,
        ) from exc
    except URLError as exc:
        raise BackendUnavailableError(f"{provider} API is unreachable: {exc}") from exc
    except TimeoutError as exc:
        raise BackendUnavailableError(f"{provider} API request timed out") from exc
    try:
        parsed = json.loads(payload) if payload else {}
    except json.JSONDecodeError as exc:
        raise BackendUnavailableError(f"{provider} API returned invalid JSON") from exc
    if require_object and not isinstance(parsed, dict):
        raise BackendUnavailableError(f"{provider} API returned a non-object response")
    return parsed
