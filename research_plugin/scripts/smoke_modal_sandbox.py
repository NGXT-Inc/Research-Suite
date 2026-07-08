#!/usr/bin/env python3
"""Live smoke test for the Modal sandbox registry (SandboxService + SSH).

Exercises the real registry end-to-end against Modal:
  health -> sandbox.request -> SSH in -> run a command -> read transcript ->
  reuse-if-alive -> get -> release -> recreate.

Requires MODAL_TOKEN_ID / MODAL_TOKEN_SECRET (directly or via
RESEARCH_PLUGIN_MODAL_ENV_FILE). Creates real, billable resources; always
releases them in a finally block.

Usage:
  RESEARCH_PLUGIN_MODAL_ENV_FILE=/path/.env \
    .venv/bin/python scripts/smoke_modal_sandbox.py [--full-image] [--gpu A100] [--keep]

By default uses a slim image (debian_slim + openssh) so the run is fast and
cheap; the SSH/registry path is identical to production. --full-image builds the
shipped torch image to prove the real image boots sshd.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.execution.backends.modal.config import ModalConfig
from backend.execution.backends.modal.sandbox_backend import ModalSandboxBackend


PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)


def check(cond: bool, msg: str) -> bool:
    print(f"  [{PASS if cond else FAIL}] {msg}", flush=True)
    if not cond:
        _failures.append(msg)
    return cond


def build_slim_image(backend: ModalSandboxBackend):
    modal = backend._modal_module()
    img = modal.Image.debian_slim(python_version="3.11").apt_install(
        "openssh-server", "ca-certificates"
    )
    return backend._with_ssh(img)


def ssh_argv(sb: dict, command: str) -> list[str]:
    ssh = sb["ssh"]
    return [
        "ssh",
        "-i", ssh["key_path"],
        "-p", str(ssh["port"]),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        f"{ssh['user']}@{ssh['host']}",
        command,
    ]


def ssh_run(sb: dict, command: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(ssh_argv(sb, command), capture_output=True, text=True, timeout=timeout)


def wait_for_ssh(sb: dict, *, deadline_s: float = 150.0) -> bool:
    deadline = time.time() + deadline_s
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            r = ssh_run(sb, "echo RP_SSH_OK", timeout=15)
            if "RP_SSH_OK" in r.stdout:
                log(f"  ssh up after {attempt} attempt(s)")
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(5)
    return False


def diagnose(backend: ModalSandboxBackend, sandbox_id: str) -> None:
    """Best-effort: print the sandbox's own stdout/stderr to explain a boot failure."""
    try:
        sb = backend._sandbox_from_id(sandbox_id)
        for name in ("stdout", "stderr"):
            stream = getattr(sb, name, None)
            if stream is None:
                continue
            try:
                text = stream.read()
                if text:
                    log(f"  --- sandbox {name} ---\n{text[:2000]}")
            except Exception:
                pass
    except Exception as exc:  # noqa: BLE001
        log(f"  (could not read sandbox streams: {exc})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full-image", action="store_true", help="use the shipped torch image")
    parser.add_argument("--gpu", default=None, help="GPU type, e.g. A100 (default: CPU-only)")
    parser.add_argument("--keep", action="store_true", help="do not release sandboxes at the end")
    args = parser.parse_args()

    repo = Path(tempfile.mkdtemp(prefix="rp-smoke-"))
    (repo / "experiments" / "smoke").mkdir(parents=True)
    (repo / "experiments" / "smoke" / "marker.txt").write_text("smoke\n")

    cfg = ModalConfig.from_env()
    backend = ModalSandboxBackend(repo_root=repo, config=cfg)
    if not args.full_image:
        log("Using slim image (openssh only). Pass --full-image for the shipped torch image.")
        backend._base_image = build_slim_image(backend)
    else:
        log("Using the shipped production image (this includes torch; first build is slow).")

    app = ResearchPluginApp(
        repo_root=repo,
        db_path=repo / ".research_plugin" / "state.sqlite",
        execution_backend=backend,
    )
    project_id = app.current_project()["project"]["id"]
    experiment_id = app.call_tool(
        "experiment.create", {"project_id": project_id, "intent": "modal smoke test"}
    )["id"]
    with app.store.transaction() as conn:
        conn.execute("UPDATE experiments SET status='ready_to_run' WHERE id=?", (experiment_id,))

    scope = {"project_id": project_id, "experiment_id": experiment_id}
    created_id = None
    try:
        log("\n1) health")
        check(app.call_tool("sandbox.health", {})["ok"], "backend health ok")

        log("\n2) sandbox.request (create + SSH wiring)")
        t0 = time.time()
        req = app.call_tool(
            "sandbox.request",
            {**scope, "gpu": args.gpu, "cpu": 1, "memory": 2048, "time_limit": 900},
        )
        created_id = req["sandbox_id"]
        log(f"  created {created_id} in {time.time() - t0:.1f}s; ssh={req['ssh']['host']}:{req['ssh']['port']}")
        check(req["status"] == "running", "status running")
        check(bool(req["sandbox_id"]), "sandbox_id present")
        check(bool(req["ssh"]["host"] and req["ssh"]["port"]), "ssh endpoint present")
        check(Path(req["ssh"]["key_path"]).exists(), "private key written locally")
        check(
            app.call_tool("experiment.get_state", scope)["status"] == "running",
            "experiment flipped to running",
        )

        log("\n3) SSH connectivity")
        up = wait_for_ssh(req)
        if not check(up, "ssh login succeeds with the generated key"):
            diagnose(backend, created_id)
            raise SystemExit("ssh never came up")

        log("\n4) run a command over SSH (writes to the synced workspace)")
        workdir = req["workdir"]
        r = ssh_run(
            req,
            f"cd {workdir} && echo hello-from-sandbox > smoke_out.txt && "
            f"cat smoke_out.txt && uname -a && (nvidia-smi -L 2>/dev/null || echo no-gpu)",
        )
        log("  stdout:\n" + "\n".join("    " + l for l in r.stdout.splitlines()))
        check(r.returncode == 0, "remote command exit 0")
        check("hello-from-sandbox" in r.stdout, "command output returned to client")

        log("\n5) sandbox.terminal (transcript recorded by the ForceCommand wrapper)")
        time.sleep(2)
        term = app.call_tool("sandbox.terminal", scope)
        check("hello-from-sandbox" in term["transcript"], "transcript captured the command output")
        check(
            "smoke_out.txt" in term["transcript"] or "uname" in term["transcript"],
            "transcript shows the command line",
        )

        log("\n6) reuse-if-alive")
        req2 = app.call_tool("sandbox.request", {**scope, "gpu": args.gpu})
        check(req2.get("reused") is True, "second request reused the live sandbox")
        check(req2["sandbox_id"] == created_id, "reuse returned the same sandbox_id")

        log("\n7) sandbox.get + liveness")
        check(app.call_tool("sandbox.get", scope)["status"] == "running", "get reports running")
        check(backend.is_alive(sandbox_id=created_id), "backend.is_alive true")

        log("\n8) sandbox.release (shutdown)")
        rel = app.call_tool("sandbox.release", scope)
        check(rel["status"] == "terminated", "release marks terminated")
        time.sleep(3)
        check(not backend.is_alive(sandbox_id=created_id), "Modal sandbox actually gone")

        log("\n9) recreate after release")
        req3 = app.call_tool("sandbox.request", {**scope, "gpu": args.gpu, "time_limit": 600})
        check(req3.get("reused") is False, "request after release creates a fresh sandbox")
        check(req3["sandbox_id"] != created_id, "fresh sandbox has a new id")
        if not args.keep:
            app.call_tool("sandbox.release", scope)
        created_id = req3["sandbox_id"] if args.keep else None

    finally:
        if not args.keep:
            log("\ncleanup: releasing any live sandbox")
            try:
                app.call_tool("sandbox.release", scope)
            except Exception as exc:  # noqa: BLE001
                log(f"  (release failed: {exc})")
        backend.shutdown()

    log("")
    if _failures:
        log(f"RESULT: {FAIL} — {len(_failures)} check(s) failed:")
        for f in _failures:
            log(f"  - {f}")
        return 1
    log(f"RESULT: {PASS} — all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
