"""Agent-facing claim follow-ups derived from completed experiment facts."""

from __future__ import annotations

from typing import Any

from ...research_core.facade import ExperimentState, infer_claim_status_from_conclusion


def claim_update_suggestions(experiment: ExperimentState) -> list[dict[str, Any]]:
    if experiment.get("status") != "complete":
        return []
    conclusion = str(experiment.get("conclusion") or "").strip()
    suggested_status = infer_claim_status_from_conclusion(conclusion)
    if not conclusion or suggested_status is None:
        return []
    suggestions = []
    for claim in experiment.get("tested_claims") or []:
        claim_id = str(claim.get("id") or "")
        if not claim_id or str(claim.get("status") or "") == suggested_status:
            continue
        suggestions.append(
            {
                "tool": "claim.update",
                "arguments": {
                    "project_id": experiment.get("project_id"),
                    "claim_id": claim_id,
                    "status": suggested_status,
                },
                "claim": {field: claim.get(field) for field in
                          ("id", "statement", "status", "confidence", "scope")},
                "suggested_status": suggested_status,
                "reason": (
                    "Experiment completed with a passing review; apply a scoped "
                    "claim.update if this conclusion changes the claim's standing."
                ),
                "conclusion": conclusion,
                "requires_confirmation": True,
            }
        )
    return suggestions


__all__ = ["claim_update_suggestions"]
