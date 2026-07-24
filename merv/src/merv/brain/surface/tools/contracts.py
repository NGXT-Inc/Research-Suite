"""Typed tool contracts shared by MCP and HTTP adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from merv.shared.artifact_roles import ARTIFACT_TARGET_TYPES, SUBMITTABLE_ROLES
from merv.shared.storage_guidance import STORAGE_RULE_OF_THUMB
from merv.shared.tool_validation import validate_openssh_public_key

from ...research_core.facade import REVIEW_VERDICT_VALUES


class ContractModel(BaseModel):
    """Strict boundary model for external tool inputs."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# Which plane serves each tool in the brain/local-proxy topology
# (docs/CONTROL_DATA_PLANE_SPLIT.md).
# "control" = record/gate/lifecycle work, cloud-servable. "data" = touches the
# local filesystem or local processes, must run on the user's machine.
ToolPlane = Literal["control", "data"]
ToolVisibility = Literal["public", "internal"]
ToolScopeStrategy = Literal["linked-project", "caller-selected", "capability", "none"]
ToolExecutionStrategy = Literal[
    "control",
    "local",
    "control-plus-local-enrichment",
    "local-orchestration",
]
ToolFeature = Literal["storage"]


@dataclass(frozen=True)
class ToolManifest:
    """One tool's complete public contract and runtime placement metadata."""

    input_model: type[ContractModel]
    description: str
    handler_identity: str
    visibility: ToolVisibility = "public"
    scope_strategy: ToolScopeStrategy | None = None
    execution_strategy: ToolExecutionStrategy = "control"
    catalog_plane: ToolPlane | None = None
    feature_requirements: tuple[ToolFeature, ...] = ()
    local_handler_identity: str | None = None
    hosted_control_sandbox_lookup: bool = False

    def __post_init__(self) -> None:
        if self.scope_strategy is None:
            inferred: ToolScopeStrategy = (
                "linked-project"
                if issubclass(self.input_model, ProjectScopedInput)
                else "none"
            )
            object.__setattr__(self, "scope_strategy", inferred)

    @property
    def plane(self) -> ToolPlane:
        if self.catalog_plane is not None:
            return self.catalog_plane
        return "data" if self.execution_strategy in {"local", "local-orchestration"} else "control"


# Compatibility name for code that describes only the schema/description half.
ToolContract = ToolManifest


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


