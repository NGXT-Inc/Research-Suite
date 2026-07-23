"""JSON-RPC 2.0 framing for the stateless MCP streamable-HTTP transport.

``POST /mcp`` speaks the streamable-HTTP MCP transport: ``initialize``,
``notifications/initialized``, ``tools/list`` and ``tools/call`` (with SSE
progress for slow calls). The transport is STATELESS — every request is
authenticated on its own bearer by the request middleware, no session is
stored, and ``Mcp-Session-Id`` is an opaque echo kept only for client
conformance. The catalog is ``tool_visible_over_mcp AND not hidden`` with no
profile filter; internal tools 403 for any non-local caller (enforced in the
tool dispatcher and mapped back to a 403 here).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, Protocol

from fastapi import FastAPI, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ... import __version__
from ...kernel.utils import ResearchPluginError, ValidationError
from ..identity import ProjectKeyScopeError, ToolVisibilityError
from ..tools.contracts import TOOL_MANIFEST
from .request_body import RequestBodyTooLarge, read_limited_body


MCP_PROTOCOL_VERSION = "2025-06-18"
MAX_MCP_REQUEST_BODY_BYTES = 36_000_000
_FAST_CALL_SECONDS = 0.05
_PROGRESS_INTERVAL_SECONDS = 10.0

JsonObject = dict[str, Any]
RequestId = str | int
ProgressToken = str | int


async def read_limited_mcp_body(request: Request) -> bytes:
    """Read the MCP body capped at the transport ceiling (read at call time)."""
    return await read_limited_body(request, limit=MAX_MCP_REQUEST_BODY_BYTES)


class ToolCatalog(Protocol):
    def __call__(self) -> list[JsonObject]: ...


class ToolFilter(Protocol):
    def __call__(self, tool: JsonObject) -> bool: ...


class ToolCaller(Protocol):
    def __call__(
        self,
        name: str,
        arguments: JsonObject,
        context: JsonObject,
        request: Request,
    ) -> JsonObject: ...


class Authorizer(Protocol):
    def __call__(self, authorization: str | None) -> None: ...


def _is_request_id(value: object) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _request_id(payload: JsonObject) -> RequestId | None:
    value = payload.get("id")
    return value if _is_request_id(value) else None


def _result(request_id: RequestId, result: JsonObject) -> JsonObject:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(
    request_id: RequestId | None,
    code: int,
    message: str,
    data: JsonObject | None = None,
) -> JsonObject:
    error: JsonObject = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _json_response(
    payload: JsonObject, *, status_code: int = 200, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(content=payload, status_code=status_code, headers=headers)


def _sse_message(payload: JsonObject) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: message\ndata: {encoded}\n\n"


def _tool_result(result: JsonObject) -> JsonObject:
    return {
        "content": [{"type": "text", "text": json.dumps(result, sort_keys=True)}],
        "structuredContent": result,
    }


def tool_visible_over_mcp(*, name: str) -> bool:
    """Unknown tools retain the dispatcher's historical error handling."""
    contract = TOOL_MANIFEST.get(name)
    return contract is None or contract.visibility == "public"


def _error_status(exc: BaseException | None) -> int:
    """Scope + internal-tool refusals surface as 403; everything else 200."""
    return 403 if isinstance(exc, (ProjectKeyScopeError, ToolVisibilityError)) else 200


def _dispatcher_error(request_id: RequestId, exc: Exception) -> JsonObject:
    if isinstance(exc, ResearchPluginError):
        code = (
            -32602
            if isinstance(exc, ValidationError) or exc.message.startswith("unknown tool:")
            else -32000
        )
        return _error(
            request_id,
            code,
            exc.message,
            {"error_code": exc.error_code, **exc.details},
        )
    return _error(request_id, -32603, "Internal error")


