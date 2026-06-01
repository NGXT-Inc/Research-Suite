"""End-to-end tests for the stdio-MCP → HTTP-daemon proxy.

The MCP server's job is to forward Codex tool calls to the long-running HTTP
daemon. These tests spin up a real daemon on a free port and exercise the
proxy's ``handle`` directly (the stdio loop is just JSON-in → JSON-out).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from backend.app import ResearchPluginApp
from mcp_server.daemon_marker import (
    discover_daemon_url,
    marker_path,
    read_marker,
    write_marker,
)
from backend.http_server import make_http_server
from backend.execution.backends.fake import FakeBackend
from mcp_server.proxy import HttpProxyMcpServer, ProxyConfig


class DaemonMarkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_write_then_read_round_trips(self) -> None:
        path = write_marker(repo_root=self.repo, host="127.0.0.1", port=8787)
        self.assertEqual(path, marker_path(repo_root=self.repo))
        info = read_marker(repo_root=self.repo)
        self.assertIsNotNone(info)
        assert info is not None  # appease type checkers
        self.assertEqual(info.host, "127.0.0.1")
        self.assertEqual(info.port, 8787)
        self.assertEqual(info.url, "http://127.0.0.1:8787")

    def test_read_returns_none_when_missing_or_corrupt(self) -> None:
        self.assertIsNone(read_marker(repo_root=self.repo))
        path = marker_path(repo_root=self.repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json {")
        self.assertIsNone(read_marker(repo_root=self.repo))

    def test_ipv6_url_is_bracketed(self) -> None:
        write_marker(repo_root=self.repo, host="::1", port=9090)
        info = read_marker(repo_root=self.repo)
        assert info is not None
        self.assertEqual(info.url, "http://[::1]:9090")

    def test_env_var_overrides_marker(self) -> None:
        write_marker(repo_root=self.repo, host="127.0.0.1", port=8787)
        with mock.patch.dict(os.environ, {"RESEARCH_PLUGIN_DAEMON_URL": "http://1.2.3.4:9999"}):
            self.assertEqual(discover_daemon_url(repo_root=self.repo), "http://1.2.3.4:9999")

    def test_discover_returns_none_without_env_or_marker(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RESEARCH_PLUGIN_DAEMON_URL", None)
            self.assertIsNone(discover_daemon_url(repo_root=self.repo))


class _LiveDaemonFixture:
    """Spin up a real HTTP daemon on a free port for end-to-end proxy tests."""

    def __init__(self, *, repo: Path) -> None:
        self.app = ResearchPluginApp(
            repo_root=repo,
            db_path=repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeBackend(),
        )
        self.server = make_http_server(self.app, "127.0.0.1", 0)
        self.host, self.port = self.server.server_address
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        # serve_forever writes the daemon marker once it actually starts the
        # uvicorn run loop, which is racy from the test thread's perspective.
        # Wait for the marker to appear so discovery tests are deterministic.
        deadline = 5.0
        step = 0.05
        elapsed = 0.0
        while elapsed < deadline and not marker_path(repo_root=repo).exists():
            import time as _t

            _t.sleep(step)
            elapsed += step
        self.url = f"http://{self.host}:{self.port}"

    def stop(self) -> None:
        try:
            self.server.shutdown()
        finally:
            self.server.server_close()


class HttpProxyMcpServerLiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.fixture = _LiveDaemonFixture(repo=self.repo)
        self.proxy = HttpProxyMcpServer(
            config=ProxyConfig(repo_root=self.repo, daemon_url=self.fixture.url),
        )

    def tearDown(self) -> None:
        self.fixture.stop()
        self.tmp.cleanup()

    def test_initialize_does_not_require_daemon(self) -> None:
        # Even if the daemon is unreachable, initialize/ping must succeed so
        # Codex can register the server and present a graceful error later.
        offline = HttpProxyMcpServer(
            config=ProxyConfig(repo_root=self.repo, daemon_url="http://127.0.0.1:1"),
        )
        init = offline.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "research-plugin")
        ping = offline.handle({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}})
        self.assertEqual(ping["result"], {})

    def test_tools_list_round_trips_through_daemon(self) -> None:
        listed = self.proxy.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tool_names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertIn("workflow.status_and_next", tool_names)
        self.assertIn("project.create", tool_names)
        self.assertIn("job.submit", tool_names)

    def test_tools_call_round_trips_with_structured_content(self) -> None:
        created = self.proxy.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "project.create", "arguments": {"name": "Proxy Project"}},
            }
        )
        project = created["result"]["structuredContent"]
        self.assertEqual(project["name"], "Proxy Project")
        # Text content is the JSON-stringified structured result.
        text = json.loads(created["result"]["content"][0]["text"])
        self.assertEqual(text["id"], project["id"])

    def test_tool_validation_errors_round_trip(self) -> None:
        missing = self.proxy.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "workflow.status_and_next", "arguments": {}},
            }
        )
        self.assertEqual(missing["error"]["code"], -32000)
        self.assertIn("project_id is required", missing["error"]["message"])
        self.assertEqual(missing["error"]["data"]["error_code"], "validation_error")

    def test_unknown_tool_returns_research_plugin_error(self) -> None:
        unknown = self.proxy.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "nonsense.tool", "arguments": {}},
            }
        )
        self.assertEqual(unknown["error"]["data"]["error_code"], "research_plugin_error")

    def test_daemon_calls_are_logged_with_mcp_source(self) -> None:
        self.proxy.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "project.create", "arguments": {"name": "Source Tag"}},
            }
        )
        events = self.fixture.app.activity.recent(limit=20, source="mcp")["events"]
        self.assertTrue(
            any(event.get("tool") == "project.create" and event.get("source") == "mcp" for event in events)
        )

    def test_proxy_rediscovers_daemon_url_each_call(self) -> None:
        # Even if the proxy was constructed without an explicit URL, it should
        # discover the daemon via the marker that the live fixture wrote.
        fresh = HttpProxyMcpServer(config=ProxyConfig(repo_root=self.repo, daemon_url=None))
        listed = fresh.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertIn("project.create", {tool["name"] for tool in listed["result"]["tools"]})


class HttpProxyMcpServerOfflineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_tools_call_returns_actionable_error_when_daemon_missing(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RESEARCH_PLUGIN_DAEMON_URL", None)
            proxy = HttpProxyMcpServer(config=ProxyConfig(repo_root=self.repo, daemon_url=None))
            response = proxy.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "project.list", "arguments": {}},
                }
            )
        self.assertEqual(response["error"]["data"]["error_code"], "daemon_not_running")
        self.assertIn("research-plugin-http", response["error"]["message"])

    def test_tools_call_returns_error_when_daemon_url_unreachable(self) -> None:
        proxy = HttpProxyMcpServer(
            config=ProxyConfig(repo_root=self.repo, daemon_url="http://127.0.0.1:1", timeout_seconds=1.0),
        )
        response = proxy.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "project.list", "arguments": {}},
            }
        )
        self.assertEqual(response["error"]["data"]["error_code"], "daemon_not_running")


if __name__ == "__main__":
    unittest.main()
