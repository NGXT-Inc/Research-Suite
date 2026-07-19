"""Reflection-wave artifact lints shared by the transition gates and the
local data-plane resource-validation helper.

Structure lives here so the two surfaces cannot drift. DB-backed checks
(claim existence, taken experiment names, active-experiment caps) are
injected as optional callbacks: the gate passes them, the preflight runs
structure-only.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from ...artifacts.markdown_images import markdown_image_links
from ...artifacts.resource_selection import preferred_associated_resource
from ...artifacts.roles import PROJECT_GRAPH_ROLE, PROJECT_GRAPH_ROLES, REFLECTION_LENS_DOC_ROLES
from .experiment_names import validate_experiment_name
from .experiment_policy import (
    ACTIVE_EXPERIMENT_CAP,
    active_experiment_cap_would_exceed_message,
)
from .reflection_gates import CORE_LENSES, CORE_LENS_IDS, ROSTER_SIZE
from .vocabulary import CLAIM_CONFIDENCES, CLAIM_STATUSES
from ...kernel.utils import ValidationError, WorkflowError

CHANGE_SPEC_SCHEMA_VERSION = 1
MAX_REFLECTION_DOC_BYTES = 16_000
REQUIRED_REFLECTION_DOC_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Summary", "summary"),
    ("Critical reading", "critical"),
    ("Decision / future directions", "decision"),
)

_CHANGE_SPEC_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_MD_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_LENS_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def reflection_doc_problems(text: str) -> list[str]:
    problems: list[str] = []
    stripped = text.strip()
    if not stripped:
        return ["reflection document is empty"]
    size = len(text.encode("utf-8"))
    if size > MAX_REFLECTION_DOC_BYTES:
        problems.append(
            f"reflection document is {size} bytes; keep it under "
            f"{MAX_REFLECTION_DOC_BYTES}"
        )
    headings = {
        re.sub(r"[^a-z0-9]+", " ", match.group(1).lower()).strip()
        for match in _MD_HEADING_RE.finditer(text)
    }
    for canonical, key in REQUIRED_REFLECTION_DOC_SECTIONS:
        if not any(heading.startswith(key) for heading in headings):
            problems.append(f"missing required section: {canonical}")
    return problems


def reflection_doc_review_problems(
    *, text: str, submitted_images: set[str], path: str
) -> list[str]:
    problems = reflection_doc_problems(text)
    for link in markdown_image_links(text):
        if link not in submitted_images:
            problems.append(
                f"image {link!r} has no submitted content: make sure the "
                f"file exists next to {path}, then re-associate the "
                "reflection document to submit it"
            )
    return problems


def reflection_lens_doc_problems(text: str) -> list[str]:
    # The roster gate rejects empty lens reflections; the byte cap is
    # enforced generically for all gated roles.
    if not text.strip():
        return ["reflection lens document is empty"]
    return []


def validate_reflection_roster(*, lenses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Envelope check for a reflection roster."""
    contract = (
        "the reflection roster must declare exactly "
        f"{ROSTER_SIZE} lenses: the {len(CORE_LENS_IDS)} core lenses "
        f"({', '.join(CORE_LENS_IDS)}) plus "
        f"{ROSTER_SIZE - len(CORE_LENS_IDS)} lenses you design for this "
        "project, each with a 'charter' and a 'why_distinct' stating how it "
        "differs from the core three and from each other"
    )
    if len(lenses) != ROSTER_SIZE:
        raise ValidationError(f"got {len(lenses)} lenses; {contract}")
    core_by_id = {lens["id"]: lens for lens in CORE_LENSES}
    roster: list[dict[str, Any]] = []
    seen: set[str] = set()
    for lens in lenses:
        lens_id = str(lens.get("id") or "").strip()
        if not _LENS_ID_RE.match(lens_id):
            raise ValidationError(
                f"invalid lens id {lens_id!r}: use a lowercase slug "
                "(letters, digits, '_', '-') — it doubles as the reflection "
                "filename (<lens_id>.md)"
            )
        if lens_id in seen:
            raise ValidationError(f"duplicate lens id: {lens_id}")
        seen.add(lens_id)
        charter = str(lens.get("charter") or "").strip()
        why = str(lens.get("why_distinct") or "").strip()
        core = core_by_id.get(lens_id)
        if core is not None:
            roster.append(
                {
                    "id": lens_id,
                    "title": str(lens.get("title") or "").strip() or core["title"],
                    "charter": charter or core["charter"],
                    "core": True,
                    "why_distinct": why,
                }
            )
            continue
        if not charter:
            raise ValidationError(
                f"lens {lens_id!r} needs a charter (what angle it reads the "
                f"project from); {contract}"
            )
        if not why:
            raise ValidationError(
                f"lens {lens_id!r} needs why_distinct (how it differs from "
                f"the core three and the other authored lens); {contract}"
            )
        roster.append(
            {
                "id": lens_id,
                "title": str(lens.get("title") or "").strip()
                or lens_id.replace("_", " ").replace("-", " "),
                "charter": charter,
                "core": False,
                "why_distinct": why,
            }
        )
    missing_core = [cid for cid in CORE_LENS_IDS if cid not in seen]
    if missing_core:
        raise ValidationError(
            f"missing core lens(es): {', '.join(missing_core)}; {contract}"
        )
    return roster


