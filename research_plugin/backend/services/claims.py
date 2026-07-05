"""Claim memory service."""

from __future__ import annotations

from typing import Any

from ..domain.vocabulary import CLAIM_CONFIDENCES, CLAIM_STATUSES
from ..utils import NotFoundError, ValidationError
from ..utils import new_id
from ..state.store import BaseStateStore, row_to_dict, rows_to_dicts
from ..utils import now_iso


class ClaimService:
    def __init__(self, *, store: BaseStateStore) -> None:
        self.store = store

    def create(
        self,
        *,
        statement: str,
        scope: str = "",
        confidence: str = "medium",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if not statement.strip():
            raise ValidationError("statement is required")
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            self._reject_stopped_project(conn=conn, project_id=project_id)
            claim_id = new_id(prefix="claim")
            conn.execute(
                """
                INSERT INTO claims (id, project_id, statement, scope, status, confidence, created_at)
                VALUES (?, ?, ?, ?, 'active', ?, ?)
                """,
                (claim_id, project_id, statement.strip(), scope.strip(), confidence, now_iso()),
            )
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type="claim.created",
                target_type="claim",
                target_id=claim_id,
                payload={"statement": statement},
            )
            row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
            return dict(row)

    def update(
        self,
        *,
        claim_id: str,
        statement: str | None = None,
        scope: str | None = None,
        status: str | None = None,
        confidence: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if status is not None and status not in CLAIM_STATUSES:
            raise ValidationError(
                f"unknown claim status: {status}. Allowed: {', '.join(sorted(CLAIM_STATUSES))}"
            )
        if confidence is not None and confidence not in CLAIM_CONFIDENCES:
            raise ValidationError(
                f"unknown claim confidence: {confidence}. Allowed: {', '.join(sorted(CLAIM_CONFIDENCES))}"
            )
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            self._reject_stopped_project(conn=conn, project_id=project_id)
            row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"claim not found: {claim_id}")
            if row["project_id"] != project_id:
                raise NotFoundError(f"claim not found in project {project_id}: {claim_id}")
            next_statement = row["statement"] if statement is None else statement.strip()
            if not next_statement:
                raise ValidationError("statement is required")
            next_scope = row["scope"] if scope is None else scope.strip()
            next_status = row["status"] if status is None else status
            next_confidence = row["confidence"] if confidence is None else confidence
            conn.execute(
                """
                UPDATE claims
                SET statement = ?, scope = ?, status = ?, confidence = ?
                WHERE id = ?
                """,
                (next_statement, next_scope, next_status, next_confidence, claim_id),
            )
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type="claim.updated",
                target_type="claim",
                target_id=claim_id,
                payload={
                    "statement": next_statement,
                    "scope": next_scope,
                    "status": next_status,
                    "confidence": next_confidence,
                },
            )
            updated = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
            return row_to_dict(row=updated) or {}

    def create_from_synthesis(
        self,
        *,
        conn,
        project_id: str,
        synthesis_id: str,
        statement: str,
        scope: str,
        status: str,
        confidence: str,
        rationale: str,
    ) -> str:
        claim_id = new_id(prefix="claim")
        statement = statement.strip()
        conn.execute(
            """
            INSERT INTO claims (id, project_id, statement, scope, status, confidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                claim_id,
                project_id,
                statement,
                scope.strip(),
                status,
                confidence,
                now_iso(),
            ),
        )
        self.store.record_event(
            conn=conn,
            project_id=project_id,
            event_type="claim.created",
            target_type="claim",
            target_id=claim_id,
            payload={
                "statement": statement,
                "source_synthesis_id": synthesis_id,
                "rationale": rationale.strip(),
            },
        )
        return claim_id

    def update_from_synthesis(
        self,
        *,
        conn,
        project_id: str,
        synthesis_id: str,
        claim_id: str,
        statement: str | None = None,
        scope: str | None = None,
        status: str | None = None,
        confidence: str | None = None,
        rationale: str,
    ) -> str:
        row = conn.execute(
            "SELECT * FROM claims WHERE id = ? AND project_id = ?",
            (claim_id, project_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"claim not found: {claim_id}")
        next_statement = str(row["statement"]) if statement is None else statement.strip()
        next_scope = str(row["scope"]) if scope is None else scope.strip()
        next_status = str(row["status"]) if status is None else status
        next_confidence = str(row["confidence"]) if confidence is None else confidence
        conn.execute(
            """
            UPDATE claims
            SET statement = ?, scope = ?, status = ?, confidence = ?
            WHERE id = ?
            """,
            (next_statement, next_scope, next_status, next_confidence, claim_id),
        )
        self.store.record_event(
            conn=conn,
            project_id=project_id,
            event_type="claim.updated",
            target_type="claim",
            target_id=claim_id,
            payload={
                "statement": next_statement,
                "scope": next_scope,
                "status": next_status,
                "confidence": next_confidence,
                "source_synthesis_id": synthesis_id,
                "rationale": rationale.strip(),
            },
        )
        return claim_id

    def list_claims(self, *, project_id: str | None = None) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            rows = conn.execute(
                "SELECT * FROM claims WHERE project_id = ? ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
            return {"claims": rows_to_dicts(rows=rows)}
        finally:
            conn.close()

    def _reject_stopped_project(self, *, conn, project_id: str) -> None:
        row = conn.execute("SELECT status FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is not None and row["status"] == "stopped":
            raise ValidationError("project is stopped; claim mutations are not allowed")
