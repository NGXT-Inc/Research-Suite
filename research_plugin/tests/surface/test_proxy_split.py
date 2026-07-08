"""Split-mode proxy routing after the daemon diet.

Split mode now has one HTTP upstream: hosted control. The stdio proxy performs
local data-plane file reads itself, resolves repo→project links from the local
SQLite link file, and forwards explicit facts/bytes to control.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from tests.support.brain import TestBrain
from backend.execution.backends.fake import FakeSandboxBackend
from backend.transport.http_api import create_fastapi_app
from mcp_server.project_links import ProjectLinks
from mcp_server.proxy import HttpProxyMcpServer, ProxyConfig


class _ControlHarness:
    def __init__(self, *, app: TestBrain, repo: Path) -> None:
        del repo
        self.url = "http://control.test"
        self.client = TestClient(create_fastapi_app(app=app))

    def http_get(self, *, url: str, is_cloud: bool) -> dict:  # noqa: ARG002
        response = self.client.get(urlsplit(url).path)
        response.raise_for_status()
        return response.json()

    def http_post(self, *, url: str, payload: dict, is_cloud: bool, timeout=None) -> dict:  # noqa: ANN001, ARG002
        response = self.client.post(urlsplit(url).path, json=payload)
        response.raise_for_status()
        return response.json()


class SplitProxyLocalDataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        db_path = self.repo / ".research_plugin" / "state.sqlite"
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=db_path,
            execution_backend=FakeSandboxBackend(),
        )
        self.cloud = _ControlHarness(app=self.app, repo=self.repo)
        self.project = self.app.projects.list_projects()["projects"][0]
        self.links_path = self.repo / "project_links.sqlite"
        ProjectLinks(db_path=self.links_path).link(
            repo_root=str(self.repo), project_id=self.project["id"]
        )
        self.proxy = HttpProxyMcpServer(
            config=ProxyConfig(
                repo_root=self.repo,
                control_url=self.cloud.url,
                project_links_path=self.links_path,
            )
        )
        self.proxy._http_get = self.cloud.http_get  # type: ignore[method-assign]
        self.proxy._http_post = self.cloud.http_post  # type: ignore[method-assign]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def _call(self, name: str, arguments: dict | None = None) -> dict:
        response = self.proxy.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        self.assertNotIn("error", response, response)
        return response["result"]["structuredContent"]

    def test_tools_list_merges_cloud_and_proxy_local_catalogs(self) -> None:
        response = self.proxy.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}

        self.assertIn("claim.create", names)
        self.assertIn("resource.register_file", names)
        self.assertIn("sandbox.get", names)
        self.assertIn("project", names)
        self.assertNotIn("project.connect", names)
        self.assertNotIn("project.list", names)
        for tool in response["result"]["tools"]:
            self.assertNotIn("plane", tool)
            if tool["name"] == "project":
                # The one schema that keeps project_id: for action=connect it is
                # the caller's explicit choice of which project to link, not
                # hidden repo context.
                self.assertIn("project_id", tool["inputSchema"]["properties"])
            else:
                self.assertNotIn("project_id", tool["inputSchema"].get("properties", {}))

    def test_control_tool_routes_to_cloud_with_project_id(self) -> None:
        claim = self._call(
            "claim.create",
            {"project_id": "caller_supplied_wrong", "statement": "A control claim."},
        )

        self.assertEqual(claim["project_id"], self.project["id"])
        listed = self._call("claim.list", {"project_id": "caller_supplied_wrong"})
        self.assertIn(claim["id"], {c["id"] for c in listed["claims"]})

    def test_project_overview_returns_full_claims_and_experiments(self) -> None:
        # overview is the whole-project read: a settled claim and a terminal
        # experiment must both appear, unlike the active-only workflow view.
        claim = self._call("claim.create", {"statement": "Overview claim."})
        self._call("claim.update", {"claim_id": claim["id"], "status": "abandoned"})
        exp = self._call("experiment.create", {"name": "dead-end", "intent": "terminal"})
        self._call(
            "experiment.transition",
            {"experiment_id": exp["id"], "transition": "abandon"},
        )

        overview = self._call("project", {"action": "overview"})

        self.assertEqual(overview["project"]["id"], self.project["id"])
        claims_by_id = {c["id"]: c for c in overview["claims"]}
        self.assertEqual(claims_by_id[claim["id"]]["status"], "abandoned")
        experiments_by_id = {e["id"]: e for e in overview["experiments"]}
        self.assertEqual(experiments_by_id[exp["id"]]["status"], "abandoned")

    def test_project_overview_unlinked_folder_behaves_like_current(self) -> None:
        unlinked = HttpProxyMcpServer(
            config=ProxyConfig(
                repo_root=self.repo / "other",
                control_url=self.cloud.url,
                project_links_path=self.repo / "empty_links.sqlite",
            )
        )
        unlinked._http_get = self.cloud.http_get  # type: ignore[method-assign]
        unlinked._http_post = self.cloud.http_post  # type: ignore[method-assign]
        response = unlinked.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "project", "arguments": {"action": "overview"}},
            }
        )
        current = response["result"]["structuredContent"]
        self.assertFalse(current["exists"])
        self.assertIn('action="connect"', current["hint"])

    def test_data_tool_reads_local_file_and_submits_observation_to_control(self) -> None:
        (self.repo / "note.txt").write_text("hello from proxy-local data plane\n")

        resource = self._call("resource.register_file", {"path": "note.txt"})

        self.assertEqual(resource["path"], "note.txt")
        self.assertEqual(resource["project_id"], self.project["id"])
        self.assertTrue(resource["current_version"]["content_sha256"])

    def test_enriched_control_health_reports_proxy_data_plane_and_cloud(self) -> None:
        health = self._call("sandbox.health")

        self.assertIn("data_plane", health)
        self.assertIn("control_plane", health)
        self.assertTrue(health["data_plane"]["reachable"])
        self.assertEqual(health["data_plane"]["mode"], "proxy")
        self.assertTrue(health["control_plane"]["reachable"])
        self.assertTrue(health["control_plane"]["configured"])

    def test_cloud_outage_blocks_control_submission_but_not_local_validation(self) -> None:
        broken = HttpProxyMcpServer(
            config=ProxyConfig(
                repo_root=self.repo,
                control_url="http://127.0.0.1:1",
                project_links_path=self.links_path,
            )
        )
        (self.repo / "plan.md").write_text("## Summary\nLocal validation only.\n")
        validation = broken.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "resource.validate",
                    "arguments": {"path": "plan.md", "role": "plan"},
                },
            }
        )
        self.assertNotIn("error", validation)
        self.assertEqual(validation["result"]["structuredContent"]["path"], "plan.md")
        self.assertIn("ok", validation["result"]["structuredContent"])

        down = broken.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "claim.list", "arguments": {}},
            }
        )
        self.assertNotIn("error", down)
        result = down["result"]["structuredContent"]
        self.assertEqual(result["error_code"], "brain_not_running")
        self.assertTrue(down["result"].get("isError"))

    def test_pull_outputs_runs_proxy_local_rsync_helper(self) -> None:
        def fake_cloud(*, name: str, arguments: dict) -> dict:
            self.assertEqual(name, "sandbox.get")
            return {
                "experiment_id": "exp_1",
                "sandbox_uid": arguments["sandbox_uid"],
                "status": "running",
                "experiment_dir": "/remote/exp",
                "ssh": {"host": "example.test", "port": 22, "user": "root"},
            }

        with (
            patch.object(self.proxy, "_call_cloud", side_effect=fake_cloud),
            patch(
                "backend.dataplane.sandbox_outputs.pull_sandbox_outputs",
                return_value={"ok": True, "copied": []},
            ) as pull,
        ):
            result = self._call(
                "sandbox.pull_outputs",
                {"sandbox_uid": "sbx_1", "key_path": "/tmp/rp-test-key"},
            )

        self.assertTrue(result["ok"])
        self.assertIn("rsync -az --itemize-changes", result["rsync"])
        self.assertIn("Use storage", result["storage_guidance"])
        sandbox = pull.call_args.kwargs["sandbox"]
        self.assertEqual(sandbox["ssh"]["key_path"], "/tmp/rp-test-key")


class ProjectConnectToolTest(unittest.TestCase):
    """project action=connect writes the folder→project link from the MCP session."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        db_path = self.repo / ".research_plugin" / "state.sqlite"
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=db_path,
            execution_backend=FakeSandboxBackend(),
        )
        self.cloud = _ControlHarness(app=self.app, repo=self.repo)
        self.seeded_project = self.app.projects.list_projects()["projects"][0]
        self.links_path = self.repo / "project_links.sqlite"
        # Deliberately unlinked: connect is the tool that establishes the link.
        self.proxy = HttpProxyMcpServer(
            config=ProxyConfig(
                repo_root=self.repo,
                control_url=self.cloud.url,
                project_links_path=self.links_path,
            )
        )
        self.proxy._http_get = self.cloud.http_get  # type: ignore[method-assign]
        self.proxy._http_post = self.cloud.http_post  # type: ignore[method-assign]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def _request(self, arguments: dict) -> dict:
        return self.proxy.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "project",
                    "arguments": {"action": "connect", **arguments},
                },
            }
        )

    def _connect(self, arguments: dict) -> dict:
        response = self._request(arguments)
        self.assertNotIn("error", response, response)
        return response["result"]["structuredContent"]

    def _connect_error(self, arguments: dict) -> dict:
        response = self._request(arguments)
        self.assertIn("error", response, response)
        return response["error"]

    def _brain_project_count(self) -> int:
        return len(self.app.projects.list_projects()["projects"])

    def test_connect_by_id_links_existing_project(self) -> None:
        result = self._connect({"project_id": self.seeded_project["id"]})

        self.assertTrue(result["linked"])
        self.assertFalse(result["created"])
        self.assertEqual(result["project"]["id"], self.seeded_project["id"])
        self.assertEqual(result["repo_root"], str(self.repo))
        self.assertEqual(self.proxy._resolve_project_id(), self.seeded_project["id"])

    def test_connect_by_name_creates_project_and_links(self) -> None:
        before = self._brain_project_count()

        result = self._connect(
            {"name": "Connected Project", "summary": "Created through the MCP."}
        )

        self.assertTrue(result["linked"])
        self.assertTrue(result["created"])
        self.assertEqual(result["project"]["name"], "Connected Project")
        self.assertEqual(self._brain_project_count(), before + 1)
        self.assertEqual(self.proxy._resolve_project_id(), result["project"]["id"])
        # The link is live for the rest of the session: action=current flips.
        current = self.proxy._call_tool(
            name="project", arguments={"action": "current"}
        )
        self.assertTrue(current["exists"])
        self.assertEqual(current["project"]["id"], result["project"]["id"])

    def test_relink_requires_overwrite_and_never_orphans_a_project(self) -> None:
        self._connect({"project_id": self.seeded_project["id"]})
        before = self._brain_project_count()

        error = self._connect_error({"name": "Replacement", "summary": ""})

        self.assertEqual(error["data"]["error_code"], "already_linked")
        self.assertEqual(error["data"]["project_id"], self.seeded_project["id"])
        # The guard fires before the cloud call: no orphan project was created.
        self.assertEqual(self._brain_project_count(), before)
        self.assertEqual(self.proxy._resolve_project_id(), self.seeded_project["id"])

        # Re-linking the same id is an idempotent no-op, no overwrite needed.
        again = self._connect({"project_id": self.seeded_project["id"]})
        self.assertTrue(again["linked"])

        relinked = self._connect({"name": "Replacement", "summary": "", "overwrite": True})
        self.assertTrue(relinked["created"])
        self.assertEqual(self.proxy._resolve_project_id(), relinked["project"]["id"])

    def test_connect_requires_exactly_one_of_id_or_name(self) -> None:
        for arguments in ({}, {"project_id": "proj_x", "name": "Both Given"}):
            error = self._connect_error(arguments)
            self.assertIn("exactly one", error["message"])
        self.assertIsNone(self.proxy._resolve_project_id())

    def test_connect_by_unknown_id_writes_no_link(self) -> None:
        response = self._request({"project_id": "proj_does_not_exist"})

        self.assertIn("error", response)
        self.assertIsNone(self.proxy._resolve_project_id())


