"""Stdio MCP server that proxies tool calls to one brain plus local data IO.

Codex launches this process over stdio. The proxy always has one HTTP upstream:
the brain server named by ``control_url``. Local deployment is just a brain on
localhost; hosted deployment is the same wire shape pointed at a remote URL.

Routing is the former split-mode path everywhere: ``control`` tools go to the
brain with an explicit ``project_id`` resolved from proxy-local
``project_links.sqlite`` state; ``data`` tools run in this process and submit
validated facts/bytes to the brain. The brain never receives ``repo_root`` and
never reads the user's checkout.

Error taxonomy is returned as TOOL RESULTS, not ``-32000`` protocol errors, so a
client never disables the server over a transient outage. Loopback brain
outages get an actionable ``brain_not_running`` hint to start
``merv-http``; remote outages keep ``cloud_unreachable``. Domain
errors stay protocol errors.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit
from urllib import error as urllib_error
from urllib.request import Request, urlopen

from merv.shared.client_config import (
    HOSTED_CONTROL_URL,
    LOCAL_BRAIN_URL,
    dual_env_value,
)
from merv.shared.errors import ValidationError

from . import __version__
from .local_data_plane import LocalDataPlane, LocalDataPlaneError
from .project_links import ProjectLinks, default_project_links_path


DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_CONTROL_URL = HOSTED_CONTROL_URL
# sandbox.request can take minutes; the proxy returns its handle promptly and
# the agent polls sandbox.get. A short bound keeps a long-running
# verb from blocking the stdio loop — it lands a row in 'provisioning' and the
# agent polls. Kept generous so a fast create still returns SSH inline.
LONG_VERB_TIMEOUT_SECONDS = 90.0
LONG_VERBS = frozenset({"sandbox.request"})
_LOCAL_ENRICHED_CONTROL_TOOLS = frozenset({"sandbox.get", "sandbox.health"})

# The checked-in JSON is the proxy's sole runtime tool registry. The generator
# renders it from the brain-owned contracts, and parity tests prevent drift.
_STATIC_CATALOG_PATH = Path(__file__).with_name("_tool_catalog.json")
_STORAGE_PROVIDER_ENV_VAR = "MERV_STORAGE_PROVIDER"


def _storage_feature_enabled() -> bool:
    """Mirror brain config's dual-spelled, explicit-s3 storage feature gate."""
    raw = (dual_env_value(_STORAGE_PROVIDER_ENV_VAR) or "").lower()
    if not raw:
        return False
    if raw != "s3":
        raise ValidationError(
            f"unknown {_STORAGE_PROVIDER_ENV_VAR}: {raw!r} "
            "(expected 's3', or unset to disable storage)",
            details={"provider": raw},
        )
    return True


# Transport failures are returned as TOOL RESULTS, not protocol errors, so a
# transient brain outage never disables the server. Domain
# errors the upstream reports (validation_error, …) stay protocol errors.
_TRANSPORT_ERROR_CODES = frozenset(
    {
        "brain_not_running",
        "cloud_unreachable",
        "daemon_bad_response",
    }
)


@dataclass(frozen=True)
class ProxyConfig:
    repo_root: Path
    control_url: str | None
    project_links_path: Path | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    # RapidReview API key (rr_sk_...) for the hosted brain; empty against a
    # loopback brain, which requires no credential.
    api_key: str = ""
    # Machine config file holding a merv-client login session (access/refresh
    # tokens); the proxy refreshes it silently as it nears expiry.
    client_config_path: Path | None = None

    def with_url(self, url: str) -> "ProxyConfig":
        return ProxyConfig(
            repo_root=self.repo_root,
            control_url=url,
            project_links_path=self.project_links_path,
            timeout_seconds=self.timeout_seconds,
            api_key=self.api_key,
            client_config_path=self.client_config_path,
        )


@dataclass(frozen=True)
class _ToolMeta:
    plane: str = "control"
    project_scoped: bool = False


