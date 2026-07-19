from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from merv.brain import __version__ as BACKEND_VERSION
from merv.proxy import __version__ as MCP_VERSION
from merv.shared.project_dirs import PROJECT_STATE_DIR_NAMES
from tests.paths import PLUGIN_ROOT


class LocalShippingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source_plugin = PLUGIN_ROOT
        self.install_dir = self.root / "installed" / "merv"
        self.research_repo = self.root / "research-repo"
        self.research_repo.mkdir(parents=True)
        self._copy_install()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _copy_install(self) -> None:
        self.install_dir.mkdir(parents=True)
        shutil.copytree(
            self.source_plugin / "src",
            self.install_dir / "src",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        shutil.copytree(self.source_plugin / "bin", self.install_dir / "bin")
        shutil.copytree(self.source_plugin / "skills", self.install_dir / "skills")
        shutil.copy2(self.source_plugin / ".mcp.codex.json", self.install_dir / ".mcp.codex.json")
        shutil.copytree(self.source_plugin / ".codex-plugin", self.install_dir / ".codex-plugin")

    def _clean_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for name in (
            "MERV_REPO_ROOT",
            "RESEARCH_PLUGIN_REPO_ROOT",
            "MERV_CONTROL_URL",
            "RESEARCH_PLUGIN_CONTROL_URL",
            "MERV_CLIENT_CONFIG",
            "RESEARCH_PLUGIN_DAEMON_SECRET_FILE",
        ):
            env.pop(name, None)
        # Deliberately the LEGACY spellings: this suite doubles as the
        # end-to-end proof that RESEARCH_PLUGIN_* input still works.
        env["RESEARCH_PLUGIN_CLIENT_CONFIG"] = str(self.root / "isolated-client.json")
        env["RESEARCH_PLUGIN_PYTHON"] = sys.executable
        return env

    def test_mcp_launcher_uses_current_repo_for_state_and_resources(self) -> None:
        # New architecture: the brain is repo-agnostic. The MCP proxy stays
        # project-local and forwards hidden repo context.
        daemon = self._start_http_daemon()
        self.addCleanup(self._stop_process, daemon)
        brain_url = self._wait_for_daemon_ready(daemon)

        proc = self._start_mcp_from_config({"RESEARCH_PLUGIN_CONTROL_URL": brain_url})
        self.addCleanup(self._stop_process, proc)

        self._rpc(proc, "initialize")
        tools = self._rpc(proc, "tools/list")["result"]["tools"]
        status_schema = next(tool for tool in tools if tool["name"] == "workflow.status_and_next")["inputSchema"]
        self.assertNotIn("project_id", status_schema.get("required", []))

        # Onboard the way a real session does: the project tool with
        # action=connect creates the hosted project AND writes the proxy-local
        # folder link in one call.
        connected = self._tool(
            proc,
            "project",
            action="connect",
            name="Shipping Smoke",
            summary="Run from arbitrary repo.",
        )
        self.assertTrue(connected["linked"])
        self.assertTrue(connected["created"])
        project = connected["project"]
        claim = self._tool(
            proc,
            "claim.create",
            statement="A tiny threshold experiment can be tracked from a separate repo.",
        )
        exp = self._tool(
            proc,
            "experiment.create",
            name="shipping",
            intent="Record plan and result resources through the installed MCP launcher.",
            tested_claim_ids=[claim["id"]],
        )
        exp_id = exp["id"]

        (self.research_repo / "experiments" / "shipping").mkdir(parents=True, exist_ok=True)
        (self.research_repo / "experiments" / "shipping" / "plan.md").write_text(
            "## Summary\nShip a plan and result through the installed launcher.\n\n"
            "## Objective & hypothesis\nThreshold rule beats the majority class.\n\n"
            "## Evaluation\nMetric: accuracy. Baseline: majority class. Success if higher.\n"
        )
        self._tool(
            proc,
            "resource.register",
            path="experiments/shipping/plan.md",
            kind="note",
            title="Shipping plan",
            target_type="experiment",
            target_id=exp_id,
            role="plan",
        )
        self._tool(proc, "experiment.transition", experiment_id=exp_id, transition="submit_design")
        self._submit_review(proc, exp_id, "design_reviewer", "pass", "Plan is scoped.")
        self._tool(
            proc,
            "experiment.transition",
            experiment_id=exp_id,
            transition="mark_ready_to_run",
        )
        self._tool(proc, "experiment.transition", experiment_id=exp_id, transition="start_running")

        (self.research_repo / "experiments" / "shipping" / "results.json").write_text('{"accuracy": 1.0}\n')
        self._tool(
            proc,
            "resource.register",
            path="experiments/shipping/results.json",
            kind="result",
            target_type="experiment",
            target_id=exp_id,
            role="result",
        )
        (self.research_repo / "experiments" / "shipping" / "report.md").write_text(
            "## Summary\nShipping smoke run completed per plan.\n\n"
            "## Results\n\n| Metric | Target | Achieved |\n|---|---|---|\n| accuracy | majority | 1.0 |\n\n"
            "## Deviations from plan\nNone.\n\n"
            "## Conclusion\nDecision rule met: accuracy beats the majority baseline.\n"
        )
        self._tool(
            proc,
            "resource.register",
            path="experiments/shipping/report.md",
            kind="report",
            target_type="experiment",
            target_id=exp_id,
            role="report",
        )
        (self.research_repo / "experiments" / "shipping" / "graph.json").write_text(
            '{"version": 1, "nodes": ['
            '{"id": "obj", "kind": "objective", "label": "Shipping smoke run"},'
            '{"id": "out", "kind": "outcome", "label": "Beat the majority baseline"}],'
            ' "edges": [{"from": "obj", "to": "out"}]}\n'
        )
        self._tool(
            proc,
            "resource.register",
            path="experiments/shipping/graph.json",
            kind="other",
            target_type="experiment",
            target_id=exp_id,
            role="graph",
        )
        self._tool(proc, "experiment.transition", experiment_id=exp_id, transition="submit_results")
        self._submit_review(proc, exp_id, "experiment_reviewer", "pass", "Result file exists.")
        completed = self._tool(
            proc,
            "experiment.transition",
            experiment_id=exp_id,
            transition="complete",
        )

        self.assertEqual(completed["status"], "complete")
        # Fresh brain roots are de-nested: state.sqlite sits directly in the
        # staging dir (legacy nested layouts keep their paths — brain_dirs).
        self.assertTrue((self.root / "brain" / "state.sqlite").exists())
        self.assertFalse((self.root / "brain" / ".research_plugin").exists())
        self.assertTrue((self.root / "project_links.sqlite").exists())
        for state_dir in PROJECT_STATE_DIR_NAMES:
            self.assertFalse((self.install_dir / state_dir).exists())

    def _start_mcp_from_config(self, extra_env: dict[str, str] | None = None):
        manifest = json.loads((self.install_dir / ".codex-plugin" / "plugin.json").read_text())
        mcp_config = json.loads((self.install_dir / manifest["mcpServers"]).read_text())
        server = mcp_config["mcpServers"]["merv"]
        command = Path(server["command"])
        if not command.is_absolute():
            command = self.install_dir / command
        env = {**self._clean_env(), **server.get("env", {}), **(extra_env or {})}
        return subprocess.Popen(
            [str(command), *server.get("args", [])],
            cwd=self.research_repo,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _start_http_daemon(self):
        """Spin up the localhost brain on a free port."""
        return subprocess.Popen(
            [
                str(self.install_dir / "bin" / "merv-http"),
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--registry-store",
                str(self.root / "registry.sqlite"),
            ],
            cwd=self.root,
            env=self._clean_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _wait_for_daemon_ready(self, daemon) -> str:
        """Wait for the brain launcher to print its bound URL and answer health."""
        line = daemon.stdout.readline()
        match = re.search(r"http://[^ ]+", line)
        if match is None:
            stderr = daemon.stderr.read()
            self.fail(f"HTTP brain did not announce its URL. stdout: {line!r}\nstderr:\n{stderr}")
        base = match.group(0).strip()
        # /health succeeds only after the FastAPI app is fully up.
        self._fetch_json(base + "/health")
        return base

    def test_plugin_manifest_paths_resolve_for_local_install(self) -> None:
        manifest = json.loads((self.install_dir / ".codex-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "merv")
        self.assertEqual(BACKEND_VERSION, MCP_VERSION)
        self.assertTrue(manifest["version"].startswith(f"{BACKEND_VERSION}+"))
        self.assertTrue((self.install_dir / manifest["skills"]).is_dir())

        mcp_config = json.loads((self.install_dir / manifest["mcpServers"]).read_text())
        command = mcp_config["mcpServers"]["merv"]["command"]
        command_path = Path(command)
        self.assertFalse(command_path.is_absolute(), "Codex MCP launcher must be plugin-relative")
        if not command_path.is_absolute():
            command_path = self.install_dir / command_path
        self.assertTrue(command_path.exists())
        self.assertTrue(os.access(command_path, os.X_OK))
        env = mcp_config["mcpServers"]["merv"].get("env", {})
        # Shipped manifests must not pin a brain URL: an empty value keeps the
        # machine config from `merv-client configure` in charge,
        # with the hosted brain as the built-in fallback.
        self.assertEqual(env["MERV_CONTROL_URL"], "")

    def test_http_launcher_rejects_explicit_repo(self) -> None:
        proc = subprocess.run(
            [
                str(self.install_dir / "bin" / "merv-http"),
                "--repo",
                str(self.research_repo),
                "--host",
                "127.0.0.1",
                "--port",
                "0",
            ],
            cwd=self.root,
            env=self._clean_env(),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unrecognized arguments: --repo", proc.stderr)
        for state_dir in PROJECT_STATE_DIR_NAMES:
            self.assertFalse((self.research_repo / state_dir / "state.sqlite").exists())
            self.assertFalse((self.install_dir / state_dir).exists())

    def _submit_review(
        self,
        proc,
        exp_id: str,
        role: str,
        verdict: str,
        notes: str,
        synopsis: str = "The plan and results check out, so the attempt stands as reported.",
    ) -> None:
        req = self._tool(
            proc,
            "review.request",
            target_type="experiment",
            target_id=exp_id,
            role=role,
            producer_session_id="main-agent",
        )
        session = self._tool(
            proc,
            "review.start",
            review_request_id=req["review_request_id"],
            reviewer_capability=req["reviewer_capability"],
            caller_session_id=f"{role}-session",
        )
        self._tool(
            proc,
            "review.submit",
            review_session_id=session["review_session_id"],
            verdict=verdict,
            notes=notes,
            synopsis=synopsis,
        )

    def _tool(self, proc, tool_name: str, **arguments):
        response = self._rpc(proc, "tools/call", {"name": tool_name, "arguments": arguments})
        return response["result"]["structuredContent"]

    def _rpc(self, proc, method: str, params: dict | None = None):
        request_id = getattr(self, "_request_id", 0) + 1
        self._request_id = request_id
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            stderr = proc.stderr.read()
            self.fail(f"MCP process exited without a response. stderr:\n{stderr}")
        response = json.loads(line)
        if "error" in response:
            self.fail(f"RPC error for {method}: {response['error']}")
        return response

    def _fetch_json(self, url: str, *, method: str = "GET", body: dict | None = None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        last_error = None
        for _ in range(20):
            try:
                with urlopen(req, timeout=5) as res:
                    return json.loads(res.read().decode("utf-8"))
            except URLError as exc:
                last_error = exc
                time.sleep(0.1)
        raise last_error

    def _stop_process(self, proc) -> None:
        if proc.poll() is not None:
            return
        if proc.stdin:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()


if __name__ == "__main__":
    unittest.main()
