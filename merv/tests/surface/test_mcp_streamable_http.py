"""Protocol tests for the stateless streamable-HTTP MCP transport.

The transport carries no server-side session: ``initialize`` echoes an opaque
``Mcp-Session-Id`` that is never stored, and ``tools/list`` / ``tools/call``
authenticate on their own bearer (the request middleware) rather than a bound
session. The catalog is ``visible-over-mcp AND not hidden`` with no profile
filter; internal tools 403 for external callers (covered with an mk_ key in
tests/surface/test_project_keys.py) while local composition keeps access.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from tests.support.brain import TestBrain
from merv.brain import __version__
from merv.brain.sandbox.execution.backends.fake import FakeSandboxBackend
from merv.brain.surface.transport.http_api import create_fastapi_app
from merv.brain.surface.transport.mcp_http import register_mcp_routes


PROTOCOL_VERSION = "2025-06-18"
MCP_ACCEPT = "application/json, text/event-stream"


def _initialize_payload(request_id: int = 1) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "merv-test-client", "version": "1.0"},
        },
    }


class _McpClient:
    """Stateless real-wire fixture: it echoes the session id but never relies
    on the server storing it."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.session_id = ""
        self.next_id = 1

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Accept": MCP_ACCEPT, "MCP-Protocol-Version": PROTOCOL_VERSION}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def initialize(self) -> dict[str, Any]:
        response = self.client.post(
            "/mcp",
            json=_initialize_payload(self.next_id),
            headers={"Accept": MCP_ACCEPT},
        )
        if response.status_code != 200:
            raise AssertionError(response.text)
        self.next_id += 1
        self.session_id = response.headers["mcp-session-id"]
        initialized = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=self.headers,
        )
        if initialized.status_code != 202:
            raise AssertionError(initialized.text)
        return response.json()

    def request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self.next_id,
            "method": method,
        }
        self.next_id += 1
        if params is not None:
            payload["params"] = params
        return self.client.post("/mcp", json=payload, headers=self.headers)


class McpStreamableHttpProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.brain = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.http = TestClient(create_fastapi_app(self.brain.http))

    def tearDown(self) -> None:
        self.brain.shutdown()
        self.tmp.cleanup()

    def test_initialize_is_stateless_and_echoes_a_session_id(self) -> None:
        initialized = self.http.post(
            "/mcp", json=_initialize_payload(7), headers={"Accept": MCP_ACCEPT}
        )
        self.assertEqual(initialized.status_code, 200, initialized.text)
        self.assertTrue(
            initialized.headers["content-type"].startswith("application/json")
        )
        self.assertEqual(
            initialized.json(),
            {
                "jsonrpc": "2.0",
                "id": 7,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "merv", "version": __version__},
                },
            },
        )
        session_id = initialized.headers.get("mcp-session-id", "")
        self.assertTrue(session_id and session_id.isascii() and session_id.isprintable())

        # notifications/initialized is accepted with or without the echoed id.
        for headers in (
            {"Accept": MCP_ACCEPT},
            {"Accept": MCP_ACCEPT, "Mcp-Session-Id": session_id},
        ):
            notification = self.http.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=headers,
            )
            self.assertEqual(notification.status_code, 202, notification.text)
            self.assertEqual(notification.content, b"")

        # tools/list works with no prior handshake and ignores any session id.
        for headers in (
            {"Accept": MCP_ACCEPT},
            {"Accept": MCP_ACCEPT, "Mcp-Session-Id": "never-issued"},
        ):
            listed = self.http.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 8, "method": "tools/list"},
                headers=headers,
            )
            self.assertEqual(listed.status_code, 200, listed.text)

    def test_tools_list_and_call_match_legacy_routes(self) -> None:
        mcp = _McpClient(self.http)
        mcp.initialize()

        legacy_catalog = self.http.get("/mcp/tools")
        streamed_catalog = mcp.request("tools/list")
        self.assertEqual(streamed_catalog.status_code, 200, streamed_catalog.text)
        self.assertEqual(streamed_catalog.json()["result"], legacy_catalog.json())
        names = {tool["name"] for tool in streamed_catalog.json()["result"]["tools"]}
        self.assertIn("workflow.status_and_next", names)  # public
        self.assertNotIn("claim.list", names)  # internal
        self.assertFalse(
            any(tool.get("hidden") for tool in streamed_catalog.json()["result"]["tools"])
        )

        project = self.http.post("/api/projects", json={"name": "MCP parity"})
        self.assertEqual(project.status_code, 201, project.text)
        call = {
            "name": "workflow.status_and_next",
            "arguments": {"project_id": project.json()["id"]},
        }
        legacy_call = self.http.post("/mcp/call", json=call)
        streamable_call = mcp.request("tools/call", call)
        self.assertEqual(streamable_call.status_code, 200, streamable_call.text)
        self.assertTrue(
            streamable_call.headers["content-type"].startswith("application/json")
        )
        tool_result = streamable_call.json()["result"]
        self.assertEqual(tool_result["structuredContent"], legacy_call.json()["result"])
        self.assertEqual(
            json.loads(tool_result["content"][0]["text"]),
            legacy_call.json()["result"],
        )

    def test_internal_tools_are_absent_from_catalog_but_reachable_internally(
        self,
    ) -> None:
        # Internal tools are never advertised over MCP, and external callers are
        # refused them (covered by test_project_keys.py with an mk_ key). This
        # pins the other half: the trusted local composition — a local principal
        # on the same /mcp/call path — must retain access.
        project = self.http.post("/api/projects", json={"name": "Internal path"})
        self.assertEqual(project.status_code, 201, project.text)
        arguments = {"project_id": project.json()["id"]}
        mcp = _McpClient(self.http)
        mcp.initialize()

        legacy_catalog = self.http.get("/mcp/tools")
        streamed_catalog = mcp.request("tools/list")
        for catalog in (legacy_catalog.json(), streamed_catalog.json()["result"]):
            names = {tool["name"] for tool in catalog["tools"]}
            self.assertNotIn("project.get", names)
            self.assertFalse(any(tool.get("hidden") for tool in catalog["tools"]))

        legacy = self.http.post(
            "/mcp/call", json={"name": "project.get", "arguments": arguments}
        )
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(legacy.json()["result"]["id"], project.json()["id"])

        internal = self.brain.call_tool("project.get", arguments)
        self.assertEqual(internal["id"], project.json()["id"])
        # activity_source=mcp alone no longer confines a trusted caller.
        internal_mcp = self.brain.call_tool(
            "project.get", arguments, activity_source="mcp"
        )
        self.assertEqual(internal_mcp["id"], project.json()["id"])

    def test_malformed_requests_unknown_methods_and_dispatch_errors(self) -> None:
        parsed = self.http.post(
            "/mcp",
            content=b'{"jsonrpc":',
            headers={"Content-Type": "application/json", "Accept": MCP_ACCEPT},
        )
        self.assertEqual(parsed.status_code, 400, parsed.text)
        self.assertEqual(parsed.json()["error"]["code"], -32700)
        self.assertIsNone(parsed.json()["id"])

        invalid = self.http.post(
            "/mcp", json=[_initialize_payload()], headers={"Accept": MCP_ACCEPT}
        )
        self.assertEqual(invalid.status_code, 400, invalid.text)
        self.assertEqual(invalid.json()["error"]["code"], -32600)

        mcp = _McpClient(self.http)
        mcp.initialize()
        unknown = mcp.request("unknown/method")
        self.assertEqual(unknown.status_code, 200, unknown.text)
        self.assertEqual(unknown.json()["error"]["code"], -32601)

        bad_params = mcp.request(
            "tools/call", {"name": "workflow.status_and_next", "arguments": []}
        )
        self.assertEqual(bad_params.status_code, 200, bad_params.text)
        self.assertEqual(bad_params.json()["error"]["code"], -32602)

        unknown_tool = mcp.request("tools/call", {"name": "missing.tool"})
        self.assertEqual(unknown_tool.status_code, 200, unknown_tool.text)
        self.assertEqual(unknown_tool.json()["error"]["code"], -32602)
        self.assertEqual(
            unknown_tool.json()["error"]["data"]["error_code"],
            "research_plugin_error",
        )

    def test_ping_returns_an_empty_result(self) -> None:
        # Spec liveness probe (FIX 5): ping is a request that returns {}.
        response = self.http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 21, "method": "ping"},
            headers={"Accept": MCP_ACCEPT, "MCP-Protocol-Version": PROTOCOL_VERSION},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"jsonrpc": "2.0", "id": 21, "result": {}})

    def test_protocol_version_header_is_validated(self) -> None:
        # FIX 5: a supplied-but-unsupported MCP-Protocol-Version is a 400; a
        # supported one and an absent one are both accepted.
        base = {"jsonrpc": "2.0", "id": 22, "method": "tools/list"}
        unsupported = self.http.post(
            "/mcp",
            json=base,
            headers={"Accept": MCP_ACCEPT, "MCP-Protocol-Version": "1999-01-01"},
        )
        self.assertEqual(unsupported.status_code, 400, unsupported.text)
        self.assertEqual(
            unsupported.json()["error"]["data"]["error_code"],
            "unsupported_protocol_version",
        )
        supported = self.http.post(
            "/mcp",
            json=base,
            headers={"Accept": MCP_ACCEPT, "MCP-Protocol-Version": PROTOCOL_VERSION},
        )
        self.assertEqual(supported.status_code, 200, supported.text)
        absent = self.http.post("/mcp", json=base, headers={"Accept": MCP_ACCEPT})
        self.assertEqual(absent.status_code, 200, absent.text)

    def test_both_mcp_endpoints_reject_declared_oversize_before_body_allocation(
        self,
    ) -> None:
        with (
            patch(
                "merv.brain.surface.transport.mcp_streamable_http."
                "MAX_MCP_REQUEST_BODY_BYTES",
                32,
            ),
            patch.object(
                Request,
                "body",
                side_effect=AssertionError("MCP endpoints must stream capped bodies"),
            ),
        ):
            legacy = self.http.post(
                "/mcp/call",
                content=b"x" * 33,
                headers={"Content-Type": "application/json"},
            )
            streamable = self.http.post(
                "/mcp",
                content=b"x" * 33,
                headers={"Content-Type": "application/json", "Accept": MCP_ACCEPT},
            )
        self.assertEqual(legacy.status_code, 413, legacy.text)
        self.assertEqual(legacy.json()["error_code"], "request_too_large")
        self.assertEqual(legacy.json()["max_body_bytes"], 32)
        self.assertEqual(streamable.status_code, 413, streamable.text)
        self.assertEqual(
            streamable.json()["error"]["data"],
            {"error_code": "request_too_large", "max_body_bytes": 32},
        )


