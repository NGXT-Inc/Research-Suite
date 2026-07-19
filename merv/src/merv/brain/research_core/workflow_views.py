"""Workflow projections for agent-facing tool surfaces."""

from __future__ import annotations

from typing import Any

from .domain.vocabulary import EXPERIMENT_ACTIVE_PROCESS_STATUSES


# Agent-facing projection of status_and_next. The next-action decision is
# computed (in _workflow_for) from just status + resource roles + the review
# verdict, so the rest of the embedded get_state — the duplicate all-attempts
# `resources` list, per-resource version bookkeeping (version_token, mtime_ns,
# *_version_id, git_commit, timestamps), full review prose, and every *other*
# experiment's intent — is pure context bloat for a call the agent polls
# constantly. The UI keeps the full shape (it calls the service method
# directly); only the MCP tool is slimmed. See docs/MCP_SERVER_CONTRACT.md.
# association_version_id is the submission pin: agents (and tests) use it to
# confirm a re-associate actually submitted new content.
_SLIM_RESOURCE_FIELDS = (
    "id", "association_role", "association_version_id", "path", "kind",
    "missing", "size_bytes",
)
_SLIM_REVIEW_FIELDS = ("id", "role", "verdict", "created_at", "synopsis")
_SANDBOX_SUMMARY_FIELDS = (
    "sandbox_id", "status", "gpu", "cpu", "memory",
    "ssh_host", "ssh_port", "ssh_user", "workdir", "sandbox_data_dir", "expires_at",
)


def slim_status_and_next(full: dict[str, Any]) -> dict[str, Any]:
    """Project the rich status_and_next result down to what the agent needs."""
    workflow = full.get("workflow") or {}
    project = full.get("project") or {}
    experiment = full.get("experiment")

    if experiment is None:
        # Project-scoped orientation, reached only at project setup (once any
        # experiment exists, status_and_next auto-resolves to the latest one).
        # Surface existing claims compactly so the agent doesn't re-create them;
        # there are no experiments to list here by definition.
        result: dict[str, Any] = {
            "scope": "project",
            "experiment": None,
            "workflow": workflow,
            "project": {
                "id": project.get("id"),
                "name": project.get("name"),
                "summary": project.get("summary"),
                "claims": [
                    {
                        "id": claim.get("id"),
                        "status": claim.get("status"),
                        "confidence": claim.get("confidence"),
                        "statement": claim.get("statement"),
                    }
                    for claim in project.get("active_claims", [])
                ],
            },
        }
        if full.get("project_reflection"):
            result["project_reflection"] = full["project_reflection"]
        return result

    result = {
        "scope": "experiment",
        "workflow": workflow,
        "experiment": _slim_experiment(experiment),
        "sandbox": _sandbox_summary(full.get("sandboxes", [])),
        "project": {"id": project.get("id"), "name": project.get("name")},
    }
    if full.get("resource_refresh"):
        result["resource_refresh"] = full["resource_refresh"]
    # Project-level reflection orientation: an open reflection wave's state +
    # guidance, or the soft staleness nudge. Already slim (the workflow layer
    # builds it via slim_reflection); absent when there is nothing to say.
    if full.get("project_reflection"):
        result["project_reflection"] = full["project_reflection"]
    return result


def _slim_experiment(exp: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": exp.get("id"),
        "name": exp.get("name"),
        "status": exp.get("status"),
        "attempt_index": exp.get("attempt_index"),
        "intent": exp.get("intent"),
        "conclusion": exp.get("conclusion"),
        "updated_at": exp.get("updated_at"),
        "tested_claim_ids": [claim.get("id") for claim in exp.get("tested_claims", [])],
        "current_attempt_resources": [
            {field: res.get(field) for field in _SLIM_RESOURCE_FIELDS}
            for res in exp.get("current_attempt_resources", [])
        ],
        "reviews": [
            {field: review.get(field) for field in _SLIM_REVIEW_FIELDS}
            for review in exp.get("reviews", [])
        ],
    }


def slim_reflection(syn: dict[str, Any]) -> dict[str, Any]:
    """Agent-facing projection of a reflection wave for orientation calls.

    Drops the corpus snapshot, full resource payloads, and review
    findings/notes (keeps the short synopsis) — the orchestrator needs
    status, the roster, which lenses still owe a reflection, and what
    artifacts the current attempt carries.
    """
    return {
        "id": syn.get("id"),
        "title": syn.get("title"),
        "status": syn.get("status"),
        "attempt_index": syn.get("attempt_index"),
        "revision_context": syn.get("revision_context"),
        "roster": [
            {
                "id": lens.get("id"),
                "title": lens.get("title"),
                "core": lens.get("core"),
            }
            for lens in syn.get("roster", [])
        ],
        "reflection_coverage": syn.get("reflection_coverage"),
        "current_attempt_resources": [
            {field: res.get(field) for field in _SLIM_RESOURCE_FIELDS}
            for res in syn.get("current_attempt_resources", [])
        ],
        "reviews": [
            {field: review.get(field) for field in _SLIM_REVIEW_FIELDS}
            for review in syn.get("reviews", [])
        ],
        "allowed_transitions": syn.get("allowed_transitions", []),
    }


def _sandbox_summary(sandboxes: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse the sandbox row(s) to 'is there an active one, and if so what'."""
    active = next(
        (
            sb
            for sb in sandboxes
            if sb.get("status") in EXPERIMENT_ACTIVE_PROCESS_STATUSES
        ),
        None,
    )
    if active is not None:
        summary: dict[str, Any] = {"active": True}
        summary.update({field: active.get(field) for field in _SANDBOX_SUMMARY_FIELDS})
        return summary
    last = sandboxes[0] if sandboxes else None
    return {
        "active": False,
        "last_status": last.get("status") if last else None,
        "note": "No active sandbox for this experiment — call sandbox.request to create or reuse one.",
    }
