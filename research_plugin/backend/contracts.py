"""Typed tool contracts shared by MCP and HTTP adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .services.permissions import RESOURCE_ROLES, RESOURCE_TARGET_TYPES


class ContractModel(BaseModel):
    """Strict boundary model for external tool inputs."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# Which plane serves a tool once the backend splits into a cloud control plane
# and a local data-plane daemon (docs/CLOUD_BACKEND_MIGRATION_PLAN.md §3.3).
# "control" = record/gate/lifecycle work, cloud-servable. "data" = touches the
# local filesystem or local processes, must run on the user's machine.
# "aggregate" = merges both planes' answers. In local mode one process serves
# all three; the annotation is the machine-checkable routing source of truth.
ToolPlane = Literal["control", "data", "aggregate"]


@dataclass(frozen=True)
class ToolContract:
    input_model: type[ContractModel]
    description: str
    plane: ToolPlane = "control"


class EmptyInput(ContractModel):
    pass


class ProjectScopedInput(ContractModel):
    project_id: str = Field(
        description=(
            "Explicit project scope. Project-local MCP adapters may fill this "
            "from hidden repo context before the call reaches core services."
        )
    )


class WorkflowStatusAndNextInput(ProjectScopedInput):
    experiment_id: str | None = None


class ProjectCreateInput(ContractModel):
    name: str = Field(
        description=(
            "User-confirmed project name. Do not infer a placeholder from the "
            "folder name unless the user explicitly asked for that."
        )
    )
    summary: str = Field(
        default="",
        description="Short user-confirmed project purpose or scope.",
    )


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
    name: str = Field(default="", description="REQUIRED. Short folder-safe name, unique within the project — it becomes the experiment folder experiments/<name>/. Letters, digits, '.', '_', '-' only; max 48 characters. The project supplies the shared context, so name the contrast: lead with what distinguishes this experiment from its siblings and do not repeat the project topic (next to 'released_adapters', prefer 'scratch_training' over 'lora_glue_scratch').")
    intent: str = Field(default="", description="Durable one-line headline for the experiment (its UI title). The full design belongs in the plan.md resource.")
    tested_claim_ids: list[str] | str | None = Field(default_factory=list)
    claim_id: str | None = Field(default=None, description="Alias for a single tested claim id.")
    claim_ids: list[str] | str | None = Field(default=None, description="Alias for tested_claim_ids.")
    title: str = Field(default="", description="Deprecated; back-compat fallback for intent. Put design detail in plan.md.")
    hypothesis: str = Field(default="", description="Deprecated; put the hypothesis in plan.md's 'Objective & hypothesis' section.")
    design: str = Field(default="", description="Deprecated; put the method in plan.md's 'Method' section.")
    success_criteria: str = Field(default="", description="Deprecated; put success criteria in plan.md's 'Evaluation' section.")
    risks: str = Field(default="", description="Deprecated; put risks in plan.md's 'Risks & confounders' section.")
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


class SynthesisLensInput(ContractModel):
    id: str = Field(
        description=(
            "Lens id slug (lowercase letters/digits/'_'/'-'). It doubles as the "
            "reflection filename: the lens's subagent submits <id>.md."
        )
    )
    title: str = ""
    charter: str = Field(
        default="",
        description=(
            "What angle this lens reads the project from. The core lenses "
            "(outcomes, dead_ends, coverage) default their charter; the two "
            "wave-authored lenses must supply one."
        ),
    )
    why_distinct: str = Field(
        default="",
        description=(
            "Required for the two wave-authored lenses: how this lens differs "
            "from the core three and from the other authored lens. Engineered "
            "diversity is the point of the roster."
        ),
    )


class SynthesisCreateInput(ProjectScopedInput):
    title: str = Field(
        default="", description="Optional short headline for this reflection wave."
    )
    lenses: list[SynthesisLensInput] = Field(
        default_factory=list,
        description=(
            "The declared reflection roster: exactly 5 lenses — the 3 core ids "
            "(outcomes, dead_ends, coverage) plus 2 you design for this "
            "project, each with a charter and why_distinct. The roster is "
            "fixed at create; every lens must submit its own reflection before "
            "submit_reflections."
        ),
    )


class SynthesisGetInput(ProjectScopedInput):
    synthesis_id: str