class McpStreamableHttpProgressTest(unittest.TestCase):
    SLOW_SECONDS = 0.4

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        def call_tool(name, arguments, context, request):
            if name == "slow.tool":
                time.sleep(self.SLOW_SECONDS)
            return {"ok": True, "name": name}

        register_mcp_routes(
            app,
            list_tools=lambda: [{"name": "fast.tool"}, {"name": "slow.tool"}],
            call_tool=call_tool,
        )
        return app

    def test_slow_call_emits_progress_before_completion_and_fast_call_is_json(self) -> None:
        app = self._build_app()
        client = TestClient(app)
        mcp = _McpClient(client)
        mcp.initialize()

        fast = mcp.request("tools/call", {"name": "fast.tool"})
        self.assertEqual(fast.status_code, 200, fast.text)
        self.assertTrue(fast.headers["content-type"].startswith("application/json"))

        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "slow.tool",
                    "_meta": {"progressToken": "slow-10"},
                },
            }
        ).encode()
        headers = [
            (b"host", b"mcp"),
            (b"content-type", b"application/json"),
            (b"accept", MCP_ACCEPT.encode()),
            (b"mcp-protocol-version", PROTOCOL_VERSION.encode()),
        ]

        async def scenario() -> tuple[float, list[dict[str, Any]]]:
            sent_request = False
            messages: list[dict[str, Any]] = []
            first_body_at: float | None = None
            started = time.monotonic()
            disconnected = asyncio.Event()

            async def receive() -> dict[str, Any]:
                nonlocal sent_request
                if not sent_request:
                    sent_request = True
                    return {"type": "http.request", "body": payload, "more_body": False}
                await disconnected.wait()
                return {"type": "http.disconnect"}

            async def send(message: dict[str, Any]) -> None:
                nonlocal first_body_at
                messages.append(message)
                if (
                    message["type"] == "http.response.body"
                    and message.get("body")
                    and first_body_at is None
                ):
                    first_body_at = time.monotonic()

            await app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/mcp",
                    "raw_path": b"/mcp",
                    "query_string": b"",
                    "headers": headers,
                    "client": ("127.0.0.1", 1234),
                    "server": ("mcp", 80),
                },
                receive,
                send,
            )
            self.assertIsNotNone(first_body_at)
            body = b"".join(
                message.get("body", b"")
                for message in messages
                if message["type"] == "http.response.body"
            ).decode()
            frames = [
                json.loads(line.removeprefix("data: "))
                for line in body.splitlines()
                if line.startswith("data: ")
            ]
            return float(first_body_at) - started, frames

        first_byte_seconds, frames = asyncio.run(scenario())
        self.assertLess(first_byte_seconds, 0.2)
        self.assertLess(first_byte_seconds, self.SLOW_SECONDS)
        self.assertEqual(frames[0]["method"], "notifications/progress")
        self.assertEqual(frames[0]["params"]["progressToken"], "slow-10")
        self.assertEqual(frames[-1]["id"], 10)
        self.assertEqual(
            frames[-1]["result"]["structuredContent"],
            {"ok": True, "name": "slow.tool"},
        )


class McpStreamablePreflightTest(unittest.TestCase):
    """FIX 6: a scope/visibility denial resolves SYNCHRONOUSLY, before the SSE
    stream can commit a 200, so it is always a transport 403 — even when the
    tool executor is slow enough that the fast-call window would have elapsed."""

    def test_scope_denial_is_http_403_even_when_execution_is_slow(self) -> None:
        from merv.brain.surface.identity import ProjectKeyScopeError

        app = FastAPI()

        def call_tool(name, arguments, context, request):
            time.sleep(0.3)  # far past the 50ms fast-call window
            return {"ok": True, "name": name}

        def authorize_scope(request, project_id):
            raise ProjectKeyScopeError(
                "project API key cannot access a different project",
                details={"requested_project_id": project_id},
            )

        register_mcp_routes(
            app,
            list_tools=lambda: [{"name": "slow.tool"}],
            call_tool=call_tool,
            authorize_scope=authorize_scope,
        )
        mcp = _McpClient(TestClient(app))
        mcp.initialize()  # Accept carries text/event-stream (would stream)
        response = mcp.request(
            "tools/call",
            {"name": "slow.tool", "arguments": {"project_id": "p_other"}},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertTrue(
            response.headers["content-type"].startswith("application/json")
        )
        self.assertEqual(
            response.json()["error"]["data"]["error_code"], "project_scope_forbidden"
        )


if __name__ == "__main__":
    unittest.main()
