#!/usr/bin/env python3
"""Run a live Ray-backed research_plugin job smoke test.

This script creates a disposable research repo, drives the MCP app like a Codex
agent would, submits a Ray job, syncs the resulting local file as a resource,
and completes the review-gated experiment workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from backend.app import ResearchPluginApp
from backend.execution.backends.ray import RayExecutionBackend, RayRestJobClient


TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", default=os.environ.get("RESEARCH_PLUGIN_RAY_ADDRESS", "http://127.0.0.1:8265"))
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--keep-repo", action="store_true")
    parser.add_argument("--force-rest", action="store_true", help="Use the REST adapter even if Ray SDK is installed.")
    args = parser.parse_args()

    os.environ["RESEARCH_PLUGIN_RAY_ADDRESS"] = args.address
    repo = Path(tempfile.mkdtemp(prefix="research-plugin-ray-smoke-")).resolve()
    cleanup = False
    try:
        summary = run_smoke(repo=repo, address=args.address, timeout=args.timeout, force_rest=args.force_rest)
        cleanup = not args.keep_repo
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    finally:
        if cleanup:
            shutil.rmtree(repo, ignore_errors=True)


def run_smoke(*, repo: Path, address: str, timeout: int, force_rest: bool) -> dict[str, Any]:
    prepare_repo(repo)
    execution_backend = (
        RayExecutionBackend(repo_root=repo, client=RayRestJobClient(address=address))
        if force_rest
        else None
    )
    app = ResearchPluginApp(
        repo_root=repo,
        db_path=repo / ".research_plugin" / "state.sqlite",
        execution_backend=execution_backend,
    )

    health = app.call_tool("job.health", {})
    if not health.get("ok"):
        raise RuntimeError(f"Ray Jobs API is not healthy: {health}")

    project = app.call_tool(
        "project.create",
        {"name": "Ray smoke project", "summary": "Validate Ray-backed research_plugin execution."},
    )
    project_id = project["id"]
    claim = app.call_tool(
        "claim.create",
        {
            "project_id": project_id,
            "statement": "The toy model can produce a measurable result file.",
            "scope": "Ray job smoke test.",
            "confidence": "medium",
        },
    )
    experiment = app.call_tool(
        "experiment.create",
        {
            "project_id": project_id,
            "intent": "Run a lightweight Ray job that writes outputs/result.json.",
            "tested_claim_ids": [claim["id"]],
        },
    )
    experiment_id = experiment["id"]

    plan = app.call_tool(
        "resource.register_file",
        {"project_id": project_id, "path": "experiments/e001/plan.md", "kind": "markdown", "title": "Experiment plan"},
    )
    app.call_tool(
        "resource.associate",
        {
            "project_id": project_id,
            "resource_id": plan["id"],
            "target_type": "experiment",
            "target_id": experiment_id,
            "role": "plan",
        },
    )
    workflow_after_plan = app.call_tool(
        "workflow.status_and_next",
        {"project_id": project_id, "experiment_id": experiment_id},
    )["workflow"]

    app.call_tool("experiment.transition", {"project_id": project_id, "experiment_id": experiment_id, "transition": "submit_design"})
    pass_review(
        app,
        project_id=project_id,
        experiment_id=experiment_id,
        role="design_reviewer",
        producer_session_id="smoke-main",
        reviewer_session_id="smoke-design-reviewer",
        notes="Design is adequate for a Ray smoke test.",
    )
    app.call_tool("experiment.transition", {"project_id": project_id, "experiment_id": experiment_id, "transition": "mark_ready_to_run"})

    submitted = app.call_tool(
        "job.submit",
        {
            "project_id": project_id,
            "experiment_id": experiment_id,
            "command": "python jobs/write_result.py",
            "expected_outputs": ["outputs/result.json"],
        },
    )
    job_id = submitted["id"]
    workflow_after_submit = app.call_tool(
        "workflow.status_and_next",
        {"project_id": project_id, "experiment_id": experiment_id},
    )["workflow"]
    final_job = wait_for_job(app, project_id=project_id, job_id=job_id, timeout=timeout)
    logs = app.call_tool("job.logs", {"project_id": project_id, "job_id": job_id, "tail": 20})
    outputs = app.call_tool("job.status", {"project_id": project_id, "job_id": job_id})["outputs"]
    workflow_after_job = app.call_tool(
        "workflow.status_and_next",
        {"project_id": project_id, "experiment_id": experiment_id},
    )["workflow"]

    if final_job["status"] != "succeeded":
        raise RuntimeError(f"job did not succeed: {final_job}\nlogs: {logs}")
    if not (repo / "outputs" / "result.json").is_file():
        raise RuntimeError("job succeeded but outputs/result.json is missing from the local repo")

    synced = app.call_tool("resource.sync_changed_files", {"project_id": project_id, "paths": ["outputs/result.json"]})
    resource_id = synced["synced"][0]["id"]
    app.call_tool(
        "resource.associate",
        {
            "project_id": project_id,
            "resource_id": resource_id,
            "target_type": "experiment",
            "target_id": experiment_id,
            "role": "result",
        },
    )
    workflow_after_sync = app.call_tool(
        "workflow.status_and_next",
        {"project_id": project_id, "experiment_id": experiment_id},
    )["workflow"]

    app.call_tool("experiment.transition", {"project_id": project_id, "experiment_id": experiment_id, "transition": "submit_results"})
    pass_review(
        app,
        project_id=project_id,
        experiment_id=experiment_id,
        role="experiment_reviewer",
        producer_session_id="smoke-main",
        reviewer_session_id="smoke-experiment-reviewer",
        notes="Job succeeded, output exists, and the result resource is associated.",
        evidence={"job_id": job_id, "resource_id": resource_id},
    )
    complete = app.call_tool("experiment.transition", {"project_id": project_id, "experiment_id": experiment_id, "transition": "complete"})
    workflow_complete = app.call_tool(
        "workflow.status_and_next",
        {"project_id": project_id, "experiment_id": experiment_id},
    )["workflow"]

    return {
        "repo": str(repo),
        "adapter": "rest" if force_rest else "auto",
        "ray_address": address,
        "project_id": project_id,
        "claim_id": claim["id"],
        "experiment_id": experiment_id,
        "experiment_status": complete["status"],
        "job_id": job_id,
        "job_status": final_job["status"],
        "outputs": outputs,
        "logs": logs["logs"],
        "resource_id": resource_id,
        "workflow_after_plan": workflow_after_plan,
        "workflow_after_submit": workflow_after_submit,
        "workflow_after_job": workflow_after_job,
        "workflow_after_sync": workflow_after_sync,
        "workflow_complete": workflow_complete,
    }


def prepare_repo(repo: Path) -> None:
    (repo / "experiments" / "e001").mkdir(parents=True)
    (repo / "jobs").mkdir()
    (repo / "outputs").mkdir()
    target = repo / "outputs" / "result.json"
    (repo / "README.md").write_text("# Ray smoke research repo\n", encoding="utf-8")
    (repo / "experiments" / "e001" / "plan.md").write_text(
        "Toy plan for Ray-backed job execution.\n",
        encoding="utf-8",
    )
    (repo / "jobs" / "write_result.py").write_text(
        "from pathlib import Path\n"
        "import json\n"
        "import time\n"
        f"target = Path({str(target)!r})\n"
        "target.parent.mkdir(parents=True, exist_ok=True)\n"
        "time.sleep(1)\n"
        "target.write_text(json.dumps({'accuracy': 0.91, 'loss': 0.12}, sort_keys=True), encoding='utf-8')\n"
        "print('wrote', target)\n",
        encoding="utf-8",
    )


def pass_review(
    app: ResearchPluginApp,
    *,
    project_id: str,
    experiment_id: str,
    role: str,
    producer_session_id: str,
    reviewer_session_id: str,
    notes: str,
    evidence: dict[str, Any] | None = None,
) -> None:
    request = app.call_tool(
        "review.request",
        {
            "project_id": project_id,
            "target_type": "experiment",
            "target_id": experiment_id,
            "role": role,
            "reason": notes,
            "producer_session_id": producer_session_id,
        },
    )
    session = app.call_tool(
        "review.start",
        {
            "review_request_id": request["review_request_id"],
            "reviewer_capability": request["reviewer_capability"],
            "declared_agent": reviewer_session_id,
            "caller_session_id": reviewer_session_id,
        },
    )
    app.call_tool(
        "review.submit",
        {
            "review_session_id": session["review_session_id"],
            "verdict": "pass",
            "notes": notes,
            "evidence": evidence or {},
        },
    )


def wait_for_job(app: ResearchPluginApp, *, project_id: str, job_id: str, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    job = app.call_tool("job.status", {"project_id": project_id, "job_id": job_id})
    while job["status"] not in TERMINAL_JOB_STATUSES and time.monotonic() < deadline:
        time.sleep(1)
        job = app.call_tool("job.status", {"project_id": project_id, "job_id": job_id})
    return job


if __name__ == "__main__":
    raise SystemExit(main())
