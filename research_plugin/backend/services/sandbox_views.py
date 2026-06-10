"""Sandbox-row projections shared across the agent surface, workflow, and UI.

These functions turn a raw ``sandboxes`` row into the dicts callers consume:

- ``agent_view`` — the rich response for ``sandbox.request``/``sandbox.get``
  (SSH command, hints, dashboards).
- ``sandbox_row_view`` — the canonical row projection used by the workflow's
  agent-facing status AND the HTTP/UI layer (formerly ``_ui_view``).
- ``agent_summary`` — the compact per-row shape for ``sandbox.list``.
- ``needs_selection_view`` — the "pick a machine" response for bundled-hardware
  backends.

They are pure projection logic — no DB or backend calls — so the service module
keeps only the state machine. ``agent_view`` takes the ``SandboxConnFiles``
helper and a pre-fetched ``env_info`` dict rather than reaching into service
state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..execution.sync_dirs import (
    DEFAULT_SYNC_DIR,
    DEFAULT_UNSYNCED_DIR,
    local_experiment_sync_dir,
    sync_hint,
)
from .sandbox_conn import SandboxConnFiles
from .sandbox_support import (
    ACTIVE_SANDBOX_STATUSES,
    POLL_AFTER_SECONDS,
    decode_dashboards,
)


def agent_view(
    *,
    row: dict[str, Any],
    key_path: Path,
    reused: bool | None,
    conn_files: SandboxConnFiles,
    env_info: dict[str, Any],
    repo_root: Path,
) -> dict[str, Any]:
    status = row.get("status") or "none"
    live = bool(
        row.get("ssh_host")
        and row.get("ssh_port")
        and status in ACTIVE_SANDBOX_STATUSES
    )
    command = conn_files.write_command_wrapper(row=row, key_path=key_path) if live else ""
    raw_command = conn_files.raw_ssh_command(row=row, key_path=key_path) if live else ""
    view: dict[str, Any] = {
        "experiment_id": row.get("experiment_id"),
        "project_id": row.get("project_id"),
        "sandbox_id": row.get("sandbox_id"),
        "status": status,
        "ssh": {
            "host": row.get("ssh_host"),
            "port": row.get("ssh_port"),
            "user": row.get("ssh_user"),
            "key_path": str(key_path),
            "command": command,
            "raw_command": raw_command,
        },
        "workdir": row.get("workdir"),
        "sync_dir": row.get("sync_dir") or row.get("workdir") or DEFAULT_SYNC_DIR,
        "unsynced_dir": row.get("unsynced_dir") or row.get("sandbox_data_dir") or DEFAULT_UNSYNCED_DIR,
        "local_sync_dir": row.get("local_sync_dir") or str(
            local_experiment_sync_dir(repo_root=repo_root, experiment_id=str(row.get("experiment_id") or ""))
        ),
        "sandbox_data_dir": row.get("sandbox_data_dir") or "",
        "volume": row.get("volume_name"),
        "gpu": row.get("gpu") or None,
        "cpu": row.get("cpu"),
        "memory": row.get("memory"),
        "instance_type": row.get("instance_type") or None,
        "region": row.get("region") or None,
        "expires_at": row.get("expires_at"),
        # Observability dashboards visible to the user. The agent receives
        # the URLs purely informationally (it talks to MLflow through the
        # in-sandbox `MLFLOW_TRACKING_URI` localhost env, not these URLs).
        "dashboards": decode_dashboards(row.get("dashboards_json")),
    }
    credential_note = ""
    if env_info.get("available_tokens"):
        view["environment"] = env_info
        if "HF_TOKEN" in env_info["available_tokens"]:
            credential_note = (
                "If you need Hugging Face access, HF_TOKEN is already "
                "available inside the sandbox environment; use it through "
                "Hugging Face tooling and do not print or write the token. "
            )
    if status == "provisioning":
        view["phase"] = row.get("phase") or "starting"
        view["detail"] = row.get("detail") or ""
        view["poll_after_seconds"] = POLL_AFTER_SECONDS
        view["hint"] = (
            "Provisioning (a large first sync or cold start can take a few "
            "minutes). Poll sandbox.get every ~10s until status is running, "
            "then run commands with ssh.command. Do not re-call "
            "sandbox.request to poll. "
            f"{credential_note}"
        )
    elif status == "failed":
        view["error"] = row.get("error") or "provisioning failed"
        view["hint"] = (
            "Provisioning failed (see error). Fix the cause if you can, then "
            "call sandbox.request to retry."
        )
    elif live:
        dashboards = view.get("dashboards") or {}
        dashboard_note = ""
        if dashboards.get("mlflow") or dashboards.get("tensorboard"):
            dashboard_note = (
                "Training observability: an MLflow tracking server "
                "(MLFLOW_TRACKING_URI=http://localhost:5000) and a "
                "TensorBoard (logdir at $RP_TB_LOGDIR) are already "
                "running inside the sandbox. HuggingFace Trainer and "
                "PyTorch Lightning's MLFlowLogger auto-pick MLflow up "
                "with no setup. For plain PyTorch, add mlflow.autolog() "
                "once at the top of the training script. The user sees "
                "the dashboards live in the UI; you do not need to "
                "fetch or share the URLs. "
            )
        view["hint"] = (
            f"Run commands with: {command} '<your shell command>' (from the repo root). "
            "Output streams back and is recorded to the experiment terminal. "
            "If you are not in the repo root, use ssh.raw_command instead: it is a "
            "full ssh command line, so run it directly and append your command in "
            "single quotes (do not store it in a shell variable and re-invoke it). "
            f"{sync_hint()} "
            "The backend automatically rsyncs the remote sync directory to "
            "local_sync_dir; call sandbox.sync for an immediate pull. "
            f"{credential_note}"
            f"{dashboard_note}"
            "Before registering result resources, call sandbox.sync and use "
            "files under local_sync_dir. "
            "The dispatcher multiplexes one SSH connection and auto-retries "
            "transient connect failures, so do not wrap it in your own retry "
            "loop; if commands keep failing to connect, call sandbox.get once "
            "to refresh the endpoint."
        )
    else:
        view["hint"] = "No live sandbox for this experiment — call sandbox.request to create one."
    if reused is not None:
        view["reused"] = reused
    return view


def agent_summary(*, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "experiment_id": row.get("experiment_id"),
        "sandbox_id": row.get("sandbox_id"),
        "status": row.get("status"),
        "gpu": row.get("gpu") or None,
        "instance_type": row.get("instance_type") or None,
        "region": row.get("region") or None,
        "expires_at": row.get("expires_at"),
    }


def sandbox_row_view(*, row: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    """Canonical sandbox-row projection (workflow status + HTTP/UI)."""
    return {
        "experiment_id": row.get("experiment_id"),
        "project_id": row.get("project_id"),
        "sandbox_id": row.get("sandbox_id"),
        "status": row.get("status"),
        "phase": row.get("phase") or "",
        "detail": row.get("detail") or "",
        "error": row.get("error") or "",
        "gpu": row.get("gpu") or "",
        "cpu": row.get("cpu"),
        "memory": row.get("memory"),
        "instance_type": row.get("instance_type") or "",
        "region": row.get("region") or "",
        "time_limit": row.get("time_limit"),
        "ssh_host": row.get("ssh_host"),
        "ssh_port": row.get("ssh_port"),
        "ssh_user": row.get("ssh_user"),
        "workdir": row.get("workdir"),
        "sync_dir": row.get("sync_dir") or row.get("workdir") or DEFAULT_SYNC_DIR,
        "unsynced_dir": row.get("unsynced_dir") or row.get("sandbox_data_dir") or DEFAULT_UNSYNCED_DIR,
        "local_sync_dir": row.get("local_sync_dir") or str(
            local_experiment_sync_dir(repo_root=repo_root, experiment_id=str(row.get("experiment_id") or ""))
        ),
        "sandbox_data_dir": row.get("sandbox_data_dir") or "",
        "volume_name": row.get("volume_name"),
        # Observability dashboards (MLflow, TensorBoard). Empty dict when
        # no tunnels are exposed (test backends, pre-Phase-1 rows). The UI
        # renders a tab per non-empty entry.
        "dashboards": decode_dashboards(row.get("dashboards_json")),
        "requested_at": row.get("requested_at"),
        "expires_at": row.get("expires_at"),
        "last_seen_at": row.get("last_seen_at"),
        "terminated_at": row.get("terminated_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def needs_selection_view(
    *,
    experiment_id: str,
    project_id: str,
    catalog: dict[str, Any],
) -> dict[str, Any]:
    """The 'pick a machine' response for bundled-hardware backends."""
    options = catalog.get("options", [])
    cheapest = options[0]["instance_type"] if options else None
    return {
        "experiment_id": experiment_id,
        "project_id": project_id,
        "status": "needs_selection",
        "provider": catalog.get("provider"),
        "select_with": catalog.get("select_with") or "instance_type",
        "reason": catalog.get("reason")
        or "This provider bundles GPU + CPU + RAM into fixed machine types.",
        "options": options,
        "regions": catalog.get("regions", []),
        "hint": (
            "No sandbox is attached and this provider procures whole machines, "
            "so choose one before provisioning. Re-call sandbox.request with "
            "instance_type=<one of options[].instance_type> (and optionally "
            "region=<one of that option's regions>). Options are sorted "
            "cheapest-first"
            + (f"; cheapest available now is '{cheapest}'. " if cheapest else ". ")
            + "Call sandbox.options anytime to re-list current availability."
        ),
    }
