"""Sandbox-row projections shared across the agent surface, workflow, and UI.

These functions turn a raw ``sandboxes`` row into the dicts callers consume:

- ``agent_row_facts`` + ``merge_agent_view`` — the rich response for
  ``sandbox.request``/``sandbox.get``, decomposed along the plane seam
  (cloud plan §3.3): row facts are provider-portable and pure; the ssh
  command, key path, and local folder come from the data-plane worker's
  enrichment and are merged back in. Local mode merges in-process so tool
  results are unchanged; in split mode the proxy/daemon performs the merge.
- ``sandbox_row_view`` — the canonical row projection used by the workflow's
  agent-facing status AND the HTTP/UI layer (formerly ``_ui_view``).
- ``agent_summary`` — the compact per-row shape for ``sandbox.list``.
- ``needs_selection_view`` — the "pick a machine" response for bundled-hardware
  backends.

They are pure projection logic — no DB, backend, or filesystem calls.
"""

from __future__ import annotations

from typing import Any

from ..execution.sync_dirs import DEFAULT_DATA_DIR, remote_experiment_dir
from .sandbox_support import (
    ACTIVE_SANDBOX_STATUSES,
    DEFAULT_AUTO_RSYNC_INTERVAL_SECONDS,
    POLL_AFTER_SECONDS,
    decode_dashboards,
    env_float,
)


def _sync_interval_seconds() -> int:
    return int(
        env_float(
            "RESEARCH_PLUGIN_SANDBOX_RSYNC_INTERVAL",
            None,
            DEFAULT_AUTO_RSYNC_INTERVAL_SECONDS,
        )
    )


def _folder_contract_note(
    *, remote_dir: str, local_dir: str, initial_pushed: int | None
) -> str:
    """The experiment-folder sync contract, told at the moment it matters.

    This is the load-bearing guidance: one folder round-trips, everything else
    stays on the VM, and while the sandbox is live the VM owns the folder.
    """
    if initial_pushed is None or initial_pushed < 0:
        pushed_note = (
            f"Your experiment folder ({local_dir}) was pushed to {remote_dir} "
            "on the sandbox ($RP_EXPERIMENT_DIR). "
        )
    elif initial_pushed == 0:
        pushed_note = (
            f"Your experiment folder ({local_dir}) was pushed to {remote_dir} "
            "on the sandbox ($RP_EXPERIMENT_DIR), but it had no eligible files, "
            "so the remote folder starts empty. "
        )
    else:
        pushed_note = (
            f"Your experiment folder ({local_dir}) was pushed to {remote_dir} "
            f"on the sandbox ($RP_EXPERIMENT_DIR) — {initial_pushed} file(s) "
            "transferred. "
        )
    interval = _sync_interval_seconds()
    return (
        pushed_note
        + "Files over 100 MB and caches/checkpoints/archives (.git, venvs, "
        "*.pt, *.ckpt, *.safetensors, tarballs, ...) are never pushed or "
        "pulled; the exception is $RP_EXPERIMENT_DIR/artifacts_to_keep, which "
        "syncs with a 5 GB per-file cap for deliberate final artifacts. "
        f"The folder is mirrored back to the local repo every ~{interval}s "
        "and on sandbox.sync, as an exact replica: deletions and renames on "
        "the sandbox propagate to the local copy, and local edits are "
        "overwritten — while this sandbox is live, the sandbox owns the "
        "folder, so make ALL experiment-file edits here over SSH (including "
        "report.md and graph.json; their local copies update via the mirror). "
        "Keep datasets, caches, and anything you do not want carried into the "
        "repo OUTSIDE the experiment folder (e.g. $RP_DATASET_DIR) — nothing "
        "outside the folder is ever synced. "
    )


def _expiry_note(expires_at: Any) -> str:
    if not expires_at:
        return ""
    return (
        f"Sandbox lifetime expires at {expires_at}. Before that deadline, call "
        "sandbox.sync and register/associate needed outputs. If it expires, "
        "the reaper attempts a final pull and metrics snapshot, terminates the "
        "sandbox, and the experiment can request a new sandbox from ready_to_run. "
    )


def _is_live(*, status: str, ssh: dict[str, Any]) -> bool:
    return bool(ssh.get("host") and ssh.get("port") and status in ACTIVE_SANDBOX_STATUSES)