class ProjectInput(ContractModel):
    """The one agent-facing project tool: current / connect / create.

    Deliberately NOT ProjectScopedInput — project_id here is the caller's
    explicit choice of which hosted project to link (action=connect), never
    hidden repo context resolved by the proxy.
    """

    action: Literal["current", "connect", "create", "overview"] = Field(
        description=(
            "current = return the brain project linked to this folder, or "
            "exists=false when no local link exists; "
            "overview = the whole-project read — every claim (incl. "
            "settled/abandoned) and every experiment (incl. terminal) — for "
            "orienting or re-grounding; connect = link this folder to a brain "
            "project (pass project_id for an existing one, or name/summary to "
            "create and link in one step); create = create a project WITHOUT "
            "linking this folder (rare — connect is the normal bootstrap)."
        )
    )
    project_id: str = Field(
        default="",
        description=(
            "action=connect: existing hosted project id to link this folder "
            "to. Leave empty when creating a new project via name."
        ),
    )
    name: str = Field(
        default="",
        description=(
            "User-confirmed project name, at least 3 characters. Required for "
            "action=create; for action=connect supply it (with project_id "
            "empty) to create a new project and link it. Do not infer a "
            "placeholder from the folder name unless the user explicitly "
            "asked for that."
        ),
    )
    summary: str = Field(
        default="",
        description="Short user-confirmed project purpose or scope.",
    )
    overwrite: bool = Field(
        default=False,
        description=(
            "action=connect only: must be true to re-link a folder that is "
            "already linked to a different project."
        ),
    )

    @model_validator(mode="after")
    def _check_action(self) -> "ProjectInput":
        if self.action in ("current", "overview"):
            # Neither carries an agent payload. A local proxy resolves both from
            # its folder link; a keyed cloud caller reaches the brain, which
            # resolves them from the key's bound project. overview tolerates an
            # explicit project_id (the agent still never supplies it locally).
            forbidden = ["name", "summary", "overwrite"]
            if self.action == "current":
                forbidden = ["project_id", *forbidden]
            extras = [field for field in forbidden if getattr(self, field)]
            if extras:
                raise ValueError(
                    f"action={self.action} takes no other fields; "
                    f"got {', '.join(extras)}"
                )
        elif self.action == "create":
            if len(self.name) < 3:
                raise ValueError("action=create requires name (at least 3 characters)")
        elif self.action == "connect":
            if not self.project_id and not self.name:
                raise ValueError(
                    "action=connect requires project_id (link an existing "
                    "project) or name (create a new project and link it)"
                )
            if self.name and len(self.name) < 3:
                raise ValueError("name must be at least 3 characters")
        return self


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
    hidden: bool | None = Field(
        default=None,
        description=(
            "Stash a project out of the UI project list without deleting it: "
            "when true, project.list omits it while the project's data and "
            "direct-by-id access are retained; false restores it. Omit to "
            "leave unchanged."
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
    status: (
        Literal["draft", "active", "supported", "weakened", "contradicted", "abandoned"]
        | None
    ) = None
    confidence: Literal["low", "medium", "high"] | None = None


class ExperimentCreateInput(ProjectScopedInput):
    name: str = Field(
        default="",
        description="REQUIRED. Short folder-safe name, unique within the project — it becomes the experiment folder experiments/<name>/. Letters, digits, '.', '_', '-' only; 3-48 characters. The project supplies the shared context, so name the contrast: lead with what distinguishes this experiment from its siblings and do not repeat the project topic (next to 'released_adapters', prefer 'scratch_training' over 'lora_glue_scratch'). See the siblings — including terminal ones you should not recreate — via the project tool with action=\"overview\".",
    )
    intent: str = Field(
        default="",
        description="Durable one-line headline for the experiment (its UI title). The full design belongs in the plan.md artifact.",
    )
    tested_claim_ids: list[str] | str | None = Field(default_factory=list)
    claim_id: str | None = Field(
        default=None, description="Alias for a single tested claim id."
    )
    claim_ids: list[str] | str | None = Field(
        default=None, description="Alias for tested_claim_ids."
    )
    title: str = Field(
        default="",
        description="Deprecated; back-compat fallback for intent. Put design detail in plan.md.",
    )
    hypothesis: str = Field(
        default="",
        description="Deprecated; put the hypothesis in plan.md's 'Objective & hypothesis' section.",
    )
    design: str = Field(
        default="",
        description="Deprecated; put the method in plan.md's 'Method' section.",
    )
    success_criteria: str = Field(
        default="",
        description="Deprecated; put success criteria in plan.md's 'Evaluation' section.",
    )
    risks: str = Field(
        default="",
        description="Deprecated; put risks in plan.md's 'Risks & confounders' section.",
    )
    status: Literal["planned"] = Field(
        default="planned", description="Create always starts planned."
    )


class ExperimentListInput(ProjectScopedInput):
    pass


class ExperimentGetStateInput(ProjectScopedInput):
    experiment_id: str


class ExperimentExhibitInput(ProjectScopedInput):
    experiment_id: str


class ExperimentMaterializeFoldersInput(ProjectScopedInput):
    experiment_id: str | None = Field(
        default=None,
        description=(
            "Optional experiment id. When provided, materialize only that "
            "experiment's folder regardless of status."
        ),
    )
    status: (
        Literal[
            "planned",
            "design_review",
            "ready_to_run",
            "running",
            "experiment_review",
            "complete",
            "failed",
            "abandoned",
        ]
        | None
    ) = Field(
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


class ArtifactSubmitInput(ProjectScopedInput):
    target_type: str = Field(
        description="Workflow target kind the artifact attaches to.",
        json_schema_extra={"enum": sorted(ARTIFACT_TARGET_TYPES)},
    )
    target_id: str = Field(
        description="Id of the experiment, reflection, claim, or review."
    )
    role: str = Field(
        description=(
            "Artifact role. Gated docs (plan, report, graph, project_graph, "
            "reflection_lens_doc, reflection_doc, change_spec) and metrics "
            "'result' JSON only — all size-capped at 16 KB."
        ),
        json_schema_extra={"enum": sorted(SUBMITTABLE_ROLES)},
    )
    path: str = Field(
        description=(
            "Relative path of the local file you wrote — the provenance label "
            "and the file the returned upload command sends."
        )
    )
    lens_id: str = Field(
        default="",
        description=(
            "REQUIRED when role=reflection_lens_doc: the roster lens this "
            "reflection covers. Invalid for any other role."
        ),
    )
    title: str = Field(default="", description="Optional display title.")

    @model_validator(mode="after")
    def _check_lens(self) -> "ArtifactSubmitInput":
        if self.role == "reflection_lens_doc" and not self.lens_id:
            raise ValueError("lens_id is required when role is reflection_lens_doc")
        if self.lens_id and self.role != "reflection_lens_doc":
            raise ValueError("lens_id only applies to reflection_lens_doc artifacts")
        return self


class ArtifactFindInput(ProjectScopedInput):
    artifact_id: str = Field(
        default="",
        description="Resolve one artifact by id. Omit to list with the filters below.",
    )
    target_type: str = Field(
        default="", description="List filter: target kind (e.g. 'experiment')."
    )
    target_id: str = Field(default="", description="List filter: target id.")
    role: str = Field(default="", description="List filter: artifact role.")


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


class StorageSubmitInput(ProjectScopedInput):
    path: str = Field(
        description=(
            "Local file path to upload. Embedded verbatim into the returned "
            "`curl -T` command (which you run) and the default object name."
        )
    )
    kind: Literal["dataset", "model", "other"]
    sha256: str = Field(
        description=(
            "Client-computed SHA-256 (hex) of the file. Feeds name+sha dedup and "
            "is bound into the presigned checksum; identity is re-verified on "
            "completion."
        )
    )
    size_bytes: int = Field(
        ge=0,
        description="File size in bytes; presigns the upload and enforces the size cap.",
    )
    name: str = Field(
        default="",
        description="Optional storage object name. Defaults to the path.",
    )
    content_type: str = ""
    producing_experiment_id: str = ""
    producing_run: str = ""
    source_uri: str = ""
    notes: str = ""


class StorageCompleteUploadInput(ProjectScopedInput):
    upload_id: str
    parts: list[dict[str, Any]] | None = None


class StorageFindInput(ProjectScopedInput):
    """List the storage ledger, or resolve a single object.

    A union of the former ``storage.list`` and ``storage.resolve`` inputs.
    Passing ``object_id`` or ``name`` (with optional ``version`` /
    ``include_download``) selects resolve mode; omitting both lists the ledger
    with the ``kind`` / ``status`` filters and ``limit`` / ``offset`` / ``compact``
    pagination.
    """

    # Resolve-mode selectors (former storage.resolve).
    object_id: str | None = None
    name: str | None = None
    version: int | None = Field(default=None, ge=1)
    include_download: bool = True
    # List-mode filters (former storage.list).
    kind: Literal["dataset", "model", "other"] | None = None
    status: (
        Literal["uploading", "completing", "available", "expired", "deleted"] | None
    ) = None
    include_expired: bool = False
    limit: int | None = Field(default=None, ge=1)
    offset: int = Field(default=0, ge=0)
    compact: bool = False

    @model_validator(mode="after")
    def _check_mode(self) -> "StorageFindInput":
        if self.object_id and self.name:
            raise ValueError("provide at most one of object_id or name")
        if self.version is not None and not (self.object_id or self.name):
            raise ValueError("version selects a resolve target; pass object_id or name")
        return self


class StorageFetchInput(ProjectScopedInput):
    path: str = Field(
        description=(
            "Local destination path. Embedded verbatim into the returned "
            "`curl -o` command, which you run."
        )
    )
    object_id: str | None = None
    name: str | None = None
    version: int | None = Field(default=None, ge=1)


class StorageObjectInput(ProjectScopedInput):
    object_id: str
    action: Literal["pin", "unpin", "renew", "delete"]


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
    synopsis: str = Field(
        description=(
            "The researcher's TLDR, 1-3 plain sentences, 40-420 chars: what "
            "was tried, what happened, and whether it holds. This is the "
            "first thing the human reads on the experiment page, so write "
            "plain prose in reader context — name things by their human "
            "names, and use at most one decisive number with its baseline. "
            "No entity ids (exp_/claim_/res_/rev_/rver_/syn_/lit_/paper_), "
            "no backticks or markdown, no newlines."
        )
    )
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
    provider: str | None = Field(
        default=None,
        description=(
            "Compute provider to serve this request when the deployment has "
            "several configured (e.g. lambda_labs, hyperstack, digitalocean). "
            "sandbox.options tags every hardware option with the provider that "
            "serves it — pass that value back together with its instance_type. "
            "Omit to use the default provider."
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
    public_key: str = Field(
        description=(
            "Required OpenSSH public key to authorize on the VM. Pass only the "
            "single-line public key, never private-key material."
        ),
    )
    additional: bool = Field(
        default=False,
        description=(
            "When true with experiment_id, provision a new sandbox and add it "
            "to that experiment's active sandbox list instead of reusing an "
            "already attached live sandbox."
        ),
    )

    @field_validator("public_key")
    @classmethod
    def _public_key_shape(cls, value: str | None) -> str | None:
        return validate_openssh_public_key(value)


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
    key_path: str = Field(
        default="",
        description=(
            "Local private key path for the caller-owned public_key authorized "
            "on the sandbox. Required when sandbox.get does not include an "
            "ssh.key_path enrichment."
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


class SandboxExtendInput(ProjectScopedInput):
    experiment_id: str | None = Field(
        default=None,
        description=(
            "Experiment whose running sandbox should be extended. Omit when "
            "sandbox_uid is supplied."
        ),
    )
    sandbox_uid: str | None = Field(
        default=None,
        description="Optional sandbox_uid to extend; omitted targets the primary sandbox.",
    )
    seconds: int = Field(
        default=1800,
        ge=1,
        le=1800,
        description="Additional lifetime in seconds. Maximum one 30-minute increment per call.",
    )


class SandboxRunsInput(ProjectScopedInput):
    experiment_id: str | None = Field(
        default=None,
        description=(
            "Experiment whose sandbox runs to list (spans every sandbox the "
            "experiment used, including released ones). Omit with sandbox_uid."
        ),
    )
    sandbox_uid: str | None = Field(
        default=None,
        description="Optional sandbox_uid to read; omitted targets the experiment's sandboxes.",
    )
    wait_seconds: int = Field(
        default=0,
        ge=0,
        le=300,
        description=(
            "Long-poll: block up to this many seconds, returning early when "
            "any run finishes (or nothing is running). 0 answers immediately. "
            "Keep <=45 unless your MCP client's tool timeout is known to allow "
            "more (many clients cut tool calls at ~60s)."
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


class LitreviewViewInput(ProjectScopedInput):
    section: str = Field(
        default="",
        max_length=200,
        description=(
            "Read one full section by id (lit_...) or exact title "
            "(case-insensitive); 'summary' addresses the General Summary. "
            "Empty = the overview: General Summary + every section's TLDR "
            "outline + paper count — the cheap glance."
        ),
    )
    papers: bool = Field(
        default=False,
        description="Return the papers ledger page (with links) instead of the document.",
    )
    cursor: int = Field(
        default=0,
        ge=0,
        description="papers=true: created_seq cursor from the previous page's next_cursor.",
    )
    limit: int = Field(
        default=20, ge=1, le=50, description="papers=true: page size."
    )


class LitreviewOrderPair(ContractModel):
    id: str = Field(max_length=64, description="Section id (lit_...).")
    revision: int = Field(ge=1, description="The revision you last read for this section.")


class LitreviewEditInput(ProjectScopedInput):
    op: Literal["add", "edit", "delete", "reorder"] = Field(
        description=(
            "add = new dynamic section (title + tldr required); edit = targeted "
            "update of one section (expected_revision required; only the fields "
            "you pass change); delete = remove one section and its citation "
            "links (expected_revision required; the General Summary cannot be "
            "deleted); reorder = set the complete section order (order "
            "required). Always make targeted edits — never rewrite the whole "
            "document."
        )
    )
    section: str = Field(
        default="",
        max_length=200,
        description=(
            "edit/delete: section id (lit_...) or exact title; 'summary' "
            "addresses the General Summary (pass expected_revision=0 to write "
            "it for the first time)."
        ),
    )
    title: str = Field(
        default="",
        max_length=200,
        description="add: required. edit: optional rename (summary title is fixed).",
    )
    tldr: str = Field(
        default="",
        max_length=500,
        description=(
            "One-glance summary of the section. Required on add and on every "
            "edit that changes body — keep it current; it is what other agents "
            "read first."
        ),
    )
    body: str = Field(
        default="",
        description="Markdown body, max 16,000 bytes. Cite papers inline by paper_ id.",
    )
    expected_revision: int | None = Field(
        default=None,
        ge=0,
        description=(
            "edit/delete: the revision you last read. A mismatch means the "
            "section changed under you — re-read it and retry."
        ),
    )
    order: list[LitreviewOrderPair] | None = Field(
        default=None,
        max_length=64,
        description="reorder: ALL dynamic sections as {id, revision} pairs in the new order.",
    )

    @field_validator("body")
    @classmethod
    def _body_byte_cap(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 16_000:
            raise ValueError("body exceeds 16,000 bytes — split the section instead")
        return value

    @model_validator(mode="after")
    def _check_op(self) -> "LitreviewEditInput":
        if self.op == "add" and not self.title:
            raise ValueError("op=add requires title")
        if self.op in ("edit", "delete"):
            if not self.section:
                raise ValueError(f"op={self.op} requires section")
            if self.expected_revision is None:
                raise ValueError(f"op={self.op} requires expected_revision")
        if self.op == "reorder" and not self.order:
            raise ValueError("op=reorder requires order")
        return self


class LitreviewCiteTarget(ContractModel):
    type: Literal["litreview_section", "experiment", "claim"]
    id: str = Field(
        max_length=200,
        description="Target id (section ids may also be exact titles).",
    )


class LitreviewCiteInput(ProjectScopedInput):
    url: str = Field(default="", max_length=2048, description="Paper URL (arXiv/DOI forms are normalized).")
    doi: str = Field(default="", max_length=256, description="Bare DOI, e.g. 10.1038/xyz.")
    arxiv_id: str = Field(default="", max_length=64, description="Bare arXiv id, e.g. 2107.03374.")
    targets: list[LitreviewCiteTarget] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Where this paper is used: lit-review sections, experiments, "
            "and/or claims. Registering with no targets is allowed."
        ),
    )
    note: str = Field(
        default="", max_length=300, description="Optional one-liner: why this paper matters here."
    )
    title: str = Field(
        default="",
        max_length=200,
        description="Fallback title, used when the paper's host is off the fetch allowlist.",
    )

    @model_validator(mode="after")
    def _one_identity(self) -> "LitreviewCiteInput":
        provided = [v for v in (self.url, self.doi, self.arxiv_id) if v]
        if len(provided) != 1:
            raise ValueError("provide exactly one of url, doi, or arxiv_id")
        return self


TOOL_MANIFEST: dict[str, ToolManifest] = {
    "workflow.status_and_next": ToolContract(
        handler_identity="workflow.status_and_next_agent",
        input_model=WorkflowStatusAndNextInput,
        description="Orient Codex from durable project/experiment state.",
    ),
    "project": ToolContract(
        handler_identity="operations.project",
        scope_strategy="caller-selected",
        execution_strategy="local-orchestration",
        # Preserve the legacy catalog plane: the brain still owns its schema
        # and the create/overview actions, while the proxy dispatches by action.
        catalog_plane="control",
        input_model=ProjectInput,
        description=(
            "Project identity for this folder, dispatched on 'action'. "
            "action=current returns the brain project linked to this folder, "
            "or exists=false when no local link exists. "
            "action=overview is the whole-project read for orienting or "
            "re-grounding: every claim (including settled/abandoned) and every "
            "experiment (including terminal), independent of what "
            "workflow.status_and_next chooses to embed. "
            "action=connect links this folder so every later tool call "
            "resolves to it: pass project_id to link an existing project, or "
            "a user-confirmed name (+ summary) to create AND link in one step "
            "(the normal bootstrap); ask the user which project first, never "
            "guess an id. The folder link is stored on this machine only; the "
            "brain never sees the folder path, and re-linking requires "
            "overwrite=true. action=create creates a project WITHOUT linking "
            "this folder (rare)."
        ),
    ),
    "project.update": ToolContract(
        handler_identity="projects.update",
        visibility="internal",
        input_model=ProjectUpdateInput,
        description="Update a project name, summary, policy knobs, or hidden state.",
    ),
    "project.get": ToolContract(
        handler_identity="projects.get",
        visibility="internal",
        input_model=ProjectGetInput,
        description="Get project metadata.",
    ),
    "project.list": ToolContract(
        handler_identity="projects.list_projects",
        visibility="internal",
        input_model=EmptyInput,
        description="List projects in the current tool scope.",
    ),
    "claim.create": ToolContract(
        handler_identity="claims.create",
        input_model=ClaimCreateInput,
        description=(
            'Create a claim. Check the project tool with action="overview" '
            "first so you do not recreate a settled or abandoned claim."
        ),
    ),
    "claim.list": ToolContract(
        handler_identity="claims.list_claims",
        visibility="internal",
        input_model=ClaimListInput,
        description="List claims.",
    ),
    "claim.update": ToolContract(
        handler_identity="claims.update",
        input_model=ClaimUpdateInput,
        description=(
            "Update a claim's status or confidence. The statement and scope "
            "are immutable — experiments and reviews reference the claim by "
            "id assuming stable meaning. To revise the text, propose a claim "
            "change in a reflection change spec (reviewed), or abandon this "
            "claim and create a corrected one."
        ),
    ),
    "experiment.create": ToolContract(
        handler_identity="create_experiment.create",
        input_model=ExperimentCreateInput,
        description=(
            "Create a planned experiment. Requires an intent and a short "
            "folder-safe 'name' unique within the project; the name becomes "
            "the experiment folder experiments/<name>/."
        ),
    ),
    "experiment.list": ToolContract(
        handler_identity="operations.experiment_list",
        visibility="internal",
        input_model=ExperimentListInput,
        description="List experiments with state.",
    ),
    "experiment.get_state": ToolContract(
        handler_identity="agent_experiment.experiment",
        input_model=ExperimentGetStateInput,
        description=(
            "Get one experiment state. Includes 'allowed_transitions': the "
            "transitions available from the current status, each with what it "
            "'requires' (e.g. a submitted plan artifact, a passing review). "
            "Once running or later, includes the central 'mlflow' context and "
            "any plugin-created 'mlflow_run' identity for quantitative logging."
        ),
    ),
    "experiment.materialize_folders": ToolContract(
        handler_identity="local.materialize_experiment_folders",
        execution_strategy="local-orchestration",
        input_model=ExperimentMaterializeFoldersInput,
        description=(
            "Create canonical local experiment folders under experiments/<name>/ "
            "for a project. Use after reflection publish or experiment.create "
            "when planned experiments exist in state but their local folders do "
            "not yet exist."
        ),
    ),
    "experiment.transition": ToolContract(
        handler_identity="experiment_transition.agent",
        input_model=ExperimentTransitionInput,
        description=(
            "Apply an allowed experiment transition. See "
            "experiment.get_state.allowed_transitions for valid transitions "
            "and their preconditions from the current status. When a transition "
            "starts the experiment running, the result includes an 'mlflow' "
            "connection block for quantitative logging and, when the backend "
            "MLflow write URI is configured, a plugin-created run id to resume."
            " Use retry_running only for infrastructure/interruption reruns "
            "where the experiment should stay running on the same attempt. "
            "At submit_results the system evaluates the attempt's metrics "
            "exhibit (up to the newest 50 attempt-window MLflow runs plus "
            "eligible pinned result JSON, each entry with provenance). It pins "
            "the exhibit when matching runs are found, or when MLflow is "
            "unavailable after a plugin-created run; when pinned, report.md "
            "must reference it. Runs logged "
            "after submit_results remain in MLflow but are outside the "
            "finalized attempt exhibit."
        ),
    ),
    "experiment.exhibit": ToolContract(
        handler_identity="experiment_exhibit.preview",
        input_model=ExperimentExhibitInput,
        description=(
            "Read-only preview of the system-generated metrics exhibit for a "
            "running experiment: up to the newest 50 MLflow runs in the "
            "current attempt window (no curation), plus eligible pinned "
            "result-file sources (metrics.json, results.json, and "
            "results/*.json associated with role 'result'). Call it before "
            "writing report.md "
            "— at submit_results the system regenerates it and pins it when "
            "matching runs are found, or when MLflow is unavailable after a "
            "plugin-created run. When pinned, the report must "
            "reference and interpret it rather than hand-copy numbers."
        ),
    ),
    "mlflow.context": ToolContract(
        handler_identity="tracking_context.execute",
        input_model=MlflowContextInput,
        description=(
            "Central MLflow bridge context. With no experiment_id, returns the "
            "project-level tracking URI, dashboard URL, namespace prefix, env, "
            "and plugin experiment-to-MLflow-name map for direct MlflowClient "
            "navigation. With experiment_id, also returns the exact "
            "merv/<project>/<experiment> experiment name and env vars to set "
            "(MLFLOW_TRACKING_URI, MLFLOW_EXPERIMENT_NAME, …) before a "
            "quantitative run, plus the plugin-created run id when available. "
            "Returns configured=false when no tracking server is set."
        ),
    ),
    "mlflow.finalize_run": ToolContract(
        handler_identity="tracking_finalize.execute",
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
        handler_identity="reflection_tools.create",
        input_model=ReflectionCreateInput,
        description=(
            "Open a project reflection wave. "
            "Declares the 5-lens reflection roster (3 core: amplify, "
            "avoid, entropy; plus 2 you design with charter + "
            "why_distinct) and snapshots the corpus of finished experiments "
            "the wave covers — including new_terminal_experiments (the new "
            "signal since the last published wave) and each lens's previous "
            "reflection path. One wave may be open at a time. See the "
            "project-reflection skill."
        ),
    ),
    "reflection.get": ToolContract(
        handler_identity="reflection_tools.get",
        input_model=ReflectionGetInput,
        description=(
            "Get one reflection wave state: roster, per-lens "
            "reflection coverage, current-attempt artifacts, reviews, and "
            "allowed_transitions with preconditions. Includes gate_checklist "
            "for missing lenses/artifacts/review state, and project_graph_diff "
            "when a submitted project graph can be compared with the previous "
            "published graph."
        ),
    ),
    "reflection.list": ToolContract(
        handler_identity="reflection_tools.list",
        visibility="internal",
        input_model=ReflectionListInput,
        description="List the project's reflection waves with state.",
    ),
    "reflection.transition": ToolContract(
        handler_identity="reflection_tools.transition",
        input_model=ReflectionTransitionInput,
        description=(
            "Apply an allowed reflection transition (submit_reflections, "
            "submit_reflection_artifacts, publish, abandon). See "
            "reflection.get.allowed_transitions for preconditions from the "
            "current status. On publish, after the reflection reviewer has "
            "passed, the reviewed change spec applies claim changes and "
            "creates the approved experiment wave."
        ),
    ),
    "litreview.view": ToolContract(
        handler_identity="litreview.view",
        input_model=LitreviewViewInput,
        description=(
            "Read the project's living literature review. No args = the "
            "overview (General Summary + every section's TLDR + paper count) — "
            "read this before editing so you know the document's shape. "
            "section=<id or title> = one full section with its cited papers. "
            "papers=true = the papers ledger with links to the sections, "
            "experiments, and claims that cite each paper."
        ),
    ),
    "litreview.edit": ToolContract(
        handler_identity="litreview.edit",
        input_model=LitreviewEditInput,
        description=(
            "Make a TARGETED change to the literature review: add, edit, "
            "delete, or reorder one thing per call — never rewrite the whole "
            "document. Every section keeps a TLDR (required on writes) so the "
            "overview stays glanceable. edit/delete require expected_revision "
            "(the revision you last read); a conflict means someone changed it "
            "— re-read and retry. Update the review whenever a new paper "
            "informs the project."
        ),
    ),
    "litreview.cite": ToolContract(
        handler_identity="litreview.cite",
        input_model=LitreviewCiteInput,
        description=(
            "Register a paper in the project's papers ledger and link it to "
            "the sections, experiments, or claims that use it. Papers are "
            "deduplicated (arXiv/DOI/URL forms of the same paper converge); "
            "metadata is fetched from known paper hosts, otherwise pass title. "
            "After citing, make a targeted litreview.edit so the review stays "
            "current."
        ),
    ),
    "artifact.submit": ToolContract(
        handler_identity="artifact_submissions.submit",
        input_model=ArtifactSubmitInput,
        description=(
            "Submit a typed artifact against a workflow target. FIRST write "
            "the document to a local file, then call this with its relative "
            "path; the result contains a one-line `run` command — execute it "
            "verbatim to upload the bytes (one-time token, expires in ~15 "
            "min). Gated roles are validated and size-capped (16 KB); for "
            "markdown with relative image links the upload response returns "
            "follow-up commands to push each figure the same way. "
            "Resubmitting the same slot replaces the previous artifact."
        ),
    ),
    "artifact.find": ToolContract(
        handler_identity="artifact_submissions.find",
        input_model=ArtifactFindInput,
        description=(
            "Find submitted artifacts. Pass artifact_id to resolve one, or "
            "filter the project's complete artifacts by target_type/"
            "target_id/role. Compact rows: id, target, role, attempt, "
            "lens_id, path label, title, size, timestamps."
        ),
    ),
    "storage.put_object": ToolContract(
        handler_identity="storage.put_object",
        visibility="internal",
        feature_requirements=("storage",),
        input_model=StoragePutObjectInput,
        description=(
            "Register a heavy storage object intent. Returns a presigned upload "
            "target unless the content is already present in the project. "
            f"{STORAGE_RULE_OF_THUMB}"
        ),
    ),
    "storage.submit": ToolContract(
        handler_identity="storage.submit",
        feature_requirements=("storage",),
        input_model=StorageSubmitInput,
        description=(
            "Register a heavy file and get a one-line `run` command to upload it. "
            "Compute the file's sha256 and size, call this, then execute the "
            "returned command verbatim — it PUTs the bytes straight to object "
            "storage and finalizes the ledger object (bytes never pass through "
            "the agent context or the brain). Omit name to use the path. "
            f"{STORAGE_RULE_OF_THUMB}"
        ),
    ),
    "storage.complete_upload": ToolContract(
        handler_identity="storage.complete_upload",
        visibility="internal",
        feature_requirements=("storage",),
        input_model=StorageCompleteUploadInput,
        description="Complete a storage upload and mark the ledger object available.",
    ),
    "storage.find": ToolContract(
        handler_identity="operations.storage_find",
        feature_requirements=("storage",),
        input_model=StorageFindInput,
        description=(
            "Find project storage objects. Pass object_id or name (with optional "
            "version, include_download) to resolve ONE object to its ledger row "
            "and, with include_download=true, a presigned download URL that renews "
            "TTL. Omit both to list the ledger: filter by kind/status, include "
            "expired rows with include_expired, paginate with limit/offset, and "
            "pass compact=true for a lean projection."
        ),
    ),
    "storage.fetch": ToolContract(
        handler_identity="storage.fetch",
        feature_requirements=("storage",),
        input_model=StorageFetchInput,
        description=(
            "Resolve a storage object and get a one-line `run` command to "
            "download it. Pass object_id or name (with optional version), then "
            "execute the returned command verbatim — it curls the bytes to your "
            "path and verifies the stored sha256."
        ),
    ),
    "storage.object": ToolContract(
        handler_identity="operations.storage_object",
        feature_requirements=("storage",),
        input_model=StorageObjectInput,
        description=(
            "Apply a lifecycle action to one storage object by object_id: pin "
            "(expiry cleanup keeps it), unpin (restore its default expiry), renew "
            "(renew its default expiry window), or delete (drop the ledger alias, "
            "keeping history, and reclaim bytes when unreferenced)."
        ),
    ),
    "review.request": ToolContract(
        handler_identity="reviews.request",
        input_model=ReviewRequestInput,
        description=(
            "Create a review request and request-scoped reviewer capability; "
            "the plaintext is returned only in this response. The "
            "response's reviewer_handoff.spawn_prompt is a ready-to-use prompt "
            "for the reviewer subagent. The reviewer presents the capability "
            "via review.start with its own caller_session_id. Starting does "
            "not consume it; the first accepted submission closes the request."
        ),
    ),
    "review.start": ToolContract(
        handler_identity="reviews.start",
        scope_strategy="capability",
        input_model=ReviewStartInput,
        description=(
            "Start a reviewer session for the pinned request snapshot. The "
            "reviewer skill supplies the procedural read-only boundary."
        ),
    ),
    "review.submit": ToolContract(
        handler_identity="reviews.submit",
        scope_strategy="capability",
        input_model=ReviewSubmitInput,
        description=(
            "Submit a review from a reviewer session. Accepts ONLY: "
            "review_session_id, verdict (pass|needs_changes|fail), synopsis "
            "(REQUIRED: 1-3 plain sentences, 40-420 chars, the researcher's "
            "TLDR — no entity ids, markdown, or backticks), return_to, "
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
        handler_identity="review_status.execute",
        visibility="internal",
        input_model=ReviewStatusInput,
        description=(
            "Inspect review requests and submissions for a target, including "
            "recovery guidance for lost or expired reviewer capabilities."
        ),
    ),
    "sandbox.request": ToolContract(
        handler_identity="local.request_sandbox",
        execution_strategy="local-orchestration",
        input_model=SandboxRequestInput,
        description=(
            "Procure (reuse or create) a project sandbox, optionally attached to "
            "an experiment, and return SSH details plus runtime guidance for the "
            "remote work folder, expiry, copy-out, and durable storage. "
            "On Thunder Compute or Lambda Labs, omit instance_type to "
            "receive a live menu of available machines to pick from. "
            "SSH key custody: the sandbox authorizes a caller-side public key. "
            "The primary path is bring-your-own-key — the requesting agent "
            "generates its own ephemeral ed25519 keypair (ssh-keygen), keeps the "
            "private key to itself in a location only it can read, and passes "
            "only the single-line OpenSSH PUBLIC key as public_key so it gets "
            "authorized on the VM. Never send private-key material. "
            "The response's persisted public_key_source is 'caller' for new "
            "requests; legacy 'managed' rows remain readable/releasable. "
            "ssh.key_path appears only when a local proxy enrichment knows the "
            "private key path."
        ),
    ),
    "sandbox.options": ToolContract(
        handler_identity="sandboxes.options",
        input_model=SandboxOptionsInput,
        description=(
            "List the hardware the active backend can provision right now "
            "(Thunder Compute/Lambda Labs: live available instance types; Modal: gpu/cpu/memory menu)."
        ),
    ),
    "sandbox.get": ToolContract(
        handler_identity="sandboxes.get",
        local_handler_identity="local.sandbox_get_enrichment",
        execution_strategy="control-plus-local-enrichment",
        input_model=SandboxGetInput,
        description=(
            "Get sandbox status, SSH details, expiry, and polling/runtime "
            "guidance by sandbox_uid or by an experiment's active sandbox "
            "association. Use it to poll provisioning and inspect terminated "
            "or expired sandboxes. Includes public_key_source so callers know "
            "whether the VM authorized a caller-supplied public key or a "
            "legacy managed fallback key."
        ),
        hosted_control_sandbox_lookup=True,
    ),
    "sandbox.attach": ToolContract(
        handler_identity="local.attach_sandbox",
        execution_strategy="local-orchestration",
        input_model=SandboxAttachInput,
        description=(
            "Associate an existing running sandbox with an experiment without "
            "changing the VM, workdir, SSH connection, or lifecycle. A live "
            "sandbox can be associated with multiple active experiments."
        ),
    ),
    "sandbox.pull_outputs": ToolContract(
        handler_identity="local.pull_sandbox_outputs",
        execution_strategy="local-orchestration",
        input_model=SandboxPullOutputsInput,
        description=(
            "Copy selected files or directories from a running sandbox's remote "
            "experiment_dir into the local experiment folder over SSH/rsync. "
            "This is a proxy-local data tool: pass key_path for the caller-owned "
            "private key when sandbox.get does not already include ssh.key_path. "
            "Use object storage tools for heavy artifacts. Use this before "
            "artifact.submit or sandbox.release; "
            "omit paths to pull common retained outputs. "
            "Existing local files are kept unless overwrite=true — ones that "
            "differ from the sandbox are reported in files_kept_stale, so check "
            "it before submitting results from a re-run. Remote symlinks and "
            "device nodes are never recreated locally. One failing path is "
            "reported in errors/paths_failed without discarding the rest."
        ),
    ),
    "sandbox.list": ToolContract(
        handler_identity="sandboxes.list_sandboxes",
        input_model=SandboxListInput,
        description=(
            "List this project's sandboxes (project-shared: every sandbox in the "
            "key's project, not just ones this caller provisioned)."
        ),
    ),
    "sandbox.release": ToolContract(
        handler_identity="sandboxes.release",
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
    "sandbox.extend": ToolContract(
        handler_identity="sandboxes.extend",
        input_model=SandboxExtendInput,
        description=(
            "Extend a running sandbox's expiry by at most one 30-minute "
            "increment, subject to provider support and tenant lifetime/spend "
            "quotas. Modal may reject this because its provider timeout is "
            "fixed when the sandbox is created."
        ),
    ),
    "sandbox.runs": ToolContract(
        handler_identity="sandboxes.runs",
        input_model=SandboxRunsInput,
        description=(
            "List merv_run launches for a sandbox or experiment: label, status "
            "(running/finished/lost), exit_code, started/finished timestamps, "
            "and log path — one compact call instead of transcript polling. "
            "Launch long work on the sandbox with `merv_run <label> -- <command>` "
            "(detaches, survives SSH disconnect, writes an exit_code sentinel); "
            "then either long-poll here with wait_seconds, or end the session "
            "and call this when next attending the experiment. Receipts "
            "outlive the sandbox: finished runs stay queryable after release "
            "or expiry (logs/outputs do not — pull those before the box dies)."
        ),
    ),
    "sandbox.terminal": ToolContract(
        handler_identity="sandboxes.terminal",
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
        handler_identity="sandboxes.health",
        local_handler_identity="local.health",
        visibility="internal",
        execution_strategy="control-plus-local-enrichment",
        input_model=EmptyInput,
        description="Check the execution backend is reachable.",
    ),
}

# Social feed (Feed_PRD.md) registers its tools from its own module so the feed
# stays a liftable feature: this is the single integration point with the tool
# manifest. The merge happens before the derived sets below so routing/catalog
# include the feed tools. (feed_contracts imports the base classes above; this
# bottom-of-section import is safe because they are already defined.)
from .feed_contracts import FEED_TOOL_CONTRACTS  # noqa: E402

TOOL_MANIFEST.update(FEED_TOOL_CONTRACTS)

# Compatibility projections. New code reads TOOL_MANIFEST; these names keep
# older adapters and SDK imports source-compatible without duplicating policy.
TOOL_CONTRACTS = TOOL_MANIFEST
STORAGE_TOOL_NAMES = {
    name for name, tool in TOOL_MANIFEST.items() if "storage" in tool.feature_requirements
}
TOOL_PLANE_REGISTRY: dict[str, ToolPlane] = {
    name: tool.plane for name, tool in TOOL_MANIFEST.items()
}
MCP_HIDDEN_TOOL_NAMES = frozenset(
    name for name, tool in TOOL_MANIFEST.items() if tool.visibility == "internal"
)


def available_tool_names(*, storage_enabled: bool) -> set[str]:
    """Tool names for the active feature set.

    Storage is optional. When it is not configured, the MCP catalog must omit
    storage tools entirely rather than advertising a feature that will fail.
    """
    names = set(TOOL_MANIFEST)
    if not storage_enabled:
        names -= STORAGE_TOOL_NAMES
    return names


PROJECT_SCOPED_TOOL_NAMES = {
    name
    for name, tool in TOOL_MANIFEST.items()
    if tool.scope_strategy == "linked-project"
}


def tool_plane(name: str) -> ToolPlane:
    return TOOL_MANIFEST[name].plane


# Plane route sets, derived so the routing table and registry cannot drift.
# The proxy uses these to keep brain calls separate from checkout-local calls.
CONTROL_PLANE_TOOL_NAMES = frozenset(
    name for name, tool in TOOL_MANIFEST.items() if tool.plane == "control"
)
DATA_PLANE_TOOL_NAMES = frozenset(
    name for name, tool in TOOL_MANIFEST.items() if tool.plane == "data"
)


def static_tool_catalog(
    *, tool_names: set[str] | None = None, storage_enabled: bool = False
) -> list[dict[str, Any]]:
    """The MCP tool catalog, derived purely from contracts.

    Same shape as the control app's ``list_tools()`` (top-level ``title``
    popped from each schema) so tool listing never needs an app instance —
    and therefore has no filesystem side effects.
    """
    selected = (
        available_tool_names(storage_enabled=storage_enabled)
        if tool_names is None
        else set(tool_names)
    )
    catalog: list[dict[str, Any]] = []
    for name, contract in TOOL_MANIFEST.items():
        if name not in selected:
            continue
        schema = contract.input_model.model_json_schema()
        schema.pop("title", None)
        tool: dict[str, Any] = {
            "name": name,
            "description": contract.description,
            "inputSchema": schema,
            # The routing source of truth: the stdlib-only proxy reads this
            # from the served catalog to route brain versus checkout-local
            # calls, without importing the pydantic-bound contracts module.
            "plane": contract.plane,
        }
        if contract.visibility == "internal":
            tool["hidden"] = True
        catalog.append(tool)
    return catalog


def proxy_tool_manifest() -> list[dict[str, Any]]:
    """Stdlib-client projection used for routing and offline tool listing."""
    catalog = {
        tool["name"]: tool for tool in static_tool_catalog(storage_enabled=True)
    }
    return [
        {
            **catalog[name],
            "visibility": tool.visibility,
            "scopeStrategy": tool.scope_strategy,
            "executionStrategy": tool.execution_strategy,
            "featureRequirements": list(tool.feature_requirements),
            "handlerIdentity": tool.handler_identity,
            "localHandlerIdentity": tool.local_handler_identity,
        }
        for name, tool in TOOL_MANIFEST.items()
    ]
