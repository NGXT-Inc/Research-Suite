"""Review-gate satisfaction predicate — the single source of truth.

Enforcement (experiment/reflection transitions), gate checklists, and
workflow guidance all read this one predicate, so the require_verified_reviews
policy cannot drift between surfaces. Kept out of reviews.py so the
experiment/reflection services can import it without a module cycle.
"""

from __future__ import annotations

from typing import Any

from .domain.gates import ReviewRequirement
from .domain.review_snapshot import review_snapshot_id
from .gate_evaluation import GateItem, RequirementEvaluation
from .projects import project_settings


def evaluate_review_gate(
    *,
    conn,
    target_type: str,
    target: dict[str, Any],
    review: ReviewRequirement,
) -> RequirementEvaluation:
    snapshot_id = review_snapshot_id(target_type=target_type, target=target)
    passes = conn.execute(
        """
        SELECT s.independence FROM reviews r
        JOIN review_sessions s ON s.id = r.session_id
        WHERE r.target_type = ? AND r.target_id = ? AND r.role = ?
          AND r.target_snapshot_id = ? AND r.verdict = 'pass'
        ORDER BY r.created_seq DESC
        """,
        (target_type, str(target["id"]), review.role, snapshot_id),
    ).fetchall()
    verified = any(
        str(row["independence"]) == "verified_agent_review" for row in passes
    )
    strict = bool(
        passes
        and project_settings(conn=conn, project_id=str(target["project_id"])).get(
            "require_verified_reviews"
        )
    )
    passed = bool(passes) and (verified or not strict)
    row = conn.execute(
        """
        SELECT id, status, expires_at
        FROM review_requests
        WHERE target_type = ? AND target_id = ? AND role = ?
          AND target_snapshot_id = ?
        ORDER BY created_seq DESC
        LIMIT 1
        """,
        (target_type, str(target["id"]), review.role, snapshot_id),
    ).fetchone()
    request = None if row is None else dict(row)
    review_status = "pending"
    if passed:
        review_status = "passed"
    elif request is not None and request.get("status") in {"requested", "started"}:
        review_status = str(request["status"])
    blocked_reason = (
        f"a {review.role} review passed but its independence is only attested "
        "(the reviewer did not present a session identity) and this project "
        "requires verified reviews (require_verified_reviews is on): request "
        "a fresh review and have the reviewer pass its own caller_session_id "
        "to review.start"
        if passes and strict and not verified
        else ""
    )
    error = "" if passed else blocked_reason or review.error
    item: GateItem = {
        "id": f"review:{review.role}",
        "kind": "review",
        "role": review.role,
        "label": review.label,
        "satisfied": bool(passed),
        "status": review_status,
        "gate": str(target["status"]),
    }
    if blocked_reason:
        item["problems"] = [blocked_reason]
    if request is not None:
        item.update(request_id=str(request["id"]), expires_at=str(request["expires_at"]))
    return RequirementEvaluation(
        role=review.role,
        status=review_status,
        blocker_code=review.blocker_code if not passed else "",
        enforcement_error=error,
        problems=(blocked_reason,) if blocked_reason else (),
        items=(item,),
    )
