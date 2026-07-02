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

from ...domain.sandbox_paths import DEFAULT_DATA_DIR, remote_experiment_dir
from ...domain.storage_guidance import STORAGE_RULE_OF_THUMB
from ...sandbox.sandbox_support import (
    ACTIVE_SANDBOX_STATUSES,
    POLL_AFTER_SECONDS,
)


def _folder_contract_note(
    *,
    remote_dir: str,
    local_dir: str,
    attached_to_experiment: bool,
    storage_enabled: bool,
) -> str:
    """The sandbox file durability rule, told at the moment it matters."""
    _ = attached_to_experiment
    folder_label = "work folder"
    local_label = "local sandbox folder"
    if storage_enabled:
        heavy_note = (
            f"{STORAGE_RULE_OF_THUMB} Upload those durable files with "
            "storage.put_object or storage.upload_file instead of rsyncing them "
            "into the repo. "
        )
    else:
        heavy_note = (
            "Heavy-file storage is not enabled on this backend, so trained models, "
            "precious datasets, and multi-GB checkpoints cannot be durably retained "
            "through the storage tools. "
        )
    return (
        f"The sandbox {folder_label} is {remote_dir} ($RP_EXPERIMENT_DIR). "
        "Work in it over SSH — scripts, results, report.md, graph.json. "
        "This sandbox is an EPHEMERAL SSH window: nothing is copied for you, and "
        "when it is released or reaped the VM and everything on it is destroyed. "
        "So pull anything you want to keep BEFORE then, yourself, from the "
        "terminal: you have the SSH connection details (ssh.key_path / ssh.host / "
        "ssh.port in this response), so rsync the light files you need off the box "
        f"into the {local_label} ({local_dir}) and then register them as "
        "resources, e.g. "
        f"rsync -az -e 'ssh -i <key_path> -p <port> -o StrictHostKeyChecking=no' "
        f"<user>@<host>:{remote_dir}/ {local_dir}/ . "
        + heavy_note
        + "Keep caches and scratch data under "
        f"$RP_DATASET_DIR, outside the {folder_label}. "
    )


def _expiry_note(
    expires_at: Any, *, attached_to_experiment: bool, storage_enabled: bool
) -> str:
    if not expires_at:
        return ""
    _ = attached_to_experiment
    local_label = "local sandbox folder"
    retry_note = "you can request a new sandbox"
    heavy_note = (
        "storage.put_object for heavy ones"
        if storage_enabled
        else "no configured heavy-file storage is available"
    )
    return (
        f"Sandbox lifetime expires at {expires_at}. Before that deadline, pull "
        f"anything you need off the box yourself (rsync light files into the {local_label}, "
        f"{heavy_note}) and register/associate the outputs. "
        "If it expires, the reaper terminates the sandbox and "
        f"{retry_note}. "
    )


def _is_live(*, status: str, ssh: dict[str, Any]) -> bool:
    return bool(ssh.get("host") and ssh.get("port") and status in ACTIVE_SANDBOX_STATUSES)


