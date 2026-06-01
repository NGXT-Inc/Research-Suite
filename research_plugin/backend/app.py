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
from .services import (
    ClaimService,
    ExperimentService,
    PermissionService,
    ProjectService,
    ResourceService,
    ReviewService,
    SandboxService,
    WorkflowService,
)
from .state import ActivityLogger, StateStore, monotonic_ms


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
    ) -> None:
        self.store = StateStore(db_path=db_path, repo_root=repo_root)
        self.activity = ActivityLogger(repo_root=self.store.repo_root)
        self.permissions = PermissionService()
        self.projects = ProjectService(store=self.store)
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
        )
        self.workflow = WorkflowService(
            store=self.store,
            experiments=self.experiments,
            reviews=self.reviews,
            sandboxes=self.sandboxes,
            resources=self.resources,
        )
        handlers: dict[str, tuple[str, Callable[..., dict[str, Any]]]] = {
            "workflow.status_and_next": ("Orient Codex and user from durable project/experiment state.", self.workflow.status_and_next),
            "project.status_and_next": ("Alias for workflow.status_and_next.", self.workflow.status_and_next),
            "project.create": ("Create a project.", self.projects.create),
            "project.update": ("Update a project name or summary.", self.projects.update),
            "project.get": ("Get project metadata.", self.projects.get),
            "project.list": ("List projects.", self.projects.list_projects),
            "claim.create": ("Create a claim.", self.claims.create),
            "claim.list": ("List claims.", self.claims.list_claims),
            "claim.update": ("Update a claim's statement, scope, status, or confidence.", self.claims.update),
            "experiment.create": ("Create a planned experiment.", self.experiments.create),
            "experiment.list": ("List experiments with state.", self.experiments.list_experiments),
            "experiment.get_state": ("Get one experiment state.", self.experiments.get_state),
            "experiment.transition": ("Apply an allowed experiment transition.", self.experiments.transition),
            "resource.register_file": ("Register or observe one repo-relative file as a resource.", self.resources.register_file),
            "resource.observe_file": ("Observe one repo-relative resource file without changing kind metadata.", self.resources.observe_file),
            "resource.sync_changed_files": ("Register or observe changed repo-relative files.", self.resources.sync_changed_files),
            "resource.associate": ("Associate a resource to a claim, experiment, review, or attempt.", self.resources.associate),
            "resource.list": ("List registered resources.", self.resources.list_resources),
            "resource.resolve": ("Resolve one registered resource.", self.resources.resolve),
            "resource.history": ("List immutable observed versions for a resource.", self.resources.history),
            "review.request": ("Create a review request and reviewer capability.", self.reviews.request),
            "review.start": ("Start a read-only reviewer session.", self.reviews.start),
            "review.submit": ("Submit a review from a reviewer session.", self.reviews.submit),
            "review.status": ("Inspect review requests and submissions for a target.", self.reviews.status),
            "sandbox.request": ("Procure (reuse or create) the experiment's sandbox and return SSH details.", self.sandboxes.request),
            "sandbox.get": ("Get the experiment's sandbox status and SSH details.", self.sandboxes.get),
            "sandbox.list": ("List sandboxes for the project.", self.sandboxes.list_sandboxes),
            "sandbox.release": ("Terminate the experiment's sandbox.", self.sandboxes.release),
            "sandbox.terminal": ("Read the experiment's terminal transcript.", self.sandboxes.terminal),
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
            self._on_tool_success(name=name, result=result)
            self.activity.tool_ok(
                source=activity_source,
                tool=name,
                arguments=arguments,
                duration_ms=monotonic_ms() - started,
                result=result,
            )
            return result
        except ResearchPluginError as exc:
            self.activity.tool_error(
                source=activity_source,
                tool=name,
                arguments=arguments,
                duration_ms=monotonic_ms() - started,
                error=exc.message,
                error_code=exc.error_code,
            )
            raise
        except Exception as exc:
            self.activity.tool_error(
                source=activity_source,
                tool=name,
                arguments=arguments,
                duration_ms=monotonic_ms() - started,
                error=str(exc),
                error_code="unexpected",
            )
            raise

    def _on_tool_success(self, *, name: str, result: dict[str, Any]) -> None:
        """Fire backend lifecycle hooks after successful mutation tools.

        Kept here (rather than inside services) so neither ProjectService nor
        the modal package needs to know about the other.
        """
        if name == "project.create":
            project_id = result.get("id") if isinstance(result, dict) else None
            if not project_id:
                return
            hook = getattr(self.execution_backend, "on_project_created", None)
            if not callable(hook):
                return
            try:
                hook(project_id=project_id)
            except Exception as exc:  # noqa: BLE001
                # Volume provisioning is best-effort at create time; the 60 s
                # poller will retry on the next tick.
                self._activity_hook(
                    "modal.sync.error",
                    {
                        "phase": "on_project_created",
                        "project_id": project_id,
                        "message": str(exc),
                    },
                )

    def _activity_hook(self, event_type: str, payload: dict[str, Any]) -> None:
        """Bridge between modal-sync emit-style logging and ActivityLogger."""
        try:
            self.activity.emit(event_type=event_type, payload=payload)
        except Exception:  # noqa: BLE001
            pass
