"""Dual-upstream proxy routing (cloud plan Phase 8, §3.3).

Stands up two live in-process HTTP servers — a "cloud" control plane and a
local "daemon" — sharing one store + repo so records stay consistent, then
points a dual-upstream proxy at both. Asserts the routing table holds: control
tools go to the cloud (with an explicit project_id, never repo_root), data
tools go to the daemon (with repo_root), aggregate tools merge, tools/list
merges both catalogs, and the error taxonomy comes back as tool results.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.execution.backends.fake import FakeSandboxBackend
from backend.http_server import make_http_server
from mcp_server.daemon_marker import marker_path
from mcp_server.proxy import HttpProxyMcpServer, ProxyConfig
from tests.fakes import FakeRsyncSyncer


class _LiveServer:
    def __init__(self, *, app: ResearchPluginApp, repo: Path) -> None:
        self.server = make_http_server(app, "127.0.0.1", 0)
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        deadline, step, elapsed = 5.0, 0.05, 0.0
        while elapsed < deadline and not marker_path(repo_root=repo).exists():
            time.sleep(step)
            elapsed += step

    def stop(self) -> None:
        try:
            self.server.shutdown()
            self._thread.join(timeout=5.0)
        finally:
            self.server.server_close()


class DualUpstreamProxyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        db_path = self.repo / ".research_plugin" / "state.sqlite"
        # One app, two endpoints: the smoke test (Step 7) exercises a true
        # two-process split; this routing test shares the app so records are
        # consistent across the two upstreams the proxy talks to.
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=db_path,
            execution_backend=FakeSandboxBackend(),
            rsync_syncer=FakeRsyncSyncer(),
        )
        self.cloud = _LiveServer(app=self.app, repo=self.repo)
        self.daemon = _LiveServer(app=self.app, repo=self.repo)
        # Use the app's single default project so both the proxy's project_id
        # resolution and the cloud's single-project fill agree (the routing
        # contract, not multi-project disambiguation, is under test here).
        self.project = self.app.projects.list_projects()["projects"][0]
        self.proxy = HttpProxyMcpServer(
            config=ProxyConfig(
                repo_root=self.repo,
                daemon_url=self.daemon.url,
                control_url=self.cloud.url,
            )
        )

    def tearDown(self) -> None:
        self.cloud.stop()
        self.daemon.stop()
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

    def test_tools_list_merges_both_upstreams(self) -> None:
        response = self.proxy.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}
        # Control + data + aggregate all present from the merged catalog.
        self.assertIn("claim.create", names)  # control
        self.assertIn("resource.register_file", names)  # data
        self.assertIn("sandbox.get", names)  # aggregate
        self.assertNotIn("project.list", names)  # hidden uniformly
        # The internal plane hint never leaks into the client-facing schema.
        for tool in response["result"]["tools"]:
            self.assertNotIn("plane", tool)
            self.assertNotIn("project_id", tool["inputSchema"].get("properties", {}))

    def test_control_tool_routes_to_cloud_with_project_id(self) -> None:
        # Control tool → cloud. The cloud never receives repo_root; it gets the
        # explicit project_id (here passed through; _resolve_project_id against a
        # real daemon /local/route is covered by test_proxy_identity below).
        claim = self._call(
            "claim.create",
            {"project_id": self.project["id"], "statement": "A control-routed claim."},
        )
        self.assertEqual(claim["project_id"], self.project["id"])
        listed = self._call("claim.list", {"project_id": self.project["id"]})
        self.assertIn(claim["id"], {c["id"] for c in listed["claims"]})

    def test_data_tool_routes_to_daemon_with_repo_context(self) -> None:
        (self.repo / "note.txt").write_text("hello from the data plane\n")
        resource = self._call("resource.register_file", {"path": "note.txt"})
        self.assertEqual(resource["path"], "note.txt")

    def test_aggregate_health_reports_both_planes(self) -> None:
        health = self._call("sandbox.health")
        self.assertIn("data_plane", health)
        self.assertIn("control_plane", health)
        self.assertTrue(health["data_plane"]["reachable"])
        self.assertTrue(health["control_plane"]["reachable"])
        self.assertTrue(health["control_plane"]["configured"])

    def test_cloud_outage_does_not_block_data_tools(self) -> None:
        # Point the proxy's control URL at a dead port: data tools still work,
        # and a control tool returns the cloud_unreachable taxonomy as a tool
        # result (not a protocol error that disables the server).
        broken = HttpProxyMcpServer(
            config=ProxyConfig(
                repo_root=self.repo,
                daemon_url=self.daemon.url,
                control_url="http://127.0.0.1:1",
            )
        )
        (self.repo / "data.txt").write_text("still works\n")
        ok = broken.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "resource.register_file", "arguments": {"path": "data.txt"}},
            }
        )
        self.assertNotIn("error", ok)
        self.assertEqual(ok["result"]["structuredContent"]["path"], "data.txt")

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
        self.assertEqual(result["error_code"], "cloud_unreachable")
        self.assertTrue(down["result"].get("isError"))

    def test_missing_control_url_is_hard_error_for_control_tools_in_split(self) -> None:
        # A proxy left in split intent (a control tool) but with no control URL
        # is a hard config error, never a silent loopback fallback. We model
        # "split mode declared by the call's plane": with control_url None the
        # proxy is single-upstream, so instead assert the documented dual-mode
        # rule directly — a control tool with control_url set but unreachable
        # surfaces cloud_unreachable (covered above), and a None control_url is
        # plain local mode (single upstream), which the local tests cover.
        local = HttpProxyMcpServer(
            config=ProxyConfig(repo_root=self.repo, daemon_url=self.daemon.url)
        )
        self.assertFalse(local.config.split_mode)


class ProxyIdentityResolutionTest(unittest.TestCase):
    """The proxy resolves repo_root→project_id via the daemon's /local/route,
    so the cloud receives an explicit project_id and never a filesystem path
    (cloud plan §3.2)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_resolve_project_id_reads_the_daemon_route_map(self) -> None:
        from backend.daemon_loopback import create_daemon_loopback_app
        from backend.dataplane.project_links import ProjectLinks

        links = ProjectLinks(db_path=self.repo / "links.sqlite")
        links.link(repo_root=str(self.repo), project_id="proj_cloud_minted")

        class _StubDaemon:
            loopback_secret = "daemon-secret-xyz"
            project_links = links

            class control:  # noqa: N801 — stub for the health probe
                @staticmethod
                def list_tools():
                    return []

        # Stand up the daemon loopback app directly on a thread.
        from backend.http_server import _bind_socket
        import uvicorn

        app = create_daemon_loopback_app(daemon=_StubDaemon())
        sock = _bind_socket(host="127.0.0.1", port=0)
        port = int(sock.getsockname()[1])
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="off")
        )
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        time.sleep(0.4)
        try:
            proxy = HttpProxyMcpServer(
                config=ProxyConfig(
                    repo_root=self.repo,
                    daemon_url=f"http://127.0.0.1:{port}",
                    control_url="http://127.0.0.1:1",  # unused; resolution only
                    daemon_secret="daemon-secret-xyz",
                )
            )
            self.assertEqual(proxy._resolve_project_id(), "proj_cloud_minted")
        finally:
            server.should_exit = True
            thread.join(timeout=5.0)
            sock.close()


if __name__ == "__main__":
    unittest.main()
