"""One-off live smoke test for the Lambda-default hardware-selection flow.

Drives the REAL Lambda Cloud API end-to-end through ResearchPluginApp:
  1. health + live availability
  2. sandbox.options + sandbox.request(no instance_type) -> needs_selection menu
  3. provision the cheapest available SKU, SSH in, run nvidia-smi
  4. sandbox.sync, then release/terminate (guaranteed teardown)

Run from research_plugin/ with the Lambda key available:
  RESEARCH_PLUGIN_LAMBDA_ENV_FILE=$PWD/.env .venv/bin/python scripts/_live_lambda_smoke.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Make the research_plugin package root importable when run as a bare script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Test-only: point the remote workspace at /home/ubuntu (exists at boot) so the
# initial rsync push doesn't race the cloud-init that creates /workspace.
os.environ.setdefault("RESEARCH_PLUGIN_LAMBDA_WORKDIR", "/home/ubuntu/rp_synced")
os.environ.setdefault("RESEARCH_PLUGIN_LAMBDA_DATA_DIR", "/home/ubuntu/rp_unsynced")
os.environ.setdefault("RESEARCH_PLUGIN_SANDBOX_AUTO_RSYNC", "0")

from backend.app import ResearchPluginApp
from backend.execution.backends.lambda_labs.config import load_lambda_env_file
from backend.execution.backends.lambda_labs import LambdaCloudClient

load_lambda_env_file()


def hr(title: str) -> None:
    print("\n" + "=" * 72 + f"\n{title}\n" + "=" * 72, flush=True)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="rp-live-")
    repo = Path(tmp)
    app = ResearchPluginApp(repo_root=repo, db_path=repo / ".research_plugin" / "state.sqlite")
    backend_name = app.execution_backend.capabilities.name
    hr(f"1. Default backend = {backend_name!r}  (health below)")
    print(app.sandboxes.health())
    assert backend_name == "lambda_labs", backend_name

    project = app.call_tool("project.create", {"name": "Live Lambda Smoke"})
    pid = project["id"]
    exp = app.call_tool("experiment.create", {"project_id": pid, "intent": "lambda smoke"})
    eid = exp["id"]
    with app.store.transaction() as conn:
        conn.execute("UPDATE experiments SET status='ready_to_run' WHERE id=?", (eid,))

    hr("2a. sandbox.options (live availability menu)")
    options = app.call_tool("sandbox.options", {"project_id": pid})
    print("backend:", options["backend"], "| selection_required:", options.get("selection_required"))
    for o in options["options"]:
        print(f"  {o['instance_type']:24s} {o['gpu']:>6s} x{o['gpu_count']}  "
              f"${o['price_usd_per_hour']:.2f}/hr  {o['regions']}")

    hr("2b. sandbox.request with NO instance_type -> needs_selection")
    menu = app.call_tool("sandbox.request", {"project_id": pid, "experiment_id": eid})
    print("status:", menu["status"])
    print("hint:", menu["hint"][:160], "...")
    assert menu["status"] == "needs_selection", menu
    cheapest = menu["options"][0]
    print("cheapest available SKU:", cheapest["instance_type"],
          f"(${cheapest['price_usd_per_hour']:.2f}/hr, {cheapest['gpu']})")

    instance_type = cheapest["instance_type"]
    app.sandboxes.request_wait_seconds = 20.0  # return provisioning fast, then poll

    hr(f"3. sandbox.request(instance_type={instance_type!r}) -> provision REAL VM")
    created = app.call_tool(
        "sandbox.request",
        {"project_id": pid, "experiment_id": eid, "instance_type": instance_type, "time_limit": 1800},
    )
    print("status:", created["status"], "| sandbox_id:", created.get("sandbox_id"))

    deadline = time.monotonic() + 12 * 60
    row = created
    while row["status"] in ("provisioning",) and time.monotonic() < deadline:
        time.sleep(10)
        row = app.call_tool("sandbox.get", {"project_id": pid, "experiment_id": eid})
        print(f"  [{int(time.monotonic())%100000}] status={row['status']} "
              f"phase={row.get('phase','')} detail={row.get('detail','')}", flush=True)

    print("\nfinal status:", row["status"])
    print("instance_type:", row.get("instance_type"), "| region:", row.get("region"),
          "| gpu:", row.get("gpu"), "| cpu:", row.get("cpu"), "| memory:", row.get("memory"))

    if row["status"] == "running":
        raw = row["ssh"]["raw_command"]
        print("ssh.raw_command:", raw)
        hr("3b. Run nvidia-smi over SSH (retry for cloud-init key-injection lag)")
        ok = False
        for attempt in range(12):
            proc = subprocess.run(
                f"{raw} 'nvidia-smi -L; echo ---; uname -a; echo ---; nproc'",
                shell=True, text=True, capture_output=True, timeout=60,
            )
            if proc.returncode == 0:
                print(proc.stdout)
                ok = True
                break
            print(f"  attempt {attempt+1}: rc={proc.returncode} {proc.stderr.strip()[:120]}", flush=True)
            time.sleep(10)
        print("SSH/nvidia-smi succeeded:", ok)

        hr("3c. sandbox.sync (rsync remote synced workspace -> local)")
        try:
            synced = app.call_tool("sandbox.sync", {"project_id": pid, "experiment_id": eid})
            print("sync provider:", synced["sync"].get("provider"),
                  "| pulled:", synced["sync"].get("pulled"))
        except Exception as exc:  # noqa: BLE001
            print("sync error (non-fatal):", exc)
    else:
        print("error:", row.get("error"))

    hr("4. sandbox.release (terminate)")
    released = app.call_tool("sandbox.release", {"project_id": pid, "experiment_id": eid})
    print("released status:", released["status"])
    return 0 if row["status"] == "running" else 1


def sweep_orphans() -> None:
    """Safety net: terminate any rp-* instance we may have created."""
    try:
        client = LambdaCloudClient()
        for inst in client.list_instances():
            name = str(inst.get("name") or "")
            status = str(inst.get("status") or "")
            if name.startswith("rp-") and status not in ("terminated", "terminating"):
                print(f"[sweep] terminating leftover {name} ({inst.get('id')}, {status})")
                client.terminate_instances([str(inst.get("id"))])
    except Exception as exc:  # noqa: BLE001
        print("[sweep] error:", exc)


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    finally:
        hr("SAFETY SWEEP: ensure no Lambda instance is left running")
        sweep_orphans()
    sys.exit(rc)