def reflection_requirement_roles(*, role: str) -> tuple[str, ...]:
    if role == "reflection_doc":
        return ("reflection_doc", "synthesis_doc")
    if role == "reflection_lens_doc":
        return REFLECTION_LENS_DOC_ROLES
    if role == PROJECT_GRAPH_ROLE:
        return PROJECT_GRAPH_ROLES
    return (role,)


def current_reflection_requirement_resource(
    *, reflection: dict[str, Any], role: str
) -> dict[str, Any] | None:
    return preferred_associated_resource(
        resources=reflection.get("current_attempt_resources") or [],
        attempt=reflection.get("attempt_index"),
        roles=reflection_requirement_roles(role=role),
    )


def reflection_coverage_for(*, reflection: dict[str, Any]) -> dict[str, Any]:
    stems: dict[str, dict[str, Any]] = {}
    for res in reflection.get("current_attempt_resources", []):
        if (
            res.get("association_role") not in REFLECTION_LENS_DOC_ROLES
            or res.get("missing")
        ):
            continue
        path = str(res.get("path") or "")
        name = path.rsplit("/", 1)[-1]
        stem = name.rsplit(".", 1)[0] if "." in name else name
        stems.setdefault(
            stem,
            {
                "path": path,
                "version_id": res.get("association_version_id"),
                "role": res.get("association_role"),
            },
        )
    lenses = []
    missing = []
    for lens in reflection.get("roster", []):
        lens_id = str(lens.get("id") or "")
        entry = stems.get(lens_id)
        lenses.append(
            {
                "lens_id": lens_id,
                "covered": entry is not None,
                "path": entry["path"] if entry else None,
                "version_id": entry.get("version_id") if entry else None,
                "role": entry.get("role") if entry else None,
            }
        )
        if entry is None:
            missing.append(lens_id)
    return {"lenses": lenses, "missing": missing, "complete": not missing}


def reflection_lens_checklist_items(
    *, reflection: dict[str, Any]
) -> list[dict[str, Any]]:
    coverage_by_lens = {
        str(row.get("lens_id") or ""): row
        for row in (reflection.get("reflection_coverage") or {}).get("lenses", [])
    }
    items: list[dict[str, Any]] = []
    for lens in reflection.get("roster", []):
        lens_id = str(lens.get("id") or "")
        coverage = coverage_by_lens.get(lens_id) or {}
        covered = bool(coverage.get("covered"))
        title = str(lens.get("title") or lens_id)
        item: dict[str, Any] = {
            "id": f"reflection_lens:{lens_id}",
            "kind": "reflection_lens",
            "role": "reflection_lens_doc",
            "lens_id": lens_id,
            "label": f"{title} reflection submitted",
            "satisfied": covered,
            "status": "present" if covered else "missing",
            "gate": "reflection_roster_incomplete",
            "action": "fan_out_reflection_subagents",
        }
        if covered:
            item["path"] = coverage.get("path")
            item["version_id"] = coverage.get("version_id")
            item["association_role"] = coverage.get("role")
        else:
            item["missing"] = (
                f"reflection doc for lens {lens_id!r} "
                "(role 'reflection_lens_doc', file <lens_id>.md)"
            )
        items.append(item)
    return items


def reflection_gate_resource_label(*, role: str) -> str:
    labels = {
        "project_graph": "Project graph present and valid",
        "reflection_doc": "Reflection document present and valid",
        "change_spec": "Change spec present and materializable",
        "reflection_lens_doc": "Per-lens reflections submitted",
    }
    return labels.get(role, f"{role} resource present")


def reflection_gate_review_label(*, role: str) -> str:
    labels = {"reflection_reviewer": "Reflection review passed"}
    return labels.get(role, f"{role} review passed")


