"""Review-gate satisfaction predicate — the single source of truth.

Enforcement (experiment/reflection transitions), gate checklists, and
workflow guidance all read this one predicate, so the require_verified_reviews
policy cannot drift between surfaces. Kept out of reviews.py so the
experiment/reflection services can import it without a module cycle.
"""

from __future__ import annotations

from typing import Any

from ..kernel.state.store import row_to_dict
from .domain.gates import ReviewRequirement
from .domain.review_snapshot import review_snapshot_id
from .projects import project_settings


def review_gate_state(
    *, conn, project_id: str, target_type: str, target_id: str, role: str, snapshot_id: str
) -> dict[str, Any]:
    """Whether reviews satisfy a gate, honoring require_verified_reviews.

    A gate is satisfied by a 'pass' review pinned to the current snapshot.
    When the project's require_verified_reviews policy is on, only passes with
    verified reviewer independence count; an attested-only pass reports a
    blocked_reason instead.
    """
    rows = conn.execute(
        """
        SELECT r.verdict, s.independence
        FROM reviews r
        JOIN review_sessions s ON s.id = r.session_id
        WHERE r.target_type = ? AND r.target_id = ? AND r.role = ?
          AND r.target_snapshot_id = ?
        ORDER BY r.created_seq DESC
        """,
        (target_type, target_id, role, snapshot_id),
    ).fetchall()
    verdict = str(rows[0]["verdict"]) if rows else None
    passes = [row for row in rows if str(row["verdict"]) == "pass"]
    if not passes:
        return {"verdict": verdict, "satisfied": False, "blocked_reason": None}
    verified = any(
        str(row["independence"]) == "verified_agent_review" for row in passes
    )
    settings = project_settings(conn=conn, project_id=project_id)
    if verified or not settings.get("require_verified_reviews"):
        return {"verdict": verdict, "satisfied": True, "blocked_reason": None}
    return {
        "verdict": verdict,
        "satisfied": False,
        "blocked_reason": (
            f"a {role} review passed but its independence is only attested "
            "(the reviewer did not present a session identity) and this "
            "project requires verified reviews (require_verified_reviews is "
            "on): request a fresh review and have the reviewer pass its own "
            "caller_session_id to review.start"
        ),
    }


def _review_checklist_item(
    *,
    conn,
    status: str,
    target_type: str,
    target: dict[str, Any],
    review: ReviewRequirement,
    label: str,
) -> dict[str, Any]:
    snapshot_id = review_snapshot_id(target_type=target_type, target=target)
    gate_state = review_gate_state(
        conn=conn,
        project_id=str(target["project_id"]),
        target_type=target_type,
        target_id=str(target["id"]),
        role=review.role,
        snapshot_id=snapshot_id,
    )
    passed = gate_state["satisfied"]
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
    request = row_to_dict(row=row)
    review_status = "pending"
    if passed:
        review_status = "passed"
    elif request is not None and request.get("status") in {"requested", "started"}:
        review_status = str(request["status"])
    item: dict[str, Any] = {
        "id": f"review:{review.role}",
        "kind": "review",
        "role": review.role,
        "label": label,
        "satisfied": passed,
        "status": review_status,
        "gate": status,
        "action": review.pass_action if passed else f"launch_{review.action_name}er",
        "skill": review.skill,
    }
    if gate_state.get("blocked_reason"):
        item["problems"] = [gate_state["blocked_reason"]]
    if request is not None:
        item["request_id"] = request["id"]
        item["expires_at"] = request["expires_at"]
    return item
