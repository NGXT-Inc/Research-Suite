"""Typed tool contracts shared by MCP and HTTP adapters."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .services.permissions import RESOURCE_ROLES, RESOURCE_TARGET_TYPES


class ContractModel(BaseModel):
    """Strict boundary model for external tool inputs."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EmptyInput(ContractModel):
    pass


class ProjectScopedInput(ContractModel):
    project_id: str = Field(description="Explicit project scope. The server never falls back to an implicit project.")


class WorkflowStatusAndNextInput(ProjectScopedInput):
    experiment_id: str | None = None


class ProjectCreateInput(ContractModel):
    name: str
    summary: str = ""


class ProjectUpdateInput(ProjectScopedInput):
    name: str | None = None
    summary: str | None = None


class ProjectGetInput(ProjectScopedInput):
    pass


class ClaimCreateInput(ProjectScopedInput):
    statement: str
    scope: str = ""
    confidence: Literal["low", "medium", "high"] = "medium"


class ClaimListInput(ProjectScopedInput):
    pass


class ClaimUpdateInput(ProjectScopedInput):
    claim_id: str
    statement: str | None = None
    scope: str | None = None
    status: Literal[
        "draft", "active", "supported", "weakened", "contradicted", "abandoned"
    ] | None = None
    confidence: Literal["low", "medium", "high"] | None = None


class ExperimentCreateInput(ProjectScopedInput):
    intent: str = Field(default="", description="Preferred durable experiment summary or question.")
    tested_claim_ids: list[str] | str | None = Field(default_factory=list)
    claim_id: str | None = Field(default=None, description="Alias for a single tested claim id.")
    claim_ids: list[str] | str | None = Field(default=None, description="Alias for tested_claim_ids.")
    title: str = Field(default="", description="Optional; folded into intent.")
    hypothesis: str = Field(default="", description="Optional; folded into intent.")
    design: str = Field(default="", description="Optional; folded into intent.")
    success_criteria: str = Field(default="", description="Optional; folded into intent.")
    risks: str = Field(default="", description="Optional; folded into intent.")
    status: Literal["planned"] = Field(default="planned", description="Create always starts planned.")


class ExperimentListInput(ProjectScopedInput):
    pass


class ExperimentGetStateInput(ProjectScopedInput):
    experiment_id: str


class ExperimentTransitionInput(ProjectScopedInput):
    experiment_id: str
    transition: Literal[
        "submit_design",
        "mark_ready_to_run",
        "start_running",
        "submit_results",
        "complete",
        "abandon",
        "mark_failed",
    ]
    evidence: dict[str, Any] | None = None


class ResourceRegisterFileInput(ProjectScopedInput):
    path: str = Field(description="Repo-relative file path.")
    kind: str = "other"
    title: str = ""
    created_by: str = "codex"


class ResourceObserveFileInput(ProjectScopedInput):
    path: str


class ResourceSyncChangedFilesInput(ProjectScopedInput):
    paths: list[str]


class ResourceAssociateInput(ProjectScopedInput):
    resource_id: str
    target_type: str = Field(json_schema_extra={"enum": sorted(RESOURCE_TARGET_TYPES)})
    target_id: str
    role: str = Field(
        description="Resource association role. Use 'result' for experiment or job output files.",
        json_schema_extra={"enum": sorted(RESOURCE_ROLES)},
    )


class ResourceListInput(ProjectScopedInput):
    pass


class ResourceResolveInput(ProjectScopedInput):
    resource_id: str


class ResourceHistoryInput(ProjectScopedInput):
    resource_id: str


class ReviewRequestInput(ProjectScopedInput):
    target_type: Literal["experiment"]
    target_id: str
    role: Literal["design_reviewer", "experiment_reviewer", "human", "automated_check"]
    reason: str = ""
    producer_session_id: str = "main"


class ReviewStartInput(ContractModel):
    review_request_id: str
    reviewer_capability: str
    declared_agent: str = ""
    caller_session_id: str = ""


class ReviewSubmitInput(ContractModel):
    review_session_id: str
    verdict: Literal["pass", "needs_changes", "fail"]
    notes: str = ""
    findings: list[dict[str, Any]] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


class ReviewStatusInput(ProjectScopedInput):
    target_type: str
    target_id: str


class JobSubmitInput(ProjectScopedInput):
    experiment_id: str
    command: str
    cwd: str = "."
    expected_outputs: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    backend_hints: dict[str, Any] = Field(default_factory=dict)


class JobStatusInput(ProjectScopedInput):
    job_id: str


class JobLogsInput(ProjectScopedInput):
    job_id: str
    tail: int | None = None


class JobCancelInput(ProjectScopedInput):
    job_id: str


class JobListInput(ProjectScopedInput):
    experiment_id: str | None = None
    status: str | None = None


TOOL_INPUT_MODELS: dict[str, type[ContractModel]] = {
    "workflow.status_and_next": WorkflowStatusAndNextInput,
    "project.status_and_next": WorkflowStatusAndNextInput,
    "project.create": ProjectCreateInput,
    "project.update": ProjectUpdateInput,
    "project.get": ProjectGetInput,
    "project.list": EmptyInput,
    "claim.create": ClaimCreateInput,
    "claim.list": ClaimListInput,
    "claim.update": ClaimUpdateInput,
    "experiment.create": ExperimentCreateInput,
    "experiment.list": ExperimentListInput,
    "experiment.get_state": ExperimentGetStateInput,
    "experiment.transition": ExperimentTransitionInput,
    "resource.register_file": ResourceRegisterFileInput,
    "resource.observe_file": ResourceObserveFileInput,
    "resource.sync_changed_files": ResourceSyncChangedFilesInput,
    "resource.associate": ResourceAssociateInput,
    "resource.list": ResourceListInput,
    "resource.resolve": ResourceResolveInput,
    "resource.history": ResourceHistoryInput,
    "review.request": ReviewRequestInput,
    "review.start": ReviewStartInput,
    "review.submit": ReviewSubmitInput,
    "review.status": ReviewStatusInput,
    "job.submit": JobSubmitInput,
    "job.status": JobStatusInput,
    "job.logs": JobLogsInput,
    "job.cancel": JobCancelInput,
    "job.list": JobListInput,
    "job.health": EmptyInput,
}
