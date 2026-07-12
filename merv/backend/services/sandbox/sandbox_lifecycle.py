"""Single owner of sandbox status transitions and destructive decisions.

Every path that terminates a provider VM or drives a row to a terminal
status routes through `SandboxLifecycle` — the reaper, release, reconcile,
and the provisioner's settle paths. Concentrating that authority here keeps
the invariants in one place:

  - provider-API errors are never read as "instance gone" (tri-state
    `liveness`); a row is stranded as terminated only once the provider
    confirms the VM is not alive — a terminated row over a live VM bills
    invisibly forever, and no sweep revisits terminated rows;
  - a terminal mark always runs teardown (mgmt-key removal + the data-plane
    teardown task), regardless of which caller marked it;
  - a live provisioning job owns its row at any age — only the lifecycle's
    job probe decides whether "provisioning" means in-flight or wedged.

The registry stays persistence-only; the provisioner keeps job threads; the
daemons keep scheduling. None of them decide life or death.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable

from ...ports.mgmt_keys import MgmtKeyStore
from ...ports.task_channel import TaskChannel
from ...sandbox.sandbox_backend import SandboxBackend
from ...sandbox.sandbox_support import ACTIVE_SANDBOX_STATUSES, parse_iso
from .sandbox_registry import SandboxRegistry


# Probe for an in-process provisioning job thread; wired to
# SandboxProvisioner.job_is_live by the facade after both exist.
JobProbe = Callable[..., bool]


class SandboxLifecycle:
    """Owns liveness policy, terminal transitions, and VM termination."""

    def __init__(
        self,
        *,
        registry: SandboxRegistry,
        backend: SandboxBackend,
        mgmt_keys: MgmtKeyStore,
        tasks: TaskChannel,
    ) -> None:
        self.registry = registry
        self.backend = backend
        self.mgmt_keys = mgmt_keys
        self.tasks = tasks
        self.job_probe: JobProbe | None = None

    # ---------- liveness ----------

    def liveness(self, *, sandbox_id: str) -> bool | None:
        """Tri-state liveness: True/False when the provider answered
        authoritatively, None when it couldn't be asked (outage, timeout).

        Callers making destructive decisions (terminate, mark_terminated,
        re-provision) must treat None as "possibly alive" — collapsing it to
        False is how a healthy VM ends up killed or stranded behind a
        terminated row, billing invisibly.
        """
        try:
            return bool(self.backend.is_alive(sandbox_id=sandbox_id))
        except Exception:  # noqa: BLE001
            return None

    def _job_is_live(self, *, experiment_id: str, sandbox_uid: str) -> bool:
        if self.job_probe is None:
            return False
        try:
            return bool(
                self.job_probe(experiment_id=experiment_id, sandbox_uid=sandbox_uid)
            )
        except Exception:  # noqa: BLE001 — a probe failure must not kill a row
            return False

    # ---------- terminal transitions (mark + teardown, one owner) ----------

    def mark_terminated(self, *, experiment_id: str, sandbox_uid: str) -> None:
        facts = self.registry.mark_terminated(
            experiment_id=experiment_id, sandbox_uid=sandbox_uid
        )
        self._teardown(experiment_id=experiment_id, facts=facts)

    def mark_failed(
        self, *, experiment_id: str, error: str, sandbox_uid: str
    ) -> None:
        facts = self.registry.mark_failed(
            experiment_id=experiment_id, error=error, sandbox_uid=sandbox_uid
        )
        self._teardown(experiment_id=experiment_id, facts=facts)

    def _teardown(self, *, experiment_id: str, facts: dict[str, Any]) -> None:
        """Tear down a terminal row's runtime attachments.

        The management keypair dies with the sandbox (per-sandbox keys):
        control-side custody, dropped here. Conn files and tunnels are
        data-plane property, released via a ``teardown`` task — in split mode
        the daemon executes it from its task loop. ``sandbox_id`` is None when
        the row itself was missing; the task then skips tunnel teardown but
        still drops the conn file. All best-effort: teardown must never block
        or abort the terminal mark.
        """
        sandbox_uid = str(facts.get("sandbox_uid") or "")
        if sandbox_uid:
            try:
                self.mgmt_keys.remove(sandbox_uid=sandbox_uid)
            except Exception:  # noqa: BLE001 — key cleanup must never block the mark
                pass
        try:
            self.tasks.submit(
                task_type="teardown",
                payload={
                    "experiment_id": experiment_id,
                    "sandbox_id": facts.get("sandbox_id"),
                    "sandbox_uid": sandbox_uid,
                    "remove_experiment_alias": True,
                },
                tenant_id=self.registry.tenant_for_sandbox(
                    experiment_id=experiment_id, sandbox_uid=sandbox_uid
                ),
            )
        except Exception:  # noqa: BLE001 — best-effort; never block the mark
            pass

    # ---------- VM termination ----------

    def terminate_quietly(self, *, sandbox_id: str) -> None:
        try:
            self.backend.terminate(sandbox_id=sandbox_id)
        except Exception:  # noqa: BLE001
            pass

    def cleanup_orphan(
        self, *, experiment_id: str, row: dict[str, Any] | None
    ) -> None:
        """Best-effort terminate any sandbox tied to this experiment.

        Covers both a recorded sandbox_id (from a prior/failed row) and the
        deterministic-named orphan a dead job may have left on the backend.
        """
        seen: set[str] = set()
        sid = (row or {}).get("sandbox_id")
        if sid:
            seen.add(str(sid))
            self.terminate_quietly(sandbox_id=str(sid))
        if not sid:
            sandbox_uid = str((row or {}).get("sandbox_uid") or "")
            active_sibling = bool(
                experiment_id
                and sandbox_uid
                and self.registry.has_active_for_experiment(
                    experiment_id=experiment_id, exclude_sandbox_uid=sandbox_uid
                )
            )
            lookup_uids: list[str] = []
            if sandbox_uid:
                lookup_uids.append(sandbox_uid)
            # Legacy fallback: old providers may only be findable by the
            # experiment-derived deterministic name. Skip that broad lookup
            # while another live sandbox is attached to the same experiment.
            if not active_sibling:
                lookup_uids.append("")
            if not lookup_uids:
                lookup_uids.append("")
            orphan = None
            for lookup_uid in lookup_uids:
                if orphan:
                    break
                try:
                    orphan = self.backend.find_sandbox_id(
                        experiment_id=experiment_id, sandbox_uid=lookup_uid
                    )
                except Exception:  # noqa: BLE001
                    orphan = None
            if orphan and str(orphan) not in seen:
                self.terminate_quietly(sandbox_id=str(orphan))

    def terminate_vm(self, *, row: dict[str, Any], try_direct: bool = True) -> str:
        """Terminate the provider VM behind a row. Returns:

        - ``"stopped"`` — the provider confirmed the terminate;
        - ``"gone"`` — terminate failed/skipped but the provider says the VM
          is not alive (or there was never an id to ask about);
        - ``"maybe_alive"`` — terminate failed and the VM may still be up (or
          the provider could not be asked): the caller must NOT mark the row
          terminal, so a later pass retries instead of stranding a billing VM.
        """
        sandbox_id = str(row.get("sandbox_id") or "")
        experiment_id = str(row.get("experiment_id") or "")
        stopped = False
        if sandbox_id and try_direct:
            try:
                stopped = self.backend.terminate(sandbox_id=sandbox_id)
            except Exception:  # noqa: BLE001
                stopped = False
        if stopped:
            return "stopped"
        # Direct terminate failed or there was no recorded id: try the
        # deterministic-name orphan cleanup path, then require confirmation.
        self.cleanup_orphan(experiment_id=experiment_id, row=row)
        if sandbox_id and self.liveness(sandbox_id=sandbox_id) is not False:
            return "maybe_alive"
        return "gone"

    # ---------- reconcile ----------

    def reconcile(self, *, row: dict[str, Any]) -> dict[str, Any]:
        """Bring a row in line with reality. Read-only-safe (never provisions).

        - running → confirm liveness; mark terminated if the sandbox is gone;
          refresh the SSH endpoint if it moved.
        - provisioning → if a live job in this process owns it, leave it for
          the agent to keep polling (a live job owns the row at ANY age —
          Lambda boots legitimately run past the stale deadline); otherwise
          the job is gone (daemon restart) or wedged, so clean up any orphan
          and mark failed. This is what guarantees a polling agent always
          reaches a terminal state.
        """
        status = row.get("status")
        exp = str(row.get("experiment_id") or "")
        sandbox_uid = str(row.get("sandbox_uid") or "")
        if status in ACTIVE_SANDBOX_STATUSES and row.get("sandbox_id"):
            alive = self.liveness(sandbox_id=str(row["sandbox_id"]))
            if alive is None:
                return row  # provider unreachable — defer judgment to the next poll
            if alive:
                self.registry.touch_alive(experiment_id=exp, sandbox_uid=sandbox_uid)
                return self.refresh_endpoint(
                    row=self.registry.get_by_uid(sandbox_uid=sandbox_uid)
                )
            self.mark_terminated(experiment_id=exp, sandbox_uid=sandbox_uid)
            self.registry.emit_event(
                project_id=str(row["project_id"]),
                event_type="sandbox.expired",
                experiment_id=exp,
                payload={
                    "sandbox_id": row.get("sandbox_id", ""),
                    "sandbox_uid": sandbox_uid,
                },
            )
            return self.registry.get_by_uid(sandbox_uid=sandbox_uid)
        if status == "provisioning":
            if self._job_is_live(experiment_id=exp, sandbox_uid=sandbox_uid):
                return row  # genuinely in flight — keep polling
            # The job may have JUST settled; re-read before declaring failure.
            fresh = self.registry.get_by_uid(sandbox_uid=sandbox_uid)
            if fresh.get("status") != "provisioning":
                return self.reconcile(row=fresh)
            self.cleanup_orphan(experiment_id=exp, row=fresh)
            self.mark_failed(
                experiment_id=exp,
                error="provisioning interrupted; call sandbox.request again",
                sandbox_uid=sandbox_uid,
            )
            self.registry.emit_event(
                project_id=str(row["project_id"]),
                event_type="sandbox.failed",
                experiment_id=exp,
                payload={"error": "provisioning interrupted"},
            )
            return self.registry.get_by_uid(sandbox_uid=sandbox_uid)
        return row

    def refresh_endpoint(self, *, row: dict[str, Any]) -> dict[str, Any]:
        """Re-read a live sandbox's SSH tunnel and persist it if it moved.

        Recovers the "sandbox alive ≠ tunnel endpoint still current" case
        (e.g. Modal relocates a sandbox): the new host/port is written back so
        the agent view + conn file hand out a working command.

        Strictly best-effort. A failure here — including a transient *local*
        resolver outage hitting the Modal control plane, the very thing the
        sbx dispatcher's retry/keepalive already absorbs — leaves the stored
        endpoint untouched and never breaks request/get. Only ``running`` rows
        with a sandbox id are probed.
        """
        sandbox_id = str(row.get("sandbox_id") or "")
        if not sandbox_id or row.get("status") not in ACTIVE_SANDBOX_STATUSES:
            return row
        try:
            endpoint = self.backend.refresh_ssh_endpoint(sandbox_id=sandbox_id)
        except Exception:  # noqa: BLE001 — refresh must never break the caller
            endpoint = None
        if not endpoint:
            return row
        host, port = str(endpoint[0] or ""), int(endpoint[1] or 0)
        if not host or not port:
            return row
        if host == str(row.get("ssh_host") or "") and port == int(row.get("ssh_port") or 0):
            return row  # unchanged — the common case; avoid a needless write
        experiment_id = str(row.get("experiment_id") or "")
        sandbox_uid = str(row.get("sandbox_uid") or "")
        if not sandbox_uid:
            return row
        self.registry.upsert(
            experiment_id=experiment_id,
            sandbox_uid=sandbox_uid,
            ssh_host=host,
            ssh_port=port,
        )
        fresh = self.registry.get_by_uid(sandbox_uid=sandbox_uid)
        # The agent's conn file must follow the endpoint: a conn_refresh task
        # re-renders it through the data plane. Best-effort, like the refresh
        # itself — the next agent view re-renders it anyway.
        try:
            self.tasks.submit(
                task_type="conn_refresh",
                payload={
                    "row": fresh,
                    "name": f"sandbox-{sandbox_uid[:12]}",
                    "use_sandbox_uid_command": True,
                },
                tenant_id=self.registry.tenant_for_project(
                    project_id=str(fresh.get("project_id") or "")
                ),
            )
        except Exception:  # noqa: BLE001 — refresh must never break the caller
            pass
        self.registry.emit_event(
            project_id=str(row.get("project_id")),
            event_type="sandbox.endpoint_refreshed",
            experiment_id=experiment_id,
            payload={"ssh_host": host, "ssh_port": port},
        )
        return fresh

    # ---------- reaping ----------

    def reap_expired(self, *, now: datetime | None = None) -> int:
        """Terminate every running sandbox whose expires_at deadline has passed.

        Idempotent and safe to call directly (tests do). Returns how many were
        reaped.
        """
        now_dt = now or datetime.now(tz=UTC)
        reaped = 0
        for row in self.registry.list_running_rows():
            expires_at = parse_iso(row.get("expires_at"))
            if expires_at is None or now_dt < expires_at:
                continue
            # Re-read: the sweep snapshot ages while earlier rows terminate
            # (provider calls take seconds each), and sandbox.extend races
            # exactly this window — a just-extended row must not be reaped
            # off the stale copy.
            fresh = self.registry.get_by_uid(
                sandbox_uid=str(row.get("sandbox_uid") or "")
            )
            fresh_expires = parse_iso(fresh.get("expires_at"))
            if (
                fresh.get("status") != "running"
                or fresh_expires is None
                or now_dt < fresh_expires
            ):
                continue
            if self.reap_row(row=fresh):
                reaped += 1
        return reaped

    def reap_row(
        self,
        *,
        row: dict[str, Any],
        event_type: str = "sandbox.expired",
        payload_extra: dict[str, Any] | None = None,
    ) -> bool:
        """Terminate + mark one row (expiry and idle reaping share this).

        Returns False — leaving the row running so the next sweep retries —
        when the VM could not be confirmed gone.
        """
        experiment_id = str(row.get("experiment_id") or "")
        sandbox_id = str(row.get("sandbox_id") or "")
        sandbox_uid = str(row.get("sandbox_uid") or "")
        outcome = self.terminate_vm(row=row)
        if outcome == "maybe_alive":
            self.registry.emit_event(
                project_id=str(row.get("project_id")),
                event_type=event_type,
                experiment_id=experiment_id,
                payload={
                    "sandbox_id": sandbox_id,
                    "sandbox_uid": sandbox_uid,
                    "reaped": False,
                    "reason": "terminate failed; instance may still be alive",
                    **(payload_extra or {}),
                },
            )
            return False
        self.mark_terminated(experiment_id=experiment_id, sandbox_uid=sandbox_uid)
        payload = {
            "sandbox_id": sandbox_id,
            "sandbox_uid": sandbox_uid,
            "reaped": True,
            "expires_at": row.get("expires_at"),
            "stopped": outcome == "stopped",
        }
        if payload_extra:
            payload.update(payload_extra)
        self.registry.emit_event(
            project_id=str(row.get("project_id")),
            event_type=event_type,
            experiment_id=experiment_id,
            payload=payload,
        )
        return True
