"""Stdio MCP server that proxies tool calls to the backend.

The MCP server itself owns no state. Codex launches it over stdio; it forwards
``tools/list`` and ``tools/call`` to the HTTP backend and returns the response
on stdout.

Two topologies (cloud plan Phase 8, §3.3), selected by config:

- **Single upstream (local mode).** ``control_url`` unset: every call goes to
  the local daemon at ``daemon_url`` exactly as before — bit-identical, with the
  same friendly ``127.0.0.1:8787`` fallback and discovery order.
- **Dual upstream (split mode).** ``control_url`` set: route on the tool's
  ``plane`` (read off the served catalog, so it can never drift from
  ``contracts``): ``control`` → the cloud, ``data`` → the local daemon,
  ``aggregate`` → both, merged. The cloud receives an explicit ``project_id``
  (resolved via the daemon's ``/local/route``) and a bearer token, NEVER
  ``repo_root``; data calls carry ``repo_root`` to the local daemon only.

Error taxonomy is returned as TOOL RESULTS, not ``-32000`` protocol errors, so a
client never disables the server over a transient outage: ``local_daemon_not_
running``, ``cloud_unreachable``, ``auth_expired``. A cloud outage never blocks
data tools and vice versa. In split mode a missing ``control_url`` is a hard
config error (no silent loopback fallback); local mode keeps the friendly one.

Discovery order for the daemon URL:

1. ``RESEARCH_PLUGIN_DAEMON_URL`` environment variable.
2. ``<repo_root>/.research_plugin/daemon.json`` written by the daemon.
3. ``RESEARCH_PLUGIN_DEFAULT_DAEMON_URL`` or ``http://127.0.0.1:8787``.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, TextIO
from urllib import error as urllib_error
from urllib.request import Request, urlopen

from . import __version__
from .daemon_marker import discover_daemon_url


DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_DAEMON_URL = "http://127.0.0.1:8787"
# sandbox.request can take minutes; the proxy returns its handle promptly and
# the agent polls sandbox.get (plan §3.3). A short bound keeps a long-running
# verb from blocking the stdio loop — it lands a row in 'provisioning' and the
# agent polls. Kept generous so a fast create still returns SSH inline.
LONG_VERB_TIMEOUT_SECONDS = 90.0
LONG_VERBS = frozenset({"sandbox.request"})

# The transport taxonomy (plan §3.3): returned as TOOL RESULTS, not protocol
# errors, so a transient outage of one plane never disables the server. Domain
# errors the upstream reports (validation_error, …) stay protocol errors.
_TRANSPORT_ERROR_CODES = frozenset(
    {
        "daemon_not_running",
        "local_daemon_not_running",
        "cloud_unreachable",
        "auth_expired",
        "daemon_bad_response",
    }
)


@dataclass(frozen=True)
class ProxyConfig:
    repo_root: Path
    daemon_url: str | None
    # Split mode (Phase 8): the cloud control-plane URL + bearer token. None ⇒
    # single-upstream local mode (byte-identical to before this phase).
    control_url: str | None = None
    token: str | None = None
    # The daemon's loopback auth secret (risk 11): sent to the local daemon so
    # the credential-holding loopback surface accepts the proxy's calls.
    daemon_secret: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def with_url(self, url: str) -> "ProxyConfig":
        return ProxyConfig(
            repo_root=self.repo_root,
            daemon_url=url,
            control_url=self.control_url,
            token=self.token,
            daemon_secret=self.daemon_secret,
            timeout_seconds=self.timeout_seconds,
        )

    @property
    def split_mode(self) -> bool:
        return bool(self.control_url)


class _UpstreamError(Exception):
    """An upstream was unreachable or returned a non-2xx.

    Carries the proxy's error taxonomy code (plan §3.3): ``daemon_not_running``
    / ``local_daemon_not_running`` (data plane), ``cloud_unreachable`` (control
    plane), ``auth_expired`` (401 from the cloud). Surfaced as a TOOL RESULT,
    not a protocol error, so the client never disables the server.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "daemon_unreachable",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.details = details or {}


def _daemon_not_running_message(*, repo_root: Path) -> str:
    return (
        "research_plugin HTTP daemon is not running for repo "
        f"{repo_root}. Start it with:\n"
        "    research-plugin-http\n"
        "The MCP proxy also tries http://127.0.0.1:8787 by default. "
        "If the daemon is on another port, set RESEARCH_PLUGIN_DAEMON_URL "
        "to the shared daemon's URL."
    )


