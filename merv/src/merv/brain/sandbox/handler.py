"""Shared projections and provider-neutral helpers for Sandbox handlers."""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Any

from ..kernel.state.store import Connection
from ..kernel.utils import ValidationError
from . import sandbox_views
from .sandbox_backend import BackendCapabilities, BackendValidationError


class SandboxHandler:
    """Give cohesive handlers access to one facade-owned collaborator set."""

    def __init__(self, host: Any) -> None:
        self._host = host

    def __getattr__(self, name: str) -> Any:
        return getattr(self._host, name)

    def _deliver_secrets_once(self, *, row: dict[str, Any], experiment_id: str) -> None:
        uid = str(row.get("sandbox_uid") or "")
        if not uid or row.get("status") != "running" or uid in self._secrets_delivered:
            return
        self._deliver_secrets(row=row, experiment_id=experiment_id)
        self._secrets_delivered.add(uid)

    def _deliver_secrets(self, *, row: dict[str, Any], experiment_id: str) -> None:
        if row.get("status") != "running":
            return
        sandbox_id = str(row.get("sandbox_id") or "")
        if not sandbox_id:
            return
        try:
            secrets = self.backend.sandbox_secrets()
        except Exception:
            secrets = {}
        if not secrets:
            return
        with suppress(Exception):
            self.backend.write_secrets(
                sandbox_id=sandbox_id,
                secrets=secrets,
                ssh_host=str(row.get("ssh_host") or ""),
                ssh_port=int(row.get("ssh_port") or 0),
                key_path=str(self._mgmt_key_path(row=row)),
            )

    def _mgmt_key_path(self, *, row: dict[str, Any]) -> Path:
        return self.mgmt_keys.key_path(sandbox_uid=str(row.get("sandbox_uid") or ""))

    def _agent_result(
        self,
        *,
        row: dict[str, Any],
        reused: bool | None,
        include_data_plane_enrichment: bool,
        use_sandbox_uid_command: bool = True,
    ) -> dict[str, Any]:
        row = self._with_active_experiment_ids(row=row)
        view = (
            self._agent_view(
                row=row, reused=reused, use_sandbox_uid_command=use_sandbox_uid_command
            )
            if include_data_plane_enrichment
            else self._agent_facts(row=row, reused=reused)
        )
        return self._with_runs_nudge(
            view=view, sandbox_uid=str(row.get("sandbox_uid") or "")
        )

    def _with_runs_nudge(
        self, *, view: dict[str, Any], sandbox_uid: str
    ) -> dict[str, Any]:
        if not sandbox_uid:
            return view
        try:
            nudge = self.runs_ledger.nudge_line(sandbox_uid=sandbox_uid)
        except Exception:
            return view
        if nudge:
            view["runs"] = nudge
        return view

    def _agent_facts(
        self, *, row: dict[str, Any], reused: bool | None
    ) -> dict[str, Any]:
        return sandbox_views.agent_row_facts(
            row=row,
            env_info=self._sandbox_environment(),
            reused=reused,
            storage_enabled=self.storage_enabled,
        )

    def _agent_view(
        self,
        *,
        row: dict[str, Any],
        reused: bool | None,
        use_sandbox_uid_command: bool = True,
    ) -> dict[str, Any]:
        sandbox_uid = str(row.get("sandbox_uid") or "")
        view_name = f"sandbox-{sandbox_uid[:12]}"
        facts = self._agent_facts(row=row, reused=reused)
        enrichment = self.worker.sandbox_enrichment(
            row=row, name=view_name, use_sandbox_uid_command=use_sandbox_uid_command
        )
        return sandbox_views.merge_agent_view(
            facts=facts, enrichment=enrichment, storage_hint=self.storage_hint
        )

    def _agent_summary(self, *, row: dict[str, Any]) -> dict[str, Any]:
        return sandbox_views.agent_summary(
            row=self._with_active_experiment_ids(row=row)
        )

    def _row_view(
        self, *, row: dict[str, Any], conn: Connection | None = None
    ) -> dict[str, Any]:
        row = self._with_active_experiment_ids(row=row)
        sandbox_uid = str(row.get("sandbox_uid") or "")
        local_key = sandbox_uid
        local_name = f"sandbox-{sandbox_uid[:12]}"
        return sandbox_views.sandbox_row_view(
            row=row,
            local_sync_dir=str(
                self.worker.local_experiment_dir(
                    experiment_id=local_key, name=local_name
                )
            ),
        )

    def _active_experiment_ids_for_row(self, *, row: dict[str, Any]) -> list[str]:
        raw = row.get("active_experiment_ids")
        if isinstance(raw, list):
            return [str(item) for item in raw if str(item)]
        sandbox_uid = str(row.get("sandbox_uid") or "")
        if not sandbox_uid:
            return []
        return self.registry.active_experiment_ids(sandbox_uid=sandbox_uid)

    def _with_active_experiment_ids(self, *, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        active = self._active_experiment_ids_for_row(row=row)
        out["active_experiment_ids"] = active
        if not out.get("experiment_id") and active:
            out["experiment_id"] = active[0]
        return out

    def _capabilities_for(self, *, provider: str | None) -> BackendCapabilities:
        try:
            return self.backend.capabilities_for(provider=provider)
        except BackendValidationError as exc:
            raise ValidationError(str(exc)) from exc

    def _price_for_instance(
        self,
        *,
        instance_type: str | None,
        region: str | None,
        provider: str | None = None,
    ) -> float | None:
        if not instance_type:
            return None
        try:
            catalog = self.backend.hardware_catalog(region=region)
        except Exception:
            return None
        if not catalog:
            return None
        for option in catalog.get("options", []) or []:
            if str(option.get("instance_type") or "") != instance_type:
                continue
            tagged = str(option.get("provider") or "")
            if provider and tagged and (tagged != provider):
                continue
            price = option.get("price_usd_per_hour")
            return float(price) if price is not None else None
        return None

    def _hardware_catalog(
        self, *, gpu: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        catalog = self.backend.hardware_catalog(gpu=gpu, region=region)
        if catalog is None:
            return {
                "provider": self.backend.capabilities.name,
                "selection_required": False,
                "options": [],
                "regions": [],
            }
        return catalog

    def _sandbox_environment(self) -> dict[str, Any]:
        try:
            result = self.backend.sandbox_environment()
        except Exception:
            return {"available_tokens": [], "notes": []}
        if not isinstance(result, dict):
            return {"available_tokens": [], "notes": []}
        tokens = [
            str(token)
            for token in result.get("available_tokens", [])
            if isinstance(token, str) and token
        ]
        notes = [
            str(note)
            for note in result.get("notes", [])
            if isinstance(note, str) and note
        ]
        return {"available_tokens": tokens, "notes": notes}


__all__ = ["SandboxHandler"]
