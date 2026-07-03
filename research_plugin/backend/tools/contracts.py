"""Typed tool contracts shared by MCP and HTTP adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..config import storage_feature_enabled
from ..domain.storage_guidance import STORAGE_RULE_OF_THUMB
from ..domain.vocabulary import (
    RESOURCE_ROLES,
    RESOURCE_TARGET_TYPES,
    REVIEW_VERDICT_VALUES,
)


class ContractModel(BaseModel):
    """Strict boundary model for external tool inputs."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# Which plane serves a tool once the backend splits into a cloud control plane
# and a local data-plane daemon (docs/CONTROL_DATA_PLANE_SPLIT.md).
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
    hosted_control_sandbox_lookup: bool = False


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
            "User-confirmed project name, at least 3 characters. Do not infer "
            "a placeholder from the folder name unless the user explicitly "
            "asked for that."
        )
    )
    summary: str = Field(
        default="",
        description="Short user-confirmed project purpose or scope.",
    )


class ProjectUpdateInput(ProjectScopedInput):
    name: str | None = Field(
        default=None,
        description="New project name, at least 3 characters when provided.",
    )
    summary: str | None = None
    require_verified_reviews: bool | None = Field(
        default=None,
        description=(
            "Policy knob: when true, only reviews with verified reviewer "
            "independence (verified_agent_review) satisfy review gates; "
            "attested reviews stop counting. Omit to leave unchanged."
        ),
    )


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
    name: str = Field(default="", description="REQUIRED. Short folder-safe name, unique within the project — it becomes the experiment folder experiments/<name>/. Letters, digits, '.', '_', '-' only; 3-48 characters. The project supplies the shared context, so name the contrast: lead with what distinguishes this experiment from its siblings and do not repeat the project topic (next to 'released_adapters', prefer 'scratch_training' over 'lora_glue_scratch').")
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


class ExperimentMaterializeFoldersInput(ProjectScopedInput):
    experiment_id: str | None = Field(
        default=None,
        description=(
            "Optional experiment id. When provided, materialize only that "
            "experiment's folder regardless of status."
        ),
    )
    status: Literal[
        "planned",
        "design_review",
        "ready_to_run",
        "running",
        "experiment_review",
        "complete",
        "failed",
        "abandoned",
    ] | None = Field(
        default="planned",
        description=(
            "When experiment_id is omitted, materialize experiments with this "
            "status. Null materializes every experiment in the project."
        ),
    )


class ExperimentTransitionInput(ProjectScopedInput):
    experiment_id: str
    transition: Literal[
        "submit_design",
        "mark_ready_to_run",
        "start_running",
        "retry_running",
        "submit_results",
        "complete",
        "abandon",
        "mark_failed",
    ]
    evidence: dict[str, Any] | None = None


class MlflowContextInput(ProjectScopedInput):
    experiment_id: str | None = Field(
        default=None,
        description=(
            "Optional plugin experiment id. Omit for project-level MLflow "
            "navigation context; provide it for the exact MLflow experiment "
            "name/env used by a quantitative run."
        ),
    )


class MlflowFinalizeRunInput(ProjectScopedInput):
    experiment_id: str
    run_id: str | None = Field(
        default=None,
        description=(
            "MLflow run id to finalize/read back. Omit to use the "
            "plugin-created run persisted on the experiment."
        ),
    )
    status: Literal["FINISHED", "FAILED", "KILLED"] | None = Field(
        default="FINISHED",
        description=(
            "Terminal status to set before readback. Pass null for readback only."
        ),
    )
    wait_seconds: float = Field(
        default=2.0,
        ge=0.0,
        le=10.0,
        description="Maximum seconds to poll until MLflow readback is terminal.",
    )


class ReflectionLensInput(ContractModel):
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
            "(amplify, avoid, entropy) default their charter; the two "
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


class ReflectionCreateInput(ProjectScopedInput):
    title: str = Field(
        default="", description="Optional short headline for this reflection wave."
    )
    lenses: list[ReflectionLensInput] = Field(
        default_factory=list,
        description=(
            "The declared reflection roster: exactly 5 lenses — the 3 core ids "
            "(amplify, avoid, entropy) plus 2 you design for this "
            "project, each with a charter and why_distinct. The roster is "
            "fixed at create; every lens must submit its own reflection before "
            "submit_reflections."
        ),
    )