class HttpProxyMcpServer:
    """JSON-RPC MCP server that forwards every tool call to the backend."""

    def __init__(self, *, config: ProxyConfig) -> None:
        self.config = config
        # Cache the daemon's repo_root→project_id resolution so split-mode cloud
        # calls don't re-hit /local/route every time.
        self._project_id: str | None = None
        self._scoped_cache: set[str] | None = None
        self._plane_cache: dict[str, str] | None = None

    # ---- stdio loop ------------------------------------------------------

    def serve(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                response = self.handle(request=request)
            except Exception as exc:  # pragma: no cover - last-resort guard
                response = self._error_response(
                    request_id=None,
                    code=-32603,
                    message="internal error",
                    data={"detail": str(exc)},
                )
                traceback.print_exc(file=sys.stderr)
            if response is not None:
                stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
                stdout.flush()

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params") or {}
        try:
            if method == "initialize":
                return self._result(
                    request_id=request_id,
                    result={
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "research-plugin", "version": __version__},
                    },
                )
            if method == "notifications/initialized":
                return None
            if method == "ping":
                return self._result(request_id=request_id, result={})
            if method == "tools/list":
                tools = self._list_tools()
                return self._result(request_id=request_id, result={"tools": tools})
            if method == "tools/call":
                name = params.get("name", "")
                arguments = params.get("arguments") or {}
                result = self._call_tool(name=name, arguments=arguments)
                return self._result(request_id=request_id, result=self._tool_result(result=result))
            return self._error_response(
                request_id=request_id,
                code=-32601,
                message=f"method not found: {method}",
            )
        except _UpstreamError as exc:
            # TRANSPORT taxonomy (plan §3.3) comes back as a TOOL RESULT so a
            # transient outage of one plane never disables the whole server (and
            # never blocks the other plane's tools). DOMAIN errors the upstream
            # reported (validation_error, not_found, …) keep the old -32000
            # protocol-error shape so existing clients/tests are unchanged.
            if method == "tools/call" and exc.error_code in _TRANSPORT_ERROR_CODES:
                return self._result(
                    request_id=request_id,
                    result=self._tool_result(
                        result={
                            "ok": False,
                            "error": exc.message,
                            "error_code": exc.error_code,
                            **exc.details,
                        },
                        is_error=True,
                    ),
                )
            return self._error_response(
                request_id=request_id,
                code=-32000,
                message=exc.message,
                data={"error_code": exc.error_code, **exc.details},
            )
        except TypeError as exc:
            return self._error_response(
                request_id=request_id,
                code=-32602,
                message=f"invalid params: {exc}",
            )
        except Exception as exc:  # pragma: no cover - exposes unexpected bugs
            traceback.print_exc(file=sys.stderr)
            return self._error_response(request_id=request_id, code=-32603, message=str(exc))

    # ---- tools/list ------------------------------------------------------

    def _list_tools(self) -> list[dict[str, Any]]:
        if not self.config.split_mode:
            # Single upstream (local): bit-identical to before this phase.
            return self._catalog_from(url=self._require_daemon_url(), is_cloud=False)
        # Dual upstream: merge both catalogs. A tool's home upstream is its
        # plane (data → daemon, control → cloud, aggregate → either; aggregate
        # tools live on both, so dedup by name preferring the daemon's schema
        # which carries the data-side enrichment shape). project_id stripping
        # and project.list hiding apply uniformly to the merged set.
        merged: dict[str, dict[str, Any]] = {}
        # Cloud first, daemon second so daemon (data/aggregate) schemas win on
        # overlap — but in practice planes are disjoint except aggregate.
        for is_cloud, tool in self._each_catalog_tool():
            if tool.get("name") == "project.list":
                continue
            shaped = self._with_hidden_project_scope(tool=tool)
            merged[shaped["name"]] = shaped
        return list(merged.values())

    def _catalog_from(self, *, url: str, is_cloud: bool) -> list[dict[str, Any]]:
        body = self._http_get(url=f"{url}/mcp/tools", is_cloud=is_cloud)
        tools = body.get("tools") or []
        if not isinstance(tools, list):
            raise _UpstreamError(
                "upstream returned an invalid /mcp/tools payload",
                error_code="daemon_bad_response",
                details={"payload": body},
            )
        return [
            self._with_hidden_project_scope(tool=tool)
            for tool in tools
            if tool.get("name") != "project.list"
        ]

    def _each_catalog_tool(self) -> Iterator[tuple[bool, dict[str, Any]]]:
        """Yield (is_cloud, raw_tool) for every reachable upstream's /mcp/tools.

        Raw = pre-strip, so callers can read 'plane' and project_id schema. A
        down upstream is skipped, never fatal.
        """
        for is_cloud, url in (
            (True, self.config.control_url),
            (False, self._daemon_url_or_none()),
        ):
            if not url:
                continue
            try:
                body = self._http_get(url=f"{url}/mcp/tools", is_cloud=is_cloud)
            except _UpstreamError:
                continue
            for tool in body.get("tools") or []:
                if isinstance(tool, dict):
                    yield is_cloud, tool

    # ---- tools/call ------------------------------------------------------

    def _call_tool(self, *, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.config.split_mode:
            # Single upstream (local): everything to the daemon with repo_root.
            return self._call_daemon(name=name, arguments=arguments)
        plane = self._plane_for(name=name)
        if plane == "control":
            return self._call_cloud(name=name, arguments=arguments)
        if plane == "data":
            return self._call_daemon(name=name, arguments=arguments)
        # aggregate: merge both planes' answers (plan §3.3).
        return self._call_aggregate(name=name, arguments=arguments)

    def _call_daemon(self, *, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        url = self._require_daemon_url()
        body = self._http_post(
            url=f"{url}/mcp/call",
            payload={
                "name": name,
                "arguments": arguments,
                # Data-plane calls carry repo_root to the LOCAL daemon only.
                "context": {"repo_root": str(self.config.repo_root)},
            },
            is_cloud=False,
            timeout=self._timeout_for(name=name),
        )
        return self._result_dict(body=body)

    def _call_cloud(self, *, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.config.control_url:
            # Split mode without a control URL is a hard config error — no
            # silent loopback fallback for control-plane tools (plan §3.3).
            raise _UpstreamError(
                "control plane URL is not configured (RESEARCH_PLUGIN_CONTROL_URL); "
                "set it to the cloud control plane or unset split mode",
                error_code="cloud_unreachable",
            )
        args = dict(arguments)
        # Identity on the wire (§3.2): the cloud gets an explicit project_id and
        # NEVER repo_root. Resolve it via the local daemon's route map — but ONLY
        # for tools that actually take project_id (e.g. review.start/submit are
        # capability-scoped, not project-scoped, and reject the extra field).
        if "project_id" not in args and self._tool_is_project_scoped(name=name):
            project_id = self._resolve_project_id()
            if project_id:
                args["project_id"] = project_id
        body = self._http_post(
            url=f"{self.config.control_url}/mcp/call",
            payload={"name": name, "arguments": args},
            is_cloud=True,
            timeout=self._timeout_for(name=name),
        )
        return self._result_dict(body=body)

    def _call_aggregate(self, *, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "sandbox.health":
            return self._aggregate_health()
        # sandbox.get and any future aggregate: cloud row facts merged with the
        # daemon's machine-local enrichment (ssh command, local_dir, conn
        # state). Cloud-down must not block; daemon-down must not block.
        cloud: dict[str, Any] = {}
        cloud_err: dict[str, Any] | None = None
        try:
            cloud = self._call_cloud(name=name, arguments=arguments)
        except _UpstreamError as exc:
            cloud_err = {"error": exc.message, "error_code": exc.error_code}
        daemon: dict[str, Any] = {}
        daemon_err: dict[str, Any] | None = None
        try:
            daemon = self._call_daemon(name=name, arguments=arguments)
        except _UpstreamError as exc:
            daemon_err = {"error": exc.message, "error_code": exc.error_code}
        # Cloud row facts are the base view. The daemon contributes only
        # machine-local enrichment, matching backend.services.sandbox.sandbox_views'
        # agent view shape without importing backend code into the stdlib proxy.
        merged = dict(cloud)
        if any(daemon.get(key) for key in ("command", "raw_command", "key_path")):
            ssh = dict(merged.get("ssh") or {})
            if daemon.get("command"):
                ssh["command"] = daemon["command"]
            if daemon.get("raw_command"):
                ssh["raw_command"] = daemon["raw_command"]
            if daemon.get("key_path"):
                ssh["key_path"] = daemon["key_path"]
            merged["ssh"] = ssh
        if daemon.get("local_dir"):
            merged["local_experiment_dir"] = daemon["local_dir"]
        if cloud_err:
            merged["control_plane"] = cloud_err
        if daemon_err:
            merged["data_plane"] = daemon_err
        return {key: value for key, value in merged.items() if not key.startswith("_")}

    def _aggregate_health(self) -> dict[str, Any]:
        # daemon self-check + cloud reachability + auth status (plan §3.3).
        data_ok, data_detail = self._probe(is_cloud=False)
        cloud_ok, cloud_detail = (True, {})
        auth_ok = True
        if self.config.split_mode:
            cloud_ok, cloud_detail = self._probe(is_cloud=True)
            auth_ok = cloud_detail.get("error_code") != "auth_expired"
        return {
            "ok": bool(data_ok and (cloud_ok or not self.config.split_mode)),
            "data_plane": {"reachable": data_ok, **data_detail},
            "control_plane": {
                "reachable": cloud_ok,
                "auth_ok": auth_ok,
                "configured": self.config.split_mode,
                **cloud_detail,
            },
        }

    def _probe(self, *, is_cloud: bool) -> tuple[bool, dict[str, Any]]:
        url = self.config.control_url if is_cloud else self._daemon_url_or_none()
        if not url:
            return False, {"error_code": "not_configured"}
        try:
            self._http_get(url=f"{url}/health", is_cloud=is_cloud)
            return True, {}
        except _UpstreamError as exc:
            return False, {"error": exc.message, "error_code": exc.error_code}

    # ---- identity resolution (split mode) --------------------------------

    def _resolve_project_id(self) -> str | None:
        if self._project_id is not None:
            return self._project_id
        url = self._daemon_url_or_none()
        if not url:
            return None
        try:
            from urllib.parse import quote

            body = self._http_get(
                url=f"{url}/local/route?repo_root={quote(str(self.config.repo_root))}",
                is_cloud=False,
            )
        except _UpstreamError:
            return None
        project_id = body.get("project_id")
        if isinstance(project_id, str) and project_id:
            self._project_id = project_id
            return project_id
        return None

    # ---- helpers ---------------------------------------------------------

    def _tool_is_project_scoped(self, *, name: str) -> bool:
        # A control tool is project-scoped iff its raw input schema declares a
        # project_id property (read from the catalog BEFORE the proxy strips it
        # for the client). Capability-scoped tools (review.start/submit) are not.
        if self._scoped_cache is None:
            scoped: set[str] = set()
            for _is_cloud, tool in self._each_catalog_tool():
                schema = tool.get("inputSchema")
                props = schema.get("properties") if isinstance(schema, dict) else None
                if isinstance(tool.get("name"), str) and isinstance(props, dict) and "project_id" in props:
                    scoped.add(tool["name"])
            self._scoped_cache = scoped
        return name in self._scoped_cache

    def _plane_for(self, *, name: str) -> str:
        # Resolve a tool's plane from the merged catalog (drift-proof). Cached
        # on first lookup; aggregate tools resolve here too.
        if self._plane_cache is None:
            planes: dict[str, str] = {}
            for _is_cloud, tool in self._each_catalog_tool():
                plane = tool.get("plane")
                if isinstance(tool.get("name"), str) and isinstance(plane, str):
                    planes.setdefault(tool["name"], plane)
            self._plane_cache = planes
        # Unknown tool ⇒ control (the conservative default: most tools are
        # control, and the cloud will reject a truly unknown tool clearly).
        return self._plane_cache.get(name, "control")

    def _result_dict(self, *, body: dict[str, Any]) -> dict[str, Any]:
        result = body.get("result")
        if not isinstance(result, dict):
            raise _UpstreamError(
                "upstream returned an invalid /mcp/call payload",
                error_code="daemon_bad_response",
                details={"payload": body},
            )
        return result

    def _timeout_for(self, *, name: str) -> float:
        return LONG_VERB_TIMEOUT_SECONDS if name in LONG_VERBS else self.config.timeout_seconds

    def _daemon_url_or_none(self) -> str | None:
        url = (
            discover_daemon_url(repo_root=self.config.repo_root)
            or self.config.daemon_url
        )
        if not url and not self.config.split_mode:
            # Local mode keeps the friendly fallback.
            url = os.environ.get("RESEARCH_PLUGIN_DEFAULT_DAEMON_URL") or DEFAULT_DAEMON_URL
        return url.rstrip("/") if url else None

    def _require_daemon_url(self) -> str:
        url = self._daemon_url_or_none()
        if not url:
            raise _UpstreamError(
                _daemon_not_running_message(repo_root=self.config.repo_root),
                error_code=(
                    "local_daemon_not_running" if self.config.split_mode else "daemon_not_running"
                ),
                details={"repo_root": str(self.config.repo_root)},
            )
        return url

    def _with_hidden_project_scope(self, *, tool: dict[str, Any]) -> dict[str, Any]:
        """Hide project_id in MCP schemas; the proxy sends repo/project context."""
        scoped = deepcopy(tool)
        # The plane field is an internal routing hint, not part of the MCP tool
        # schema — strip it from what the client sees.
        scoped.pop("plane", None)
        schema = scoped.get("inputSchema")
        if not isinstance(schema, dict):
            return scoped
        properties = schema.get("properties")
        if isinstance(properties, dict):
            properties.pop("project_id", None)
        required = schema.get("required")
        if isinstance(required, list):
            schema["required"] = [field for field in required if field != "project_id"]
        return scoped

    def _http_get(self, *, url: str, is_cloud: bool) -> dict[str, Any]:
        req = Request(url, method="GET", headers=self._headers(is_cloud=is_cloud))
        return self._send(req=req, is_cloud=is_cloud, timeout=self.config.timeout_seconds)

    def _http_post(
        self, *, url: str, payload: dict[str, Any], is_cloud: bool, timeout: float | None = None
    ) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = Request(
            url, data=data, method="POST", headers=self._headers(is_cloud=is_cloud)
        )
        return self._send(req=req, is_cloud=is_cloud, timeout=timeout or self.config.timeout_seconds)

    def _headers(self, *, is_cloud: bool) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        # Version/compat handshake (cloud plan Phase 9): stamp the proxy's
        # version so the control plane can reject below-floor clients with an
        # actionable upgrade error. The header name is duplicated as a literal
        # (not imported from backend) so the proxy stays stdlib-only; it matches
        # backend.version.CLIENT_VERSION_HEADER, pinned by a surface test.
        headers["X-RP-Client-Version"] = __version__
        if is_cloud and self.config.token:
            # Never logged.
            headers["Authorization"] = f"Bearer {self.config.token}"
        elif not is_cloud and self.config.daemon_secret:
            headers["Authorization"] = f"Bearer {self.config.daemon_secret}"
        return headers

    def _send(self, *, req: Request, is_cloud: bool, timeout: float) -> dict[str, Any]:
        try:
            with urlopen(req, timeout=timeout) as response:
                body_bytes = response.read()
        except urllib_error.HTTPError as exc:
            raise self._error_from_http(exc=exc, is_cloud=is_cloud) from exc
        except urllib_error.URLError as exc:
            if is_cloud:
                raise _UpstreamError(
                    f"control plane unreachable: {exc.reason}",
                    error_code="cloud_unreachable",
                    details={"reason": str(exc.reason)},
                ) from exc
            raise _UpstreamError(
                _daemon_not_running_message(repo_root=self.config.repo_root),
                error_code=(
                    "local_daemon_not_running"
                    if self.config.split_mode
                    else "daemon_not_running"
                ),
                details={"reason": str(exc.reason)},
            ) from exc
        try:
            return json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _UpstreamError(
                "upstream returned non-JSON response",
                error_code="daemon_bad_response",
                details={"body": body_bytes[:512].decode("utf-8", errors="replace")},
            ) from exc

    def _error_from_http(
        self, *, exc: urllib_error.HTTPError, is_cloud: bool
    ) -> _UpstreamError:
        raw = b""
        try:
            raw = exc.read() or b""
        except Exception:  # noqa: BLE001
            pass
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        # A 401 from the cloud is the auth taxonomy, not a domain error.
        if is_cloud and exc.code == 401:
            return _UpstreamError(
                str(body.get("detail") or "control plane rejected the token"),
                error_code="auth_expired",
                details={"status": 401},
            )
        message = body.get("detail") or exc.reason or "upstream returned HTTP error"
        error_code = body.get("error_code") or "daemon_http_error"
        details = {k: v for k, v in body.items() if k not in {"detail", "error_code"}}
        details.setdefault("status", exc.code)
        return _UpstreamError(str(message), error_code=str(error_code), details=details)

    # ---- JSON-RPC helpers ------------------------------------------------

    def _tool_result(
        self, *, result: dict[str, Any], is_error: bool = False
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "content": [{"type": "text", "text": json.dumps(result, sort_keys=True)}],
            "structuredContent": result,
        }
        if is_error:
            payload["isError"] = True
        return payload

    def _result(self, *, request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error_response(
        self,
        *,
        request_id: Any,
        code: int,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}
