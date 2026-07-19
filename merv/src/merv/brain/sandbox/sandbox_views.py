"""Sandbox-row projections shared across the agent surface, workflow, and UI.

These functions turn a raw ``sandboxes`` row into the dicts callers consume:

- ``agent_row_facts`` + ``merge_agent_view`` — the rich response for
  ``sandbox.request``/``sandbox.get``. Row facts are provider-portable and pure;
  checkout-local key paths and folders belong to stdio-proxy enrichment.
- ``sandbox_row_view`` — the canonical row projection used by the workflow's
  agent-facing status AND the HTTP/UI layer (formerly ``_ui_view``).
- ``agent_summary`` — the compact per-row shape for ``sandbox.list``.
- ``needs_selection_view`` — the "pick a machine" response for bundled-hardware
  backends.

They are pure projection logic — no DB, backend, or filesystem calls.
"""

from __future__ import annotations
from typing import Any

from .sandbox_paths import DEFAULT_DATA_DIR, remote_experiment_dir
from .sandbox_support import (
    ACTIVE_SANDBOX_STATUSES,
    POLL_AFTER_SECONDS,
)


def _folder_contract_note(
    *,
    remote_dir: str,
    local_dir: str,
    attached_to_experiment: bool,
    storage_enabled: bool,
    storage_hint: str = "",
) -> str:
    """The sandbox file durability rule, told at the moment it matters.

    ``storage_hint`` is composition-injected guidance prose (the storage rule
    of thumb); this module embeds the string it is handed rather than
    importing storage guidance.
    """
    _ = attached_to_experiment
    folder_label = "work folder"
    local_label = "local sandbox folder"
    if local_dir:
        local_destination = f"the {local_label} ({local_dir})"
    else:
        local_destination = "a caller-chosen path inside the local checkout"
    if storage_enabled:
        heavy_note = (
            f"{storage_hint} Upload those durable files with "
            "storage.upload_file instead of rsyncing them "
            "into the repo. "
        )
    else:
        heavy_note = (
            "Heavy-file storage is not enabled on this backend, so trained models, "
            "precious datasets, and multi-GB checkpoints cannot be durably retained "
            "through the storage tools. "
        )
    return (
        f"The sandbox {folder_label} is {remote_dir} ($MERV_EXPERIMENT_DIR). "
        "Work in it over SSH — scripts, results, report.md, graph.json. "
        "This sandbox is an EPHEMERAL SSH window: nothing is copied for you, and "
        "when it is released or reaped the VM and everything on it is destroyed. "
        "So pull anything you want to keep BEFORE then with "
        "sandbox.pull_outputs — it copies the light retained files into "
        f"{local_destination} without clobbering existing local files "
        "(kept files are reported as files_kept_stale; pass overwrite=true to "
        "replace them) — and then register them as resources. "
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
        "storage.upload_file for heavy ones"
        if storage_enabled
        else "no configured heavy-file storage is available"
    )
    return (
        f"Sandbox lifetime expires at {expires_at}. Before that deadline, pull "
        f"anything you need off the box (sandbox.pull_outputs for light files into the {local_label}, "
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
        # Empty on pre-multi-provider rows = the configured default backend.
        "provider": row.get("provider") or None,
        "instance_type": row.get("instance_type") or None,
        "region": row.get("region") or None,
        "public_key_source": row.get("public_key_source") or "managed",
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
    *, facts: dict[str, Any], enrichment: dict[str, Any], storage_hint: str = ""
) -> dict[str, Any]:
    """Compose the agent view from row facts + data-plane enrichment.

    ``enrichment`` carries ``command``/``raw_command``/``key_path``/
    ``local_dir`` from the worker (the conn file is already written for live
    rows). The hint prose is built here because it quotes both halves.
    ``storage_hint`` is the composition-injected storage rule of thumb.
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
            "Provisioning. A cloud sandbox commonly takes 5-15 minutes "
            "to boot and bootstrap. Poll "
            "sandbox.get every 30-60 seconds until status is running, then "
            "construct SSH from the returned host, port, and user facts plus "
            "the caller-owned private key. Do not re-call sandbox.request to "
            "poll. "
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
                "$MERV_EXPERIMENT_DIR (e.g. figures/*.png, results/*.json/csv) "
                "so sandbox.pull_outputs can bring them off and report.md can "
                "reference them. "
            )
        else:
            output_note = (
                "Save selected outputs under $MERV_EXPERIMENT_DIR so "
                "sandbox.pull_outputs can bring them off before release. "
            )
        if command:
            connection_note = (
                f"Run commands with the local convenience command: {command} "
                "'<your shell command>'. "
            )
            if raw_command:
                connection_note += (
                    f"The corresponding raw SSH prefix is: {raw_command}. "
                )
        else:
            connection_note = (
                "Connect with the caller-owned private key and the returned "
                "ssh.host, ssh.port, and ssh.user facts. Construct that SSH "
                "command locally; the brain does not know the private-key path. "
            )
        view["hint"] = (
            connection_note
            + "Commands should work inside the sandbox work folder. Use "
            "`merv_run <label> -- <command>` for long jobs, then inspect "
            "sandbox.runs rather than assuming a dropped SSH connection stopped "
            "the job. "
            + _folder_contract_note(
                remote_dir=remote_dir,
                local_dir=local_dir,
                attached_to_experiment=attached_to_experiment,
                storage_enabled=bool(view.get("storage_enabled")),
                storage_hint=storage_hint,
            )
            + "Before registering result resources, pull the files you need off "
            "the box with sandbox.pull_outputs (see the folder note) into the local "
            + "sandbox folder"
            + ", then register those local files. "
            + _expiry_note(
                view.get('expires_at'),
                attached_to_experiment=attached_to_experiment,
                storage_enabled=bool(view.get("storage_enabled")),
            )
            + credential_note
            + output_note
            + "If SSH connection facts go stale, call sandbox.get once to "
            "refresh the endpoint."
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
        "provider": row.get("provider") or None,
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
        "provider": row.get("provider") or "",
        "instance_type": row.get("instance_type") or "",
        "region": row.get("region") or "",
        "public_key_source": row.get("public_key_source") or "managed",
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
    providers = catalog.get("providers")
    view = {
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
            "region=<one of that option's regions>"
            + (
                "; multiple compute providers are configured, so also pass the "
                "chosen option's provider"
                if providers
                else ""
            )
            + "). Options are sorted cheapest-first"
            + (f"; cheapest available now is '{cheapest}'. " if cheapest else ". ")
            + "Call sandbox.options anytime to re-list current availability."
        ),
    }
    if providers:
        view["providers"] = providers
    return view