class LocalEnrichedControlMergeTest(unittest.TestCase):
    def test_sandbox_get_merges_proxy_local_experiment_dir_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proxy = HttpProxyMcpServer(
                config=ProxyConfig(
                    repo_root=Path(tmp),
                    control_url="http://control.invalid",
                )
            )

            proxy._call_cloud = lambda **_: {
                "experiment_id": "exp_1",
                "sandbox_uid": "sbx_1234567890abcdef",
                "status": "running",
                "ssh": {"host": "example", "port": 22, "user": "root"},
            }
            proxy._call_local_data = lambda **_: {
                "local_dir": f"{tmp}/experiments/sandbox-sbx_12345678",
            }

            merged = proxy._call_local_enriched_control(
                name="sandbox.get", arguments={"experiment_id": "exp_1"}
            )

        self.assertIn("local_experiment_dir", merged)
        for key in ("command", "raw_command", "key_path", "local_dir", "local_sync_dir"):
            self.assertNotIn(key, merged)
        self.assertNotIn("command", merged["ssh"])
        self.assertNotIn("raw_command", merged["ssh"])
        self.assertNotIn("key_path", merged["ssh"])


class ProxyIdentityResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.links_path = self.repo / "links.sqlite"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _proxy(self) -> HttpProxyMcpServer:
        return HttpProxyMcpServer(
            config=ProxyConfig(
                repo_root=self.repo,
                control_url="http://control.invalid",
                project_links_path=self.links_path,
            )
        )

    def test_resolve_project_id_reads_proxy_link_store(self) -> None:
        links = ProjectLinks(db_path=self.links_path)
        links.link(repo_root=str(self.repo), project_id="proj_cloud_minted")
        proxy = self._proxy()

        self.assertEqual(proxy._resolve_project_id(), "proj_cloud_minted")
        links.link(repo_root=str(self.repo), project_id="proj_relinked")
        self.assertEqual(proxy._resolve_project_id(), "proj_relinked")

    def test_split_proxy_overrides_caller_supplied_project_id(self) -> None:
        proxy = self._proxy()
        proxy._tool_meta = lambda **_: SimpleNamespace(project_scoped=True)  # type: ignore[method-assign]
        proxy._resolve_project_id = lambda: "proj_authoritative"  # type: ignore[method-assign]
        captured: dict = {}

        def _capture_post(**kwargs):  # noqa: ANN003
            captured.update(kwargs.get("payload") or {})
            return {"result": {}}

        proxy._http_post = _capture_post  # type: ignore[method-assign]
        proxy._call_cloud(name="claim.list", arguments={"project_id": "proj_evil"})

        self.assertEqual(captured["arguments"]["project_id"], "proj_authoritative")

    def test_project_current_reports_unlinked_folder_without_cloud_lookup(self) -> None:
        proxy = self._proxy()

        response = proxy.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "project", "arguments": {"action": "current"}},
            }
        )

        self.assertNotIn("error", response)
        current = response["result"]["structuredContent"]
        self.assertFalse(current["exists"])
        self.assertIsNone(current["project"])
        self.assertIn('action="connect"', current["hint"])
        self.assertEqual(current["repo_root"], str(self.repo))

    def test_project_current_fetches_linked_cloud_project_by_id(self) -> None:
        ProjectLinks(db_path=self.links_path).link(
            repo_root=str(self.repo), project_id="proj_linked"
        )
        proxy = self._proxy()
        captured: dict = {}

        def _fake_cloud(**kwargs):  # noqa: ANN003
            captured.update(kwargs)
            return {"id": kwargs["arguments"]["project_id"], "name": "Linked"}

        proxy._call_cloud = _fake_cloud  # type: ignore[method-assign]

        response = proxy.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "project", "arguments": {"action": "current"}},
            }
        )

        self.assertNotIn("error", response)
        self.assertEqual(captured["name"], "project.get")
        self.assertEqual(captured["arguments"], {"project_id": "proj_linked"})
        current = response["result"]["structuredContent"]
        self.assertTrue(current["exists"])
        self.assertEqual(current["project"]["id"], "proj_linked")
        self.assertEqual(current["project"]["repo_root"], str(self.repo))

    def test_split_proxy_strips_project_id_from_proxy_local_data_calls(self) -> None:
        proxy = self._proxy()
        captured: dict = {}

        class _Executor:
            def call_tool(self, *, name: str, arguments: dict) -> dict:
                captured["name"] = name
                captured["arguments"] = arguments
                return {}

        proxy._local_data_plane = _Executor()  # type: ignore[assignment]
        proxy._call_local_data(
            name="resource.register_file", arguments={"project_id": "proj_evil"}
        )

        self.assertEqual(captured["name"], "resource.register_file")
        self.assertNotIn("project_id", captured["arguments"])


if __name__ == "__main__":
    unittest.main()
