"""HTTP client for a Merv brain.

``HttpControlPlaneClient`` implements the same
``ControlPlaneClient.call(name, arguments)`` contract by POSTing to a running
brain's ``/mcp/call`` endpoint. The
dual-wiring contract suite (``test_control_plane_contract.py``) runs the exact
same scenario corpus through it over a real in-process HTTP server — the
plane-seam analog of ``test_sandbox_backend_contract.py``.

Stdlib-only on purpose (urllib + json): proxy-local split-mode code reuses the
same plumbing without importing provider SDKs.

Error translation: the control plane returns its ``ResearchPluginError`` as a
JSON body ``{detail, error_code, ...}`` (see ``http_api.research_error_handler``).
This client rebuilds the matching exception type so callers — and the contract
scenarios — observe identical results to the in-process wiring.
"""

from __future__ import annotations

import json
from typing import Any
from urllib import error as urllib_error
from urllib.request import Request, urlopen

from ...kernel.utils import (
    NotFoundError,
    PermissionDeniedError,
    ResearchPluginError,
    ValidationError,
    WorkflowError,
)


DEFAULT_TIMEOUT_SECONDS = 60.0

# Maps an error_code on the wire back to its exception type so a remote
# ValidationError stays a ValidationError client-side (identical results across
# the in-process and HTTP wirings). Unknown codes fall back to the base type
# with the code preserved on the instance.
_ERROR_CODE_TYPES: dict[str, type[ResearchPluginError]] = {
    NotFoundError.error_code: NotFoundError,
    PermissionDeniedError.error_code: PermissionDeniedError,
    ValidationError.error_code: ValidationError,
    WorkflowError.error_code: WorkflowError,
}


class ControlPlaneUnreachableError(ResearchPluginError):
    """The brain could not be reached at all (transport failure).

    Distinct from a domain error returned by the brain: this is the proxy's
    ``cloud_unreachable`` taxonomy — the remote brain being down
    must not be confused with a validation failure it reported.
    """

    error_code = "cloud_unreachable"


class HttpControlPlaneClient:
    """One tool call against the control plane over HTTP.

    ``call(name, arguments)`` returns the tool result dict, or raises the
    rebuilt ``ResearchPluginError`` subtype the control plane reported.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        extra_context: dict[str, Any] | None = None,
    ) -> None:
        # The brain NEVER receives repo_root: extra_context is
        # for explicit project_id / client_id only; the proxy resolves the
        # repo→project mapping locally and attaches project_id here.
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.extra_context = dict(extra_context or {})

    def call(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": name,
            "arguments": dict(arguments or {}),
        }
        if self.extra_context:
            payload["context"] = dict(self.extra_context)
        body = self._post(path="/mcp/call", payload=payload)
        result = body.get("result")
        if not isinstance(result, dict):
            raise ControlPlaneUnreachableError(
                "control plane returned an invalid /mcp/call payload",
                details={"payload": body},
            )
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        body = self._get(path="/mcp/tools")
        tools = body.get("tools")
        if not isinstance(tools, list):
            raise ControlPlaneUnreachableError(
                "control plane returned an invalid /mcp/tools payload",
                details={"payload": body},
            )
        return tools

    def submit_resource_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(path="/api/data-plane/resources/observe", payload=payload)

    def validate_resource_association(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(path="/api/data-plane/resources/validate-association", payload=payload)

    def submit_resource_association(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(path="/api/data-plane/resources/associate", payload=payload)

    def validate_feed_post(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(path="/api/data-plane/feed/validate-post", payload=payload)

    def request_sandbox(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(path="/api/data-plane/sandboxes/request", payload=payload)

    def attach_sandbox(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(path="/api/data-plane/sandboxes/attach", payload=payload)

    def submit_feed_post(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(path="/api/data-plane/feed/post", payload=payload)

    # ---- transport (stdlib only) ----

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "Accept": "application/json"}

    def _get(self, *, path: str) -> dict[str, Any]:
        req = Request(self.base_url + path, method="GET", headers=self._headers())
        return self._send(req=req)

    def _post(self, *, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = Request(self.base_url + path, data=data, method="POST", headers=self._headers())
        return self._send(req=req)

    def _send(self, *, req: Request) -> dict[str, Any]:
        try:
            with urlopen(req, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib_error.HTTPError as exc:
            raise self._error_from_http(exc=exc) from exc
        except urllib_error.URLError as exc:
            raise ControlPlaneUnreachableError(
                f"control plane unreachable at {self.base_url}: {exc.reason}",
                details={"base_url": self.base_url},
            ) from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ControlPlaneUnreachableError(
                "control plane returned non-JSON response",
                details={"body": raw[:512].decode("utf-8", errors="replace")},
            ) from exc

    def _error_from_http(self, *, exc: urllib_error.HTTPError) -> ResearchPluginError:
        raw = b""
        try:
            raw = exc.read() or b""
        except Exception:  # noqa: BLE001
            pass
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        if exc.code == 401:
            return ControlPlaneUnreachableError(
                str(body.get("detail") or "control plane rejected the request"),
                details={"status": 401},
            )
        message = str(body.get("detail") or exc.reason or "control plane error")
        error_code = str(body.get("error_code") or "research_plugin_error")
        details = {
            k: v for k, v in body.items() if k not in {"detail", "error_code"}
        }
        exc_type = _ERROR_CODE_TYPES.get(error_code)
        if exc_type is not None:
            return exc_type(message, details=details)
        rebuilt = ResearchPluginError(message, details=details)
        # Preserve the wire code on the base instance so callers can branch.
        rebuilt.error_code = error_code  # type: ignore[misc]
        return rebuilt
