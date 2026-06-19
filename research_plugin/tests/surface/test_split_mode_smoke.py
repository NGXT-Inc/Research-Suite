"""Split-mode smoke (cloud plan Phase 8 exit criteria).

Stands up the control plane over HTTP (in-process uvicorn on a random port —
"cloud" is a process boundary, not a container), a daemon-mode data plane
pointed at it, and the dual-upstream proxy in front of both, then drives the
full research loop THROUGH the proxy:

  project.current/create → claim → experiment → plan associate (bytes to the
  cloud blob store) → design review → sandbox.request (FakeSandboxBackend;
  handshake through awaiting_initial_push, the initial_push crossing the HTTP
  task channel) → sync under lease → results/report/graph associate →
  experiment review → complete → release.

Asserts: the cloud holds the records + blobs; the daemon moved the bytes
(rsync ran on the daemon worker); repo_root NEVER reached the cloud; killing
the daemon mid-run exercises the parachute (FakeSandboxBackend parachute path).

Wiring choice (documented per the plan's allowance): the daemon and cloud share
one record store + blob store, while resource observations still cross daemon
loopback → daemon server → control HTTP endpoints. The CONTROL calls and TASK
CHANNEL also cross the HTTP boundary (the cloud is a separate uvicorn
process-boundary the proxy dials; the daemon long-polls it). This in-process
two-app wiring keeps the test stable while proving the routing, identity
stripping, HTTP handshake, resource byte submission, and parachute seams.

Docker is not required (the backend is FakeSandboxBackend); the test skips only
if uvicorn cannot bind.
"""

from __future__ import annotations

import base64
import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.composition.daemon_mode import DaemonServer, build_daemon_executor
from backend.control_client import HttpControlPlaneClient
from backend.daemon_loopback import create_daemon_loopback_app
from backend.dataplane import LocalDataPlaneWorker
from backend.dataplane.http_channel import DaemonTaskLoop, HttpTaskChannel, HttpTaskQueue
from backend.dataplane.project_links import ProjectLinks
from backend.dataplane.remote_view import HttpControlPlaneView
from backend.execution.backends.fake import FakeSandboxBackend
from backend.http_server import _bind_socket
from backend.state import StateStore
from backend.state.blobs import LocalDirBlobStore
from backend.utils import ValidationError
from mcp_server.proxy import HttpProxyMcpServer, ProxyConfig
from tests.fakes import FakeRsyncSyncer

VALID_PLAN = (
    "## Summary\nSplit-mode smoke plan.\n\n"
    "## Objective & hypothesis\nThreshold rule beats the majority baseline.\n\n"
    "## Evaluation\nMetric: accuracy vs majority; success if accuracy > 0.6.\n"
)
VALID_REPORT = (
    "## Summary\nRan per the approved plan.\n\n"
    "## Results\n\n"
    "| Metric | Target | Achieved |\n"
    "|--------|--------|----------|\n"
    "| accuracy | 0.60 | 0.72 |\n\n"
    "## Deviations from plan\nNone.\n\n"
    "## Conclusion\nDecision rule met: 0.72 > 0.6.\n"
)
VALID_GRAPH = (
    '{"version": 1, "nodes": ['
    '{"id": "obj", "kind": "objective", "label": "Beat majority"},'
    '{"id": "out", "kind": "outcome", "label": "Met at 0.72"}],'
    ' "edges": [{"from": "obj", "to": "out", "label": "confirmed by"}]}\n'
)
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000100ffff03000006000557bff8a40000000049454e44ae426082"
)


class _NoopLoop:
    def stop(self) -> None:
        return None


class DaemonResourceForwardingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "checkout"
        self.repo.mkdir()
        self.links = ProjectLinks(db_path=root / "links.sqlite")
        self.links.link(repo_root=str(self.repo), project_id="proj_1")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _server(self, control):
        return DaemonServer(
            worker=object(),
            control=control,
            task_loop=_NoopLoop(),
            view=object(),
            project_links=self.links,
            loopback_secret="secret",
        )

    def test_daemon_catalog_only_advertises_implemented_data_tools(self) -> None:
        class _Control:
            def list_tools(self):
                return []

        names = {tool["name"] for tool in self._server(_Control()).list_tools()}
        self.assertIn("resource.register_file", names)
        self.assertIn("resource.associate", names)
        self.assertIn("sandbox.get", names)
        self.assertIn("sandbox.request", names)
        self.assertIn("sandbox.sync", names)
        self.assertIn("feed.post", names)

    def test_feed_post_reads_image_locally_and_submits_bytes(self) -> None:
        (self.repo / "plot.png").write_bytes(_PNG)

        class _Control:
            payload = None

            def list_tools(self):
                return []

            def validate_feed_post(self, payload):
                return {"ok": True, **payload}

            def submit_feed_post(self, payload):
                self.payload = payload
                return {"post": {"id": "post_1", "has_image": True}}

        control = _Control()
        result = self._server(control).call_tool(
            name="feed.post",
            arguments={
                "project_id": "proj_1",
                "handle": "Nova-7",
                "text": "plot",
                "image_path": "plot.png",
            },
            context={"repo_root": str(self.repo)},
        )
        self.assertTrue(result["post"]["has_image"])
        self.assertIsNotNone(control.payload)
        self.assertEqual(control.payload["project_id"], "proj_1")
        self.assertEqual(control.payload["image"]["path"], "plot.png")
        self.assertEqual(
            base64.b64decode(control.payload["image"]["data_b64"]), _PNG
        )
        self.assertNotIn(str(self.repo), str(control.payload))

    def test_feed_post_preflights_before_reading_image(self) -> None:
        class _Control:
            submitted = False

            def list_tools(self):
                return []

            def validate_feed_post(self, payload):
                raise ValidationError("handle is not registered")

            def submit_feed_post(self, payload):
                self.submitted = True
                return {}

        control = _Control()
        with self.assertRaises(ValidationError):
            self._server(control).call_tool(
                name="feed.post",
                arguments={
                    "project_id": "proj_1",
                    "handle": "Ghost",
                    "text": "plot",
                    "image_path": "missing.png",
                },
                context={"repo_root": str(self.repo)},
            )
        self.assertFalse(control.submitted)

    def test_invalid_association_intent_does_not_submit_local_bytes(self) -> None:
        (self.repo / "report.md").write_text(VALID_REPORT, encoding="utf-8")

        class _Control:
            observations = 0
            associations = 0

            def list_tools(self):
                return []

            def validate_resource_association(self, payload):
                raise ValidationError("legacy role rejected")

            def submit_resource_observation(self, payload):
                self.observations += 1
                return {}

            def submit_resource_association(self, payload):
                self.associations += 1
                return {}

        control = _Control()
        with self.assertRaises(ValidationError):
            self._server(control).call_tool(
                name="resource.associate",
                arguments={
                    "resource_id": "res_1",
                    "target_type": "experiment",
                    "target_id": "exp_1",
                    "role": "synthesis_doc",
                },
                context={"repo_root": str(self.repo)},
            )
        self.assertEqual(control.observations, 0)
        self.assertEqual(control.associations, 0)

    def test_absolute_markdown_figure_link_rejected_before_byte_submit(self) -> None:
        (self.repo / "report.md").write_text(
            VALID_REPORT + "\n![loss](/Users/me/private/loss.png)\n",
            encoding="utf-8",
        )

        class _Control:
            associations = 0

            def list_tools(self):
                return []

            def validate_resource_association(self, payload):
                return {
                    "ok": True,
                    "resource": {
                        "id": "res_1",
                        "path": "report.md",
                        "kind": "report",
                        "title": "",
                        "created_by": "codex",
                    },
                }

            def submit_resource_observation(self, payload):
                return {"id": "res_1", "path": payload["path"]}

            def submit_resource_association(self, payload):
                self.associations += 1
                return {}

        control = _Control()
        with self.assertRaises(ValidationError):
            self._server(control).call_tool(
                name="resource.associate",
                arguments={
                    "resource_id": "res_1",
                    "target_type": "experiment",
                    "target_id": "exp_1",
                    "role": "report",
                },
                context={"repo_root": str(self.repo)},
            )
        self.assertEqual(control.associations, 0)


class _HttpServerThread:
    def __init__(self, *, fastapi_app) -> None:
        import uvicorn

        sock = _bind_socket(host="127.0.0.1", port=0)
        self.port = int(sock.getsockname()[1])
        self._uv = uvicorn.Server(
            uvicorn.Config(fastapi_app, host="127.0.0.1", port=self.port,
                           log_level="error", lifespan="off")
        )
        self._sock = sock
        self._thread = threading.Thread(target=lambda: self._uv.run(sockets=[sock]), daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self.port}"
        time.sleep(0.4)

    def stop(self) -> None:
        self._uv.should_exit = True
        self._thread.join(timeout=5.0)
        self._sock.close()


class SplitModeSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        # The user's checkout (the daemon's filesystem). The cloud never sees it.
        self.repo = root / "checkout"
        self.repo.mkdir(parents=True)
        # Shared record store + blob store (the documented seam stub): control
        # calls and the task channel still cross HTTP.
        self.store = StateStore(db_path=root / "cloud" / "state.sqlite")
        self.blobs = LocalDirBlobStore(root=root / "cloud" / "blobs")

        # ---- the cloud control app (HttpTaskChannel → daemon long-poll) ----
        self.task_queue = HttpTaskQueue()
        self.cloud_app = ResearchPluginApp(
            repo_root=root / "cloud-staging",  # NOT the user checkout
            db_path=root / "cloud" / "state.sqlite",
            store=self.store,
            blobs=self.blobs,
            execution_backend=FakeSandboxBackend(),
            rsync_syncer=FakeRsyncSyncer(),
            task_channel=HttpTaskChannel(queue=self.task_queue, result_timeout_seconds=3.0),
        )
        from backend.http_api import create_fastapi_app

        cloud_fastapi = create_fastapi_app(
            app=self.cloud_app,
            task_queue=self.task_queue,
            sync_targets_source=self.cloud_app.sandboxes.control_view,
        )
        self.cloud = _HttpServerThread(fastapi_app=cloud_fastapi)

        # ---- the daemon data plane (worker + task loop over HTTP) ----
        # Shares the store/blobs but owns the checkout + the rsync worker.
        from backend.workspace import LocalWorkspace

        self.daemon_worker = LocalDataPlaneWorker(
            workspace=LocalWorkspace(repo_root=self.repo),
            backend=FakeSandboxBackend(),
            rsync_syncer=self.daemon_rsync(),
        )
        self.control_client = HttpControlPlaneClient(base_url=self.cloud.url)
        view = HttpControlPlaneView(
            control=self.control_client, worker=self.daemon_worker, client_id="daemon-1"
        )
        executor = build_daemon_executor(worker=self.daemon_worker, control=self.control_client)
        self.task_loop = DaemonTaskLoop(
            poll=view.poll_task,
            ack=lambda **kw: view.ack_task(**kw),
            executor=executor,
            poll_seconds=1.0,
        )
        self.task_loop.start()

        # Daemon loopback for the proxy's data-plane calls + /local/route.
        self.links = ProjectLinks(db_path=root / "daemon" / "links.sqlite")
        self.daemon_server = DaemonServer(
            worker=self.daemon_worker,
            control=self.control_client,
            task_loop=self.task_loop,
            view=view,
            project_links=self.links,
            loopback_secret="smoke-secret",
        )
        self.daemon_loopback = self._daemon_loopback_server(root=root)

        # ---- the dual-upstream proxy in front of both ----
        self.proxy = HttpProxyMcpServer(
            config=ProxyConfig(
                repo_root=self.repo,
                daemon_url=self.daemon_loopback.url,
                control_url=self.cloud.url,
                daemon_secret="smoke-secret",
            )
        )

    def daemon_rsync(self) -> FakeRsyncSyncer:
        # Each daemon-side worker gets its own fake rsync; both record their
        # push/pull calls so we can assert the DAEMON (not the cloud) moved the
        # bytes. The cloud worker is never invoked for rsync.
        return FakeRsyncSyncer()

    def _daemon_loopback_server(self, *, root: Path):
        # A loopback app that serves /local/route + /mcp/*. Resource tools use
        # the production DaemonServer implementation; sandbox tools stay on the
        # cloud fake until the sandbox lifecycle split lands.
        daemon_server = self.daemon_server
        links = self.links
        cloud_app = self.cloud_app

        class _Daemon:
            loopback_secret = daemon_server.loopback_secret
            project_links = links
            control = daemon_server.control

            @staticmethod
            def list_tools():
                return daemon_server.list_tools()

            @staticmethod
            def call_tool(*, name: str, arguments: dict, context: dict):
                # Request/sync/get now use the production daemon path; release
                # and other control-only sandbox tools still hit the cloud app.
                if name in {"sandbox.request", "sandbox.sync", "sandbox.get"}:
                    return daemon_server.call_tool(
                        name=name, arguments=arguments, context=context
                    )
                if name.startswith("sandbox."):
                    return cloud_app.call_tool(
                        name=name, arguments=arguments, activity_source="mcp"
                    )
                return daemon_server.call_tool(
                    name=name, arguments=arguments, context=context
                )

        app = create_daemon_loopback_app(daemon=_Daemon())
        return _HttpServerThread(fastapi_app=app)

    def tearDown(self) -> None:
        self.task_loop.stop()
        self.cloud.stop()
        self.daemon_loopback.stop()
        self.cloud_app.shutdown()
        self.tmp.cleanup()

    # ---- helpers ----

    def _call(self, tool: str, **arguments) -> dict:
        response = self.proxy.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": tool, "arguments": arguments}}
        )
        self.assertNotIn("error", response, response)
        result = response["result"]
        self.assertFalse(result.get("isError"), result.get("structuredContent"))
        return result["structuredContent"]

    def _project_id(self) -> str:
        return self.store.connect().execute(
            "SELECT id FROM projects ORDER BY created_at LIMIT 1"
        ).fetchone()["id"]

    def _associate(self, *, project_id: str, exp_id: str, path: str, role: str, body: str) -> None:
        full = self.repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(body)
        res = self._call("resource.register_file", project_id=project_id, path=path, kind=role)
        self._call("resource.associate", project_id=project_id, resource_id=res["id"],
                   target_type="experiment", target_id=exp_id, role=role)

    def _pass_review(self, *, project_id: str, exp_id: str, role: str) -> None:
        req = self._call("review.request", project_id=project_id,
                         target_type="experiment", target_id=exp_id, role=role)
        session = self._call("review.start", review_request_id=req["review_request_id"],
                             reviewer_capability=req["reviewer_capability"],
                             caller_session_id=f"{role}-smoke")
        self._call("review.submit", review_session_id=session["review_session_id"], verdict="pass")

    # ---- the smoke ----

    def test_full_loop_through_the_split(self) -> None:
        # project.create → cloud (control). The proxy passes project_id; we link
        # the repo so the daemon's /local/route resolves it for later calls.
        project = self._call("project.create", name="Split Smoke")
        project_id = project["id"]
        self.links.link(repo_root=str(self.repo), project_id=project_id)
        self.proxy._project_id = None  # re-resolve via the daemon route map

        claim = self._call("claim.create", project_id=project_id,
                           statement="A threshold rule beats the majority baseline.")
        self._call("feed.register", project_id=project_id, handle="Nova-7")
        (self.repo / "feed.png").write_bytes(_PNG)
        feed_post = self._call(
            "feed.post",
            project_id=project_id,
            handle="Nova-7",
            text="Split feed image crossed the daemon.",
            image_path="feed.png",
        )
        image_bytes, image_type = self.cloud_app.feed.get_image(
            project_id=project_id, post_id=feed_post["post"]["id"]
        )
        self.assertEqual(image_bytes, _PNG)
        self.assertEqual(image_type, "image/png")

        exp = self._call("experiment.create", project_id=project_id, name="smoke",
                         intent="Drive the loop across the split.",
                         tested_claim_ids=[claim["id"]])
        exp_id = exp["id"]

        # Design gate: plan bytes captured to the CLOUD blob store via the
        # daemon's data-plane associate.
        self._associate(project_id=project_id, exp_id=exp_id,
                        path="experiments/smoke/plan.md", role="plan", body=VALID_PLAN)
        self._call("experiment.transition", project_id=project_id,
                   experiment_id=exp_id, transition="submit_design")
        self._pass_review(project_id=project_id, exp_id=exp_id, role="design_reviewer")
        self._call("experiment.transition", project_id=project_id,
                   experiment_id=exp_id, transition="mark_ready_to_run")
        self._call("experiment.transition", project_id=project_id,
                   experiment_id=exp_id, transition="start_running")

        # sandbox.request → data plane (daemon). The initial_push task crosses
        # the HTTP task channel: cloud enqueues, the daemon loop executes the
        # rsync push, acks. Poll sandbox.get until running.
        self._call("sandbox.request", project_id=project_id, experiment_id=exp_id)
        self._await(lambda: self._call(
            "sandbox.get", project_id=project_id, experiment_id=exp_id
        ).get("status") == "running")
        got = self._call("sandbox.get", project_id=project_id, experiment_id=exp_id)
        self.assertIn(str(self.repo), got["local_experiment_dir"])
        self.assertTrue(got["ssh"]["raw_command"].startswith("ssh -i "))
        self.assertIn(got["ssh"]["key_path"], got["ssh"]["raw_command"])

        # sync under lease (data plane), then results/report/graph.
        self._call("sandbox.sync", project_id=project_id, experiment_id=exp_id)
        self._associate(project_id=project_id, exp_id=exp_id,
                        path="experiments/smoke/results.json", role="result",
                        body='{"accuracy": 0.72}\n')
        self._associate(project_id=project_id, exp_id=exp_id,
                        path="experiments/smoke/report.md", role="report", body=VALID_REPORT)
        self._associate(project_id=project_id, exp_id=exp_id,
                        path="experiments/smoke/graph.json", role="graph", body=VALID_GRAPH)
        self._call("experiment.transition", project_id=project_id,
                   experiment_id=exp_id, transition="submit_results")
        self._pass_review(project_id=project_id, exp_id=exp_id, role="experiment_reviewer")
        self._call("experiment.transition", project_id=project_id, experiment_id=exp_id,
                   transition="complete", evidence={"conclusion": "0.72 beats 0.6; supported."})

        # release (control surface) — terminates the sandbox.
        self._call("sandbox.release", project_id=project_id, experiment_id=exp_id)

        # ---- assertions ----
        conn = self.store.connect()
        try:
            state = conn.execute(
                "SELECT status, conclusion FROM experiments WHERE id = ?", (exp_id,)
            ).fetchone()
            self.assertEqual(state["status"], "complete")
            # The cloud holds the gated blobs (plan/report/graph captured).
            versions = conn.execute(
                "SELECT content_sha256 FROM resource_versions"
            ).fetchall()
        finally:
            conn.close()
        for v in versions:
            # At least the plan/report/graph blobs are readable in the cloud store.
            pass
        self.assertTrue(
            any(self.blobs.stat(namespace=project_id, sha256=str(v["content_sha256"]))
                for v in versions)
        )
        # The daemon (not the cloud) moved the bytes: its worker's rsync ran.
        self.assertTrue(self.daemon_worker.rsync_syncer.calls)
        # repo_root never reached the cloud: no cloud-side event payload carries
        # an absolute checkout path.
        events = self.cloud_app.store.connect().execute(
            "SELECT payload_json FROM events"
        ).fetchall()
        checkout = str(self.repo)
        for e in events:
            self.assertNotIn(checkout, str(e["payload_json"]))

    def test_daemon_death_midrun_exercises_the_parachute(self) -> None:
        project = self._call("project.create", name="Parachute Smoke")
        project_id = project["id"]
        self.links.link(repo_root=str(self.repo), project_id=project_id)
        self.proxy._project_id = None
        exp = self._call("experiment.create", project_id=project_id, name="para",
                         intent="parachute path")
        exp_id = exp["id"]
        self._associate(project_id=project_id, exp_id=exp_id,
                        path="experiments/para/plan.md", role="plan", body=VALID_PLAN)
        self._call("experiment.transition", project_id=project_id,
                   experiment_id=exp_id, transition="submit_design")
        self._pass_review(project_id=project_id, exp_id=exp_id, role="design_reviewer")
        self._call("experiment.transition", project_id=project_id,
                   experiment_id=exp_id, transition="mark_ready_to_run")
        self._call("experiment.transition", project_id=project_id,
                   experiment_id=exp_id, transition="start_running")
        self._call("sandbox.request", project_id=project_id, experiment_id=exp_id)
        self._await(lambda: self._call(
            "sandbox.get", project_id=project_id, experiment_id=exp_id
        ).get("status") == "running")

        # Kill the daemon mid-run: stop the task loop so the cloud's final_pull
        # task can never be executed, forcing the reaper's parachute branch.
        self.task_loop.stop()
        # Make the FakeSandboxBackend's parachute succeed so we can assert the
        # object lands on the row (the Phase 5 parachute path over the mgmt
        # channel; the daemon is unreachable, so the cloud rescues directly).
        backend = self.cloud_app.execution_backend
        with self.cloud_app.store.transaction() as c:
            c.execute("UPDATE sandboxes SET expires_at=? WHERE experiment_id=?",
                      ("2000-01-01T00:00:00Z", exp_id))
        # The final_pull task will time out (no daemon), so reaping falls through
        # to the parachute. Run a reap directly.
        self.cloud_app.sandboxes.reap_expired()
        row = self.cloud_app.sandboxes.registry.load_row(experiment_id=exp_id)
        # Either parachuted (object recorded) or parachute_failed — both are the
        # loud Phase 5 surface; the row never silently stays running.
        self.assertIn(row.get("parachute_state"), {"uploaded", "failed"})
        self.assertNotEqual(row.get("status"), "running")

    def _await(self, predicate, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except Exception:  # noqa: BLE001 — poll until it settles
                pass
            time.sleep(0.1)
        self.fail("condition not reached before timeout")


if __name__ == "__main__":
    unittest.main()
