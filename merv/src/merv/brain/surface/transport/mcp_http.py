"""MCP-shaped HTTP routes shared by local and control HTTP surfaces.

Registers the legacy ``GET /mcp/tools`` + ``POST /mcp/call`` pair and the
stateless streamable ``POST /mcp`` endpoint. The internal-tool block and
key-project scope enforcement live downstream (the tool dispatcher and the
request gateway); these routes only add the shared body cap and the
``not hidden`` catalog filter so internal tools are never advertised.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import Header, Request, Response
from fastapi.concurrency import run_in_threadpool

from ...kernel.utils import ValidationError
from .mcp_streamable_http import (
    McpStreamableHttp,
    RequestBodyTooLarge,
    ScopeAuthorizer,
    read_limited_mcp_body,
)

ToolCatalog = Callable[[], list[dict[str, Any]]]
ToolFilter = Callable[[dict[str, Any]], bool]
ToolCaller = Callable[
    [str, dict[str, Any], dict[str, Any], Request],
    dict[str, Any],
]
Authorizer = Callable[[str | None], None]


def register_mcp_routes(
    http: Any,
    *,
    list_tools: ToolCatalog,
    call_tool: ToolCaller,
    allow_tool: ToolFilter | None = None,
    authorize: Authorizer | None = None,
    authorize_scope: ScopeAuthorizer | None = None,
) -> None:
    def check_authorized(authorization: str | None) -> None:
        if authorize is not None:
            authorize(authorization)

    def catalog() -> list[dict[str, Any]]:
        tools = list_tools()
        if allow_tool is not None:
            tools = [tool for tool in tools if allow_tool(tool)]
        return [tool for tool in tools if not tool.get("hidden")]

    @http.get("/mcp/tools")
    def mcp_tools_list(
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        check_authorized(authorization)
        return {"tools": catalog()}

    @http.post("/mcp/call")
    async def mcp_call(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> Any:
        check_authorized(authorization)
        try:
            raw_body = await read_limited_mcp_body(request)
        except RequestBodyTooLarge as exc:
            return Response(
                content=json.dumps(
                    {
                        "detail": str(exc),
                        "error_code": "request_too_large",
                        "max_body_bytes": exc.limit,
                    }
                ),
                media_type="application/json",
                status_code=413,
            )
        if raw_body:
            try:
                payload = json.loads(raw_body)
            except ValueError as exc:
                raise ValidationError(
                    "request body must be valid JSON", details={"field": "body"}
                ) from exc
        else:
            payload = {}
        if not isinstance(payload, dict):
            raise ValidationError(
                "request body must be an object", details={"field": "body"}
            )
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise ValidationError("tool name is required", details={"field": "name"})
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValidationError(
                "arguments must be an object", details={"field": "arguments"}
            )
        context = payload.get("context") or {}
        if not isinstance(context, dict):
            raise ValidationError(
                "context must be an object", details={"field": "context"}
            )
        # call_tool is synchronous and may do slow outbound IO (e.g. MLflow
        # REST calls inside transitions). Run it in the threadpool — like every
        # sync route in http_api — so one slow tool call never stalls the event
        # loop for every other agent and UI request.
        result = await run_in_threadpool(call_tool, name, arguments, context, request)
        return {"result": result}

    McpStreamableHttp(
        list_tools=list_tools,
        call_tool=call_tool,
        allow_tool=allow_tool,
        authorize=authorize,
        authorize_scope=authorize_scope,
    ).register(http)
