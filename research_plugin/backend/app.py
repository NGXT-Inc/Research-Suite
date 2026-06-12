"""Application composition root and MCP tool facade."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError as PydanticValidationError

from .contracts import ContractModel, TOOL_CONTRACTS
from .dataplane import LocalDataPlaneWorker
from .utils import ResearchPluginError
from .utils import ValidationError as ToolValidationError
from .execution import SandboxBackend, build_sandbox_backend
from .execution.ssh_rsync import SshRsyncSyncer
from .workspace import LocalWorkspace
from .services import (
    ClaimService,
    ExperimentService,
    PermissionService,
    ProjectService,
    ResourceService,
    ReviewService,
    SandboxService,
    SynthesisService,
    WorkflowService,
)
from .services.sandbox_mgmt_keys import LocalMgmtKeyStore
from .state import ActivityLogger, BaseStateStore, StateStore, ToolCallStore, monotonic_ms
from .state.blobs import LocalDirBlobStore


@dataclass(frozen=True)
class ToolSpec:
    description: str
    input_model: type[ContractModel]
    handler: Callable[..., dict[str, Any]]

    def input_schema(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return schema

    def call(self, *, raw_arguments: dict[str, Any]) -> dict[str, Any]:
        request = self.input_model.model_validate(raw_arguments)
        return self.handler(**request.model_dump())


def _contract_error_message(*, exc: PydanticValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(part) for part in first.get("loc", ())) or "input"
    error_type = first.get("type")
    if error_type == "missing":
        return f"{loc} is required"
    if error_type == "extra_forbidden":
        return f"unexpected field: {loc}"
    return f"{loc}: {first.get('msg', 'invalid value')}"


def _assert_tool_contracts_match_handlers(
    *, handlers: dict[str, Callable[..., dict[str, Any]]]
) -> None:
    handler_names = set(handlers)
    contract_names = set(TOOL_CONTRACTS)
    if handler_names == contract_names:
        return
    missing_handlers = sorted(contract_names - handler_names)
    missing_contracts = sorted(handler_names - contract_names)
    raise AssertionError(
        "tool handler/contract mismatch"
        f"; missing handlers: {', '.join(missing_handlers) or 'none'}"
        f"; missing contracts: {', '.join(missing_contracts) or 'none'}"
    )


class ResearchPluginApp:
    """Composes isolated components behind tool-call contracts."""

    def __init__(
        self,
        *,
        repo_root: Path,
        db_path: Path,
        execution_backend: SandboxBackend | None = None,
        rsync_syncer: SshRsyncSyncer | None = None,
        store: BaseStateStore | None = None,
    ) -> None:
        # The plane seam (cloud plan Phase 3): the record store knows nothing
        # about the checkout; local paths flow from the workspace and every
        # local-IO duty routes through the data-plane worker. This constructor
        # IS the local-mode composition — it binds both planes in one process.
        self.workspace = LocalWorkspace(repo_root=repo_root)
        # Store injection (cloud plan Phase 6): the dual-dialect contract
        # tests hand in a PostgresStateStore; absent that, local mode builds
        # its SQLite store at db_path exactly as before. The control profile
        # (Phase 8) gets its own composition root rather than db_url plumbing
        # through this constructor.
        self.store = store if store is not None else StateStore(db_path=db_path)
        # Telemetry sinks are machine-local by construction: composition hands
        # them explicit paths (the control composition gets its own sinks).
        self.activity = ActivityLogger(repo_root=self.workspace.repo_root)
        # Full-fidelity tool-call recorder backing the debug analyzer. Isolated in
        # its own SQLite file so its churn never touches the state DB.
        self.tool_calls = ToolCallStore(
            db_path=self.workspace.research_dir / "tool_calls.sqlite"
        )
        self.permissions = PermissionService()
        # Content-addressed store for gated-artifact bytes (and, later, figures
        # and parachute objects). Local mode roots it next to the state DB.
        self.blobs = LocalDirBlobStore(root=self.workspace.research_dir / "blobs")
        if execution_backend is None:
            execution_backend = build_sandbox_backend(
                repo_root=self.workspace.repo_root,
                activity=self._activity_hook,
            )
        self.execution_backend = execution_backend
        self.worker = LocalDataPlaneWorker(
            workspace=self.workspace,
            backend=execution_backend,
            rsync_syncer=rsync_syncer,
        )
        self.projects = ProjectService(store=self.store)
        self.claims = ClaimService(store=self.store)
        self.experiments = ExperimentService(
            store=self.store,
            blobs=self.blobs,
            ensure_workspace=self.worker.ensure_workspace,
        )
        self.resources = ResourceService(
            store=self.store,
            permissions=self.permissions,
            workspace=self.workspace,
            blobs=self.blobs,
        )
        # One-time local upgrade: capture bytes for gated associations made
        # before byte capture existed (idempotent, skips present blobs).
        self.resources.backfill_gated_blobs()
        self.syntheses = SynthesisService(store=self.store, blobs=self.blobs)
        self.reviews = ReviewService(
            store=self.store,
            permissions=self.permissions,
            experiments=self.experiments,
            syntheses=self.syntheses,
            blobs=self.blobs,
        )
        self.sandboxes = SandboxService(
            store=self.store,
            sandbox_backend=execution_backend,
            worker=self.worker,
            activity=self.activity,
            experiments=self.experiments,
            # Per-sandbox management keys (plan Phase 5): control-plane
            # custody — local mode roots them under .research_plugin/ beside
            # the rest of the control state.
            mgmt_keys=LocalMgmtKeyStore(
                root=self.workspace.research_dir / "mgmt_keys"
            ),
            # Decision 7's one shared blob store also holds parachute objects.
            blobs=self.blobs,
        )
        self.workflow = WorkflowService(
            store=self.store,
            experiments=self.experiments,
            reviews=self.reviews,
            sandboxes=self.sandboxes,
            resources=self.resources,
            syntheses=self.syntheses,
        )
        handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "workflow.status_and_next": self.workflow.status_and_next_agent,
            "project.create": self.projects.create,
            "project.update": self.projects.update,
            "project.get": self.projects.get,
            "project.current": self.projects.current,
            "project.list": self.projects.list_projects,
            "claim.create": self.claims.create,
            "claim.list": self.claims.list_claims,
            "claim.update": self.claims.update,
            "experiment.create": self.experiments.create,
            "experiment.list": self.experiments.list_experiments_agent,
            "experiment.get_state": self.experiments.get_state_agent,
            "experiment.transition": self.experiments.transition,
            "synthesis.create": self.syntheses.create,
            "synthesis.get": self.syntheses.get_state,
            "synthesis.list": self.syntheses.list_syntheses,
            "synthesis.transition": self.syntheses.transition,
            "resource.register_file": self.resources.register_file,
            "resource.associate": self.resources.associate,
            "resource.delete": self.resources.delete,
            "resource.list": self.resources.list_resources,
            "resource.resolve": self.resources.resolve,
            "review.request": self.reviews.request,
            "review.start": self.reviews.start,
            "review.submit": self.reviews.submit,
            "review.status": self.reviews.status,
            "sandbox.request": self.sandboxes.request,
            "sandbox.options": self.sandboxes.options,
            "sandbox.get": self.sandboxes.get,
            "sandbox.sync": self.sandboxes.sync,
            "sandbox.list": self.sandboxes.list_sandboxes,
            "sandbox.release": self.sandboxes.release,
            "sandbox.terminal": self.sandboxes.terminal,
            "sandbox.health": self.sandboxes.health,
        }
        _assert_tool_contracts_match_handlers(handlers=handlers)
        self._tools = {
            name: ToolSpec(contract.description, contract.input_model, handlers[name])
            for name, contract in TOOL_CONTRACTS.items()
        }

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "description": spec.description, "inputSchema": spec.input_schema()}
            for name, spec in self._tools.items()
        ]

    def shutdown(self) -> None:
        """Best-effort: stop background provisioning jobs and the sync poller."""
        try:
            self.sandboxes.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.execution_backend.shutdown()
        except Exception:  # noqa: BLE001
            pass

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        activity_source: str = "app",
    ) -> dict[str, Any]:
        arguments = arguments or {}
        started = monotonic_ms()
        try:
            if name not in self._tools:
                raise ResearchPluginError(f"unknown tool: {name}", details={"tool": name})
            self.permissions.reject_reviewer_mutation(
                tool_name=name,
                review_session_id=arguments.get("review_session_id"),
            )
            try:
                result = self._tools[name].call(raw_arguments=arguments)
            except PydanticValidationError as exc:
                raise ToolValidationError(
                    _contract_error_message(exc=exc),
                    details={"tool": name, "errors": exc.errors()},
                ) from exc
            duration_ms = monotonic_ms() - started
            self.activity.tool_ok(
                source=activity_source,
                tool=name,
                arguments=arguments,
                duration_ms=duration_ms,
                result=result,
            )
            self.tool_calls.record(
                tool=name,
                source=activity_source,
                status="ok",
                duration_ms=duration_ms,
                arguments=arguments,
                result=result,
            )
            return result
        except ResearchPluginError as exc:
            duration_ms = monotonic_ms() - started
            self.activity.tool_error(
                source=activity_source,
                tool=name,
                arguments=arguments,
                duration_ms=duration_ms,
                error=exc.message,
                error_code=exc.error_code,
            )
            self.tool_calls.record(
                tool=name,
                source=activity_source,
                status="error",
                duration_ms=duration_ms,
                arguments=arguments,
                error=exc.message,
                error_code=exc.error_code,
            )
            raise
        except Exception as exc:
            duration_ms = monotonic_ms() - started
            self.activity.tool_error(
                source=activity_source,
                tool=name,
                arguments=arguments,
                duration_ms=duration_ms,
                error=str(exc),
                error_code="unexpected",
            )
            self.tool_calls.record(
                tool=name,
                source=activity_source,
                status="error",
                duration_ms=duration_ms,
                arguments=arguments,
                error=str(exc),
                error_code="unexpected",
            )
            raise

    def _activity_hook(self, event_type: str, payload: dict[str, Any]) -> None:
        """Bridge backend emit-style logging and ActivityLogger."""
        try:
            self.activity.emit(event_type=event_type, payload=payload)
        except Exception:  # noqa: BLE001
            pass
