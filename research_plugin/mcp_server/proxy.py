"""Stdio MCP server that proxies tool calls to a running HTTP daemon.

The MCP server itself owns no state. Codex launches it over stdio; it forwards
``tools/list`` and ``tools/call`` to the HTTP daemon's ``/mcp`` endpoints and
returns the daemon's response on stdout.

Discovery order for the daemon URL:

1. ``RESEARCH_PLUGIN_DAEMON_URL`` environment variable.
2. ``<repo_root>/.research_plugin/daemon.json`` written by the daemon on
   startup.

If neither is available, the proxy still serves ``initialize`` and ``ping`` so
Codex can register the MCP cleanly, but any tool call returns a structured
error telling the user how to start the daemon.
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO
from urllib import error as urllib_error
from urllib.request import Request, urlopen

from . import __version__
from .daemon_marker import discover_daemon_url


DEFAULT_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class ProxyConfig:
    repo_root: Path
    daemon_url: str | None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def with_url(self, url: str) -> "ProxyConfig":
        return ProxyConfig(repo_root=self.repo_root, daemon_url=url, timeout_seconds=self.timeout_seconds)


class _HttpDaemonError(Exception):
    """Raised when the HTTP daemon is unreachable or returns a non-2xx."""

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
        f"    research-plugin-http --repo {repo_root}\n"
        "Or set RESEARCH_PLUGIN_DAEMON_URL to the daemon's URL."
    )


class HttpProxyMcpServer:
    """JSON-RPC MCP server that forwards every tool call to an HTTP daemon."""

    def __init__(self, *, config: ProxyConfig) -> None:
        self.config = config

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
        except _HttpDaemonError as exc:
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

    # ---- HTTP forwarding -------------------------------------------------

    def _list_tools(self) -> list[dict[str, Any]]:
        url = self._require_daemon_url()
        body = self._http_get(url=f"{url}/mcp/tools")
        tools = body.get("tools") or []
        if not isinstance(tools, list):
            raise _HttpDaemonError(
                "daemon returned an invalid /mcp/tools payload",
                error_code="daemon_bad_response",
                details={"payload": body},
            )
        return tools

    def _call_tool(self, *, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        url = self._require_daemon_url()
        body = self._http_post(
            url=f"{url}/mcp/call",
            payload={"name": name, "arguments": arguments},
        )
        result = body.get("result")
        if not isinstance(result, dict):
            raise _HttpDaemonError(
                "daemon returned an invalid /mcp/call payload",
                error_code="daemon_bad_response",
                details={"payload": body},
            )
        return result

    def _require_daemon_url(self) -> str:
        # Re-discover on every call so the proxy survives a daemon restart on
        # a different port without the user having to restart Codex.
        url = discover_daemon_url(repo_root=self.config.repo_root) or self.config.daemon_url
        if not url:
            raise _HttpDaemonError(
                _daemon_not_running_message(repo_root=self.config.repo_root),
                error_code="daemon_not_running",
                details={"repo_root": str(self.config.repo_root)},
            )
        return url.rstrip("/")

    def _http_get(self, *, url: str) -> dict[str, Any]:
        req = Request(url, method="GET", headers={"Accept": "application/json"})
        return self._send(req=req)

    def _http_post(self, *, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        return self._send(req=req)

    def _send(self, *, req: Request) -> dict[str, Any]:
        try:
            with urlopen(req, timeout=self.config.timeout_seconds) as response:
                body_bytes = response.read()
        except urllib_error.HTTPError as exc:
            raise self._error_from_http(exc=exc) from exc
        except urllib_error.URLError as exc:
            raise _HttpDaemonError(
                _daemon_not_running_message(repo_root=self.config.repo_root),
                error_code="daemon_not_running",
                details={"reason": str(exc.reason)},
            ) from exc
        try:
            return json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _HttpDaemonError(
                "daemon returned non-JSON response",
                error_code="daemon_bad_response",
                details={"body": body_bytes[:512].decode("utf-8", errors="replace")},
            ) from exc

    def _error_from_http(self, *, exc: urllib_error.HTTPError) -> _HttpDaemonError:
        raw = b""
        try:
            raw = exc.read() or b""
        except Exception:  # noqa: BLE001
            pass
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        message = body.get("detail") or exc.reason or "daemon returned HTTP error"
        error_code = body.get("error_code") or "daemon_http_error"
        details = {k: v for k, v in body.items() if k not in {"detail", "error_code"}}
        details.setdefault("status", exc.code)
        return _HttpDaemonError(str(message), error_code=str(error_code), details=details)

    # ---- JSON-RPC helpers ------------------------------------------------

    def _tool_result(self, *, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": json.dumps(result, sort_keys=True)}],
            "structuredContent": result,
        }

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