def agent_row_facts(
    *,
    row: dict[str, Any],
    env_info: dict[str, Any],
    reused: bool | None,
    storage_enabled: bool = False,
) -> dict[str, Any]:
    """Provider-portable half of the agent view — pure row projection.

    No conn files, no repo paths: everything here can be served by the cloud
    row in split mode.
    The machine-local enrichment (ssh command, key path, local folder, hint
    prose built on them) is merged by ``merge_agent_view``.
    """
    status = row.get("status") or "none"
    experiment_id = str(row.get("experiment_id") or "")
    active_experiment_ids = list(row.get("active_experiment_ids") or [])
    remote_dir = str(
        row.get("sync_dir")
        or row.get("workdir")
        or remote_experiment_dir(
            experiment_id=str(row.get("sandbox_uid") or experiment_id)
        )
    )
    data_dir = str(
        row.get("sandbox_data_dir") or row.get("unsynced_dir") or DEFAULT_DATA_DIR
    )
    facts: dict[str, Any] = {
        "sandbox_uid": row.get("sandbox_uid"),
        "experiment_id": row.get("experiment_id"),
        "active_experiment_ids": active_experiment_ids,
        "project_id": row.get("project_id"),
        "sandbox_id": row.get("sandbox_id"),
        "status": status,
        "ssh": {
            "host": row.get("ssh_host"),
            "port": row.get("ssh_port"),
            "user": row.get("ssh_user"),
        },
        "workdir": row.get("workdir"),
        # The work folder on the box; the agent rsyncs what it needs off it over
        # SSH before the sandbox is destroyed (nothing is copied automatically).
        "experiment_dir": remote_dir,
        # VM-local conventional home for datasets/caches. Never copied —
        # like everything else outside the work folder.
        "data_dir": data_dir,
        "volume": row.get("volume_name"),
        "gpu": row.get("gpu") or None,
        "cpu": row.get("cpu"),
        "memory": row.get("memory"),
        "instance_type": row.get("instance_type") or None,
        "region": row.get("region") or None,
        "expires_at": row.get("expires_at"),
        "storage_enabled": bool(storage_enabled),
    }
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
    attached_to_experiment = bool(view.get("active_experiment_ids") or [])
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
    if status == "provisioning":
        view["hint"] = (
            "Provisioning. A fresh GPU VM commonly takes 5-15 minutes "
            "to boot and bootstrap. Poll "
            "sandbox.get every 30-60 seconds until status is running, then "
            "run commands with ssh.command. Do not re-call sandbox.request "
            "to poll. "
            + _expiry_note(
                view.get('expires_at'),
                attached_to_experiment=attached_to_experiment,
                storage_enabled=bool(view.get("storage_enabled")),
            )
            + credential_note
        )
    elif status == "failed":
        view["hint"] = (
            "Provisioning failed (see error). Fix the cause if you can, then "
            "call sandbox.request to retry."
        )
    elif live:
        if attached_to_experiment:
            output_note = (
                "Save selected plot images or compact result tables under "
                "$RP_EXPERIMENT_DIR (e.g. figures/*.png, results/*.json/csv) "
                "so you can rsync them off and reference them from report.md. "
            )
        else:
            output_note = (
                "Save selected outputs under $RP_EXPERIMENT_DIR so "
                "you can rsync them off before release. "
            )
        view["hint"] = (
            f"Run commands with: {command} '<your shell command>' (from the repo root). "
            + (
                "Commands start inside the sandbox work folder. "
                "Output streams back and is recorded to the sandbox terminal. "
                if attached_to_experiment
                else (
                    "Commands start inside the sandbox work folder. "
                    "Output streams back and is recorded to the sandbox terminal. "
                )
            )
            + "Every command runs under a tmux supervisor on the sandbox and "
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
                attached_to_experiment=attached_to_experiment,
                storage_enabled=bool(view.get("storage_enabled")),
            )
            + "Before registering result resources, rsync the files you need off "
            "the box yourself (see the folder note) into the local "
            + "sandbox folder"
            + ", then register those local files. "
            + _expiry_note(
                view.get('expires_at'),
                attached_to_experiment=attached_to_experiment,
                storage_enabled=bool(view.get("storage_enabled")),
            )
            + credential_note
            + output_note
            + "The dispatcher multiplexes one SSH connection and auto-retries "
            "transient connect failures, so do not wrap it in your own retry "
            "loop; if commands keep failing to connect, call sandbox.get once "
            "to refresh the endpoint."
        )
    else:
        view["hint"] = "No live sandbox found — call sandbox.request to create one."
    return view


def agent_summary(*, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "sandbox_uid": row.get("sandbox_uid"),
        "experiment_id": row.get("experiment_id"),
        "active_experiment_ids": list(row.get("active_experiment_ids") or []),
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
) -> dict[str, Any]:
    """Canonical sandbox-row projection (workflow status + HTTP/UI).

    ``local_sync_dir`` is machine-local data-plane enrichment: callers resolve
    it through the worker (it no longer lives in the row).
    """
    experiment_id = str(row.get("experiment_id") or "")
    active_experiment_ids = list(row.get("active_experiment_ids") or [])
    remote_dir = str(
        row.get("sync_dir")
        or row.get("workdir")
        or remote_experiment_dir(
            experiment_id=str(row.get("sandbox_uid") or experiment_id)
        )
    )
    data_dir = str(
        row.get("sandbox_data_dir") or row.get("unsynced_dir") or DEFAULT_DATA_DIR
    )
    view = {
        "sandbox_uid": row.get("sandbox_uid"),
        "experiment_id": row.get("experiment_id"),
        "active_experiment_ids": active_experiment_ids,
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
        "requested_at": row.get("requested_at"),
        "expires_at": row.get("expires_at"),
        "last_seen_at": row.get("last_seen_at"),
        "terminated_at": row.get("terminated_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
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