class ReflectionGetInput(ProjectScopedInput):
    reflection_id: str


class ReflectionListInput(ProjectScopedInput):
    pass


class ReflectionTransitionInput(ProjectScopedInput):
    reflection_id: str
    transition: Literal[
        "submit_reflections",
        "submit_reflection_artifacts",
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


class ResourceValidateInput(ProjectScopedInput):
    path: str = Field(
        description=(
            "Repo-relative file path to lint before registering or associating "
            "it as a resource."
        )
    )
    role: str = Field(
        description=(
            "Intended resource association role. For gated artifacts, the "
            "validator applies the same role-specific lint used by workflow gates."
        ),
        json_schema_extra={"enum": sorted(RESOURCE_ROLES)},
    )


class ResourceAssociationItemInput(ContractModel):
    resource_id: str
    target_type: str = Field(json_schema_extra={"enum": sorted(RESOURCE_TARGET_TYPES)})
    target_id: str
    role: str = Field(
        description="Resource association role. Use 'result' for experiment output files.",
        json_schema_extra={"enum": sorted(RESOURCE_ROLES)},
    )


class ResourceAssociateBatchInput(ProjectScopedInput):
    associations: list[ResourceAssociationItemInput] = Field(
        min_length=1,
        description=(
            "Resource association rows to apply in order. Each row has "
            "resource_id, target_type, target_id, and role."
        ),
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


class StoragePutObjectInput(ProjectScopedInput):
    name: str
    kind: Literal["dataset", "model", "other"]
    sha256: str
    size_bytes: int = Field(ge=0)
    content_type: str = "application/octet-stream"
    producing_experiment_id: str = ""
    producing_run: str = ""
    source_uri: str = ""
    notes: str = ""


class StorageUploadFileInput(ProjectScopedInput):
    path: str = Field(
        description=(
            "Repo-relative file path to upload ('..' and absolute paths are "
            "rejected)."
        )
    )
    kind: Literal["dataset", "model", "other"]
    name: str = Field(
        default="",
        description="Optional storage object name. Defaults to the repo-relative path.",
    )
    content_type: str = ""
    producing_experiment_id: str = ""
    producing_run: str = ""
    source_uri: str = ""
    notes: str = ""


class StorageCompleteUploadInput(ProjectScopedInput):
    upload_id: str
    parts: list[dict[str, Any]] | None = None


class StorageListInput(ProjectScopedInput):
    kind: Literal["dataset", "model", "other"] | None = None
    name: str | None = None
    status: (
        Literal["uploading", "completing", "available", "expired", "deleted"]
        | None
    ) = None
    include_expired: bool = False
    limit: int | None = Field(default=None, ge=1)
    offset: int = Field(default=0, ge=0)
    compact: bool = False


class StorageResolveInput(ProjectScopedInput):
    object_id: str | None = None
    name: str | None = None
    version: int | None = Field(default=None, ge=1)
    include_download: bool = True


class StorageDownloadFileInput(ProjectScopedInput):
    path: str = Field(
        description=(
            "Repo-relative destination path ('..' and absolute paths are "
            "rejected)."
        )
    )
    object_id: str | None = None
    name: str | None = None
    version: int | None = Field(default=None, ge=1)
    overwrite: bool = Field(
        default=False,
        description="Refuse to replace an existing local file unless true.",
    )


class StorageObjectInput(ProjectScopedInput):
    object_id: str


class ReviewRequestInput(ProjectScopedInput):
    target_type: Literal["experiment", "reflection"]
    target_id: str
    role: Literal[
        "design_reviewer",
        "experiment_reviewer",
        "reflection_reviewer",
        "human",
        "automated_check",
    ]
    reason: str = ""
    producer_session_id: str = "main"


class ReviewStartInput(ContractModel):
    review_request_id: str
    reviewer_capability: str
    declared_agent: str = ""
    caller_session_id: str = Field(
        description=(
            "The reviewer's OWN session identity (any stable identifier for "
            "the reviewing agent's session). Required: it must be non-empty "
            "and differ from the producer session that requested the review, "
            "so reviewer independence can be verified."
        )
    )


class ReviewSubmitInput(ContractModel):
    review_session_id: str
    verdict: Literal[*REVIEW_VERDICT_VALUES]
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
            "reflections stand but the reflection artifacts (project graph, "
            "reflection doc, and/or change spec) must be revised."
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
    target_type: Literal["experiment", "reflection"]
    target_id: str


class SandboxRequestInput(ProjectScopedInput):
    experiment_id: str | None = Field(
        default=None,
        description=(
            "Optional experiment to attach the sandbox to. Omit to create a "
            "standalone sandbox addressed by sandbox_uid."
        ),
    )
    instance_type: str | None = Field(
        default=None,
        description=(
            "Provider-bundled machine SKU (GPU + CPU + RAM together). Required by "
            "the Lambda Labs and Thunder Compute backends: call this with no instance_type (or "
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
    additional: bool = Field(
        default=False,
        description=(
            "When true with experiment_id, provision a new sandbox and add it "
            "to that experiment's active sandbox list instead of reusing an "
            "already attached live sandbox."
        ),
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
    experiment_id: str | None = Field(
        default=None,
        description=(
            "Experiment whose live sandbox association should be read. Omit "
            "when sandbox_uid is supplied."
        ),
    )
    sandbox_uid: str | None = Field(
        default=None,
        description="Optional sandbox_uid to read; omitted targets the primary sandbox.",
    )


class SandboxAttachInput(ProjectScopedInput):
    experiment_id: str = Field(
        description="Target experiment to attach the live sandbox to."
    )
    sandbox_uid: str = Field(
        description="Existing running sandbox_uid to associate with the target experiment."
    )


class SandboxPullOutputsInput(ProjectScopedInput):
    experiment_id: str | None = Field(
        default=None,
        description=(
            "Experiment whose running sandbox should be copied from. Omit when "
            "sandbox_uid is supplied."
        ),
    )
    sandbox_uid: str | None = Field(
        default=None,
        description="Optional sandbox_uid to copy from; omitted targets the primary sandbox.",
    )
    paths: list[str] = Field(
        default_factory=list,
        description=(
            "Repo-relative paths under the sandbox experiment_dir to pull. Omit "
            "to pull existing common outputs: results/, figures/, report.md, "
            "graph.json, metrics.json, and results.json."
        ),
    )
    destination_path: str = Field(
        default="",
        description=(
            "Repo-relative local destination directory. Defaults to the "
            "sandbox's local_experiment_dir."
        ),
    )
    overwrite: bool = Field(
        default=False,
        description=(
            "When false, existing local files are preserved/refused. Set true "
            "only when replacing local retained outputs is intentional."
        ),
    )


class SandboxListInput(ProjectScopedInput):
    pass


class SandboxReleaseInput(ProjectScopedInput):
    experiment_id: str | None = Field(
        default=None,
        description=(
            "Experiment whose sandbox(es) should be released. Omit when "
            "terminating a specific sandbox_uid."
        ),
    )
    sandbox_uid: str | None = Field(
        default=None,
        description=(
            "Optional sandbox_uid to terminate just one sandbox. Omit to "
            "terminate all live sandboxes for the experiment."
        ),
    )
    confirm_retained: bool = Field(
        default=False,
        description=(
            "Release permanently destroys the sandbox and everything on it. "
            "The first call without this flag does NOT delete — it returns a "
            "retention checklist. Set true only after you have retained "
            "everything you need (rsync files off the box yourself over SSH, "
            "and use durable heavy-file storage only when that feature is "
            "enabled) to actually terminate."
        ),
    )


class SandboxTerminalInput(ProjectScopedInput):
    experiment_id: str | None = Field(
        default=None,
        description="Experiment whose sandbox transcript to read. Omit with sandbox_uid.",
    )
    sandbox_uid: str | None = Field(
        default=None,
        description="Optional sandbox_uid to read; omitted targets the primary sandbox.",
    )
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
        description=(
            "Get the current folder's project, or report that none exists yet. "
            "When a project exists, includes a compact at_a_glance block: "
            "reflection age summary, recent experiments/claims, latest "
            "reflection/project graph resource ids, ids changed since "
            "reflection, and open reflection id."
        ),
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
            "'requires' (e.g. a registered plan resource, a passing review). "
            "Once running or later, includes the central 'mlflow' context and "
            "any plugin-created 'mlflow_run' identity for quantitative logging."
        ),
    ),
    "experiment.materialize_folders": ToolContract(
        input_model=ExperimentMaterializeFoldersInput,
        description=(
            "Create canonical local experiment folders under experiments/<name>/ "
            "for a project. Use after reflection publish or experiment.create "
            "when planned experiments exist in state but their local folders do "
            "not yet exist."
        ),
        plane="data",
    ),
    "experiment.transition": ToolContract(
        input_model=ExperimentTransitionInput,
        description=(
            "Apply an allowed experiment transition. See "
            "experiment.get_state.allowed_transitions for valid transitions "
            "and their preconditions from the current status. When a transition "
            "starts the experiment running, the result includes an 'mlflow' "
            "connection block for quantitative logging and, when the backend "
            "MLflow write URI is configured, a plugin-created run id to resume."
            " Use retry_running only for infrastructure/interruption reruns "
            "where the experiment should stay running on the same attempt."
        ),
    ),
    "mlflow.context": ToolContract(
        input_model=MlflowContextInput,
        description=(
            "Central MLflow bridge context. With no experiment_id, returns the "
            "project-level tracking URI, dashboard URL, namespace prefix, env, "
            "and plugin experiment-to-MLflow-name map for direct MlflowClient "
            "navigation. With experiment_id, also returns the exact "
            "rp/<project>/<experiment> experiment name and env vars to set "
            "(MLFLOW_TRACKING_URI, MLFLOW_EXPERIMENT_NAME, …) before a "
            "quantitative run, plus the plugin-created run id when available. "
            "Returns configured=false when no tracking server is set."
        ),
    ),
    "mlflow.finalize_run": ToolContract(
        input_model=MlflowFinalizeRunInput,
        description=(
            "Finalize a plugin experiment's MLflow run and read it back through "
            "the backend MLflow API. Omit run_id to use the plugin-created run "
            "from experiment state; pass status=null for readback only. The "
            "helper updates the persisted mlflow_run status so immediate stale "
            "RUNNING readbacks do not linger in experiment state."
        ),
    ),
    "reflection.create": ToolContract(
        input_model=ReflectionCreateInput,
        description=(
            "Open a project reflection wave. "
            "Declares the 5-lens reflection roster (3 core: amplify, "
            "avoid, entropy; plus 2 you design with charter + "
            "why_distinct) and snapshots the corpus of finished experiments "
            "the wave covers. One wave may be open at a time. See the "
            "project-reflection skill."
        ),
    ),
    "reflection.get": ToolContract(
        input_model=ReflectionGetInput,
        description=(
            "Get one reflection wave state: roster, per-lens "
            "reflection coverage, current-attempt resources, reviews, and "
            "allowed_transitions with preconditions. Includes gate_checklist "
            "for missing lenses/artifacts/review state, and project_graph_diff "
            "when a submitted project graph can be compared with the previous "
            "published graph."
        ),
    ),
    "reflection.list": ToolContract(
        input_model=ReflectionListInput,
        description="List the project's reflection waves with state.",
    ),
    "reflection.transition": ToolContract(
        input_model=ReflectionTransitionInput,
        description=(
            "Apply an allowed reflection transition (submit_reflections, "
            "submit_reflection_artifacts, publish, abandon). See "
            "reflection.get.allowed_transitions for preconditions from the "
            "current status. On publish, after the reflection reviewer has "
            "passed, the reviewed change spec applies claim changes and "
            "either stops the project or creates the approved experiment wave."
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
            "Gated-role artifacts (plan, report, graph, project_graph, "
            "reflection_doc, reflection_lens_doc, change_spec) are "
            "size-capped at associate time — keep them lean and reference raw "
            "data instead of inlining it."
        ),
        plane="data",
    ),
    "resource.validate": ToolContract(
        input_model=ResourceValidateInput,
        description=(
            "Preflight-lint a repo file before resource.register_file or "
            "resource.associate. Covers every gated role — plan, report, "
            "graph/project_graph, reflection_doc, reflection_lens_doc, and "
            "change_spec — with the same byte caps and structural lints the "
            "workflow transitions enforce. Two checks remain gate-only: "
            "change-spec claim/experiment existence (needs canonical state) "
            "and pinned-byte staleness — gates lint the bytes captured at "
            "associate, so re-associate after editing the file."
        ),
        plane="data",
    ),
    "resource.associate_batch": ToolContract(
        input_model=ResourceAssociateBatchInput,
        description=(
            "Associate multiple resources to claims, experiments, reviews, or "
            "attempts in one data-plane call. Rows are applied in order through "
            "the same validation and gated-byte capture path as resource.associate."
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
    "storage.put_object": ToolContract(
        input_model=StoragePutObjectInput,
        description=(
            "Register a heavy storage object intent. Returns a presigned upload "
            "target unless the content is already present in the project. "
            f"{STORAGE_RULE_OF_THUMB}"
        ),
    ),
    "storage.upload_file": ToolContract(
        input_model=StorageUploadFileInput,
        description=(
            "Upload a local file to durable storage and complete the ledger "
            "object in one call. Relative paths are resolved against the "
            "project repo root; omit name to use the repo-relative path. "
            f"{STORAGE_RULE_OF_THUMB}"
        ),
        plane="data",
    ),
    "storage.complete_upload": ToolContract(
        input_model=StorageCompleteUploadInput,
        description="Complete a storage upload and mark the ledger object available.",
    ),
    "storage.list": ToolContract(
        input_model=StorageListInput,
        description=(
            "List project storage objects. Filter by kind/name/status, paginate "
            "with limit/offset, and pass compact=true for a lean projection."
        ),
    ),
    "storage.resolve": ToolContract(
        input_model=StorageResolveInput,
        description=(
            "Resolve one storage object by id or name/version. With "
            "include_download=true, returns a presigned download URL and renews TTL."
        ),
    ),
    "storage.download_file": ToolContract(
        input_model=StorageDownloadFileInput,
        description=(
            "Resolve a storage object and download it to a local file, verifying "
            "size and sha256 before replacing the destination."
        ),
        plane="data",
    ),
    "storage.pin": ToolContract(
        input_model=StorageObjectInput,
        description="Pin a storage object so expiry cleanup keeps it.",
    ),
    "storage.unpin": ToolContract(
        input_model=StorageObjectInput,
        description="Unpin a storage object and restore its default expiry.",
    ),
    "storage.renew": ToolContract(
        input_model=StorageObjectInput,
        description="Renew a storage object's default expiry window.",
    ),
    "storage.delete": ToolContract(
        input_model=StorageObjectInput,
        description="Delete a storage ledger alias and reclaim bytes when unreferenced.",
    ),
    "review.request": ToolContract(
        input_model=ReviewRequestInput,
        description=(
            "Create a review request and one-time reviewer capability. The "
            "response's reviewer_handoff.spawn_prompt is a ready-to-use prompt "
            "for the reviewer subagent — the reviewer itself consumes the "
            "capability via review.start, never the requesting session."
        ),
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
        description=(
            "Inspect review requests and submissions for a target, including "
            "recovery guidance for lost or expired one-time reviewer capabilities."
        ),
    ),
    "sandbox.request": ToolContract(
        input_model=SandboxRequestInput,
        description=(
            "Procure (reuse or create) a project sandbox, optionally attached to "
            "an experiment, and return SSH details plus runtime guidance for the "
            "remote work folder, expiry, copy-out, and durable storage. "
            "On Thunder Compute or Lambda Labs, omit instance_type to "
            "receive a live menu of available machines to pick from."
        ),
        plane="data",
    ),
    "sandbox.options": ToolContract(
        input_model=SandboxOptionsInput,
        description=(
            "List the hardware the active backend can provision right now "
            "(Thunder Compute/Lambda Labs: live available instance types; Modal: gpu/cpu/memory menu)."
        ),
    ),
    "sandbox.get": ToolContract(
        input_model=SandboxGetInput,
        description=(
            "Get sandbox status, SSH details, expiry, and polling/runtime "
            "guidance by sandbox_uid or by an experiment's active sandbox "
            "association. Use it to poll provisioning and inspect terminated "
            "or expired sandboxes."
        ),
        plane="aggregate",
        hosted_control_sandbox_lookup=True,
    ),
    "sandbox.attach": ToolContract(
        input_model=SandboxAttachInput,
        description=(
            "Associate an existing running sandbox with an experiment without "
            "changing the VM, workdir, SSH connection, or lifecycle. A live "
            "sandbox can be associated with multiple active experiments."
        ),
        plane="data",
    ),
    "sandbox.pull_outputs": ToolContract(
        input_model=SandboxPullOutputsInput,
        description=(
            "Copy selected files or directories from a running sandbox's remote "
            "experiment_dir into the local experiment folder over SSH/rsync. "
            "Use this before resource.register_file/resource.associate or "
            "sandbox.release; omit paths to pull common retained outputs. "
            "Existing local files are kept unless overwrite=true — ones that "
            "differ from the sandbox are reported in files_kept_stale, so check "
            "it before registering results from a re-run. Remote symlinks and "
            "device nodes are never recreated locally. One failing path is "
            "reported in errors/paths_failed without discarding the rest."
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
            "Terminate a sandbox by experiment_id or sandbox_uid (permanently "
            "destroys the VM and everything on it) and capture a best-effort "
            "metrics snapshot. "
            "Two-step by design: the first call WITHOUT confirm_retained does "
            "not delete — it returns a retention checklist asking you to confirm "
            "you have everything you need. Retain first with sandbox.pull_outputs "
            "for light files and configured durable storage for heavy ones when "
            "available, then "
            "re-call with confirm_retained=true to actually terminate."
        ),
    ),
    "sandbox.terminal": ToolContract(
        input_model=SandboxTerminalInput,
        description=(
            "Read a sandbox terminal transcript by experiment_id or sandbox_uid. "
            "For polling, pass "
            "since=<cursor from the last response> to get only NEW output "
            "instead of re-pulling the whole tail; 'running' indicates whether "
            "the sandbox is still alive so you can stop polling a finished one. "
            "Per-command status: 'command_running' is true while a command is "
            "in flight, and once it finishes 'last_exit_code' (0 = success) and "
            "'last_command_finished_at' report its result — so you can tell a "
            "command is done and whether it succeeded without re-reading output "
            "(null on sandboxes created before this was added). The structured "
            "'last_command' block persists the latest parsed command id, text, "
            "status, exit code, timestamps, and output tail; "
            "'command_status_stale' is true when that block is from the last "
            "successful transcript read because the current read failed."
        ),
    ),
    "sandbox.health": ToolContract(
        input_model=EmptyInput,
        description="Check the execution backend is reachable.",
        plane="aggregate",
    ),
}

# Social feed (Feed_PRD.md) registers its tools from its own module so the feed
# stays a liftable feature: this is the single integration point with the tool
# manifest. The merge happens before the derived sets below so routing/catalog
# include the feed tools. (feed_contracts imports the base classes above; this
# bottom-of-section import is safe because they are already defined.)
from .feed_contracts import FEED_TOOL_CONTRACTS  # noqa: E402

TOOL_CONTRACTS.update(FEED_TOOL_CONTRACTS)

STORAGE_TOOL_NAMES = {
    "storage.put_object",
    "storage.upload_file",
    "storage.complete_upload",
    "storage.list",
    "storage.resolve",
    "storage.download_file",
    "storage.pin",
    "storage.unpin",
    "storage.renew",
    "storage.delete",
}


def available_tool_names(*, storage_enabled: bool | None = None) -> set[str]:
    """Tool names for the active feature set.

    Storage is optional. When it is not configured, the MCP catalog must omit
    storage tools entirely rather than advertising a feature that will fail.
    """
    enabled = storage_feature_enabled() if storage_enabled is None else storage_enabled
    names = set(TOOL_CONTRACTS)
    if not enabled:
        names -= STORAGE_TOOL_NAMES
    return names


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


def static_tool_catalog(
    *, tool_names: set[str] | None = None, storage_enabled: bool | None = None
) -> list[dict[str, Any]]:
    """The MCP tool catalog, derived purely from contracts.

    Same shape as ``ResearchPluginApp.list_tools()`` (top-level ``title``
    popped from each schema) so tool listing never needs an app instance —
    and therefore has no filesystem side effects.
    """
    selected = (
        available_tool_names(storage_enabled=storage_enabled)
        if tool_names is None
        else set(tool_names)
    )
    catalog: list[dict[str, Any]] = []
    for name, contract in TOOL_CONTRACTS.items():
        if name not in selected:
            continue
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