class SynthesisListInput(ProjectScopedInput):
    pass


class SynthesisTransitionInput(ProjectScopedInput):
    synthesis_id: str
    transition: Literal[
        "submit_reflections",
        "submit_synthesis",
        "publish",
        "abandon",
    ]


class ResourceRegisterFileInput(ProjectScopedInput):
    path: str | None = Field(
        default=None, description="Repo-relative file path for a single file."
    )
    paths: list[str] | None = Field(
        default=None,
        description=(
            "Repo-relative paths to register/observe as a batch (changed-files "
            "sweep). Provide either 'path' (one file) or 'paths' (many)."
        ),
    )
    kind: str = "other"
    title: str = ""
    created_by: str = "codex"


class ResourceAssociateInput(ProjectScopedInput):
    resource_id: str
    target_type: str = Field(json_schema_extra={"enum": sorted(RESOURCE_TARGET_TYPES)})
    target_id: str
    role: str = Field(
        description="Resource association role. Use 'result' for experiment output files.",
        json_schema_extra={"enum": sorted(RESOURCE_ROLES)},
    )


class ResourceDeleteInput(ProjectScopedInput):
    resource_id: str


class ResourceListInput(ProjectScopedInput):
    kind: str | None = Field(
        default=None, description="Filter to one resource kind (e.g. 'dataset', 'code')."
    )
    experiment_id: str | None = Field(
        default=None, description="Only resources associated with this experiment."
    )
    missing: bool | None = Field(
        default=None,
        description="Filter by file presence: true=only missing-on-disk, false=only present.",
    )
    compact: bool = Field(
        default=False,
        description=(
            "Return a lean projection (id, path, kind, title, version_token, "
            "current_version_id, missing, updated_at) and OMIT the heavy nested "
            "current_version + associations. Use version_token to detect changes "
            "without re-pulling full payloads."
        ),
    )
    limit: int | None = Field(
        default=None, ge=1, description="Max resources to return (page size)."
    )
    offset: int = Field(
        default=0, ge=0, description="Number of resources to skip (pagination)."
    )


class ResourceResolveInput(ProjectScopedInput):
    resource_id: str
    include_history: bool = Field(
        default=False,
        description=(
            "Also return the resource's immutable observed 'versions' "
            "(oldest-first) — the former resource.history."
        ),
    )


class ReviewRequestInput(ProjectScopedInput):
    target_type: Literal["experiment", "synthesis"]
    target_id: str
    role: Literal[
        "design_reviewer",
        "experiment_reviewer",
        "synthesis_reviewer",
        "human",
        "automated_check",
    ]
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
    return_to: Literal["", "planned", "running", "reflecting", "synthesizing"] = Field(
        default="",
        description=(
            "Where a rejected target goes next. Omit on pass. REQUIRED on "
            "experiment-attempt-review rejections (needs_changes/fail): 'planned' if "
            "the results show the plan itself is flawed; 'running' if the plan "
            "stands but execution or the conclusion is flawed (fix and re-run "
            "without redoing design review). Design-review rejections always "
            "return to 'planned'. REQUIRED on project-reflection-review rejections: "
            "'reflecting' to re-launch the reflection fan-out (every lens "
            "re-submits for the new attempt), or 'synthesizing' if the "
            "reflections stand but the synthesis (project graph and/or "
            "proposals) must be revised."
        ),
    )
    notes: str = Field(default="", description="Free-text summary of the review.")
    findings: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "List of issue objects. Each item should have an 'issue' (str); "
            "conventionally also 'severity' (e.g. 'high'/'medium'/'low'). "
            'Example: [{"issue": "no held-out test set", "severity": "high"}].'
        ),
    )
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Free-form dict of supporting data for the verdict (e.g. metrics, "
            "checks run). Put structured rationale HERE — unknown TOP-LEVEL fields "
            "are rejected (this input forbids extras)."
        ),
    )


class ReviewStatusInput(ProjectScopedInput):
    target_type: str
    target_id: str


