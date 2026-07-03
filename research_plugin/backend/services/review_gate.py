"""Review-gate satisfaction predicate — the single source of truth.

Enforcement (experiment/reflection transitions), gate checklists, and
workflow guidance all read this one predicate, so the require_verified_reviews
policy cannot drift between surfaces. Kept out of reviews.py so the
experiment/synthesis services can import it without a module cycle.
"""

from __future__ import annotations

from typing import Any

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