def claim_change_problems(
    spec: dict[str, Any],
    *,
    problems: list[str],
    claim_exists: Callable[[str], bool] | None = None,
) -> dict[str, dict[str, Any]]:
    raw = spec.get("claim_changes", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        problems.append("claim_changes must be a list")
        return {}
    claim_keys: dict[str, dict[str, Any]] = {}
    updated_claim_ids: set[str] = set()
    for index, change in enumerate(raw):
        label = f"claim_changes[{index}]"
        if not isinstance(change, dict):
            problems.append(f"{label} must be an object")
            continue
        op = str(change.get("op") or "").strip()
        if op not in {"create", "update"}:
            problems.append(f"{label}.op must be 'create' or 'update'")
            continue
        if not str(change.get("rationale") or "").strip():
            problems.append(f"{label} needs a rationale")
        confidence = change.get("confidence")
        if confidence is not None and confidence not in CLAIM_CONFIDENCES:
            problems.append(
                f"{label}.confidence must be one of {', '.join(sorted(CLAIM_CONFIDENCES))}"
            )
        status = change.get("status")
        if status is not None and status not in CLAIM_STATUSES:
            problems.append(
                f"{label}.status must be one of {', '.join(sorted(CLAIM_STATUSES))}"
            )
        if op == "create":
            key = str(change.get("key") or "").strip()
            if key:
                if not _CHANGE_SPEC_KEY_RE.fullmatch(key):
                    problems.append(
                        f"{label}.key must start with a letter and use only "
                        "letters, digits, '_' and '-'"
                    )
                elif key in claim_keys:
                    problems.append(f"duplicate claim key: {key}")
                else:
                    claim_keys[key] = change
            if not str(change.get("statement") or "").strip():
                problems.append(f"{label}.statement is required for create")
        else:
            claim_id = str(change.get("claim_id") or "").strip()
            if not claim_id:
                problems.append(f"{label}.claim_id is required for update")
            elif claim_id in updated_claim_ids:
                problems.append(f"duplicate claim update: {claim_id}")
            elif claim_exists is not None and not claim_exists(claim_id):
                problems.append(f"{label}.claim_id not found in project: {claim_id}")
            else:
                updated_claim_ids.add(claim_id)
            if not any(
                field in change
                for field in ("statement", "scope", "status", "confidence")
            ):
                problems.append(
                    f"{label} update must include at least one of "
                    "statement, scope, status, confidence"
                )
    return claim_keys


def claim_refs(proposal: dict[str, Any]) -> list[str]:
    raw = proposal.get("tested_claim_refs", proposal.get("tested_claim_ids", []))
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def decision_problems(
    spec: dict[str, Any],
    *,
    problems: list[str],
    claim_keys: dict[str, dict[str, Any]],
    claim_exists: Callable[[str], bool] | None = None,
    experiment_name_taken: Callable[[str], bool] | None = None,
    non_terminal_experiments: Callable[[], list[str]] | None = None,
) -> None:
    decision = spec.get("decision")
    if not isinstance(decision, dict):
        problems.append("decision must be an object")
        return
    typ = str(decision.get("type") or "").strip()
    if typ != "create_experiments":
        problems.append("decision.type must be 'create_experiments'")
        return
    experiments = decision.get("experiments")
    if not isinstance(experiments, list):
        problems.append("decision.experiments must be a list")
        return
    if not experiments:
        problems.append(
            "decision.experiments must contain at least one experiment — the "
            "next wave the project runs; stopping is the researcher's call, "
            "not the reflection's"
        )
    if len(experiments) > 3:
        problems.append(
            "decision.experiments must contain no more than three experiments"
        )
    if non_terminal_experiments is not None:
        active_count = len(non_terminal_experiments())
        if active_count + len(experiments) > ACTIVE_EXPERIMENT_CAP:
            problems.append(
                active_experiment_cap_would_exceed_message(
                    active_count=active_count,
                    proposed_count=len(experiments),
                )
            )
    seen_names: set[str] = set()
    for index, proposal in enumerate(experiments):
        label = f"decision.experiments[{index}]"
        if not isinstance(proposal, dict):
            problems.append(f"{label} must be an object")
            continue
        key = str(proposal.get("key") or "").strip()
        if key and not _CHANGE_SPEC_KEY_RE.fullmatch(key):
            problems.append(
                f"{label}.key must start with a letter and use only "
                "letters, digits, '_' and '-'"
            )
        name = str(proposal.get("name") or "").strip()
        try:
            name = validate_experiment_name(name)
        except ValidationError as exc:
            problems.append(f"{label}.name invalid: {exc}")
            name = ""
        if name:
            lowered = name.lower()
            if lowered in seen_names:
                problems.append(f"duplicate experiment name in change spec: {name}")
            seen_names.add(lowered)
            if experiment_name_taken is not None and experiment_name_taken(name):
                problems.append(f"experiment name already exists in project: {name}")
        if not str(proposal.get("intent") or "").strip():
            problems.append(f"{label}.intent is required")
        if len(experiments) >= 2 and not str(proposal.get("parallelism") or "").strip():
            problems.append(
                f"{label}.parallelism is required for a multi-experiment wave; "
                "state why this experiment can run independently of the rest"
            )
        refs = claim_refs(proposal)
        if not refs:
            problems.append(f"{label} must reference at least one tested claim")
        for ref in refs:
            if ref in claim_keys:
                continue
            if claim_exists is not None and not claim_exists(ref):
                problems.append(f"{label} references unknown claim or claim key: {ref}")


def change_spec_structure_problems(text: str) -> list[str]:
    """Structure-only lint for preflight; the gate layers DB-backed checks
    (claim existence, taken names, active caps) on the same builders."""
    if not text.strip():
        return ["change spec is empty"]
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"change spec is not valid JSON: {exc}"]
    if not isinstance(spec, dict):
        return ["change spec must be a JSON object"]
    problems: list[str] = []
    if spec.get("version") != CHANGE_SPEC_SCHEMA_VERSION:
        problems.append(f"version must be {CHANGE_SPEC_SCHEMA_VERSION}")
    claim_keys = claim_change_problems(spec, problems=problems)
    decision_problems(spec, problems=problems, claim_keys=claim_keys)
    return problems


