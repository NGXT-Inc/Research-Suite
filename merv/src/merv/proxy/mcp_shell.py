"""Small JSON-RPC-over-stdio MCP shell."""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, TextIO

from . import __version__
from .errors import UpstreamError


_TRANSPORT_ERRORS = {"brain_not_running", "cloud_unreachable", "daemon_bad_response"}


class McpShell:
    """Protocol framing around `_list_tools` and `_call_tool` implementations."""

    def serve(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                response = self.handle(request=json.loads(line))
            except Exception as exc:  # pragma: no cover - last-resort framing guard
                traceback.print_exc(file=sys.stderr)
                response = self._error_response(
                    None,
                    -32603,
                    "internal error",
                    {"detail": str(exc)},
                )
            if response is not None:
                stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
                stdout.flush()

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method, request_id = request.get("method"), request.get("id")
        params = request.get("params") or {}
        try:
            if method == "initialize":
                return self._result(
                    request_id,
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "merv", "version": __version__},
                    },
                )
            if method == "notifications/initialized":
                return None
            if method == "ping":
                return self._result(request_id, {})
            if method == "tools/list":
                return self._result(request_id, {"tools": self._list_tools()})
            if method == "tools/call":
                result = self._call_tool(
                    name=params.get("name", ""),
                    arguments=params.get("arguments") or {},
                )
                return self._result(request_id, self._tool_result(result))
            return self._error_response(
                request_id, -32601, f"method not found: {method}"
            )
        except UpstreamError as exc:
            if method == "tools/call" and exc.error_code in _TRANSPORT_ERRORS:
                result = {
                    "ok": False,
                    "error": exc.message,
                    "error_code": exc.error_code,
                    **exc.details,
                }
                return self._result(
                    request_id, self._tool_result(result, is_error=True)
                )
            return self._error_response(
                request_id,
                -32000,
                exc.message,
                {"error_code": exc.error_code, **exc.details},
            )
        except TypeError as exc:
            return self._error_response(request_id, -32602, f"invalid params: {exc}")
        except Exception as exc:  # pragma: no cover - exposes unexpected bugs
            traceback.print_exc(file=sys.stderr)
            return self._error_response(request_id, -32603, str(exc))

    @staticmethod
    def _tool_result(result: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "content": [{"type": "text", "text": json.dumps(result, sort_keys=True)}],
            "structuredContent": result,
        }
        if is_error:
            payload["isError"] = True
        return payload

    @staticmethod
    def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error_response(
        request_id: Any,
        code: int,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}