def _lease_facts(lease: dict[str, Any] | None) -> dict[str, Any] | None:
    """Who is syncing this experiment (plan Phase 4): the lease holder +
    expiry, so a second client can see why its own syncs are refused and
    when it can take over. None when no client has ever held the lease."""
    if lease is None:
        return None
    return {
        "holder_client_id": lease.get("holder_client_id"),
        "expires_at": lease.get("expires_at"),
    }


def agent_row_facts(
    *,
    row: dict[str, Any],
    env_info: dict[str, Any],
    reused: bool | None,
    lease: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Provider-portable half of the agent view — pure row projection.

    No conn files, no repo paths: everything here can be served by the cloud
    row in split mode (the lease record arrives as an argument, like the row).
    The machine-local enrichment (ssh command, key path, local folder, hint
    prose built on them) is merged by ``merge_agent_view``.
    """
    status = row.get("status") or "none"
    experiment_id = str(row.get("experiment_id") or "")
    remote_dir = str(
        row.get("sync_dir")
        or row.get("workdir")
        or remote_experiment_dir(experiment_id=experiment_id)
    )
    data_dir = str(
        row.get("sandbox_data_dir") or row.get("unsynced_dir") or DEFAULT_DATA_DIR
    )
    raw_pushed = row.get("initial_pushed")
    initial_pushed = int(raw_pushed) if raw_pushed is not None else -1
    facts: dict[str, Any] = {
        "experiment_id": row.get("experiment_id"),
        "project_id": row.get("project_id"),
        "sandbox_id": row.get("sandbox_id"),
        "status": status,
        "ssh": {
            "host": row.get("ssh_host"),
            "port": row.get("ssh_port"),
            "user": row.get("ssh_user"),
        },
        "workdir": row.get("workdir"),
        # The one synced location: the experiment's folder, pushed at
        # provisioning and mirrored back while the sandbox lives.
        "experiment_dir": remote_dir,
        # VM-local conventional home for datasets/caches. Never synced —
        # like everything else outside the experiment folder.
        "data_dir": data_dir,
        "files_pushed": initial_pushed if initial_pushed >= 0 else None,
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
    lease_facts = _lease_facts(lease)
    if lease_facts is not None:
        facts["sync_lease"] = lease_facts
    if env_info.get("available_tokens"):
        facts["environment"] = env_info
    if status == "provisioning":
        facts["phase"] = row.get("phase") or "starting"
        facts["detail"] = row.get("detail") or ""
        facts["poll_after_seconds"] = POLL_AFTER_SECONDS
    elif status == "failed":
        facts["error"] = row.get("error") or "provisioning failed"
    if reused is not None:
        facts["reused"] = reused
    return facts


def merge_agent_view(
    *, facts: dict[str, Any], enrichment: dict[str, Any]
) -> dict[str, Any]:
    """Compose the agent view from row facts + data-plane enrichment.

    ``enrichment`` carries ``command``/``raw_command``/``key_path``/
    ``local_dir`` from the worker (the conn file is already written for live
    rows). The hint prose is built here because it quotes both halves.
    """
    view = dict(facts)
    status = str(view.get("status") or "none")
    live = _is_live(status=status, ssh=view.get("ssh") or {})
    command = str(enrichment.get("command") or "") if live else ""
    raw_command = str(enrichment.get("raw_command") or "") if live else ""
    local_dir = str(enrichment.get("local_dir") or "")
    view["ssh"] = {
        **(view.get("ssh") or {}),
        "key_path": str(enrichment.get("key_path") or ""),
        "command": command,
        "raw_command": raw_command,
    }
    view["local_experiment_dir"] = local_dir
    remote_dir = str(view.get("experiment_dir") or "")
    initial_pushed = view.get("files_pushed")
    initial_pushed = int(initial_pushed) if initial_pushed is not None else -1
    credential_note = ""
    env_info = view.get("environment") or {}
    if "HF_TOKEN" in (env_info.get("available_tokens") or []):
        credential_note = (
            "If you need Hugging Face access, HF_TOKEN is already "
            "available inside the sandbox environment; use it through "
            "Hugging Face tooling and do not print or write the token. "
        )
    if status == "provisioning":
        view["hint"] = (
            "Provisioning. A fresh Lambda Labs VM commonly takes 5-15 minutes "
            "to boot and bootstrap (a large first sync adds time). Poll "
            "sandbox.get every 30-60 seconds until status is running, then "
            "run commands with ssh.command. Do not re-call sandbox.request "
            "to poll. When the sandbox boots, your local experiment folder "
            f"({local_dir}) is pushed to {remote_dir} on the VM, so anything "
            "the run needs (scripts, configs, notes) should already be in "
            "that folder. "
            f"{_expiry_note(view.get('expires_at'))}"
            f"{credential_note}"
        )
    elif status == "failed":
        view["hint"] = (
            "Provisioning failed (see error). Fix the cause if you can, then "
            "call sandbox.request to retry."
        )
    elif live:
        dashboard_note = (
            "Training observability: the backend starts MLflow and "
            "TensorBoard inside the sandbox. Use "
            "MLFLOW_TRACKING_URI=http://localhost:5000 for run params, "
            "metrics, and artifacts; write TensorBoard events to "
            "$RP_TB_LOGDIR. Framework integrations such as Hugging Face "
            "Trainer and PyTorch Lightning's MLFlowLogger can use these "
            "env vars directly; for plain PyTorch, add mlflow.autolog() "
            "when useful. The user sees dashboard tabs in the UI once "
            "the servers are reachable; you do not need to fetch or "
            "share the URLs. Also save selected plot images or compact result "
            "tables under $RP_EXPERIMENT_DIR (e.g. figures/*.png, "
            "results/*.json/csv) so they sync and can be referenced from "
            "report.md. "
        )
        view["hint"] = (
            f"Run commands with: {command} '<your shell command>' (from the repo root). "
            "Commands start inside the experiment folder. "
            "Output streams back and is recorded to the experiment terminal. "
            "Every command runs under a tmux supervisor on the sandbox and "
            "keeps running if SSH drops or your call times out - a timeout "
            "means you stopped watching, not that the command stopped. Check "
            "the terminal transcript for the command's exit marker before "
            "re-running anything long. "
            "If you are not in the repo root, use ssh.raw_command instead: it is a "
            "full ssh command line, so run it directly and append your command in "
            "single quotes (do not store it in a shell variable and re-invoke it). "
            + _folder_contract_note(
                remote_dir=remote_dir,
                local_dir=local_dir,
                initial_pushed=initial_pushed,
            )
            + "Before registering result resources, call sandbox.sync and use "
            "files under the local experiment folder. "
            f"{_expiry_note(view.get('expires_at'))}"
            f"{credential_note}"
            f"{dashboard_note}"
            "The dispatcher multiplexes one SSH connection and auto-retries "
            "transient connect failures, so do not wrap it in your own retry "
            "loop; if commands keep failing to connect, call sandbox.get once "
            "to refresh the endpoint."
        )
    else:
        view["hint"] = "No live sandbox for this experiment — call sandbox.request to create one."
    return view


def agent_summary(
    *, row: dict[str, Any], lease: dict[str, Any] | None = None
) -> dict[str, Any]:
    summary = {
        "experiment_id": row.get("experiment_id"),
        "sandbox_id": row.get("sandbox_id"),
        "status": row.get("status"),
        "gpu": row.get("gpu") or None,
        "instance_type": row.get("instance_type") or None,
        "region": row.get("region") or None,
        "expires_at": row.get("expires_at"),
    }
    lease_facts = _lease_facts(lease)
    if lease_facts is not None:
        summary["sync_lease"] = lease_facts
    return summary


def sandbox_row_view(*, row: dict[str, Any], local_sync_dir: str) -> dict[str, Any]:
    """Canonical sandbox-row projection (workflow status + HTTP/UI).

    ``local_sync_dir`` is machine-local data-plane enrichment: callers resolve
    it through the worker (it no longer lives in the row).
    """
    experiment_id = str(row.get("experiment_id") or "")
    remote_dir = str(
        row.get("sync_dir")
        or row.get("workdir")
        or remote_experiment_dir(experiment_id=experiment_id)
    )
    data_dir = str(
        row.get("sandbox_data_dir") or row.get("unsynced_dir") or DEFAULT_DATA_DIR
    )
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
        # The experiment's one synced folder on the VM, plus its local mirror.
        # (Key names kept stable for the UI; `sync_dir` IS the experiment dir.)
        "sync_dir": remote_dir,
        "local_sync_dir": local_sync_dir,
        "sandbox_data_dir": data_dir,
        "initial_pushed": row.get("initial_pushed"),
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
