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

import shlex
from typing import Any

from ...domain.sandbox_paths import DEFAULT_DATA_DIR, remote_experiment_dir
from ...sandbox.sandbox_support import (
    ACTIVE_SANDBOX_STATUSES,
    POLL_AFTER_SECONDS,
    decode_dashboards,
)


def _folder_contract_note(*, remote_dir: str, local_dir: str) -> str:
    """The sandbox file durability rule, told at the moment it matters."""
    return (
        f"The sandbox experiment folder is {remote_dir} ($RP_EXPERIMENT_DIR). "
        "Work in it over SSH — scripts, results, report.md, graph.json. "
        "This sandbox is an EPHEMERAL SSH window: nothing is synced for you, and "
        "when it is released or reaped the VM and everything on it is destroyed. "
        "So pull anything you want to keep BEFORE then, yourself, from the "
        "terminal: you have the SSH connection details (ssh.key_path / ssh.host / "
        "ssh.port in this response), so rsync the light files you need off the box "
        f"into the local experiment folder ({local_dir}) and then register them as "
        "resources, e.g. "
        f"rsync -az -e 'ssh -i <key_path> -p <port> -o StrictHostKeyChecking=no' "
        f"<user>@<host>:{remote_dir}/ {local_dir}/ . "
        "Heavy files (trained models, precious datasets, multi-GB checkpoints) "
        "should NOT be rsynced into the repo — upload them to durable storage with "
        "storage.put_object instead. Keep caches and scratch data under "
        "$RP_DATASET_DIR, outside the experiment folder. "
    )


def _expiry_note(expires_at: Any) -> str:
    if not expires_at:
        return ""
    return (
        f"Sandbox lifetime expires at {expires_at}. Before that deadline, pull "
        "anything you need off the box yourself (rsync light files into the local "
        "experiment folder, storage.put_object for heavy ones) and "
        "register/associate the outputs. If it expires, the reaper terminates the "
        "sandbox and the experiment can request a new one from ready_to_run. "
    )


def _is_live(*, status: str, ssh: dict[str, Any]) -> bool:
    return bool(ssh.get("host") and ssh.get("port") and status in ACTIVE_SANDBOX_STATUSES)


