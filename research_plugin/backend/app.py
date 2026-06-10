"""Application composition root and MCP tool facade."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError as PydanticValidationError

from .contracts import ContractModel, TOOL_INPUT_MODELS
from .utils import ResearchPluginError
from .utils import ValidationError as ToolValidationError
from .execution import SandboxBackend, build_sandbox_backend
from .execution.ssh_rsync import SshRsyncSyncer
from .services import (
    ClaimService,
    ComputeService,
    ExperimentService,
    PermissionService,
    ProjectService,
    ResourceService,
    ReviewService,
    SandboxService,
    WorkflowService,
)
from .state import ActivityLogger, StateStore, ToolCallStore, monotonic_ms


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


class ResearchPluginApp:
    """Composes isolated components behind tool-call contracts."""

    def __init__(
        self,
        *,
        repo_root: Path,
        db_path: Path,
        execution_backend: SandboxBackend | None = None,
        compute_service: ComputeService | None = None,
        rsync_syncer: SshRsyncSyncer | None = None,
    ) -> None:
        self.store = StateStore(db_path=db_path, repo_root=repo_root)
        self.activity = ActivityLogger(repo_root=self.store.repo_root)
        # Full-fidelity tool-call recorder backing the debug analyzer. Isolated in
        # its own SQLite file so its churn never touches the state DB.
        self.tool_calls = ToolCallStore(
            db_path=self.store.repo_root / ".research_plugin" / "tool_calls.sqlite"
        )
        self.permissions = PermissionService()
        self.projects = ProjectService(store=self.store)
        self.compute = compute_service or ComputeService()
        self.claims = ClaimService(store=self.store)
        self.experiments = ExperimentService(store=self.store)
        self.resources = ResourceService(
            store=self.store,
            permissions=self.permissions,
        )
        self.reviews = ReviewService(
            store=self.store,
            permissions=self.permissions,
            experiments=self.experiments,
        )
        if execution_backend is None:
            execution_backend = build_sandbox_backend(
                repo_root=self.store.repo_root,
                activity=self._activity_hook,
            )
        self.execution_backend = execution_backend
        self.sandboxes = SandboxService(
            store=self.store,
            sandbox_backend=execution_backend,
            activity=self.activity,
            rsync_syncer=rsync_syncer,
        )
        self.workflow = WorkflowService(
            store=self.store,
            experiments=self.experiments,
            reviews=self.reviews,
            sandboxes=self.sandboxes,
            resources=self.resources,
        )
        handlers: dict[str, tuple[str, Callable[..., dict[str, Any]]]] = {
            "workflow.status_and_next": ("Orient Codex from durable project/experiment state.", self.workflow.status_and_next_agent),
            "project.create": (
                "Create a project for this folder using a user-confirmed name and summary. "
                "If project.current returned exists:false and the user has not already "
                "provided the project name/purpose, ask before calling this tool.",
                self.projects.create,
            ),
            "project.update": ("Update a project name or summary.", self.projects.update),
            "project.get": ("Get project metadata.", self.projects.get),
            "project.current": ("Get the current folder's project, or report that none exists yet.", self.projects.current),
            "project.list": ("List projects in the current tool scope.", self.projects.list_projects),
            "claim.create": ("Create a claim.", self.claims.create),
            "claim.list": ("List claims.", self.claims.list_claims),
            "claim.update": ("Update a claim's statement, scope, status, or confidence.", self.claims.update),
            "experiment.create": ("Create a planned experiment.", self.experiments.create),
            "experiment.list": ("List experiments with state.", self.experiments.list_experiments_agent),
            "experiment.get_state": (
                "Get one experiment state. Includes 'allowed_transitions': the "
                "transitions available from the current status, each with what it "
                "'requires' (e.g. a synced plan resource, a passing review).",
                self.experiments.get_state_agent,
            ),
            "experiment.transition": (
                "Apply an allowed experiment transition. See "
                "experiment.get_state.allowed_transitions for valid transitions "
                "and their preconditions from the current status.",
                self.experiments.transition,
            ),
            "resource.register_file": (
                "Register or observe repo-relative file(s) as resources. Pass "
                "'path' for one file, or 'paths' for a changed-files batch.",
                self.resources.register_file,
            ),
            "resource.associate": ("Associate a resource to a claim, experiment, review, or attempt.", self.resources.associate),
            "resource.delete": ("Delete a resource from active project tracking while preserving observed version history.", self.resources.delete),
            "resource.list": (
                "List registered resources. Filter by kind/experiment_id/missing, "
                "paginate with limit/offset, and pass compact=true for a lean "
                "projection (omits the heavy current_version; use version_token to "
                "detect changes) instead of re-pulling full payloads.",
                self.resources.list_resources,
            ),
            "resource.resolve": (
                "Resolve one registered resource. Pass include_history=true to also "
                "return its immutable observed versions (the former resource.history).",
                self.resources.resolve,
            ),
            "review.request": ("Create a review request and reviewer capability.", self.reviews.request),
            "review.start": ("Start a read-only reviewer session.", self.reviews.start),
            "review.submit": (
                "Submit a review from a reviewer session. Accepts ONLY: "
                "review_session_id, verdict (pass|needs_changes|fail), notes, "
                "findings (list of {issue, severity?}), and evidence (free-form "
                "dict). Put structured rationale inside 'evidence' — unknown "
                "top-level fields are rejected.",
                self.reviews.submit,
            ),
            "review.status": ("Inspect review requests and submissions for a target.", self.reviews.status),
            "sandbox.request": ("Procure (reuse or create) the experiment's sandbox and return SSH details. On Lambda Labs, omit instance_type to receive a live menu of available machines to pick from.", self.sandboxes.request),
            "sandbox.options": ("List the hardware the active backend can provision right now (Lambda Labs: live available instance types; Modal: gpu/cpu/memory menu).", self.sandboxes.options),
            "sandbox.get": ("Get the experiment's sandbox status and SSH details.", self.sandboxes.get),
            "sandbox.sync": ("Pull remote synced workspace files back to the local experiment folder with SSH rsync.", self.sandboxes.sync),
            "sandbox.list": ("List sandboxes for the project.", self.sandboxes.list_sandboxes),
            "sandbox.release": ("Terminate the experiment's sandbox.", self.sandboxes.release),
            "sandbox.terminal": (
                "Read the experiment's terminal transcript. For polling, pass "
                "since=<cursor from the last response> to get only NEW output "
                "instead of re-pulling the whole tail; 'running' indicates whether "
                "the sandbox is still alive so you can stop polling a finished one. "
                "Per-command status: 'command_running' is true while a command is "
                "in flight, and once it finishes 'last_exit_code' (0 = success) and "
                "'last_command_finished_at' report its result — so you can tell a "
                "command is done and whether it succeeded without re-reading output "
                "(null on sandboxes created before this was added).",
                self.sandboxes.terminal,
            ),
            "sandbox.health": ("Check the execution backend is reachable.", self.sandboxes.health),
        }
        self._tools = {
            name: ToolSpec(description, TOOL_INPUT_MODELS[name], handler)
            for name, (description, handler) in handlers.items()
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
        backend_shutdown = getattr(self.execution_backend, "shutdown", None)
        if callable(backend_shutdown):
            try:
                backend_shutdown()
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