class SandboxRequestInput(ProjectScopedInput):
    experiment_id: str
    instance_type: str | None = Field(
        default=None,
        description=(
            "Provider-bundled machine SKU (GPU + CPU + RAM together). Required by "
            "the default Lambda Labs backend: call this with no instance_type (or "
            "use sandbox.options) to get a live menu, then pick one of "
            "options[].instance_type. Ignored by Modal (which composes the machine "
            "from gpu/cpu/memory)."
        ),
    )
    region: str | None = Field(
        default=None,
        description=(
            "Optional datacenter/region for the chosen instance_type (Lambda "
            "Labs). Omit to auto-pick a region that currently has capacity."
        ),
    )
    gpu: str | None = Field(
        default=None,
        description=(
            "GPU type. On Modal a concrete attachable GPU (e.g. 'A100', 'H100'); "
            "omit for a CPU-only sandbox. On Lambda Labs a free-form filter over "
            "live instance types — prefer instance_type there."
        ),
    )
    cpu: float | None = Field(
        default=None,
        description=(
            "Requested Modal CPU cores (1 core = 2 vCPUs). Default 2 cores. "
            "Ignored by Lambda Labs, where the instance_type fixes the vCPUs."
        ),
    )
    memory: int | None = Field(
        default=None,
        description=(
            "Requested sandbox memory in MiB. Default 8192. Ignored by Lambda "
            "Labs, where the instance_type fixes the RAM."
        ),
    )
    time_limit: int | None = Field(
        default=None,
        description="Max sandbox lifetime in seconds (60..86400). Default 3600.",
    )


class SandboxOptionsInput(ProjectScopedInput):
    gpu: str | None = Field(
        default=None,
        description="Optional GPU filter (e.g. 'H100') over the available machines.",
    )
    region: str | None = Field(
        default=None,
        description="Optional region filter for available capacity.",
    )


class SandboxGetInput(ProjectScopedInput):
    experiment_id: str


class SandboxSyncInput(ProjectScopedInput):
    experiment_id: str


class SandboxListInput(ProjectScopedInput):
    pass


class SandboxReleaseInput(ProjectScopedInput):
    experiment_id: str


class SandboxTerminalInput(ProjectScopedInput):
    experiment_id: str
    tail: int | None = Field(
        default=None, description="Return only the last N characters of the transcript."
    )
    since: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Incremental poll: return only transcript characters AFTER this "
            "cursor offset. Pass the 'cursor' from the previous response to get "
            "only new output instead of re-pulling the whole tail."
        ),
    )


