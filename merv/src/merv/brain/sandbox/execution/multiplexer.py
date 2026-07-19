"""Fan one SandboxBackend port out over several configured providers.

`MultiplexingSandboxBackend` itself implements the `SandboxBackend` Protocol,
so services keep exactly one backend reference. Routing is by data value only:

  - ``acquire`` routes on ``request.provider`` (None = the default backend)
    and returns ids prefixed ``"<provider>:<native_id>"``.
  - Every id-addressed operation decodes that prefix and dispatches to the
    owning backend. Un-prefixed ids (rows that predate the multiplexer) route
    to the default backend; an id whose prefix is not a configured provider
    RAISES instead of answering — asking the wrong provider about an id would
    404 into "gone" and get a live, billing VM marked terminated.

Single-backend deployments never construct this class (see
``build_sandbox_backend``), so their ids stay un-prefixed and behavior is
byte-identical.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from ..sandbox_backend import (
    BackendCapabilities,
    BackendUnavailableError,
    BackendValidationError,
    OnCreated,
    OnPhase,
    ProvisionedSandbox,
    SandboxBackend,
    SandboxBackendBase,
    SandboxRequest,
    TranscriptTail,
)


class MultiplexingSandboxBackend(SandboxBackendBase):
    """Route one provider-neutral port across several provider backends."""

    def __init__(
        self,
        *,
        backends: dict[str, SandboxBackend],
        default: str,
        aliases: Mapping[str, str] | None = None,
    ) -> None:
        if not backends:
            raise BackendValidationError("multiplexer requires at least one backend")
        if default not in backends:
            raise BackendValidationError(f"default backend is not configured: {default}")
        self.backends = dict(backends)
        self.default = default
        self._aliases = dict(aliases or {})
        # Mirror the default backend so top-level fields (options() "backend",
        # catalog "provider") keep meaning "the default"; enforce_expiry is the
        # conservative union — billing protection wins over any one backend.
        base = self.backends[default].capabilities
        self.capabilities = BackendCapabilities(
            name=base.name,
            enforce_expiry=any(
                backend.capabilities.enforce_expiry for backend in self.backends.values()
            ),
            lifetime_extension_supported=base.lifetime_extension_supported,
            requires_hardware_selection=base.requires_hardware_selection,
            configurable_resources=base.configurable_resources,
        )

    # ---------- routing ----------

    def _resolve_provider(self, provider: str | None) -> str:
        requested = (provider or "").strip().lower()
        name = self._aliases.get(requested, requested)
        if not name:
            return self.default
        if name not in self.backends:
            configured = ", ".join(sorted(self.backends))
            raise BackendValidationError(
                f"unknown sandbox provider: {provider}. Configured providers: {configured}."
            )
        return name

    def _decode(self, sandbox_id: str) -> tuple[SandboxBackend, str]:
        """Owning backend + native id for a stored sandbox id.

        No ':' = a legacy id from before the multiplexer — the default
        backend's. A prefix that is not a configured provider raises (the
        provider may have been removed from the config): no backend can answer
        authoritatively for it, and a guessed answer risks a live VM.
        """
        prefix, sep, native = sandbox_id.partition(":")
        if not sep:
            return self.backends[self.default], sandbox_id
        backend = self.backends.get(prefix)
        if backend is None:
            raise BackendUnavailableError(
                f"sandbox id {sandbox_id!r} belongs to provider {prefix!r}, "
                "which is not configured in MERV_EXECUTION_BACKENDS"
            )
        return backend, native

    def _encode(self, provider: str, native_id: str) -> str:
        return f"{provider}:{native_id}" if native_id else native_id

    # ---------- capabilities ----------

    def capabilities_for(self, *, provider: str | None = None) -> BackendCapabilities:
        return self.backends[self._resolve_provider(provider)].capabilities

    # ---------- provisioning ----------

    def acquire(
        self,
        *,
        request: SandboxRequest,
        on_phase: OnPhase | None = None,
        on_created: OnCreated | None = None,
    ) -> ProvisionedSandbox:
        name = self._resolve_provider(request.provider)
        backend = self.backends[name]

        def prefixed_on_created(sandbox_id: str, sandbox_name: str) -> None:
            # The id is persisted the instant it exists (orphan recovery); it
            # must already carry its owner so cleanup routes correctly.
            if on_created is not None:
                on_created(self._encode(name, sandbox_id), sandbox_name)

        provisioned = backend.acquire(
            request=request,
            on_phase=on_phase,
            on_created=prefixed_on_created if on_created is not None else None,
        )
        return replace(
            provisioned, sandbox_id=self._encode(name, provisioned.sandbox_id)
        )

    # ---------- id-addressed operations ----------

    def is_alive(self, *, sandbox_id: str) -> bool:
        backend, native = self._decode(sandbox_id)
        return backend.is_alive(sandbox_id=native)

    def terminate(self, *, sandbox_id: str) -> bool:
        backend, native = self._decode(sandbox_id)
        return backend.terminate(sandbox_id=native)

    def read_transcript(
        self,
        *,
        sandbox_id: str,
        experiment_id: str,
        volume_name: str,
        workdir: str,
        tail: int | None = None,
        ssh_host: str = "",
        ssh_port: int = 0,
        ssh_user: str = "",
        key_path: str = "",
    ) -> TranscriptTail:
        backend, native = self._decode(sandbox_id)
        return backend.read_transcript(
            sandbox_id=native,
            experiment_id=experiment_id,
            volume_name=volume_name,
            workdir=workdir,
            tail=tail,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            key_path=key_path,
        )

    def sample_metrics(
        self,
        *,
        sandbox_id: str,
        ssh_host: str = "",
        ssh_port: int = 0,
        ssh_user: str = "",
        key_path: str = "",
    ) -> dict[str, Any] | None:
        backend, native = self._decode(sandbox_id)
        return backend.sample_metrics(
            sandbox_id=native,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            key_path=key_path,
        )

    def read_runs(
        self,
        *,
        sandbox_id: str,
        workdir: str,
        ssh_host: str = "",
        ssh_port: int = 0,
        ssh_user: str = "",
        key_path: str = "",
    ) -> list[dict[str, Any]] | None:
        backend, native = self._decode(sandbox_id)
        return backend.read_runs(
            sandbox_id=native,
            workdir=workdir,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            key_path=key_path,
        )

    def refresh_ssh_endpoint(self, *, sandbox_id: str) -> tuple[str, int] | None:
        backend, native = self._decode(sandbox_id)
        return backend.refresh_ssh_endpoint(sandbox_id=native)

    def write_secrets(
        self,
        *,
        sandbox_id: str,
        secrets: Mapping[str, str],
        ssh_host: str = "",
        ssh_port: int = 0,
        key_path: str = "",
    ) -> bool:
        backend, native = self._decode(sandbox_id)
        return backend.write_secrets(
            sandbox_id=native,
            secrets=secrets,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            key_path=key_path,
        )

    # ---------- fleet-wide operations ----------

    def find_sandbox_id(
        self, *, experiment_id: str, sandbox_uid: str = ""
    ) -> str | None:
        """First provider that recognizes the experiment wins, prefix attached.

        Orphans can live on any configured provider; deterministic sandbox
        names are unique across them, so the first hit is the only hit.
        """
        for name, backend in self.backends.items():
            try:
                found = backend.find_sandbox_id(
                    experiment_id=experiment_id, sandbox_uid=sandbox_uid
                )
            except Exception:  # noqa: BLE001 — one provider outage must not mask the rest
                continue
            if found:
                return self._encode(name, str(found))
        return None

    def hardware_catalog(
        self, *, gpu: str | None = None, region: str | None = None
    ) -> dict[str, Any] | None:
        """One merged, cheapest-first menu; every option tagged with its provider.

        Top-level ``provider`` keeps its legacy meaning (the default backend);
        ``providers`` lists everything configured. A chosen option's
        ``provider`` is what the agent passes back on ``sandbox.request``.
        """
        merged: list[dict[str, Any]] = []
        regions: set[str] = set()
        catalogs: dict[str, dict[str, Any]] = {}
        for name, backend in self.backends.items():
            try:
                catalog = backend.hardware_catalog(gpu=gpu, region=region)
            except Exception:  # noqa: BLE001 — one provider outage must not empty the menu
                continue
            if not catalog:
                continue
            catalogs[name] = catalog
            regions.update(str(r) for r in catalog.get("regions", []) or [])
            for option in catalog.get("options", []) or []:
                merged.append({**option, "provider": name})
        if not catalogs:
            return None
        merged.sort(
            key=lambda o: (
                float(o.get("price_usd_per_hour") or 0.0),
                str(o.get("instance_type") or ""),
            )
        )
        base = catalogs.get(self.default, {})
        return {
            "provider": self.capabilities.name,
            "providers": sorted(catalogs),
            "selection_required": any(
                bool(c.get("selection_required")) for c in catalogs.values()
            ),
            "select_with": base.get("select_with") or "instance_type",
            "reason": (
                "Several compute providers are configured; each options[] entry "
                "carries the provider that serves it. Pass that provider (and "
                "its instance_type) back on sandbox.request."
            ),
            "regions": sorted(regions),
            "count": len(merged),
            "options": merged,
        }

    def sandbox_environment(self) -> dict:
        tokens: list[str] = []
        notes: list[str] = []
        for backend in self.backends.values():
            try:
                env = backend.sandbox_environment()
            except Exception:  # noqa: BLE001
                continue
            tokens.extend(t for t in env.get("available_tokens", []) if t not in tokens)
            notes.extend(n for n in env.get("notes", []) if n not in notes)
        return {"available_tokens": tokens, "notes": notes}

    def sandbox_secrets(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for backend in self.backends.values():
            try:
                merged.update(backend.sandbox_secrets())
            except Exception:  # noqa: BLE001
                continue
        return merged

    def health(self) -> dict:
        reports = {}
        for name, backend in self.backends.items():
            try:
                reports[name] = backend.health()
            except Exception as exc:  # noqa: BLE001
                reports[name] = {"ok": False, "error": str(exc)}
        failing = sorted(
            name for name, report in reports.items() if not report.get("ok")
        )
        result: dict[str, Any] = {
            "ok": not failing,
            "backend": self.capabilities.name,
            "backends": reports,
        }
        if failing:
            result["error"] = "; ".join(
                f"{name}: {reports[name].get('error') or 'unhealthy'}" for name in failing
            )
        return result

    def shutdown(self) -> None:
        for backend in self.backends.values():
            try:
                backend.shutdown()
            except Exception:  # noqa: BLE001
                continue


__all__ = ["MultiplexingSandboxBackend"]