def parse_change_spec(
    *,
    text: str,
    path: str,
    claim_exists: Callable[[str], bool] | None = None,
    experiment_name_taken: Callable[[str], bool] | None = None,
    non_terminal_experiments: Callable[[], list[str]] | None = None,
) -> dict[str, Any]:
    """Validate a reviewed reflection change spec and return its JSON object."""
    problems: list[str] = []
    if not text.strip():
        raise WorkflowError(
            f"change spec {path!r} is empty — write it and "
            "re-associate to submit the content"
        )
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkflowError(
            f"change spec {path!r} is not valid JSON: {exc}. "
            "Write the role 'change_spec' artifact from "
            "skills/project-reflection/reflection-artifacts-template.md and "
            "re-associate it."
        ) from exc
    if not isinstance(spec, dict):
        raise WorkflowError(f"change spec {path!r} must be a JSON object")
    if spec.get("version") != CHANGE_SPEC_SCHEMA_VERSION:
        problems.append(f"version must be {CHANGE_SPEC_SCHEMA_VERSION}")

    claim_keys = claim_change_problems(
        spec,
        problems=problems,
        claim_exists=claim_exists,
    )
    decision_problems(
        spec,
        problems=problems,
        claim_keys=claim_keys,
        claim_exists=claim_exists,
        experiment_name_taken=experiment_name_taken,
        non_terminal_experiments=non_terminal_experiments,
    )
    if problems:
        raise WorkflowError(
            "change spec is not ready for review: "
            + "; ".join(problems)
            + ". Fix the file and re-associate it to submit the revision — "
            "see skills/project-reflection/reflection-artifacts-template.md."
        )
    return spec


def graph_diff(
    *, base_graph: dict[str, Any], current_graph: dict[str, Any]
) -> dict[str, Any]:
    base_nodes = _graph_node_index(graph=base_graph)
    current_nodes = _graph_node_index(graph=current_graph)
    base_edges = _graph_edge_index(graph=base_graph)
    current_edges = _graph_edge_index(graph=current_graph)
    return {
        "nodes": _diff_indexed_items(base=base_nodes, current=current_nodes),
        "edges": _diff_indexed_items(base=base_edges, current=current_edges),
    }


def graph_diff_summary(*, diff: dict[str, Any]) -> str:
    nodes = diff.get("nodes") or {}
    edges = diff.get("edges") or {}
    return (
        "Project graph diff: "
        f"{len(nodes.get('added') or [])} nodes added, "
        f"{len(nodes.get('removed') or [])} removed, "
        f"{len(nodes.get('changed') or [])} changed; "
        f"{len(edges.get('added') or [])} edges added, "
        f"{len(edges.get('removed') or [])} removed, "
        f"{len(edges.get('changed') or [])} changed."
    )


def _graph_node_index(*, graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "")
        if node_id:
            indexed[node_id] = _sorted_json_object(node)
    return indexed


def _graph_edge_index(*, graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for edge in graph.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        frm = str(edge.get("from") or "")
        to = str(edge.get("to") or "")
        if frm and to:
            indexed[f"{frm}->{to}"] = _sorted_json_object(edge)
    return indexed


def _sorted_json_object(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in sorted(item)}


def _diff_indexed_items(
    *, base: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    base_keys = set(base)
    current_keys = set(current)
    changed = []
    for key in sorted(base_keys & current_keys):
        before = base[key]
        after = current[key]
        if before == after:
            continue
        changed.append(
            {
                "id": key,
                "before": before,
                "after": after,
                "changed_fields": [
                    field
                    for field in sorted(set(before) | set(after))
                    if before.get(field) != after.get(field)
                ],
            }
        )
    return {
        "added": [current[key] for key in sorted(current_keys - base_keys)],
        "removed": [base[key] for key in sorted(base_keys - current_keys)],
        "changed": changed,
        "unchanged_count": len(base_keys & current_keys) - len(changed),
    }
