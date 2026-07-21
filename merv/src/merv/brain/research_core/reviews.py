"""Review request, session, and submission logic."""

from __future__ import annotations

from contextlib import closing
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from merv.shared.artifact_roles import EXHIBIT_ROLE, GATED_ROLES

from ..kernel.secret_tokens import hash_secret, mint_secret, secret_digest_matches
from ..kernel.events import StoredEvent, freeze_json_object
from ..kernel.utils import (
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
    format_iso,
    new_id,
    now_iso,
    parse_iso,
)
from ..artifacts.pinned import PinnedStore
from .domain.review_gates import (
    expected_review_gate_role,
    is_review_gate_exempt,
)
from .domain.review_handoff import reviewer_handoff_payload
from .domain.review_returns import (
    resolve_review_return,
    revision_context_for_review_return,
)
from .domain.review_snapshot import snapshot_from_id
from .domain.review_validation import validate_review_role, validate_review_verdict
from .domain.synopsis import validate_synopsis
from .domain.vocabulary import LOCAL_TENANT_ID
from ..kernel.state.store import BaseStateStore, next_created_seq, row_to_dict
from .experiments import ExperimentService
from .review_gate import review_gate_state
from .reflections import ReflectionService


class ReviewService:
    """Owns review gates and capability-scoped reviewer sessions.

    Reviews are target-polymorphic: an experiment review pins the experiment's
    snapshot and routes rejections to planned/running; a reflection review pins
    the reflection wave's snapshot and routes rejections to
    reflecting/synthesizing. The capability machinery (plaintext returned once,
    snapshot pinning, and producer-session rejection) is shared. Reviewer skills
    provide the procedural read-only boundary.
    """

    def __init__(
        self,
        *,
        store: BaseStateStore,
        experiments: ExperimentService,
        reflections: ReflectionService,
        pinned: PinnedStore | None = None,
    ) -> None:
        self.store = store
        self.experiments = experiments
        self.reflections = reflections
        self.pinned = pinned

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
        validate_review_role(role=role)
        if target_type not in {"experiment", "reflection"}:
            raise ValidationError("review targets must be 'experiment' or 'reflection'")
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            if target_type == "experiment":
                target = self.experiments.get_state(
                    experiment_id=target_id, project_id=project_id, conn=conn
                )
            else:
                target = self.reflections.get_state(
                    reflection_id=target_id, project_id=project_id, conn=conn
                )
            self._validate_role_matches_gate(
                target_type=target_type, target_status=target["status"], role=role
            )
            # Refresh is revoke-and-reissue: a new capability for the same gate
            # closes every prior open request, so a lost or stale capability can
            # never race the fresh one to submit.
            superseded = [
                str(row["id"])
                for row in conn.execute(
                    """
                    SELECT id FROM review_requests
                    WHERE project_id = ? AND target_type = ? AND target_id = ?
                      AND role = ? AND status IN ('requested', 'started')
                    """,
                    (project_id, target_type, target_id, role),
                ).fetchall()
            ]
            if superseded:
                placeholders = ", ".join("?" for _ in superseded)
                conn.execute(
                    f"UPDATE review_requests SET status = 'superseded' WHERE id IN ({placeholders})",
                    (*superseded,),
                )
            request_id = new_id(prefix="rr")
            # The plaintext capability is minted here, returned ONCE to the
            # caller, and never stored — only its sha256 lands in the row (cloud
            # plan Phase 7). review.start resolves by hashing the presented token.
            capability = mint_secret(prefix="rp_", nbytes=24)
            expires_at = format_iso(datetime.now(UTC) + timedelta(hours=1))
            snapshot_id = self._target_snapshot_id(
                conn=conn, target_type=target_type, target_id=target_id
            )
            conn.execute(
                """
                INSERT INTO review_requests (
                  id, project_id, target_type, target_id, role, reason, capability_hash,
                  status, target_snapshot_id, producer_session_id, expires_at, created_at,
                  created_seq
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'requested', ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    project_id,
                    target_type,
                    target_id,
                    role,
                    reason,
                    hash_secret(capability),
                    snapshot_id,
                    producer_session_id,
                    expires_at,
                    now_iso(),
                    next_created_seq(conn=conn, table="review_requests"),
                ),
            )
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type="review.requested",
                target_type=target_type,
                target_id=target_id,
                payload={
                    "role": role,
                    "request_id": request_id,
                    "superseded_request_ids": superseded,
                },
            )
            return {
                "review_request_id": request_id,
                "reviewer_capability": capability,
                "role": role,
                "target_snapshot_id": snapshot_id,
                "target_snapshot": self.snapshot_from_id(snapshot_id=snapshot_id),
                "expires_at": expires_at,
                "reviewer_handoff": self.reviewer_handoff(
                    role=role,
                    target_type=target_type,
                    target_id=target_id,
                    review_request_id=request_id,
                    reviewer_capability=capability,
                ),
            }

    def start(
        self,
        *,
        review_request_id: str,
        reviewer_capability: str,
        declared_agent: str = "",
        caller_session_id: str = "",
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        # ``tenant_id`` is reserved for the future user-auth layer. None means
        # the current private/local surface and skips tenant scoping.
        caller_session_id = caller_session_id.strip()
        if not caller_session_id:
            raise ValidationError(
                "caller_session_id is required: pass the reviewer's own "
                "session identity (any stable identifier for the reviewing "
                "agent's session, distinct from the producer session that "
                "requested the review) so reviewer independence can be "
                "verified"
            )
        with self.store.transaction() as conn:
            req = conn.execute(
                "SELECT * FROM review_requests WHERE id = ?", (review_request_id,)
            ).fetchone()
            if req is None:
                raise NotFoundError(f"review request not found: {review_request_id}")
            if tenant_id is not None:
                owner = conn.execute(
                    "SELECT tenant_id FROM projects WHERE id = ?", (req["project_id"],)
                ).fetchone()
                if owner is None or str(owner["tenant_id"]) != tenant_id:
                    # Same shape as an unknown request: do not confirm the
                    # target exists to a foreign tenant.
                    raise NotFoundError(
                        f"review request not found: {review_request_id}"
                    )
            self._validate_request_open(req=req, capability=reviewer_capability)
            if caller_session_id == req["producer_session_id"]:
                raise PermissionDeniedError(
                    "reviewer session must differ from producer session"
                )
            snapshot_now = self._target_snapshot_id(
                conn=conn, target_type=req["target_type"], target_id=req["target_id"]
            )
            if snapshot_now != req["target_snapshot_id"]:
                raise PermissionDeniedError(
                    "target changed after review capability was issued"
                )
            session_id = new_id(prefix="rvs")
            # caller_session_id is mandatory, so every new session is verified;
            # 'attested_agent_review' survives only on legacy rows.
            independence = "verified_agent_review"
            conn.execute(
                """
                INSERT INTO review_sessions (
                  id, request_id, declared_agent, caller_session_id, tenant_id,
                  independence, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'started', ?)
                """,
                (
                    session_id,
                    review_request_id,
                    declared_agent,
                    caller_session_id,
                    tenant_id if tenant_id is not None else LOCAL_TENANT_ID,
                    independence,
                    now_iso(),
                ),
            )
            conn.execute(
                "UPDATE review_requests SET status = 'started' WHERE id = ?",
                (review_request_id,),
            )
            self.store.record_event(
                conn=conn,
                project_id=req["project_id"],
                event_type="review.started",
                target_type=req["target_type"],
                target_id=req["target_id"],
                payload={
                    "role": req["role"],
                    "request_id": review_request_id,
                    "session_id": session_id,
                },
            )
            return {
                "review_session_id": session_id,
                "role": req["role"],
                "target_type": req["target_type"],
                "target_id": req["target_id"],
                "independence": independence,
                "read_scope": [
                    "claim",
                    "experiment",
                    "reflection",
                    "resource",
                    "review",
                ],
                # The reviewer grades the SUBMITTED artifacts — the bytes
                # pinned at associate — not whatever the working tree holds
                # now. Hydrated here so a reviewer never has to trust disk.
                "submitted_artifacts": self._submitted_artifacts(
                    conn=conn,
                    target_type=str(req["target_type"]),
                    target_id=str(req["target_id"]),
                ),
            }

    def _submitted_artifacts(
        self, *, conn, target_type: str, target_id: str
    ) -> list[dict[str, Any]]:
        """The target's current-attempt gated-role artifacts, with content."""
        if self.pinned is None:
            return []
        table = {"experiment": "experiments", "reflection": "reflections"}.get(
            target_type
        )
        if table is None:
            return []
        attempt = conn.execute(
            f"SELECT attempt_index FROM {table} WHERE id = ?", (target_id,)
        ).fetchone()
        if attempt is None:
            return []
        rows = conn.execute(
            """
            SELECT a.role, a.version_id, r.path, v.project_id, v.content_sha256
            FROM resource_associations a
            JOIN resources r ON r.id = a.resource_id
            LEFT JOIN resource_versions v ON v.id = a.version_id
            WHERE a.target_type = ? AND a.target_id = ? AND a.attempt_index = ?
              AND r.deleted = 0
            ORDER BY a.created_seq
            """,
            (target_type, target_id, int(attempt["attempt_index"])),
        ).fetchall()
        artifacts: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in reversed(rows):  # newest association per (role, path) wins
            role = str(row["role"])
            # Gated agent artifacts plus the system metrics exhibit: the
            # exhibit exists only as pinned bytes (no working-tree file), so
            # hydration is the reviewer's one way to read it.
            if role not in GATED_ROLES and role != EXHIBIT_ROLE:
                continue
            key = (role, str(row["path"]))
            if key in seen:
                continue
            seen.add(key)
            entry: dict[str, Any] = {
                "role": role,
                "path": str(row["path"]),
                "version_id": str(row["version_id"]) if row["version_id"] else None,
            }
            try:
                entry["content"] = self.pinned.submitted_text(
                    project_id=str(row["project_id"]),
                    sha256=str(row["content_sha256"]),
                )
            except Exception:  # noqa: BLE001 — hydration is best-effort
                entry["content"] = None
                entry["note"] = (
                    "submitted content unavailable; ask the producer to re-associate"
                )
            artifacts.append(entry)
        artifacts.reverse()
        return artifacts

    def submit(
        self,
        *,
        review_session_id: str,
        verdict: str,
        synopsis: str,
        notes: str = "",
        findings: list[dict[str, Any]] | None = None,
        evidence: dict[str, Any] | None = None,
        return_to: str = "",
    ) -> dict[str, Any]:
        validate_review_verdict(verdict=verdict)
        try:
            synopsis = validate_synopsis(synopsis)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        with self.store.transaction() as conn:
            session = conn.execute(
                "SELECT * FROM review_sessions WHERE id = ?", (review_session_id,)
            ).fetchone()
            if session is None:
                raise NotFoundError(f"review session not found: {review_session_id}")
            if session["status"] == "submitted":
                raise PermissionDeniedError("review session already submitted")
            req = conn.execute(
                "SELECT * FROM review_requests WHERE id = ?", (session["request_id"],)
            ).fetchone()
            if req is None:
                raise NotFoundError(
                    f"review request not found: {session['request_id']}"
                )
            if req["status"] != "started":
                raise PermissionDeniedError(
                    "review request is no longer open (superseded by a fresh "
                    "capability or already submitted)"
                )
            # The verdict applies to the pinned snapshot the reviewer graded.
            # If the target moved on (e.g. a sibling review already passed the
            # gate), a stale session must not mutate it.
            snapshot_now = self._target_snapshot_id(
                conn=conn, target_type=req["target_type"], target_id=req["target_id"]
            )
            if snapshot_now != req["target_snapshot_id"]:
                raise PermissionDeniedError(
                    "target changed after this review started; the verdict no "
                    "longer applies — request a fresh review"
                )
            try:
                return_to = resolve_review_return(
                    target_type=req["target_type"],
                    role=req["role"],
                    verdict=verdict,
                    return_to=return_to,
                )
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            review_id = new_id(prefix="rev")
            conn.execute(
                """
                INSERT INTO reviews (
                  id, project_id, request_id, session_id, target_snapshot_id, target_type, target_id,
                  role, verdict, return_to, notes, synopsis, findings_json, evidence_json, created_at,
                  created_seq
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    return_to,
                    notes,
                    synopsis,
                    json.dumps(findings or [], sort_keys=True),
                    json.dumps(evidence or {}, sort_keys=True),
                    now_iso(),
                    next_created_seq(conn=conn, table="reviews"),
                ),
            )
            conn.execute(
                "UPDATE review_sessions SET status = 'submitted' WHERE id = ?",
                (review_session_id,),
            )
            conn.execute(
                "UPDATE review_requests SET status = 'submitted' WHERE id = ?",
                (req["id"],),
            )
            self.store.record_event(
                conn=conn,
                project_id=req["project_id"],
                event_type="review.submitted",
                target_type=req["target_type"],
                target_id=req["target_id"],
                payload={
                    "role": req["role"],
                    "verdict": verdict,
                    "review_id": review_id,
                    "return_to": return_to,
                    "synopsis": synopsis,
                },
            )
            if verdict in {"needs_changes", "fail"}:
                revision_context = revision_context_for_review_return(
                    target_type=req["target_type"],
                    role=req["role"],
                    verdict=verdict,
                    notes=notes,
                    findings=findings or [],
                    return_to=return_to,
                )
                if req["target_type"] == "experiment":
                    if return_to == "running":
                        self.experiments.send_back_to_running(
                            conn=conn,
                            experiment_id=req["target_id"],
                            revision_context=revision_context,
                        )
                    else:
                        self.experiments.send_back_to_planned(
                            conn=conn,
                            experiment_id=req["target_id"],
                            revision_context=revision_context,
                        )
                elif req["target_type"] == "reflection":
                    if return_to == "reflecting":
                        self.reflections.send_back_to_reflecting(
                            conn=conn,
                            reflection_id=req["target_id"],
                            revision_context=revision_context,
                        )
                    else:
                        self.reflections.send_back_to_synthesizing(
                            conn=conn,
                            reflection_id=req["target_id"],
                            revision_context=revision_context,
                        )
            review = conn.execute(
                "SELECT * FROM reviews WHERE id = ?", (review_id,)
            ).fetchone()
            return self._hydrate_review(row=review)

    def status(
        self, *, target_type: str, target_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        with closing(self.store.connect()) as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            requests = conn.execute(
                """
                SELECT id, target_type, target_id, role, status, target_snapshot_id,
                       producer_session_id, expires_at, created_at
                FROM review_requests
                WHERE project_id = ? AND target_type = ? AND target_id = ?
                ORDER BY created_seq DESC
                """,
                (project_id, target_type, target_id),
            ).fetchall()
            reviews = conn.execute(
                "SELECT * FROM reviews WHERE project_id = ? AND target_type = ? AND target_id = ? ORDER BY created_seq DESC",
                (project_id, target_type, target_id),
            ).fetchall()
            return {
                "requests": [self._with_snapshot(row=row) for row in requests],
                "reviews": [self._hydrate_review(row=row) for row in reviews],
            }

    def latest_submitted_event(
        self, *, target_type: str, target_id: str, project_id: str | None = None
    ) -> StoredEvent | None:
        """Return the durable event for the newest verdict without appending one."""
        with closing(self.store.connect()) as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            row = conn.execute(
                """
                SELECT id, project_id, type, target_type, target_id, payload_json, created_at
                FROM events
                WHERE project_id = ? AND type = 'review.submitted'
                  AND target_type = ? AND target_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (project_id, target_type, target_id),
            ).fetchone()
        if row is None:
            return None
        return StoredEvent(
            id=int(row["id"]),
            project_id=str(row["project_id"]),
            type=str(row["type"]),
            target_type=str(row["target_type"]),
            target_id=str(row["target_id"]),
            payload=freeze_json_object(json.loads(str(row["payload_json"]))),
            created_at=str(row["created_at"]),
        )

    def queue(self, *, project_id: str | None = None) -> dict[str, Any]:
        with closing(self.store.connect()) as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            req_rows = conn.execute(
                """
                SELECT id, target_type, target_id, role, status, reason, target_snapshot_id,
                       producer_session_id, expires_at, created_at
                FROM review_requests
                WHERE project_id = ?
                ORDER BY created_seq DESC
                """,
                (project_id,),
            ).fetchall()
            review_rows = conn.execute(
                """
                SELECT id, request_id, target_snapshot_id, target_type, target_id, role, verdict,
                       notes, synopsis, created_at
                FROM reviews
                WHERE project_id = ?
                ORDER BY created_seq DESC
                """,
                (project_id,),
            ).fetchall()
            return {
                "requests": [self._with_snapshot(row=row) for row in req_rows],
                "reviews": [self._with_snapshot(row=row) for row in review_rows],
            }

    def open_requests_for_target(
        self,
        *,
        project_id: str | None,
        experiment_id: str,
        statuses: tuple[str, ...] = ("requested", "started"),
    ) -> list[dict[str, Any]]:
        if not statuses:
            return []
        with closing(self.store.connect()) as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            placeholders = ", ".join("?" for _ in statuses)
            rows = conn.execute(
                f"""
                SELECT id, role, status, reason, created_at
                FROM review_requests
                WHERE project_id = ? AND target_type = 'experiment' AND target_id = ?
                  AND status IN ({placeholders})
                ORDER BY created_seq
                """,
                (project_id, experiment_id, *statuses),
            ).fetchall()
            return [row_to_dict(row=row) or {} for row in rows]

    def assert_request_in_project(
        self, *, project_id: str | None, review_request_id: Any
    ) -> None:
        with closing(self.store.connect()) as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            if not review_request_id:
                raise ValidationError("review_request_id is required")
            row = conn.execute(
                "SELECT project_id FROM review_requests WHERE id = ?",
                (review_request_id,),
            ).fetchone()
            if row is None or row["project_id"] != project_id:
                raise NotFoundError(
                    f"review request not found in project {project_id}: {review_request_id}"
                )

    def request_project_id(self, *, review_request_id: Any) -> str | None:
        if not review_request_id:
            return None
        with closing(self.store.connect()) as conn:
            row = conn.execute(
                "SELECT project_id FROM review_requests WHERE id = ?",
                (str(review_request_id),),
            ).fetchone()
            return str(row["project_id"]) if row else None

    def assert_session_in_project(
        self, *, project_id: str | None, review_session_id: Any
    ) -> None:
        with closing(self.store.connect()) as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            if not review_session_id:
                raise ValidationError("review_session_id is required")
            row = conn.execute(
                """
                SELECT rr.project_id AS project_id
                FROM review_sessions rs
                JOIN review_requests rr ON rr.id = rs.request_id
                WHERE rs.id = ?
                """,
                (review_session_id,),
            ).fetchone()
            if row is None or row["project_id"] != project_id:
                raise NotFoundError(
                    f"review session not found in project {project_id}: {review_session_id}"
                )

    def gate_state(
        self, *, conn, target_type: str, target_id: str, role: str
    ) -> dict[str, Any]:
        """review_gate_state for the target's current pinned snapshot."""
        table = "experiments" if target_type == "experiment" else "reflections"
        row = conn.execute(
            f"SELECT project_id FROM {table} WHERE id = ?", (target_id,)
        ).fetchone()
        return review_gate_state(
            conn=conn,
            project_id=str(row["project_id"]) if row else "",
            target_type=target_type,
            target_id=target_id,
            role=role,
            snapshot_id=self._target_snapshot_id(
                conn=conn, target_type=target_type, target_id=target_id
            ),
        )

    def open_request(
        self, *, conn, target_type: str, target_id: str, role: str
    ) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT id, target_type, target_id, role, status, target_snapshot_id,
                   producer_session_id, expires_at, created_at
            FROM review_requests
            WHERE target_type = ? AND target_id = ? AND role = ?
              AND status IN ('requested', 'started')
            ORDER BY created_seq DESC
            LIMIT 1
            """,
            (target_type, target_id, role),
        ).fetchone()
        return None if row is None else row_to_dict(row=row)

    def _with_snapshot(self, *, row) -> dict[str, Any]:
        data = row_to_dict(row=row) or {}
        data["target_snapshot"] = self.snapshot_from_id(
            snapshot_id=data.get("target_snapshot_id", "")
        )
        if "status" in data and "expires_at" in data:
            data["recovery"] = self._request_recovery(request=data)
        return data

    def reviewer_handoff(
        self,
        *,
        role: str,
        target_type: str,
        target_id: str,
        review_request_id: str = "",
        reviewer_capability: str = "",
    ) -> dict[str, Any]:
        return reviewer_handoff_payload(
            role=role,
            target_type=target_type,
            target_id=target_id,
            review_request_id=review_request_id,
            reviewer_capability=reviewer_capability,
        )

    def snapshot_from_id(self, *, snapshot_id: str) -> dict[str, Any]:
        return snapshot_from_id(snapshot_id=snapshot_id)

    def _validate_request_open(self, *, req, capability: str) -> None:
        # Constant-time compare of the presented token's hash against the stored
        # hash (cloud plan Phase 7): the plaintext capability never sits at rest.
        presented = hash_secret(capability)
        if not secret_digest_matches(
            stored_digest=req["capability_hash"], presented_digest=presented
        ):
            raise PermissionDeniedError("invalid reviewer capability")
        if req["status"] not in {"requested", "started"}:
            raise PermissionDeniedError("review request is no longer open")
        expires = parse_iso(req["expires_at"])
        if expires is None or datetime.now(UTC) > expires:
            raise PermissionDeniedError("reviewer capability expired")

    def _validate_role_matches_gate(
        self, *, target_type: str, target_status: str, role: str
    ) -> None:
        if is_review_gate_exempt(role=role):
            return
        expected = expected_review_gate_role(
            target_type=target_type,
            target_status=target_status,
        )
        if expected is None:
            raise PermissionDeniedError(
                f"{target_type} is not currently awaiting {role}"
            )
        if role != expected:
            raise PermissionDeniedError(f"active gate requires {expected}, not {role}")

    def _target_snapshot_id(self, *, conn, target_type: str, target_id: str) -> str:
        if target_type == "experiment":
            return self.experiments.target_snapshot_id(
                conn=conn, experiment_id=target_id
            )
        if target_type == "reflection":
            return self.reflections.target_snapshot_id(
                conn=conn, reflection_id=target_id
            )
        return f"{target_type}:{target_id}"

    def _request_recovery(self, *, request: dict[str, Any]) -> dict[str, Any]:
        status = str(request.get("status") or "")
        expires = parse_iso(str(request.get("expires_at") or ""))
        expired = expires is None or datetime.now(UTC) > expires
        open_status = status in {"requested", "started"}
        can_refresh = open_status
        reason = (
            "capability lost or expired; request a fresh reviewer capability "
            "for the same target and role (this revokes the open request — "
            "the old capability can no longer start or submit)"
            if can_refresh
            else "review request is closed; inspect submitted reviews instead"
        )
        recovery: dict[str, Any] = {
            "capability_returned_once": True,
            "capability_available": False,
            "expired": expired,
            "can_request_fresh_capability": can_refresh,
            "reason": reason,
        }
        if can_refresh:
            recovery["tool"] = "review.request"
            recovery["arguments"] = {
                "target_type": request.get("target_type"),
                "target_id": request.get("target_id"),
                "role": request.get("role"),
            }
        return recovery

    def _hydrate_review(self, *, row) -> dict[str, Any]:
        data = row_to_dict(row=row) or {}
        data["findings"] = json.loads(data.pop("findings_json", "[]"))
        data["evidence"] = json.loads(data.pop("evidence_json", "{}"))
        data["target_snapshot"] = self.snapshot_from_id(
            snapshot_id=data.get("target_snapshot_id", "")
        )
        return data