class McpStreamableHttp:
    """Stateless streamable-HTTP adapter around the shared tool collaborators."""

    def __init__(
        self,
        *,
        list_tools: ToolCatalog,
        call_tool: ToolCaller,
        allow_tool: ToolFilter | None,
        authorize: Authorizer | None,
    ) -> None:
        self._list_tools = list_tools
        self._call_tool = call_tool
        self._allow_tool = allow_tool
        self._authorize = authorize

    def register(self, http: FastAPI) -> None:
        @http.post("/mcp")
        async def mcp_streamable_http(
            request: Request,
            authorization: str | None = Header(default=None),
        ) -> Response:
            if self._authorize is not None:
                self._authorize(authorization)
            try:
                raw_body = await read_limited_mcp_body(request)
            except RequestBodyTooLarge as exc:
                return _json_response(
                    _error(
                        None,
                        -32004,
                        str(exc),
                        {"error_code": "request_too_large", "max_body_bytes": exc.limit},
                    ),
                    status_code=413,
                )
            try:
                payload = json.loads(raw_body)
            except (UnicodeDecodeError, ValueError):
                return _json_response(
                    _error(None, -32700, "Parse error"), status_code=400
                )
            if not isinstance(payload, dict):
                return _json_response(
                    _error(None, -32600, "Invalid Request"), status_code=400
                )
            return await self._handle(request=request, payload=payload)

    async def _handle(self, *, request: Request, payload: JsonObject) -> Response:
        if payload.get("jsonrpc") != "2.0":
            return _json_response(
                _error(None, -32600, "Invalid Request"), status_code=400
            )

        has_id = "id" in payload
        request_id = _request_id(payload)
        if has_id and request_id is None:
            return _json_response(
                _error(None, -32600, "Invalid Request"), status_code=400
            )

        method = payload.get("method")
        # A JSON-RPC response echoed back (no method, carries result/error) is
        # accepted and dropped: the stateless server issues no server->client
        # requests, so it never expects one.
        if method is None and has_id and ("result" in payload or "error" in payload):
            return Response(status_code=202)
        if not isinstance(method, str) or not method:
            return _json_response(
                _error(None, -32600, "Invalid Request"), status_code=400
            )
        params = payload.get("params", {})
        if not isinstance(params, dict):
            response_id = request_id if has_id else None
            return _json_response(
                _error(response_id, -32602, "Invalid params"),
                status_code=200 if has_id else 400,
            )

        if method == "initialize":
            if not has_id or request_id is None:
                return _json_response(
                    _error(None, -32600, "Initialize must be a request"),
                    status_code=400,
                )
            return self._initialize(request_id=request_id, params=params)

        if method == "notifications/initialized":
            # Stateless: accept the handshake completion without tracking it.
            if has_id:
                return _json_response(
                    _error(request_id, -32600, "Initialized must be a notification")
                )
            return Response(status_code=202)

        if not has_id or request_id is None:
            # Notifications never receive a JSON-RPC response; unknown ones are
            # accepted and ignored (no response channel to report on).
            return Response(status_code=202)

        if method == "tools/list":
            return self._tools_list(request_id=request_id, params=params)
        if method == "tools/call":
            return await self._tools_call(
                request=request, request_id=request_id, params=params
            )
        return _json_response(
            _error(request_id, -32601, f"Method not found: {method}")
        )

    def _initialize(self, *, request_id: RequestId, params: JsonObject) -> JSONResponse:
        requested_version = params.get("protocolVersion")
        capabilities = params.get("capabilities")
        client_info = params.get("clientInfo")
        if (
            not isinstance(requested_version, str)
            or not requested_version
            or not isinstance(capabilities, dict)
            or not isinstance(client_info, dict)
            or not isinstance(client_info.get("name"), str)
            or not isinstance(client_info.get("version"), str)
        ):
            return _json_response(
                _error(request_id, -32602, "Invalid initialize params")
            )
        return _json_response(
            _result(
                request_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "merv", "version": __version__},
                },
            ),
            # Opaque, unstored session id for client conformance only.
            headers={"Mcp-Session-Id": uuid.uuid4().hex},
        )

    def _catalog(self) -> list[JsonObject]:
        tools = self._list_tools()
        if self._allow_tool is not None:
            tools = [tool for tool in tools if self._allow_tool(tool)]
        return [
            tool
            for tool in tools
            if tool_visible_over_mcp(name=str(tool.get("name") or ""))
            and not tool.get("hidden")
        ]

    def _tools_list(self, *, request_id: RequestId, params: JsonObject) -> JSONResponse:
        cursor = params.get("cursor")
        if cursor is not None:
            return _json_response(_error(request_id, -32602, "Invalid cursor"))
        return _json_response(_result(request_id, {"tools": self._catalog()}))

    async def _tools_call(
        self, *, request: Request, request_id: RequestId, params: JsonObject
    ) -> Response:
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not name:
            return _json_response(_error(request_id, -32602, "Tool name is required"))
        if not isinstance(arguments, dict):
            return _json_response(
                _error(request_id, -32602, "Tool arguments must be an object")
            )
        progress_token, token_error = self._progress_token(params)
        if token_error is not None:
            return _json_response(_error(request_id, -32602, token_error))

        task = asyncio.create_task(
            run_in_threadpool(self._call_tool, name, arguments, {}, request)
        )
        done, _pending = await asyncio.wait((task,), timeout=_FAST_CALL_SECONDS)
        if done:
            payload = await self._completed_call(task, request_id)
            return _json_response(payload, status_code=_error_status(task.exception()))

        if "text/event-stream" not in request.headers.get("accept", "").lower():
            payload = await self._completed_call(task, request_id)
            return _json_response(payload, status_code=_error_status(task.exception()))
        return StreamingResponse(
            self._stream_call(
                task=task, request_id=request_id, progress_token=progress_token
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @staticmethod
    def _progress_token(
        params: JsonObject,
    ) -> tuple[ProgressToken | None, str | None]:
        meta = params.get("_meta")
        if meta is None:
            return None, None
        if not isinstance(meta, dict):
            return None, "Tool _meta must be an object"
        token = meta.get("progressToken")
        if token is None:
            return None, None
        if not _is_request_id(token):
            return None, "Progress token must be a string or integer"
        return token, None

    @staticmethod
    async def _completed_call(
        task: asyncio.Task[JsonObject], request_id: RequestId
    ) -> JsonObject:
        try:
            result = await task
        except Exception as exc:
            return _dispatcher_error(request_id, exc)
        return _result(request_id, _tool_result(result))

    async def _stream_call(
        self,
        *,
        task: asyncio.Task[JsonObject],
        request_id: RequestId,
        progress_token: ProgressToken | None,
    ) -> AsyncIterator[str]:
        progress = 0
        while not task.done():
            if progress_token is None:
                yield ": tool call in progress\n\n"
            else:
                progress += 1
                yield _sse_message(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": {
                            "progressToken": progress_token,
                            "progress": progress,
                            "message": "Tool call is still running",
                        },
                    }
                )
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=_PROGRESS_INTERVAL_SECONDS
                )
            except TimeoutError:
                continue
        yield _sse_message(await self._completed_call(task, request_id))
