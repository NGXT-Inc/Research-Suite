"""Review request, session, and submission logic."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from ..utils import NotFoundError, PermissionDeniedError, ValidationError
from .experiments import ExperimentService
from ..utils import new_id
from .permissions import PermissionService
from ..state.store import StateStore, row_to_dict
from ..utils import now_iso


class ReviewService:
    """Owns review gates and capability-scoped reviewer sessions."""

    def __init__(
        self,
        *,
        store: StateStore,
        permissions: PermissionService,
        experiments: ExperimentService,
    ) -> None:
        self.store = store
        self.permissions = permissions
        self.experiments = experiments

    def request(
        self,
        *,
        target_type: str,
        target_id: str,
        role: str,
        reason: str = "",
        producer_session_id: str = "main",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        self.permissions.validate_review_role(role=role)
        if target_type != "experiment":
            raise ValidationError("v0.0001 supports experiment review targets only")
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            experiment = self.experiments.get_state(experiment_id=target_id, project_id=project_id, conn=conn)
            self._validate_role_matches_gate(experiment_status=experiment["status"], role=role)
            request_id = new_id(prefix="rr")
            capability = f"rp_{secrets.token_urlsafe(24)}"
            expires_at = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            snapshot_id = self._target_snapshot_id(conn=conn, target_type=target_type, target_id=target_id)
            conn.execute(
                """
                INSERT INTO review_requests (
                  id, project_id, target_type, target_id, role, reason, capability,
                  status, target_snapshot_id, producer_session_id, expires_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'requested', ?, ?, ?, ?)
                """,
                (
                    request_id,
                    project_id,
                    target_type,
                    target_id,
                    role,
                    reason,
                    capability,
                    snapshot_id,
                    producer_session_id,
                    expires_at,
                    now_iso(),
                ),
            )
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type="review.requested",
                target_type=target_type,
                target_id=target_id,
                payload={"role": role, "request_id": request_id},
            )
            return {
                "review_request_id": request_id,
                "reviewer_capability": capability,
                "role": role,
                "target_snapshot_id": snapshot_id,
                "target_snapshot": self.snapshot_from_id(snapshot_id=snapshot_id),
                "expires_at": expires_at,
                "reviewer_handoff": self.reviewer_handoff(role=role, target_type=target_type, target_id=target_id),
            }

    def start(
        self,
        *,
        review_request_id: str,
        reviewer_capability: str,
        declared_agent: str = "",
        caller_session_id: str = "",
    ) -> dict[str, Any]:
        with self.store.transaction() as conn:
            req = conn.execute("SELECT * FROM review_requests WHERE id = ?", (review_request_id,)).fetchone()
            if req is None:
                raise NotFoundError(f"review request not found: {review_request_id}")
            self._validate_request_open(req=req, capability=reviewer_capability)
            if caller_session_id and caller_session_id == req["producer_session_id"]:
                raise PermissionDeniedError("reviewer session must differ from producer session")
            snapshot_now = self._target_snapshot_id(conn=conn, target_type=req["target_type"], target_id=req["target_id"])
            if snapshot_now != req["target_snapshot_id"]:
                raise PermissionDeniedError("target changed after review capability was issued")
            session_id = new_id(prefix="rvs")
            independence = "verified_agent_review" if caller_session_id else "attested_agent_review"
            conn.execute(
                """
                INSERT INTO review_sessions (
                  id, request_id, declared_agent, caller_session_id, independence, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, 'started', ?)
                """,
                (session_id, review_request_id, declared_agent, caller_session_id, independence, now_iso()),
            )
            conn.execute("UPDATE review_requests SET status = 'started' WHERE id = ?", (review_request_id,))
            self.store.record_event(
                conn=conn,
                project_id=req["project_id"],
                event_type="review.started",
                target_type=req["target_type"],
                target_id=req["target_id"],
                payload={"role": req["role"], "request_id": review_request_id, "session_id": session_id},
            )
            return {
                "review_session_id": session_id,
                "role": req["role"],
                "target_type": req["target_type"],
                "target_id": req["target_id"],
                "independence": independence,
                "read_scope": ["claim", "experiment", "resource", "review"],
            }

    def submit(
        self,
        *,
        review_session_id: str,
        verdict: str,
        notes: str = "",
        findings: list[dict[str, Any]] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.permissions.validate_review_verdict(verdict=verdict)
        with self.store.transaction() as conn:
            session = conn.execute("SELECT * FROM review_sessions WHERE id = ?", (review_session_id,)).fetchone()
            if session is None:
                raise NotFoundError(f"review session not found: {review_session_id}")
            if session["status"] == "submitted":
                raise PermissionDeniedError("review session already submitted")
            req = conn.execute("SELECT * FROM review_requests WHERE id = ?", (session["request_id"],)).fetchone()
            if req is None:
                raise NotFoundError(f"review request not found: {session['request_id']}")
            review_id = new_id(prefix="rev")
            conn.execute(
                """
                INSERT INTO reviews (
                  id, project_id, request_id, session_id, target_snapshot_id, target_type, target_id,
                  role, verdict, notes, findings_json, evidence_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    req["project_id"],
                    req["id"],
                    review_session_id,
                    req["target_snapshot_id"],
                    req["target_type"],
                    req["target_id"],
                    req["role"],
                    verdict,
                    notes,
                    json.dumps(findings or [], sort_keys=True),
                    json.dumps(evidence or {}, sort_keys=True),
                    now_iso(),
                ),
            )
            conn.execute("UPDATE review_sessions SET status = 'submitted' WHERE id = ?", (review_session_id,))
            conn.execute("UPDATE review_requests SET status = 'submitted' WHERE id = ?", (req["id"],))
            self.store.record_event(
                conn=conn,
                project_id=req["project_id"],
                event_type="review.submitted",
                target_type=req["target_type"],
                target_id=req["target_id"],
                payload={"role": req["role"], "verdict": verdict, "review_id": review_id},
            )
            if req["target_type"] == "experiment" and verdict in {"needs_changes", "fail"}:
                revision_context = self._revision_context(
                    role=req["role"],
                    verdict=verdict,
                    notes=notes,
                    findings=findings or [],
                )
                self.experiments.send_back_to_planned(
                    conn=conn,
                    experiment_id=req["target_id"],
                    revision_context=revision_context,
                )
            review = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
            return self._hydrate_review(row=review)

    def status(self, *, target_type: str, target_id: str, project_id: str | None = None) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            requests = conn.execute(
                """
                SELECT id, target_type, target_id, role, status, target_snapshot_id,
                       producer_session_id, expires_at, created_at
                FROM review_requests
                WHERE project_id = ? AND target_type = ? AND target_id = ?
                ORDER BY rowid DESC
                """,
                (project_id, target_type, target_id),
            ).fetchall()
            reviews = conn.execute(
                "SELECT * FROM reviews WHERE project_id = ? AND target_type = ? AND target_id = ? ORDER BY rowid DESC",
                (project_id, target_type, target_id),
            ).fetchall()
            return {
                "requests": [self._hydrate_request(row=row) for row in requests],
                "reviews": [self._hydrate_review(row=row) for row in reviews],
            }
        finally:
            conn.close()

    def latest_verdict(self, *, conn, target_type: str, target_id: str, role: str) -> str | None:
        snapshot_id = self._target_snapshot_id(conn=conn, target_type=target_type, target_id=target_id)
        row = conn.execute(
            """
            SELECT verdict FROM reviews
            WHERE target_type = ? AND target_id = ? AND role = ? AND target_snapshot_id = ?
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (target_type, target_id, role, snapshot_id),
        ).fetchone()
        return None if row is None else str(row["verdict"])

    def has_open_request(self, *, conn, target_type: str, target_id: str, role: str) -> bool:
        return self.open_request(conn=conn, target_type=target_type, target_id=target_id, role=role) is not None

    def open_request(self, *, conn, target_type: str, target_id: str, role: str) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT id, target_type, target_id, role, status, target_snapshot_id,
                   producer_session_id, expires_at, created_at
            FROM review_requests
            WHERE target_type = ? AND target_id = ? AND role = ?
              AND status IN ('requested', 'started')
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (target_type, target_id, role),
        ).fetchone()
        return None if row is None else row_to_dict(row=row)

    def reviewer_handoff(self, *, role: str, target_type: str, target_id: str) -> dict[str, Any]:
        skill = {
            "design_reviewer": "design-review",
            "experiment_reviewer": "experiment-review",
        }.get(role, "")
        return {
            "role": role,
            "skill": skill,
            "target_type": target_type,
            "target_id": target_id,
            "read_only": True,
            "start_tool": "review.start",
            "submit_tool": "review.submit",
        }

    def snapshot_from_id(self, *, snapshot_id: str) -> dict[str, Any]:
        if "|" not in snapshot_id:
            target_type, _, target_id = snapshot_id.partition(":")
            return {"target_type": target_type, "target_id": target_id, "resources": []}
        parts = snapshot_id.split("|", 4)
        resources = []
        for token in (parts[4].split(",") if len(parts) > 4 and parts[4] else []):
            try:
                resource_and_version, role, attempt_index = token.rsplit(":", 2)
                resource_id, version_ref = resource_and_version.split(":", 1)
            except ValueError:
                resources.append({"raw": token})
                continue
            item: dict[str, Any] = {
                "resource_id": resource_id,
                "role": role,
                "attempt_index": self._int_or_zero(value=attempt_index),
            }
            if version_ref.startswith("rver_"):
                item["version_id"] = version_ref
            else:
                item["version_token"] = version_ref
            resources.append(item)
        return {
            "target_type": parts[0] if len(parts) > 0 else "",
            "target_id": parts[1] if len(parts) > 1 else "",
            "status": parts[2] if len(parts) > 2 else "",
            "attempt_index": self._int_or_zero(value=parts[3]) if len(parts) > 3 else 0,
            "resources": resources,
        }

    def _validate_request_open(self, *, req, capability: str) -> None:
        if req["capability"] != capability:
            raise PermissionDeniedError("invalid reviewer capability")
        if req["status"] not in {"requested", "started"}:
            raise PermissionDeniedError("review request is no longer open")
        expires = datetime.fromisoformat(str(req["expires_at"]).replace("Z", "+00:00"))
        if datetime.now(UTC) > expires:
            raise PermissionDeniedError("reviewer capability expired")

    def _validate_role_matches_gate(self, *, experiment_status: str, role: str) -> None:
        expected = {
            "design_review": "design_reviewer",
            "experiment_review": "experiment_reviewer",
        }.get(experiment_status)
        if role in {"human", "automated_check"}:
            return
        if expected is None:
            raise PermissionDeniedError(f"experiment is not currently awaiting {role}")
        if role != expected:
            raise PermissionDeniedError(f"active gate requires {expected}, not {role}")

    def _target_snapshot_id(self, *, conn, target_type: str, target_id: str) -> str:
        if target_type != "experiment":
            return f"{target_type}:{target_id}"
        exp = self.experiments.get_state(experiment_id=target_id, conn=conn)
        resource_tokens = [
            f"{res['id']}:{res.get('association_version_id') or res['version_token']}:{res.get('association_role', '')}:{res.get('association_attempt_index', 0)}"
            for res in exp.get("current_attempt_resources", [])
        ]
        return "|".join(
            [
                "experiment",
                exp["id"],
                exp["status"],
                str(exp["attempt_index"]),
                ",".join(sorted(resource_tokens)),
            ]
        )

    def _revision_context(self, *, role: str, verdict: str, notes: str, findings: list[dict[str, Any]]) -> str:
        finding_text = "; ".join(str(item.get("issue", "")) for item in findings if item.get("issue"))
        pieces = [f"{role} returned {verdict}"]
        if notes:
            pieces.append(notes)
        if finding_text:
            pieces.append(f"Findings: {finding_text}")
        return " | ".join(pieces)

    def _hydrate_request(self, *, row) -> dict[str, Any]:
        data = row_to_dict(row=row) or {}
        data["target_snapshot"] = self.snapshot_from_id(snapshot_id=data.get("target_snapshot_id", ""))
        return data

    def _hydrate_review(self, *, row) -> dict[str, Any]:
        data = row_to_dict(row=row) or {}
        data["findings"] = json.loads(data.pop("findings_json", "[]"))
        data["evidence"] = json.loads(data.pop("evidence_json", "{}"))
        data["target_snapshot"] = self.snapshot_from_id(snapshot_id=data.get("target_snapshot_id", ""))
        return data

    def _int_or_zero(self, *, value: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