TOOL_CONTRACTS: dict[str, ToolContract] = {
    "workflow.status_and_next": ToolContract(
        input_model=WorkflowStatusAndNextInput,
        description="Orient Codex from durable project/experiment state.",
    ),
    "project.create": ToolContract(
        input_model=ProjectCreateInput,
        description=(
            "Create a project for this folder using a user-confirmed name and summary. "
            "If project.current returned exists:false and the user has not already "
            "provided the project name/purpose, ask before calling this tool."
        ),
    ),
    "project.update": ToolContract(
        input_model=ProjectUpdateInput,
        description="Update a project name or summary.",
    ),
    "project.get": ToolContract(
        input_model=ProjectGetInput,
        description="Get project metadata.",
    ),
    "project.current": ToolContract(
        input_model=EmptyInput,
        description="Get the current folder's project, or report that none exists yet.",
    ),
    "project.list": ToolContract(
        input_model=EmptyInput,
        description="List projects in the current tool scope.",
    ),
    "claim.create": ToolContract(
        input_model=ClaimCreateInput,
        description="Create a claim.",
    ),
    "claim.list": ToolContract(
        input_model=ClaimListInput,
        description="List claims.",
    ),
    "claim.update": ToolContract(
        input_model=ClaimUpdateInput,
        description="Update a claim's statement, scope, status, or confidence.",
    ),
    "experiment.create": ToolContract(
        input_model=ExperimentCreateInput,
        description=(
            "Create a planned experiment. Requires an intent and a short "
            "folder-safe 'name' unique within the project; the name becomes "
            "the experiment folder experiments/<name>/."
        ),
    ),
    "experiment.list": ToolContract(
        input_model=ExperimentListInput,
        description="List experiments with state.",
    ),
    "experiment.get_state": ToolContract(
        input_model=ExperimentGetStateInput,
        description=(
            "Get one experiment state. Includes 'allowed_transitions': the "
            "transitions available from the current status, each with what it "
            "'requires' (e.g. a synced plan resource, a passing review)."
        ),
    ),
    "experiment.transition": ToolContract(
        input_model=ExperimentTransitionInput,
        description=(
            "Apply an allowed experiment transition. See "
            "experiment.get_state.allowed_transitions for valid transitions "
            "and their preconditions from the current status."
        ),
    ),
    "synthesis.create": ToolContract(
        input_model=SynthesisCreateInput,
        description=(
            "Open a project reflection wave (a gated synthesis record). "
            "Declares the 5-lens reflection roster (3 core: outcomes, "
            "dead_ends, coverage; plus 2 you design with charter + "
            "why_distinct) and snapshots the corpus of finished experiments "
            "the wave covers. One wave may be open at a time. See the "
            "project-reflection skill."
        ),
    ),
    "synthesis.get": ToolContract(
        input_model=SynthesisGetInput,
        description=(
            "Get one synthesis (reflection wave) state: roster, per-lens "
            "reflection coverage, current-attempt resources, reviews, and "
            "allowed_transitions with preconditions."
        ),
    ),
    "synthesis.list": ToolContract(
        input_model=SynthesisListInput,
        description="List the project's syntheses (reflection waves) with state.",
    ),
    "synthesis.transition": ToolContract(
        input_model=SynthesisTransitionInput,
        description=(
            "Apply an allowed synthesis transition (submit_reflections, "
            "submit_synthesis, publish, abandon). See "
            "synthesis.get.allowed_transitions for preconditions from the "
            "current status."
        ),
    ),
    "resource.register_file": ToolContract(
        input_model=ResourceRegisterFileInput,
        description=(
            "Register or observe repo-relative file(s) as resources. Pass "
            "'path' for one file, or 'paths' for a changed-files batch."
        ),
        plane="data",
    ),
    "resource.associate": ToolContract(
        input_model=ResourceAssociateInput,
        description=(
            "Associate a resource to a claim, experiment, review, or attempt. "
            "Gated-role artifacts (plan, report, graph, proposals, reflection) "
            "are size-capped at associate time (16KB each) — "
            "keep them lean and reference raw data instead of inlining it."
        ),
        plane="data",
    ),
    "resource.delete": ToolContract(
        input_model=ResourceDeleteInput,
        description=(
            "Delete a resource from active project tracking while preserving "
            "observed version history."
        ),
    ),
    "resource.list": ToolContract(
        input_model=ResourceListInput,
        description=(
            "List registered resources. Filter by kind/experiment_id/missing, "
            "paginate with limit/offset, and pass compact=true for a lean "
            "projection (omits the heavy current_version; use version_token to "
            "detect changes) instead of re-pulling full payloads."
        ),
    ),
    "resource.resolve": ToolContract(
        input_model=ResourceResolveInput,
        description=(
            "Resolve one registered resource. Pass include_history=true to also "
            "return its immutable observed versions (the former resource.history)."
        ),
    ),
    "review.request": ToolContract(
        input_model=ReviewRequestInput,
        description="Create a review request and reviewer capability.",
    ),
    "review.start": ToolContract(
        input_model=ReviewStartInput,
        description="Start a read-only reviewer session.",
    ),
    "review.submit": ToolContract(
        input_model=ReviewSubmitInput,
        description=(
            "Submit a review from a reviewer session. Accepts ONLY: "
            "review_session_id, verdict (pass|needs_changes|fail), return_to, "
            "notes, findings (list of {issue, severity?}), and evidence "
            "(free-form dict). On experiment-attempt-review rejections return_to is "
            "REQUIRED: 'planned' if the results show the plan itself is "
            "flawed, 'running' if the plan stands but execution or the "
            "conclusion is flawed (the experiment resumes running with its "
            "approved plan intact). Put structured rationale inside "
            "'evidence' — unknown top-level fields are rejected."
        ),
    ),
    "review.status": ToolContract(
        input_model=ReviewStatusInput,
        description="Inspect review requests and submissions for a target.",
    ),
    "sandbox.request": ToolContract(
        input_model=SandboxRequestInput,
        description=(
            "Procure (reuse or create) the experiment's sandbox and return SSH "
            "details plus runtime guidance for the synced folder, expiry, and "
            "MLflow/TensorBoard observability. A fresh sandbox starts with the "
            "experiment's local folder (experiments/<name>/) pushed to it, so "
            "put anything the run needs in that folder first. On Lambda Labs, "
            "omit instance_type to receive a live menu of available machines to "
            "pick from."
        ),
        plane="data",
    ),
    "sandbox.options": ToolContract(
        input_model=SandboxOptionsInput,
        description=(
            "List the hardware the active backend can provision right now "
            "(Lambda Labs: live available instance types; Modal: gpu/cpu/memory menu)."
        ),
    ),
    "sandbox.get": ToolContract(
        input_model=SandboxGetInput,
        description=(
            "Get the experiment's sandbox status, SSH details, expiry, and "
            "polling/runtime guidance. Use it to poll provisioning and inspect "
            "terminated or expired sandboxes."
        ),
        plane="aggregate",
    ),
    "sandbox.sync": ToolContract(
        input_model=SandboxSyncInput,
        description=(
            "Mirror the sandbox's experiment folder back to the local "
            "experiment folder with SSH rsync (exact replica; the durable "
            "handoff before registering resources)."
        ),
        plane="data",
    ),
    "sandbox.list": ToolContract(
        input_model=SandboxListInput,
        description="List sandboxes for the project.",
    ),
    "sandbox.release": ToolContract(
        input_model=SandboxReleaseInput,
        description=(
            "Terminate the experiment's sandbox after a best-effort final pull "
            "and metrics snapshot. Prefer sandbox.sync before release for a "
            "deliberate handoff."
        ),
    ),
    "sandbox.terminal": ToolContract(
        input_model=SandboxTerminalInput,
        description=(
            "Read the experiment's terminal transcript. For polling, pass "
            "since=<cursor from the last response> to get only NEW output "
            "instead of re-pulling the whole tail; 'running' indicates whether "
            "the sandbox is still alive so you can stop polling a finished one. "
            "Per-command status: 'command_running' is true while a command is "
            "in flight, and once it finishes 'last_exit_code' (0 = success) and "
            "'last_command_finished_at' report its result — so you can tell a "
            "command is done and whether it succeeded without re-reading output "
            "(null on sandboxes created before this was added)."
        ),
    ),
    "sandbox.health": ToolContract(
        input_model=EmptyInput,
        description="Check the execution backend is reachable.",
        plane="aggregate",
    ),
}

