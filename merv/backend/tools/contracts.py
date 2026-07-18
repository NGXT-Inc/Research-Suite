"""Typed tool contracts shared by MCP and HTTP adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import storage_feature_enabled
from ..domain.storage_guidance import STORAGE_RULE_OF_THUMB
from ..artifacts.roles import RESOURCE_ROLES, RESOURCE_TARGET_TYPES
from ..domain.vocabulary import REVIEW_VERDICT_VALUES


class ContractModel(BaseModel):
    """Strict boundary model for external tool inputs."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# Which plane serves each tool in the brain/local-proxy topology
# (docs/CONTROL_DATA_PLANE_SPLIT.md).
# "control" = record/gate/lifecycle work, cloud-servable. "data" = touches the
# local filesystem or local processes, must run on the user's machine.
ToolPlane = Literal["control", "data"]


@dataclass(frozen=True)
class ToolContract:
    input_model: type[ContractModel]
    description: str
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
            # Neither carries an agent payload. current is fully proxy-served;
            # overview is proxy-served too but the proxy injects the resolved
            # project_id as scope before forwarding, so project_id is the one
            # field overview tolerates (the agent still never supplies it).
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
                raise ValueError(
                    "action=create requires name (at least 3 characters)"
                )
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
    status: Literal[
        "draft", "active", "supported", "weakened", "contradicted", "abandoned"
    ] | None = None
    confidence: Literal["low", "medium", "high"] | None = None


class ExperimentCreateInput(ProjectScopedInput):
    name: str = Field(default="", description="REQUIRED. Short folder-safe name, unique within the project — it becomes the experiment folder experiments/<name>/. Letters, digits, '.', '_', '-' only; 3-48 characters. The project supplies the shared context, so name the contrast: lead with what distinguishes this experiment from its siblings and do not repeat the project topic (next to 'released_adapters', prefer 'scratch_training' over 'lora_glue_scratch'). See the siblings — including terminal ones you should not recreate — via the project tool with action=\"overview\".")
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


class ResourceRegisterInput(ProjectScopedInput):
    path: str | None = Field(
        default=None, description="Repo-relative file path for a single file to register."
    )
    paths: list[str] | None = Field(
        default=None,
        description=(
            "Repo-relative paths to register/observe as a batch (changed-files "
            "sweep). Provide 'path' (one file), 'paths' (many), or 'resource_id' "
            "(associate an already-registered resource)."
        ),
    )
    resource_id: str | None = Field(
        default=None,
        description=(
            "Associate an ALREADY-registered resource to the target trio "
            "instead of registering a new file. Requires target_type, "
            "target_id, and role."
        ),
    )
    kind: str = "other"
    title: str = ""
    created_by: str = "codex"
    target_type: str | None = Field(
        default=None,
        description=(
            "Association target kind. Provide the trio (target_type, target_id, "
            "role) together to associate the registered/observed resource(s)."
        ),
        json_schema_extra={"enum": sorted(RESOURCE_TARGET_TYPES)},
    )
    target_id: str | None = Field(
        default=None, description="Id of the claim, experiment, review, or attempt to associate to."
    )
    role: str | None = Field(
        default=None,
        description="Resource association role. Use 'result' for experiment output files.",
        json_schema_extra={"enum": sorted(RESOURCE_ROLES)},
    )

    @model_validator(mode="after")
    def _check_modes(self) -> "ResourceRegisterInput":
        sources = [
            self.path is not None,
            self.paths is not None,
            self.resource_id is not None,
        ]
        if sum(sources) != 1:
            raise ValueError(
                "provide exactly one of 'path', 'paths', or 'resource_id'"
            )
        trio_present = [
            self.target_type is not None,
            self.target_id is not None,
            self.role is not None,
        ]
        if any(trio_present) and not all(trio_present):
            raise ValueError(
                "target_type, target_id, and role must be provided together"
            )
        if self.resource_id is not None and not all(trio_present):
            raise ValueError(
                "resource_id association mode requires target_type, target_id, and role"
            )
        return self


class ResourceDeleteInput(ProjectScopedInput):
    resource_id: str