class _UpstreamError(Exception):
    """An upstream was unreachable or returned a non-2xx.

    Carries the proxy's error taxonomy code. Transport failures are surfaced as
    TOOL RESULTS, not protocol errors, so the client never disables the server.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "cloud_unreachable",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.details = details or {}


def _brain_not_running_message(*, control_url: str | None) -> str:
    return (
        "Merv brain server is not running"
        + (f" at {control_url}" if control_url else "")
        + ". Start it with:\n"
        "    merv-http\n"
        "If it is on another port, set MERV_CONTROL_URL "
        "to the brain URL."
    )


def _is_loopback_url(url: str | None) -> bool:
    if not url:
        return False
    host = (urlsplit(url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


class HttpProxyMcpServer:
    """JSON-RPC MCP server that forwards every tool call to the backend."""

    def __init__(self, *, config: ProxyConfig) -> None:
        self.config = config
        self._tool_cache: dict[str, _ToolMeta] | None = None
        self._session_cache: dict[str, Any] | None = None
        self._project_links: ProjectLinks | None = None
        self._local_data_plane: LocalDataPlane | None = None

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
                        "serverInfo": {"name": "merv", "version": __version__},
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
                return self._result(
                    request_id=request_id, result=self._tool_result(result=result)
                )
            return self._error_response(
                request_id=request_id,
                code=-32601,
                message=f"method not found: {method}",
            )
        except _UpstreamError as exc:
            # Transport failures come back as a TOOL RESULT so a
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
            return self._error_response(
                request_id=request_id, code=-32603, message=str(exc)
            )

    # ---- tools/list ------------------------------------------------------

    def _list_tools(self) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        catalog, _complete = self._catalog_tools()
        for is_cloud, tool in catalog:
            # Hidden tools stay in the served catalog (their plane/schema still
            # routes proxy-internal calls) but are not advertised to the agent.
            # project.list predates the flag; the literal covers older brains.
            if tool.get("hidden") or tool.get("name") == "project.list":
                continue
            shaped = self._with_hidden_project_scope(tool=tool)
            merged[shaped["name"]] = shaped
        return list(merged.values())

    def _catalog_tools(self) -> tuple[list[tuple[bool, dict[str, Any]]], bool]:
        """Collect (is_cloud, raw_tool) from the brain and proxy-local catalog.

        Raw = pre-strip, so callers can read 'plane' and project_id schema.
        The local half is in-process and always present; the brain half may be
        down and is skipped. ``complete`` reports whether the brain answered.
        """
        tools: list[tuple[bool, dict[str, Any]]] = []
        complete = True
        try:
            body = self._http_get(
                url=f"{self._require_control_url()}/mcp/tools", is_cloud=True
            )
        except _UpstreamError:
            complete = False
        else:
            for tool in body.get("tools") or []:
                if isinstance(tool, dict):
                    tools.append((True, tool))
        tools.extend((False, tool) for tool in self._local_tool_catalog())
        return tools, complete

    # ---- tools/call ------------------------------------------------------

    def _call_tool(self, *, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "project":
            action = str(arguments.get("action") or "")
            if action == "current":
                return self._current_project()
            if action == "connect":
                return self._connect_project(arguments=arguments)
            if action == "overview":
                # Whole-project read: mirror current's identity handling —
                # resolve the linked project_id locally and forward it as the
                # explicit scope (never an agent-supplied field). Unlinked folder
                # returns the same not-linked report current gives.
                project_id = self._resolve_project_id()
                if not project_id:
                    return self._current_project()
                return self._call_cloud_raw(
                    name="project",
                    arguments={**arguments, "project_id": project_id},
                )
            # create (and any other/invalid action) forwards upstream as the
            # `project` tool. It takes no project scope, so route through the raw
            # path that never injects the linked project_id; the brain's pydantic
            # validation returns the enum error for an unknown action.
            return self._call_cloud_raw(name="project", arguments=arguments)
        plane = self._tool_meta(name=name).plane
        if name in _LOCAL_ENRICHED_CONTROL_TOOLS:
            return self._call_local_enriched_control(name=name, arguments=arguments)
        if plane == "control":
            return self._call_cloud(name=name, arguments=arguments)
        if plane == "data":
            return self._call_local_data(name=name, arguments=arguments)
        # Backward tolerance for older catalogs that still say "aggregate".
        return self._call_local_enriched_control(name=name, arguments=arguments)

    def _call_cloud(self, *, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        args = self._call_arguments(arguments=arguments)
        # Identity on the wire (§3.2): the cloud gets an explicit project_id and
        # NEVER repo_root. Resolve it via the proxy-local link map — but ONLY
        # for tools that actually take project_id (e.g. review.start/submit are
        # capability-scoped, not project-scoped, and reject the extra field).
        if self._tool_meta(name=name).project_scoped:
            args["project_id"] = self._resolve_project_id_required()
        return self._call_cloud_raw(name=name, arguments=args)

    def _call_cloud_raw(
        self, *, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Forward arguments verbatim — no link-derived project_id rewriting."""
        body = self._http_post(
            url=f"{self._require_control_url()}/mcp/call",
            payload={"name": name, "arguments": arguments},
            is_cloud=True,
            timeout=self._timeout_for(name=name, arguments=arguments),
        )
        return self._result_dict(body=body)

    def _call_control_api(
        self, *, path: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self._http_post(
            url=f"{self._require_control_url()}{path}",
            payload=payload,
            is_cloud=True,
            timeout=self.config.timeout_seconds,
        )

    def _call_local_data(
        self, *, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            return self._local_executor().call_tool(
                name=name,
                arguments=self._call_arguments(arguments=arguments),
            )
        except LocalDataPlaneError as exc:
            raise _UpstreamError(
                exc.message,
                error_code=exc.error_code,
                details=exc.details,
            ) from exc
        except Exception as exc:
            error_code = str(getattr(exc, "error_code", "") or "validation_error")
            details = getattr(exc, "details", {})
            raise _UpstreamError(
                str(exc),
                error_code=error_code,
                details=details if isinstance(details, dict) else {},
            ) from exc

    def _call_local_enriched_control(
        self, *, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if name == "sandbox.health":
            return self._enriched_health()
        # sandbox.get and any legacy aggregate: cloud row facts merged with
        # proxy-local machine facts. The enrichment itself dials the cloud for
        # row facts; either half may fail without blocking the other, with the
        # failure surfaced under the control_plane / data_plane error keys.
        cloud: dict[str, Any] = {}
        cloud_err: dict[str, Any] | None = None
        try:
            cloud = self._call_cloud(name=name, arguments=arguments)
        except _UpstreamError as exc:
            cloud_err = {"error": exc.message, "error_code": exc.error_code}
        local: dict[str, Any] = {}
        local_err: dict[str, Any] | None = None
        try:
            local = self._call_local_data(name=name, arguments=arguments)
        except _UpstreamError as exc:
            local_err = {"error": exc.message, "error_code": exc.error_code}
        # Cloud row facts are the base view. The proxy contributes only
        # machine-local enrichment, matching merv.brain.sandbox.sandbox_views'
        # agent view shape without importing backend code into the stdlib proxy.
        merged = dict(cloud)
        if any(local.get(key) for key in ("command", "raw_command", "key_path")):
            ssh = dict(merged.get("ssh") or {})
            if local.get("command"):
                ssh["command"] = local["command"]
            if local.get("raw_command"):
                ssh["raw_command"] = local["raw_command"]
            if local.get("key_path"):
                ssh["key_path"] = local["key_path"]
            merged["ssh"] = ssh
        if local.get("local_dir"):
            merged["local_experiment_dir"] = local["local_dir"]
        if cloud_err:
            merged["control_plane"] = cloud_err
        if local_err:
            merged["data_plane"] = local_err
        return {key: value for key, value in merged.items() if not key.startswith("_")}

    def _enriched_health(self) -> dict[str, Any]:
        data_ok, data_detail = True, {"mode": "proxy"}
        cloud_ok, cloud_detail = self._probe(is_cloud=True)
        return {
            "ok": bool(data_ok and cloud_ok),
            "data_plane": {"reachable": data_ok, **data_detail},
            "control_plane": {
                "reachable": cloud_ok,
                "configured": bool(self.config.control_url),
                **cloud_detail,
            },
        }

    def _current_project(self) -> dict[str, Any]:
        """Resolve the current folder without sending repo_root to the cloud."""
        project_id = self._resolve_project_id()
        if not project_id:
            return {
                "exists": False,
                "project": None,
                "repo_root": str(self.config.repo_root),
                "hint": (
                    "No hosted Merv project is linked for this folder. "
                    "Ask the user which existing project_id to link and call the "
                    'project tool with action="connect" and that project_id; or '
                    "ask for a project name and short summary and call it with "
                    'action="connect" and name/summary to create and link in one '
                    "step."
                ),
            }
        project = dict(
            self._call_cloud(name="project.get", arguments={"project_id": project_id})
        )
        project["repo_root"] = str(self.config.repo_root)
        return {
            "exists": True,
            "project": project,
            "repo_root": str(self.config.repo_root),
        }

    def _probe(self, *, is_cloud: bool) -> tuple[bool, dict[str, Any]]:
        url = self.config.control_url
        if not url:
            return False, {"error_code": "not_configured"}
        try:
            self._http_get(url=f"{url}/health", is_cloud=is_cloud)
            return True, {}
        except _UpstreamError as exc:
            return False, {"error": exc.message, "error_code": exc.error_code}

    # ---- identity resolution (split mode) --------------------------------

    def _resolve_project_id(self) -> str | None:
        return self._links().project_for_repo(repo_root=str(self.config.repo_root))

    def _resolve_project_id_required(self) -> str:
        project_id = self._resolve_project_id()
        if not project_id:
            raise _UpstreamError(
                "no hosted project link found for repo; call the project tool "
                'with action="connect" to link this folder to a project',
                error_code="project_not_linked",
                details={"repo_root": str(self.config.repo_root)},
            )
        return project_id

    def _connect_project(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        """Establish the folder→project link; repo_root never leaves the machine.

        The one call where project_id is caller-authoritative: it validates the
        id against the brain (or creates the project), then writes the link to
        the proxy-local store every other tool resolves identity from.
        """
        project_id = str(arguments.get("project_id") or "").strip()
        name = str(arguments.get("name") or "").strip()
        summary = str(arguments.get("summary") or "").strip()
        overwrite = bool(arguments.get("overwrite") or False)
        if bool(project_id) == bool(name):
            raise _UpstreamError(
                "pass exactly one of project_id (link an existing project) or "
                "name (create a new project and link it)",
                error_code="validation_error",
            )
        existing = self._resolve_project_id()
        # Guard before any cloud call so a refused re-link never creates an
        # orphan project on the brain.
        if existing and existing != project_id and not overwrite:
            raise _UpstreamError(
                f"this folder is already linked to {existing}; pass "
                "overwrite=true to re-link it",
                error_code="already_linked",
                details={"project_id": existing},
            )
        created = False
        if project_id:
            project = dict(
                self._call_cloud_raw(
                    name="project.get", arguments={"project_id": project_id}
                )
            )
        else:
            project = dict(
                self._call_cloud_raw(
                    name="project",
                    arguments={"action": "create", "name": name, "summary": summary},
                )
            )
            created = True
            project_id = str(project.get("id") or "")
            if not project_id:
                raise _UpstreamError(
                    "project create returned no project id",
                    error_code="daemon_bad_response",
                    details={"project": project},
                )
        self._links().link(repo_root=str(self.config.repo_root), project_id=project_id)
        return {
            "linked": True,
            "created": created,
            "project": project,
            "repo_root": str(self.config.repo_root),
        }

    # ---- helpers ---------------------------------------------------------

    def _tool_meta(self, *, name: str) -> _ToolMeta:
        if self._tool_cache is None:
            catalog, complete = self._catalog_tools()
            metadata: dict[str, _ToolMeta] = {}
            for _is_cloud, tool in catalog:
                tool_name = tool.get("name")
                if not isinstance(tool_name, str):
                    continue
                schema = tool.get("inputSchema")
                props = schema.get("properties") if isinstance(schema, dict) else None
                metadata.setdefault(
                    tool_name,
                    _ToolMeta(
                        plane=(
                            tool.get("plane")
                            if isinstance(tool.get("plane"), str)
                            else "control"
                        ),
                        project_scoped=isinstance(props, dict)
                        and "project_id" in props,
                    ),
                )
            # Pin the cache only when the brain answered: a partial catalog
            # would misroute tools for the rest of the process lifetime.
            if not complete:
                return metadata.get(name, _ToolMeta())
            self._tool_cache = metadata
        # Unknown tool ⇒ control; the cloud will reject a truly unknown tool clearly.
        return self._tool_cache.get(name, _ToolMeta())

    def _local_tool_catalog(self) -> list[dict[str, Any]]:
        # Runtime routing is deterministic and dependency-free; regeneration
        # and parity checks are the only bridge to the brain-owned contracts.
        tools = json.loads(_STATIC_CATALOG_PATH.read_text(encoding="utf-8"))["tools"]
        if not _storage_feature_enabled():
            tools = [
                tool
                for tool in tools
                if not str(tool.get("name", "")).startswith("storage.")
            ]
        return tools

    def _local_executor(self) -> LocalDataPlane:
        if self._local_data_plane is None:
            self._local_data_plane = LocalDataPlane(
                repo_root=self.config.repo_root,
                project_id_resolver=self._resolve_project_id,
                control_api_post=lambda path, payload: self._call_control_api(
                    path=path, payload=payload
                ),
                control_tool_call=lambda tool, args: self._call_cloud(
                    name=tool, arguments=args
                ),
            )
        return self._local_data_plane

    def _links(self) -> ProjectLinks:
        if self._project_links is None:
            db_path = self.config.project_links_path or default_project_links_path()
            self._project_links = ProjectLinks(db_path=db_path)
        return self._project_links

    def _call_arguments(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        args = dict(arguments)
        args.pop("project_id", None)
        return args

    def _result_dict(self, *, body: dict[str, Any]) -> dict[str, Any]:
        result = body.get("result")
        if not isinstance(result, dict):
            raise _UpstreamError(
                "upstream returned an invalid /mcp/call payload",
                error_code="daemon_bad_response",
                details={"payload": body},
            )
        return result

    def _timeout_for(
        self, *, name: str, arguments: dict[str, Any] | None = None
    ) -> float:
        # sandbox.runs long-polls server-side for wait_seconds; the proxy's
        # HTTP timeout must outlast the requested wait or it would cut the
        # slow call it exists to enable.
        if name == "sandbox.runs":
            try:
                wait = float((arguments or {}).get("wait_seconds") or 0)
            except (TypeError, ValueError):
                wait = 0.0
            return max(self.config.timeout_seconds, wait + 30.0)
        return (
            LONG_VERB_TIMEOUT_SECONDS
            if name in LONG_VERBS
            else self.config.timeout_seconds
        )

    def _require_control_url(self) -> str:
        url = (self.config.control_url or "").strip().rstrip("/")
        if not url:
            raise _UpstreamError(
                "control_url is required; set MERV_CONTROL_URL to "
                f"the hosted brain ({HOSTED_CONTROL_URL}) or to "
                f"{LOCAL_BRAIN_URL} for a local brain",
                error_code="cloud_unreachable",
            )
        return url

    def _with_hidden_project_scope(self, *, tool: dict[str, Any]) -> dict[str, Any]:
        """Hide project_id in MCP schemas; the proxy sends repo/project context."""
        scoped = deepcopy(tool)
        # The plane field is an internal routing hint, not part of the MCP tool
        # schema — strip it from what the client sees.
        scoped.pop("plane", None)
        # The `project` tool keeps project_id visible: for action=connect it is
        # the caller's explicit choice of which project to link, not hidden repo
        # context the proxy injects.
        if scoped.get("name") == "project":
            return scoped
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
        return self._send(req=req, timeout=self.config.timeout_seconds)

    def _http_post(
        self,
        *,
        url: str,
        payload: dict[str, Any],
        is_cloud: bool,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = Request(
            url, data=data, method="POST", headers=self._headers(is_cloud=is_cloud)
        )
        return self._send(req=req, timeout=timeout or self.config.timeout_seconds)

    def _headers(self, *, is_cloud: bool) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        # Version/compat handshake: stamp the proxy's
        # version so the control plane can reject below-floor clients with an
        # actionable upgrade error. The header name is duplicated as a literal
        # (not imported from backend) so the proxy stays stdlib-only; it matches
        # merv.brain.kernel.version.CLIENT_VERSION_HEADER, pinned by a surface test.
        headers["X-RP-Client-Version"] = __version__
        # Hosted auth rides only on cloud calls; the loopback brain never sees
        # a credential. Header name is a literal for the same stdlib-only
        # reason as above.
        if is_cloud:
            bearer = self._cloud_bearer()
            if bearer:
                headers["Authorization"] = f"Bearer {bearer}"
        return headers

    # ---- hosted credential (API key or merv-client login session) ---------

    def _cloud_bearer(self) -> str:
        # An API key (env > machine config) is static and wins outright.
        if self.config.api_key:
            return self.config.api_key
        session = self._login_session()
        if not session:
            return ""
        # Refresh preemptively so long-lived MCP sessions never trip a mid-call
        # 401: a five-minute skew comfortably outlives one request.
        if float(session.get("expires_at") or 0) - time.time() < 300:
            session = self._refresh_login_session(session) or session
        return str(session.get("access_token") or "")

    def _login_session(self) -> dict[str, Any] | None:
        if self._session_cache is None:
            path = self.config.client_config_path
            try:
                raw = json.loads(path.read_text(encoding="utf-8")) if path else {}
            except (OSError, ValueError):
                raw = {}
            self._session_cache = raw if raw.get("access_token") else {}
        return self._session_cache or None

    def _refresh_login_session(self, session: dict[str, Any]) -> dict[str, Any] | None:
        refresh_token = str(session.get("refresh_token") or "")
        if not refresh_token or not self.config.control_url:
            return None
        request = Request(
            f"{self.config.control_url}/api/sdk/auth/refresh",
            data=json.dumps({"refresh_token": refresh_token}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                fresh = json.loads(response.read().decode("utf-8"))
        except (
            Exception
        ):  # noqa: BLE001 - stale token still errors upstream with the login hint
            return None
        updated = dict(session)
        updated.update(
            access_token=str(fresh.get("access_token") or ""),
            refresh_token=str(fresh.get("refresh_token") or refresh_token),
            expires_at=int(time.time()) + int(fresh.get("expires_in") or 3600),
        )
        self._session_cache = updated
        path = self.config.client_config_path
        if path is not None:
            try:
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(updated), encoding="utf-8")
                tmp.chmod(0o600)
                tmp.replace(path)
            except OSError:
                pass  # in-memory session still carries this proxy process
        return updated

    def _send(self, *, req: Request, timeout: float) -> dict[str, Any]:
        try:
            with urlopen(req, timeout=timeout) as response:
                body_bytes = response.read()
        except urllib_error.HTTPError as exc:
            raise self._error_from_http(exc=exc) from exc
        except urllib_error.URLError as exc:
            if _is_loopback_url(self.config.control_url):
                raise _UpstreamError(
                    _brain_not_running_message(control_url=self.config.control_url),
                    error_code="brain_not_running",
                    details={"reason": str(exc.reason)},
                ) from exc
            raise _UpstreamError(
                f"control plane unreachable: {exc.reason}",
                error_code="cloud_unreachable",
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

    def _error_from_http(self, *, exc: urllib_error.HTTPError) -> _UpstreamError:
        raw = b""
        try:
            raw = exc.read() or b""
        except Exception:  # noqa: BLE001
            pass
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        message = body.get("detail") or exc.reason or "upstream returned HTTP error"
        error_code = body.get("error_code") or "upstream_http_error"
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