def agent_row_facts(
    *,
    row: dict[str, Any],
    env_info: dict[str, Any],
    mlflow: dict[str, object] | None = None,
    reused: bool | None,
) -> dict[str, Any]:
    """Provider-portable half of the agent view — pure row projection.

    No conn files, no repo paths: everything here can be served by the cloud
    row in split mode.
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
    facts: dict[str, Any] = {
        "sandbox_uid": row.get("sandbox_uid"),
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
        # The experiment folder on the box; the agent rsyncs what it needs off
        # it over SSH before the sandbox is destroyed (nothing is auto-synced).
        "experiment_dir": remote_dir,
        # VM-local conventional home for datasets/caches. Never synced —
        # like everything else outside the experiment folder.
        "data_dir": data_dir,
        "volume": row.get("volume_name"),
        "gpu": row.get("gpu") or None,
        "cpu": row.get("cpu"),
        "memory": row.get("memory"),
        "instance_type": row.get("instance_type") or None,
        "region": row.get("region") or None,
        "expires_at": row.get("expires_at"),
        # Observability dashboards visible to the user.
        "dashboards": decode_dashboards(row.get("dashboards_json")),
    }
    if mlflow:
        facts["mlflow"] = mlflow
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
    credential_note = ""
    env_info = view.get("environment") or {}
    if "HF_TOKEN" in (env_info.get("available_tokens") or []):
        credential_note = (
            "If you need Hugging Face access, HF_TOKEN is already "
            "available inside the sandbox environment; use it through "
            "Hugging Face tooling and do not print or write the token. "
        )
    mlflow_info = view.get("mlflow") if isinstance(view.get("mlflow"), dict) else {}
    mlflow_env = mlflow_info.get("env") if isinstance(mlflow_info, dict) else {}
    mlflow_configured = bool(
        isinstance(mlflow_info, dict) and mlflow_info.get("configured")
    )
    mlflow_access = mlflow_info.get("access") if isinstance(mlflow_info, dict) else {}
    mlflow_ready = not isinstance(mlflow_access, dict) or (
        mlflow_access.get("ready") is not False
    )
    if mlflow_configured and mlflow_ready and isinstance(mlflow_env, dict):
        mlflow_assignments = " ".join(
            f"{key}={shlex.quote(str(value))}"
            for key, value in sorted(mlflow_env.items())
        )
        mlflow_note = (
            "Quantitative experiment tracking: use MLflow for params, metrics, "
            "and artifacts. The backend owns the MLflow server; use the "
            f"provided mlflow.env block, or prefix training commands with: "
            f"{mlflow_assignments}. "
        )
    elif mlflow_configured:
        mlflow_note = (
            "Centralized MLflow is configured, but the sandbox access path is "
            "not ready yet; poll sandbox.get until mlflow.access.ready is true. "
        )
    else:
        mlflow_note = (
            "Quantitative experiment tracking should use centralized MLflow, "
            "but this backend has not provided an MLflow tracking URI yet. "
        )
    if status == "provisioning":
        view["hint"] = (
            "Provisioning. A fresh GPU VM commonly takes 5-15 minutes "
            "to boot and bootstrap. Poll "
            "sandbox.get every 30-60 seconds until status is running, then "
            "run commands with ssh.command. Do not re-call sandbox.request "
            "to poll. "
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
            "Training observability: the backend provides centralized MLflow "
            "tracking context and the sandbox provides TensorBoard event "
            "logging. Write TensorBoard events to $RP_TB_LOGDIR. Framework "
            "integrations such as Hugging Face "
            "Trainer and PyTorch Lightning's MLFlowLogger can use these "
            "env vars directly; for plain PyTorch, add mlflow.autolog() "
            "when useful. The user sees dashboard tabs in the UI once "
            "the servers are reachable; you do not need to fetch or "
            "share the URLs. Also save selected plot images or compact result "
            "tables under $RP_EXPERIMENT_DIR (e.g. figures/*.png, "
            "results/*.json/csv) so you can rsync them off and reference them "
            "from report.md. "
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
            )
            + "Before registering result resources, rsync the files you need off "
            "the box yourself (see the folder note) into the local experiment "
            "folder, then register those local files. "
            f"{_expiry_note(view.get('expires_at'))}"
            f"{credential_note}"
            f"{mlflow_note}"
            f"{dashboard_note}"
            "The dispatcher multiplexes one SSH connection and auto-retries "
            "transient connect failures, so do not wrap it in your own retry "
            "loop; if commands keep failing to connect, call sandbox.get once "
            "to refresh the endpoint."
        )
    else:
        view["hint"] = "No live sandbox for this experiment — call sandbox.request to create one."
    return view


def agent_summary(*, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "sandbox_uid": row.get("sandbox_uid"),
        "experiment_id": row.get("experiment_id"),
        "sandbox_id": row.get("sandbox_id"),
        "status": row.get("status"),
        "gpu": row.get("gpu") or None,
        "instance_type": row.get("instance_type") or None,
        "region": row.get("region") or None,
        "expires_at": row.get("expires_at"),
    }


def sandbox_row_view(
    *,
    row: dict[str, Any],
    local_sync_dir: str,
    mlflow: dict[str, object] | None = None,
) -> dict[str, Any]:
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
    view = {
        "sandbox_uid": row.get("sandbox_uid"),
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
        # Stable API names: `sync_dir` is the remote experiment directory, and
        # `local_sync_dir` is where explicitly copied files should land.
        "sync_dir": remote_dir,
        "local_sync_dir": local_sync_dir,
        "sandbox_data_dir": data_dir,
        "volume_name": row.get("volume_name"),
        # Sandbox-local dashboards. Centralized MLflow is surfaced separately
        # because it is backend-owned, not a sandbox tunnel.
        "dashboards": decode_dashboards(row.get("dashboards_json")),
        "requested_at": row.get("requested_at"),
        "expires_at": row.get("expires_at"),
        "last_seen_at": row.get("last_seen_at"),
        "terminated_at": row.get("terminated_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
    if mlflow:
        view["mlflow"] = mlflow
    return view


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
