"""JSON-RPC boundary tests for the stdio MCP proxy.

The proxy always talks to one brain URL and executes data-plane tools locally.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from tests.support.brain import TestBrain
from backend.execution.backends.fake import FakeSandboxBackend
from backend.transport.http_api import create_fastapi_app
from backend.transport.http_policy import HttpSurfacePolicy
from mcp_server.project_links import ProjectLinks
from mcp_server.proxy import HttpProxyMcpServer, ProxyConfig


class _HttpHarness:
    def __init__(self, *, app: TestBrain, url: str) -> None:
        self.url = url
        self.client = TestClient(
            create_fastapi_app(
                app=app,
                surface_policy=HttpSurfacePolicy.for_surface(
                    restrict_cors=False,
                    hosted_control=False,
                ),
            )
        )

    def bind(self, proxy: HttpProxyMcpServer) -> None:
        proxy._http_get = self.http_get  # type: ignore[method-assign]
        proxy._http_post = self.http_post  # type: ignore[method-assign]

    def http_get(self, *, url: str, is_cloud: bool) -> dict:  # noqa: ARG002
        response = self.client.get(urlsplit(url).path)
        response.raise_for_status()
        return response.json()

    def http_post(self, *, url: str, payload: dict, is_cloud: bool, timeout=None) -> dict:  # noqa: ANN001, ARG002
        response = self.client.post(urlsplit(url).path, json=payload)
        response.raise_for_status()
        return response.json()


class HttpProxyMcpServerUnifiedBrainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.harness = _HttpHarness(app=self.app, url="http://local.test")
        self.proxy = HttpProxyMcpServer(
            config=ProxyConfig(repo_root=self.repo, control_url=self.harness.url),
        )
        self.harness.bind(self.proxy)

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def test_initialize_does_not_require_http_backend(self) -> None:
        offline = HttpProxyMcpServer(
            config=ProxyConfig(repo_root=self.repo, control_url="http://127.0.0.1:1"),
        )
        init = offline.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "research-plugin")
        ping = offline.handle({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}})
        self.assertEqual(ping["result"], {})

    def test_tools_list_round_trips_through_local_http_backend(self) -> None:
        listed = self.proxy.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {tool["name"]: tool for tool in listed["result"]["tools"]}

        self.assertIn("workflow.status_and_next", tools)
        # The merged project tool replaces project.current/connect/create.
        self.assertIn("project", tools)
        self.assertNotIn("project.current", tools)
        self.assertNotIn("project.connect", tools)
        self.assertNotIn("project.create", tools)
        self.assertIn("sandbox.request", tools)
        self.assertNotIn("project.list", tools)
        # UI-facing tools stay dispatchable but are hidden from the agent list.
        self.assertNotIn("project.get", tools)
        self.assertNotIn("project.update", tools)
        # The project tool keeps project_id visible (action=connect needs it),
        # unlike every other tool whose repo scope the proxy injects.
        project_schema = tools["project"]["inputSchema"]
        self.assertIn("project_id", project_schema.get("properties", {}))
        # review.status is a REST/UI + internal read; agents poll
        # workflow.status_and_next instead, so it is dropped from tools/list
        # while the rest of the review surface stays agent-facing.
        self.assertNotIn("review.status", tools)
        self.assertIn("review.request", tools)
        self.assertIn("review.submit", tools)
        self.assertFalse(any("hidden" in tool for tool in tools.values()))
        workflow_schema = tools["workflow.status_and_next"]["inputSchema"]
        self.assertNotIn("project_id", workflow_schema.get("properties", {}))
        self.assertNotIn("project_id", workflow_schema.get("required", []))

    def test_tools_call_round_trips_with_structured_content(self) -> None:
        created = self.proxy.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "project",
                    "arguments": {"action": "create", "name": "Proxy Project"},
                },
            }
        )

        project = created["result"]["structuredContent"]
        self.assertEqual(project["name"], "Proxy Project")
        text = json.loads(created["result"]["content"][0]["text"])
        self.assertEqual(text["id"], project["id"])


class HttpProxyMcpServerSplitLinkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo_a = self.root / "repo-a"
        self.repo_b = self.root / "repo-b"
        self.repo_a.mkdir()
        self.repo_b.mkdir()
        self.control_app = TestBrain(
            repo_root=self.root / "control",
            db_path=self.root / "control.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.control = _HttpHarness(app=self.control_app, url="http://control.test")
        self.links_path = self.root / "project_links.sqlite"

    def tearDown(self) -> None:
        self.control_app.shutdown()
        self.tmp.cleanup()

    def _proxy(self, repo: Path) -> HttpProxyMcpServer:
        proxy = HttpProxyMcpServer(
            config=ProxyConfig(
                repo_root=repo,
                control_url=self.control.url,
                project_links_path=self.links_path,
            )
        )
        self.control.bind(proxy)
        return proxy

    def _call(self, repo: Path, name: str, arguments: dict | None = None) -> dict:
        response = self._proxy(repo).handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        self.assertNotIn("error", response, response)
        return response["result"]["structuredContent"]

    def test_project_current_unlinked_folder_does_not_create_project(self) -> None:
        current = self._call(self.repo_a, "project", {"action": "current"})

        self.assertFalse(current["exists"])
        self.assertIsNone(current["project"])
        self.assertIn('action="connect"', current["hint"])
        self.assertEqual(ProjectLinks(db_path=self.links_path).list_links(), [])

    def test_project_create_returns_cloud_project_without_implicit_link(self) -> None:
        created = self._call(
            self.repo_a,
            "project",
            {"action": "create", "name": "Project A", "summary": "Hosted project."},
        )
        current = self._call(self.repo_a, "project", {"action": "current"})

        self.assertEqual(created["name"], "Project A")
        self.assertFalse(current["exists"])

    def test_project_current_fetches_linked_cloud_project(self) -> None:
        project = self._call(
            self.repo_a,
            "project",
            {"action": "create", "name": "Linked Project", "summary": "Hosted project."},
        )
        ProjectLinks(db_path=self.links_path).link(
            repo_root=str(self.repo_a), project_id=project["id"]
        )

        current = self._call(self.repo_a, "project", {"action": "current"})

        self.assertTrue(current["exists"])
        self.assertEqual(current["project"]["id"], project["id"])
        self.assertEqual(current["project"]["repo_root"], str(self.repo_a))

    def test_sandbox_health_does_not_require_folder_project(self) -> None:
        health = self._call(self.repo_a, "sandbox.health")

        self.assertTrue(health["ok"])
        self.assertTrue(health["data_plane"]["reachable"])
        self.assertEqual(health["data_plane"]["mode"], "proxy")
        self.assertTrue(health["control_plane"]["reachable"])

    def test_project_scoped_tool_requires_registered_folder_project(self) -> None:
        response = self._proxy(self.repo_a).handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "workflow.status_and_next", "arguments": {}},
            }
        )

        self.assertEqual(response["error"]["data"]["error_code"], "project_not_linked")
        self.assertIn('action="connect"', response["error"]["message"])

    def test_project_scoped_tool_uses_hidden_project_link(self) -> None:
        project = self.control_app.projects.list_projects()["projects"][0]
        ProjectLinks(db_path=self.links_path).link(
            repo_root=str(self.repo_a), project_id=project["id"]
        )

        status = self._call(self.repo_a, "workflow.status_and_next")

        self.assertEqual(status["project"]["id"], project["id"])

    def test_many_folders_can_link_to_many_projects(self) -> None:
        project_a = self._call(self.repo_a, "project", {"action": "create", "name": "Project A"})
        project_b = self._call(self.repo_b, "project", {"action": "create", "name": "Project B"})
        links = ProjectLinks(db_path=self.links_path)
        links.link(repo_root=str(self.repo_a), project_id=project_a["id"])
        links.link(repo_root=str(self.repo_b), project_id=project_b["id"])

        current_a = self._call(self.repo_a, "project", {"action": "current"})
        current_b = self._call(self.repo_b, "project", {"action": "current"})

        self.assertEqual(current_a["project"]["id"], project_a["id"])
        self.assertEqual(current_b["project"]["id"], project_b["id"])
        self.assertNotEqual(project_a["id"], project_b["id"])


class HttpProxyMcpServerOfflineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_tools_call_returns_actionable_error_when_local_backend_missing(self) -> None:
        proxy = HttpProxyMcpServer(
            config=ProxyConfig(repo_root=self.repo, control_url="http://127.0.0.1:1")
        )
        response = proxy.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "project",
                    "arguments": {"action": "create", "name": "Offline"},
                },
            }
        )
        self.assertNotIn("error", response)
        result = response["result"]
        self.assertTrue(result.get("isError"))
        structured = result["structuredContent"]
        self.assertEqual(structured["error_code"], "brain_not_running")
        self.assertIn("research-plugin-http", structured["error"])


if __name__ == "__main__":
    unittest.main()
