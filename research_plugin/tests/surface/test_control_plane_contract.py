"""Behavioral contract suite for the control plane (cloud plan §7).

One scenario corpus — the full research loop, driven only through the tool
surface — executed through a CONTROL-PLANE CLIENT abstraction. Phase 3 wires
the single in-process client; Phase 8 adds ``HttpControlPlaneClient`` as a
second ``harness_factory`` and runs the same scenarios over the wire with
identical results (the plane-seam analog of test_sandbox_backend_contract.py).

The harness writes artifact files into a throwaway repo: in split mode that
repo is the daemon's checkout, so file writes stay a fixture concern, never a
client-API concern.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar, Protocol

from backend.app import ResearchPluginApp
from backend.control_client import HttpControlPlaneClient
from backend.execution.backends.fake import FakeSandboxBackend
from backend.http_server import make_http_server
from mcp_server.daemon_marker import marker_path
from tests.fakes import FakeRsyncSyncer

# Artifact bodies that satisfy the gate lints (plan spine, report spine +
# metrics table, graph envelope), so the loop exercises gates as passes.
VALID_PLAN = (
    "## Summary\n"
    "A toy experiment used by the contract suite.\n\n"
    "## Objective & hypothesis\n"
    "Test that the threshold rule beats the majority baseline.\n\n"
    "## Evaluation\n"
    "Metric: accuracy vs the majority-class baseline; success if accuracy > 0.6.\n"
)

VALID_REPORT = (
    "## Summary\n"
    "Ran the toy experiment per the approved plan.\n\n"
    "## Results\n\n"
    "| Metric | Target | Achieved |\n"
    "|--------|--------|----------|\n"
    "| accuracy | 0.60 | 0.72 |\n\n"
    "## Deviations from plan\n"
    "None.\n\n"
    "## Conclusion\n"
    "Decision rule met: accuracy 0.72 > 0.6 threshold.\n"
)

VALID_GRAPH = (
    '{"version": 1, "nodes": ['
    '{"id": "obj", "kind": "objective", "label": "Beat the majority baseline"},'
    '{"id": "out", "kind": "outcome", "label": "Threshold met at 0.72"}],'
    ' "edges": [{"from": "obj", "to": "out", "label": "confirmed by"}]}\n'
)


class ControlPlaneClient(Protocol):
    """One tool call against the control plane, however it is wired."""

    def call(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...


class InProcessControlPlaneClient:
    """Local-mode wiring: tool calls go straight to ResearchPluginApp."""

    def __init__(self, *, app: ResearchPluginApp) -> None:
        self._app = app

    def call(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._app.call_tool(name, dict(arguments or {}))


@dataclass
class ClientHarness:
    """A client plus the repo its artifact files live in."""

    client: ControlPlaneClient
    repo: Path
    _closers: list[Callable[[], None]] = field(default_factory=list)

    def close(self) -> None:
        for closer in self._closers:
            closer()


def in_process_harness() -> ClientHarness:
    tmp = tempfile.TemporaryDirectory()
    repo = Path(tmp.name)
    app = ResearchPluginApp(
        repo_root=repo,
        db_path=repo / ".research_plugin" / "state.sqlite",
        execution_backend=FakeSandboxBackend(),
        rsync_syncer=FakeRsyncSyncer(),
    )
    return ClientHarness(
        client=InProcessControlPlaneClient(app=app),
        repo=repo,
        _closers=[app.shutdown, tmp.cleanup],
    )


def http_harness() -> ClientHarness:
    """Split-mode wiring: a real in-process HTTP server fronts the app, and the
    HttpControlPlaneClient drives the SAME scenarios over the wire (plan Phase
    8). The server's app uses the throwaway repo as its checkout, so the
    scenarios' artifact file writes land where the control plane reads them —
    exactly as the daemon's checkout would in a true split deployment.

    This proves the plane seam: identical results to the in-process client,
    confirming the contract holds across a process/network boundary before any
    topology change.
    """
    tmp = tempfile.TemporaryDirectory()
    repo = Path(tmp.name)
    app = ResearchPluginApp(
        repo_root=repo,
        db_path=repo / ".research_plugin" / "state.sqlite",
        execution_backend=FakeSandboxBackend(),
        rsync_syncer=FakeRsyncSyncer(),
    )
    server = make_http_server(app, "127.0.0.1", 0)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline, step, elapsed = 5.0, 0.05, 0.0
    while elapsed < deadline and not marker_path(repo_root=repo).exists():
        time.sleep(step)
        elapsed += step
    client = HttpControlPlaneClient(base_url=f"http://{host}:{port}")

    def _stop() -> None:
        try:
            server.shutdown()
            thread.join(timeout=5.0)
        finally:
            server.server_close()

    return ClientHarness(
        client=client,
        repo=repo,
        _closers=[_stop, app.shutdown, tmp.cleanup],
    )


class ControlPlaneContractScenarios:
    """Scenario corpus shared by every client wiring.

    Subclasses provide ``harness_factory``; the scenarios never reach past the
    client (no app internals, no raw SQL), so they can run over the wire
    unchanged when the HTTP client lands in Phase 8.
    """

    harness_factory: ClassVar[Callable[[], ClientHarness]]

    def setUp(self) -> None:
        self.harness = type(self).harness_factory()
        self.repo = self.harness.repo
        self.project_id = self.call("project.create", name="Contract Project")["id"]

    def tearDown(self) -> None:
        self.harness.close()

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        return self.harness.client.call(tool, arguments)

    # ---- scenario helpers (tool-surface only) ----

    def _associate_file(
        self, *, exp_id: str, path: str, role: str, body: str
    ) -> dict[str, Any]:
        full = self.repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(body)
        resource = self.call(
            "resource.register_file", project_id=self.project_id, path=path, kind=role
        )
        return self.call(
            "resource.associate",
            project_id=self.project_id,
            resource_id=resource["id"],
            target_type="experiment",
            target_id=exp_id,
            role=role,
        )

    def _pass_review(self, *, exp_id: str, role: str) -> None:
        request = self.call(
            "review.request",
            project_id=self.project_id,
            target_type="experiment",
            target_id=exp_id,
            role=role,
        )
        session = self.call(
            "review.start",
            review_request_id=request["review_request_id"],
            reviewer_capability=request["reviewer_capability"],
            caller_session_id=f"{role}-contract",
        )
        self.call(
            "review.submit",
            review_session_id=session["review_session_id"],
            verdict="pass",
        )

    def _transition(self, *, exp_id: str, transition: str, **extra: Any) -> dict[str, Any]:
        return self.call(
            "experiment.transition",
            project_id=self.project_id,
            experiment_id=exp_id,
            transition=transition,
            **extra,
        )

    def _status(self, *, exp_id: str) -> dict[str, Any]:
        return self.call(
            "experiment.get_state", project_id=self.project_id, experiment_id=exp_id
        )

    # ---- the scenario corpus ----

    def test_full_experiment_loop_to_complete(self) -> None:
        claim = self.call(
            "claim.create",
            project_id=self.project_id,
            statement="A threshold rule beats the majority baseline.",
        )
        exp = self.call(
            "experiment.create",
            project_id=self.project_id,
            name="contract-loop",
            intent="Drive the full loop through the client.",
            tested_claim_ids=[claim["id"]],
        )
        exp_id = exp["id"]
        self.assertEqual(exp["status"], "planned")
        self.assertEqual(exp["folder"], "experiments/contract-loop/")

        # Design gate: plan bytes submitted at associate, then review.
        self._associate_file(
            exp_id=exp_id,
            path="experiments/contract-loop/plan.md",
            role="plan",
            body=VALID_PLAN,
        )
        self._transition(exp_id=exp_id, transition="submit_design")
        self.assertEqual(self._status(exp_id=exp_id)["status"], "design_review")
        self._pass_review(exp_id=exp_id, role="design_reviewer")
        self._transition(exp_id=exp_id, transition="mark_ready_to_run")
        self._transition(exp_id=exp_id, transition="start_running")
        self.assertEqual(self._status(exp_id=exp_id)["status"], "running")

        # Results gate: result metadata + report/graph bytes, then review.
        self._associate_file(
            exp_id=exp_id,
            path="experiments/contract-loop/results.json",
            role="result",
            body='{"accuracy": 0.72}\n',
        )
        self._associate_file(
            exp_id=exp_id,
            path="experiments/contract-loop/report.md",
            role="report",
            body=VALID_REPORT,
        )
        self._associate_file(
            exp_id=exp_id,
            path="experiments/contract-loop/graph.json",
            role="graph",
            body=VALID_GRAPH,
        )
        self._transition(exp_id=exp_id, transition="submit_results")
        self.assertEqual(self._status(exp_id=exp_id)["status"], "experiment_review")
        self._pass_review(exp_id=exp_id, role="experiment_reviewer")

        conclusion = "Accuracy 0.72 beat the 0.6 threshold; claim supported."
        self._transition(
            exp_id=exp_id,
            transition="complete",
            evidence={"conclusion": conclusion},
        )
        state = self._status(exp_id=exp_id)
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["attempt_index"], 1)
        self.assertEqual(state["conclusion"], conclusion)
        self.assertEqual(
            [c["id"] for c in state.get("tested_claims", [])], [claim["id"]]
        )

    def test_status_and_next_tracks_the_gates(self) -> None:
        exp_id = self.call(
            "experiment.create",
            project_id=self.project_id,
            name="contract-gates",
            intent="Gate guidance through the client.",
        )["id"]
        before = self.call(
            "workflow.status_and_next",
            project_id=self.project_id,
            experiment_id=exp_id,
        )
        self.assertEqual(before["workflow"]["current_gate"], "plan_required")
        self._associate_file(
            exp_id=exp_id,
            path="experiments/contract-gates/plan.md",
            role="plan",
            body=VALID_PLAN,
        )
        ready = self.call(
            "workflow.status_and_next",
            project_id=self.project_id,
            experiment_id=exp_id,
        )
        self.assertEqual(ready["workflow"]["current_gate"], "design_review_required")

    def test_review_verdicts_are_recorded_per_attempt(self) -> None:
        exp_id = self.call(
            "experiment.create",
            project_id=self.project_id,
            name="contract-review",
            intent="Review records through the client.",
        )["id"]
        self._associate_file(
            exp_id=exp_id,
            path="experiments/contract-review/plan.md",
            role="plan",
            body=VALID_PLAN,
        )
        self._transition(exp_id=exp_id, transition="submit_design")
        self._pass_review(exp_id=exp_id, role="design_reviewer")
        status = self.call(
            "review.status",
            project_id=self.project_id,
            target_type="experiment",
            target_id=exp_id,
        )
        reviews = status.get("reviews", [])
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["role"], "design_reviewer")
        self.assertEqual(reviews[0]["verdict"], "pass")


class InProcessControlPlaneContractTest(
    ControlPlaneContractScenarios, unittest.TestCase
):
    """The Phase 3 wiring: scenarios against the in-process client."""

    harness_factory = staticmethod(in_process_harness)


class HttpControlPlaneContractTest(
    ControlPlaneContractScenarios, unittest.TestCase
):
    """The Phase 8 wiring: the SAME scenarios over a real HTTP boundary.

    Identical assertions to the in-process subclass — the seam proof. If this
    diverges from InProcessControlPlaneContractTest, the wire contract drifted.
    """

    harness_factory = staticmethod(http_harness)


if __name__ == "__main__":
    unittest.main()