class ResourceFindInput(ProjectScopedInput):
    resource_id: str | None = Field(
        default=None,
        description=(
            "Resolve one registered resource by id (hydrated row). Omit to list "
            "resources with the filters below."
        ),
    )
    include_history: bool = Field(
        default=False,
        description=(
            "In resolve mode, also return the resource's immutable observed "
            "'versions' (oldest-first) — the former resource.history."
        ),
    )
    kind: str | None = Field(
        default=None, description="List filter: one resource kind (e.g. 'dataset', 'code')."
    )
    experiment_id: str | None = Field(
        default=None, description="List filter: only resources associated with this experiment."
    )
    missing: bool | None = Field(
        default=None,
        description="List filter by file presence: true=only missing-on-disk, false=only present.",
    )
    compact: bool = Field(
        default=False,
        description=(
            "List mode: return a lean projection (id, path, kind, title, "
            "version_token, current_version_id, missing, updated_at) and OMIT "
            "the heavy nested current_version + associations. Use version_token "
            "to detect changes without re-pulling full payloads."
        ),
    )
    limit: int | None = Field(
        default=None, ge=1, description="List mode: max resources to return (page size)."
    )
    offset: int = Field(
        default=0, ge=0, description="List mode: number of resources to skip (pagination)."
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
        Literal["uploading", "completing", "available", "expired", "deleted"]
        | None
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
            raise ValueError(
                "version selects a resolve target; pass object_id or name"
            )
        return self


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
            "No entity ids (exp_/claim_/res_/rev_/rver_/syn_), no backticks "
            "or markdown, no newlines."
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


_PUBLIC_KEY_PREFIXES = (
    "ssh-ed25519 ",
    "ssh-rsa ",
    "ecdsa-sha2-nistp256 ",
    "ecdsa-sha2-nistp384 ",
    "ecdsa-sha2-nistp521 ",
    "sk-ssh-ed25519@openssh.com ",
    "sk-ecdsa-sha2-nistp256@openssh.com ",
)


def _validate_openssh_public_key(value: str | None) -> str | None:
    if value is None:
        return None
    key = value.strip()
    if not key:
        return None
    lowered = key.lower()
    if "private key" in lowered or key.startswith("-----BEGIN "):
        raise ValueError(
            "public_key must be an OpenSSH public key, not private-key material"
        )
    if "\n" in key or "\r" in key:
        raise ValueError("public_key must be a single line")
    if len(key) < 40 or len(key) > 8192:
        raise ValueError("public_key length is outside the accepted OpenSSH range")
    if not key.startswith(_PUBLIC_KEY_PREFIXES):
        raise ValueError(
            "public_key must start with a supported OpenSSH public key type"
        )
    parts = key.split()
    if len(parts) < 2:
        raise ValueError("public_key must include key type and base64 payload")
    return key


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
        return _validate_openssh_public_key(value)


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


TOOL_CONTRACTS: dict[str, ToolContract] = {
    "workflow.status_and_next": ToolContract(
        input_model=WorkflowStatusAndNextInput,
        description="Orient Codex from durable project/experiment state.",
    ),
    "project": ToolContract(
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
        input_model=ProjectUpdateInput,
        description="Update a project name, summary, policy knobs, or hidden state.",
    ),
    "project.get": ToolContract(
        input_model=ProjectGetInput,
        description="Get project metadata.",
    ),
    "project.list": ToolContract(
        input_model=EmptyInput,
        description="List projects in the current tool scope.",
    ),
    "claim.create": ToolContract(
        input_model=ClaimCreateInput,
        description=(
            "Create a claim. Check the project tool with action=\"overview\" "
            "first so you do not recreate a settled or abandoned claim."
        ),
    ),
    "claim.list": ToolContract(
        input_model=ClaimListInput,
        description="List claims.",
    ),
    "claim.update": ToolContract(
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
            "the wave covers — including new_terminal_experiments (the new "
            "signal since the last published wave) and each lens's previous "
            "reflection path. One wave may be open at a time. See the "
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
            "creates the approved experiment wave."
        ),
    ),
    "resource.register": ToolContract(
        input_model=ResourceRegisterInput,
        description=(
            "Register repo file(s) as resources and, optionally, associate them "
            "to a claim, experiment, review, or attempt in one call. Pass 'path' "
            "(one file) or 'paths' (a changed-files batch) to register/observe "
            "the file(s); add the association trio (target_type, target_id, "
            "role) to also associate each registered resource. Or pass "
            "'resource_id' to associate an ALREADY-registered resource — that "
            "mode requires the trio. Association validates the file against the "
            "role: gated artifacts (plan, report, graph, project_graph, "
            "reflection_doc, reflection_lens_doc, change_spec) are size-capped "
            "and their figure links resolved at register time, so keep them lean "
            "and reference raw data instead of inlining it."
        ),
    ),
    "resource.delete": ToolContract(
        input_model=ResourceDeleteInput,
        description=(
            "Delete a resource from active project tracking while preserving "
            "observed version history."
        ),
    ),
    "resource.find": ToolContract(
        input_model=ResourceFindInput,
        description=(
            "Find registered resources. Pass 'resource_id' (with optional "
            "include_history=true for its immutable observed versions) to "
            "resolve one hydrated resource; otherwise list resources with "
            "filters (kind/experiment_id/missing), pagination (limit/offset), "
            "and compact=true for a lean projection that omits the heavy "
            "current_version (use version_token to detect changes)."
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
    ),
    "storage.complete_upload": ToolContract(
        input_model=StorageCompleteUploadInput,
        description="Complete a storage upload and mark the ledger object available.",
    ),
    "storage.find": ToolContract(
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
    "storage.download_file": ToolContract(
        input_model=StorageDownloadFileInput,
        description=(
            "Resolve a storage object and download it to a local file, verifying "
            "size and sha256 before replacing the destination."
        ),
    ),
    "storage.object": ToolContract(
        input_model=StorageObjectInput,
        description=(
            "Apply a lifecycle action to one storage object by object_id: pin "
            "(expiry cleanup keeps it), unpin (restore its default expiry), renew "
            "(renew its default expiry window), or delete (drop the ledger alias, "
            "keeping history, and reclaim bytes when unreferenced)."
        ),
    ),
    "review.request": ToolContract(
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
        input_model=ReviewStartInput,
        description=(
            "Start a reviewer session for the pinned request snapshot. The "
            "reviewer skill supplies the procedural read-only boundary."
        ),
    ),
    "review.submit": ToolContract(
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
        input_model=ReviewStatusInput,
        description=(
            "Inspect review requests and submissions for a target, including "
            "recovery guidance for lost or expired reviewer capabilities."
        ),
    ),
    "sandbox.request": ToolContract(
        input_model=SandboxRequestInput,
        description=(
            "Procure (reuse or create) a project sandbox, optionally attached to "
            "an experiment, and return SSH details plus runtime guidance for the "
            "remote work folder, expiry, copy-out, and durable storage. "
            "On Thunder Compute or Lambda Labs, omit instance_type to "
            "receive a live menu of available machines to pick from. "
            "SSH key custody: the sandbox authorizes a caller-side public key. "
            "The primary path is bring-your-own-key — the requesting side "
            "generates its own ed25519 keypair (ssh-keygen) under the "
            "checkout's state dir: .merv/sandboxes/keys/, or the legacy "
            ".research_plugin/sandboxes/keys/ when this checkout already has "
            "a .research_plugin/ directory (the legacy dir wins when "
            "present). Before writing key material, create the "
            "state dir with a .gitignore containing '*' so keys can never be "
            "staged. It then passes its single-line "
            "OpenSSH PUBLIC key as public_key so it gets authorized on the VM. "
            "The response's persisted public_key_source is 'caller' for new "
            "requests; legacy 'managed' rows remain readable/releasable. "
            "ssh.key_path appears only when a local proxy enrichment knows the "
            "private key path."
        ),
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
            "or expired sandboxes. Includes public_key_source so callers know "
            "whether the VM authorized a caller-supplied public key or a "
            "legacy managed fallback key."
        ),
        hosted_control_sandbox_lookup=True,
    ),
    "sandbox.attach": ToolContract(
        input_model=SandboxAttachInput,
        description=(
            "Associate an existing running sandbox with an experiment without "
            "changing the VM, workdir, SSH connection, or lifecycle. A live "
            "sandbox can be associated with multiple active experiments."
        ),
    ),
    "sandbox.pull_outputs": ToolContract(
        input_model=SandboxPullOutputsInput,
        description=(
            "Copy selected files or directories from a running sandbox's remote "
            "experiment_dir into the local experiment folder over SSH/rsync. "
            "This is a proxy-local data tool: pass key_path for the caller-owned "
            "private key when sandbox.get does not already include ssh.key_path. "
            "Use object storage tools for heavy artifacts. Use this before "
            "resource.register or sandbox.release; "
            "omit paths to pull common retained outputs. "
            "Existing local files are kept unless overwrite=true — ones that "
            "differ from the sandbox are reported in files_kept_stale, so check "
            "it before registering results from a re-run. Remote symlinks and "
            "device nodes are never recreated locally. One failing path is "
            "reported in errors/paths_failed without discarding the rest."
        ),
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
    "sandbox.extend": ToolContract(
        input_model=SandboxExtendInput,
        description=(
            "Extend a running sandbox's expiry by at most one 30-minute "
            "increment, subject to provider support and tenant lifetime/spend "
            "quotas. Modal may reject this because its provider timeout is "
            "fixed when the sandbox is created."
        ),
    ),
    "sandbox.runs": ToolContract(
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
    "storage.download_file",
    "storage.find",
    "storage.object",
}

TOOL_PLANE_REGISTRY: dict[str, ToolPlane] = {
    "workflow.status_and_next": "control",
    # The merged agent-facing project tool. action=current/connect are served
    # by the local proxy (it owns the folder→project link store); action=create
    # forwards to the brain. Control-plane so the brain carries its schema and
    # serves create; the proxy intercepts current/connect before dispatch.
    "project": "control",
    "project.update": "control",
    "project.get": "control",
    "project.list": "control",
    "claim.create": "control",
    "claim.list": "control",
    "claim.update": "control",
    "experiment.create": "control",
    "experiment.list": "control",
    "experiment.get_state": "control",
    "experiment.materialize_folders": "data",
    "experiment.transition": "control",
    "experiment.exhibit": "control",
    "mlflow.context": "control",
    "mlflow.finalize_run": "control",
    "reflection.create": "control",
    "reflection.get": "control",
    "reflection.list": "control",
    "reflection.transition": "control",
    "resource.register": "data",
    "resource.delete": "control",
    "resource.find": "control",
    "storage.put_object": "control",
    "storage.upload_file": "data",
    "storage.complete_upload": "control",
    "storage.download_file": "data",
    "storage.find": "control",
    "storage.object": "control",
    "review.request": "control",
    "review.start": "control",
    "review.submit": "control",
    "review.status": "control",
    "sandbox.request": "data",
    "sandbox.options": "control",
    "sandbox.get": "control",
    "sandbox.attach": "data",
    "sandbox.pull_outputs": "data",
    "sandbox.list": "control",
    "sandbox.release": "control",
    "sandbox.extend": "control",
    "sandbox.terminal": "control",
    "sandbox.runs": "control",
    "sandbox.health": "control",
    "feed.register": "control",
    "feed.post": "data",
    "feed.list": "control",
}

_PLANE_REGISTRY_MISMATCH = set(TOOL_CONTRACTS) ^ set(TOOL_PLANE_REGISTRY)
if _PLANE_REGISTRY_MISMATCH:  # pragma: no cover - import-time contract guard
    raise RuntimeError(
        "TOOL_PLANE_REGISTRY must classify every tool exactly once: "
        f"{sorted(_PLANE_REGISTRY_MISMATCH)}"
    )

# Implemented and dispatchable (REST/UI reads, proxy-internal calls) but not
# advertised to agents. Hidden tools STAY in the served catalog carrying a
# ``hidden`` flag — the stdio proxy needs their plane/schema to route internal
# calls (e.g. the project tool's current action dials project.get upstream) —
# and the proxy drops them from the client-facing tools/list.
MCP_HIDDEN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "project.get",
        "project.update",
        # UI project picker only; the merged `project` tool (action=current)
        # is how agents orient. The proxy also hides it by literal for older
        # brains whose catalog predates the hidden flag.
        "project.list",
        # workflow.status_and_next already re-reports review state on every
        # poll, so the agent never needs a standalone review poll. Keep the
        # tool for REST/UI reads and internal dispatch; hide it from agents.
        "review.status",
        # Manual presign path: storage.upload_file's data plane composes these
        # by tool name (control_tool_call), so they stay dispatchable but are
        # dropped from the agent-facing tools/list.
        "storage.put_object",
        "storage.complete_upload",
        # UI-convenience delete: kept dispatchable for the REST/UI resource
        # panel, but dropped from the agent-facing tools/list.
        "resource.delete",
        # Enumeration readers whose payloads the agent already gets elsewhere:
        # workflow.status_and_next embeds active claims/experiments and the
        # live sandbox, reflection.get covers the open wave, and sandbox.get
        # supersets sandbox.list. They keep serving the REST/UI routes and
        # proxy-internal calls (materialize_folders lists experiments by tool
        # name; the doctor probes sandbox.health).
        "claim.list",
        "experiment.list",
        "reflection.list",
        "sandbox.list",
        "sandbox.health",
    }
)

_HIDDEN_UNKNOWN = MCP_HIDDEN_TOOL_NAMES - set(TOOL_CONTRACTS)
if _HIDDEN_UNKNOWN:  # pragma: no cover - import-time contract guard
    raise RuntimeError(
        f"MCP_HIDDEN_TOOL_NAMES references unknown tools: {sorted(_HIDDEN_UNKNOWN)}"
    )


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

def tool_plane(name: str) -> ToolPlane:
    return TOOL_PLANE_REGISTRY[name]


# Plane route sets, derived so the routing table and registry cannot drift.
# The proxy uses these to keep brain calls separate from checkout-local calls.
CONTROL_PLANE_TOOL_NAMES = frozenset(
    name for name, plane in TOOL_PLANE_REGISTRY.items() if plane == "control"
)
DATA_PLANE_TOOL_NAMES = frozenset(
    name for name, plane in TOOL_PLANE_REGISTRY.items() if plane == "data"
)


def static_tool_catalog(
    *, tool_names: set[str] | None = None, storage_enabled: bool | None = None
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
    for name, contract in TOOL_CONTRACTS.items():
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
            "plane": tool_plane(name),
        }
        if name in MCP_HIDDEN_TOOL_NAMES:
            tool["hidden"] = True
        catalog.append(tool)
    return catalog
