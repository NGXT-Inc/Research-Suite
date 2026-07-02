"""Reflection-wave artifact lints shared by the transition gates and the
data-plane preflight (`resource.validate`).

Structure lives here so the two surfaces cannot drift. DB-backed checks
(claim existence, taken experiment names, active-experiment caps) are
injected as optional callbacks: the gate passes them, the preflight runs
structure-only.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from .experiment_names import validate_experiment_name
from .experiment_policy import (
    ACTIVE_EXPERIMENT_CAP,
    active_experiment_cap_would_exceed_message,
)
from .vocabulary import CLAIM_CONFIDENCES, CLAIM_STATUSES
from ..utils import ValidationError

CHANGE_SPEC_SCHEMA_VERSION = 1
MAX_SYNTHESIS_DOC_BYTES = 16_000
REQUIRED_SYNTHESIS_DOC_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Summary", "summary"),
    ("Critical reading", "critical"),
    ("Decision / future directions", "decision"),
)

_CHANGE_SPEC_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_MD_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)


def reflection_doc_problems(text: str) -> list[str]:
    problems: list[str] = []
    stripped = text.strip()
    if not stripped:
        return ["reflection document is empty"]
    size = len(text.encode("utf-8"))
    if size > MAX_SYNTHESIS_DOC_BYTES:
        problems.append(
            f"reflection document is {size} bytes; keep it under "
            f"{MAX_SYNTHESIS_DOC_BYTES}"
        )
    headings = {
        re.sub(r"[^a-z0-9]+", " ", match.group(1).lower()).strip()
        for match in _MD_HEADING_RE.finditer(text)
    }
    for canonical, key in REQUIRED_SYNTHESIS_DOC_SECTIONS:
        if not any(heading.startswith(key) for heading in headings):
            problems.append(f"missing required section: {canonical}")
    return problems


def reflection_lens_doc_problems(text: str) -> list[str]:
    # The roster gate rejects empty lens reflections; the byte cap is
    # enforced generically for all gated roles.
    if not text.strip():
        return ["reflection lens document is empty"]
    return []


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
    if typ == "hard_stop":
        if not str(decision.get("rationale") or "").strip():
            problems.append("decision.rationale is required for hard_stop")
        if non_terminal_experiments is not None:
            active = non_terminal_experiments()
            if active:
                problems.append(
                    "hard_stop requires no non-terminal experiments; active: "
                    + ", ".join(active)
                )
        return
    if typ != "create_experiments":
        problems.append("decision.type must be 'hard_stop' or 'create_experiments'")
        return
    experiments = decision.get("experiments")
    if not isinstance(experiments, list):
        problems.append("decision.experiments must be a list")
        return
    if len(experiments) < 2:
        problems.append(
            "decision.experiments must contain at least two experiments so "
            "the approved wave can run in parallel"
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
        if not str(proposal.get("parallelism") or "").strip():
            problems.append(
                f"{label}.parallelism is required; state why this experiment "
                "can run independently of the rest of the wave"
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