TOOL_INPUT_MODELS: dict[str, type[ContractModel]] = {
    name: contract.input_model for name, contract in TOOL_CONTRACTS.items()
}

PROJECT_SCOPED_TOOL_NAMES = {
    name
    for name, contract in TOOL_CONTRACTS.items()
    if issubclass(contract.input_model, ProjectScopedInput)
}

# Plane route sets, derived so the routing table and the registry cannot
# drift. The proxy's dual-upstream routing (split mode) keys on these.
CONTROL_PLANE_TOOL_NAMES = {
    name for name, contract in TOOL_CONTRACTS.items() if contract.plane == "control"
}
DATA_PLANE_TOOL_NAMES = {
    name for name, contract in TOOL_CONTRACTS.items() if contract.plane == "data"
}
AGGREGATE_TOOL_NAMES = {
    name for name, contract in TOOL_CONTRACTS.items() if contract.plane == "aggregate"
}


def static_tool_catalog() -> list[dict[str, Any]]:
    """The MCP tool catalog, derived purely from contracts.

    Same shape as ``ResearchPluginApp.list_tools()`` (top-level ``title``
    popped from each schema) so tool listing never needs an app instance —
    and therefore has no filesystem side effects.
    """
    catalog: list[dict[str, Any]] = []
    for name, contract in TOOL_CONTRACTS.items():
        schema = contract.input_model.model_json_schema()
        schema.pop("title", None)
        catalog.append(
            {
                "name": name,
                "description": contract.description,
                "inputSchema": schema,
                # The routing source of truth (cloud plan §3.3): the stdlib-only
                # proxy reads this off the served catalog to build its
                # dual-upstream route map, so the proxy never imports the
                # pydantic-bound contracts module and the routing cannot drift
                # from TOOL_CONTRACTS.
                "plane": contract.plane,
            }
        )
    return catalog
