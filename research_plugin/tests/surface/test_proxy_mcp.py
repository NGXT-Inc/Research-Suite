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
from backend.project_router import ProjectRouter
from mcp_server.daemon_marker import (
    discover_daemon_url,
    marker_path,
    read_marker,
    write_marker,
)
from backend.http_server import make_http_server
from backend.execution.backends.fake import FakeSandboxBackend
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

    def test_env_var_overrides_marker(self) -> None:
        write_marker(repo_root=self.repo, host="127.0.0.1", port=8787)
        with mock.patch.dict(os.environ, {"RESEARCH_PLUGIN_DAEMON_URL": "http://1.2.3.4:9999"}):
            self.assertEqual(discover_daemon_url(repo_root=self.repo), "http://1.2.3.4:9999")


class _LiveDaemonFixture:
    """Spin up a real HTTP daemon on a free port for end-to-end proxy tests."""

    def __init__(self, *, repo: Path) -> None:
        self.app = ResearchPluginApp(
            repo_root=repo,
            db_path=repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
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
            self._thread.join(timeout=5.0)
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
        tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
        tool_names = set(tools)
        self.assertIn("workflow.status_and_next", tool_names)
        self.assertIn("project.current", tool_names)
        self.assertIn("project.create", tool_names)
        self.assertIn("sandbox.request", tool_names)
        self.assertIn("sandbox.sync", tool_names)
        self.assertNotIn("project.list", tool_names)
        workflow_schema = tools["workflow.status_and_next"]["inputSchema"]
        self.assertNotIn("project_id", workflow_schema.get("properties", {}))
        self.assertNotIn("project_id", workflow_schema.get("required", []))

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

class HttpProxyMcpServerRoutedDaemonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo_a = self.root / "repo-a"
        self.repo_b = self.root / "repo-b"
        self.router = ProjectRouter(
            registry_db_path=self.root / "registry.sqlite",
            execution_backend_factory=lambda _repo: FakeSandboxBackend(),
        )
        self.server = make_http_server(router=self.router, host="127.0.0.1", port=0)
        self.host, self.port = self.server.server_address
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://{self.host}:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self._thread.join(timeout=5.0)
        self.server.server_close()
        self.router.shutdown()
        self.tmp.cleanup()

    def _project_current(self, repo: Path) -> dict:
        proxy = HttpProxyMcpServer(config=ProxyConfig(repo_root=repo, daemon_url=self.url))
        response = proxy.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "project.current", "arguments": {}},
            }
        )
        self.assertNotIn("error", response)
        return response["result"]["structuredContent"]

    def _create_project(self, repo: Path, name: str) -> dict:
        proxy = HttpProxyMcpServer(config=ProxyConfig(repo_root=repo, daemon_url=self.url))
        response = proxy.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "project.create", "arguments": {"name": name}},
            }
        )
        self.assertNotIn("error", response)
        return response["result"]["structuredContent"]

    def test_mcp_project_current_is_scoped_and_does_not_create_project(self) -> None:
        current_a = self._project_current(self.repo_a)

        self.assertFalse(current_a["exists"])
        self.assertIsNone(current_a["project"])
        self.assertIn("project.create", current_a["hint"])
        self.assertIn("Ask the user", current_a["hint"])
        self.assertFalse((self.repo_a / ".research_plugin" / "state.sqlite").exists())
        self.assertEqual(self.router.list_projects()["projects"], [])

    def test_markerless_fresh_repo_can_use_default_shared_daemon_url(self) -> None:
        self.assertFalse(marker_path(repo_root=self.repo_a).exists())
        with mock.patch.dict(os.environ, {"RESEARCH_PLUGIN_DEFAULT_DAEMON_URL": self.url}):
            proxy = HttpProxyMcpServer(config=ProxyConfig(repo_root=self.repo_a, daemon_url=None))
            response = proxy.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "project.current", "arguments": {}},
                }
            )

        self.assertNotIn("error", response)
        current = response["result"]["structuredContent"]
        self.assertFalse(current["exists"])
        self.assertIn("project.create", current["hint"])
        self.assertIn("Ask the user", current["hint"])

    def test_mcp_sandbox_health_does_not_require_folder_project(self) -> None:
        proxy = HttpProxyMcpServer(config=ProxyConfig(repo_root=self.repo_a, daemon_url=self.url))
        response = proxy.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "sandbox.health", "arguments": {}},
            }
        )

        self.assertNotIn("error", response)
        health = response["result"]["structuredContent"]
        self.assertTrue(health["ok"])
        self.assertEqual(health["mode"], "multi_project")

    def test_mcp_project_create_establishes_folder_mapping(self) -> None:
        project_a = self._create_project(self.repo_a, "Project A")
        project_b = self._create_project(self.repo_b, "Project B")

        current_a = self._project_current(self.repo_a)
        current_b = self._project_current(self.repo_b)

        self.assertTrue(current_a["exists"])
        self.assertTrue(current_b["exists"])
        self.assertEqual(current_a["project"]["id"], project_a["id"])
        self.assertEqual(current_b["project"]["id"], project_b["id"])
        self.assertNotEqual(project_a["id"], project_b["id"])
        self.assertTrue((self.repo_a / ".research_plugin" / "state.sqlite").exists())
        self.assertTrue((self.repo_b / ".research_plugin" / "state.sqlite").exists())

        global_projects = self.router.list_projects()["projects"]
        self.assertEqual({p["id"] for p in global_projects}, {project_a["id"], project_b["id"]})

    def test_mcp_project_current_recovers_existing_folder_project(self) -> None:
        app = ResearchPluginApp(
            repo_root=self.repo_a,
            db_path=self.repo_a / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        existing = app.projects.list_projects()["projects"][0]
        app.projects.update(project_id=existing["id"], name="Existing Project")
        app.shutdown()

        current = self._project_current(self.repo_a)

        self.assertTrue(current["exists"])
        self.assertEqual(current["project"]["id"], existing["id"])
        self.assertEqual(current["project"]["name"], "Existing Project")
        self.assertEqual(self.router.list_projects()["projects"][0]["id"], existing["id"])

    def test_routed_project_scoped_tool_uses_hidden_repo_context(self) -> None:
        project_a = self._create_project(self.repo_a, "Project A")
        proxy = HttpProxyMcpServer(config=ProxyConfig(repo_root=self.repo_a, daemon_url=self.url))
        status = proxy.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "workflow.status_and_next", "arguments": {}},
            }
        )
        self.assertNotIn("error", status)
        project = status["result"]["structuredContent"]["project"]
        self.assertEqual(project["id"], project_a["id"])

    def test_routed_project_scoped_tool_requires_registered_folder_project(self) -> None:
        proxy = HttpProxyMcpServer(config=ProxyConfig(repo_root=self.repo_a, daemon_url=self.url))
        status = proxy.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "workflow.status_and_next", "arguments": {}},
            }
        )
        self.assertEqual(status["error"]["data"]["error_code"], "validation_error")
        self.assertIn("project.create", status["error"]["message"])

    def test_shared_daemon_clears_project_marker_on_close(self) -> None:
        self._create_project(self.repo_a, "Project A")
        marker = marker_path(repo_root=self.repo_a)
        self.assertTrue(marker.exists())
        self.assertEqual(discover_daemon_url(repo_root=self.repo_a), self.url)

        self.server.server_close()

        self.assertFalse(marker.exists())


class HttpProxyMcpServerOfflineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_tools_call_returns_actionable_error_when_daemon_missing(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"RESEARCH_PLUGIN_DEFAULT_DAEMON_URL": "http://127.0.0.1:1"},
            clear=False,
        ):
            os.environ.pop("RESEARCH_PLUGIN_DAEMON_URL", None)
            proxy = HttpProxyMcpServer(config=ProxyConfig(repo_root=self.repo, daemon_url=None))
            response = proxy.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "project.current", "arguments": {}},
                }
            )
        self.assertEqual(response["error"]["data"]["error_code"], "daemon_not_running")
        self.assertIn("research-plugin-http", response["error"]["message"])

if __name__ == "__main__":
    unittest.main()
