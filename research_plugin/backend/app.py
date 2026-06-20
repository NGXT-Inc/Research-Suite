"""Application composition root and MCP tool facade."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError as PydanticValidationError

from .contracts import ContractModel, TOOL_CONTRACTS
from .dataplane import LocalDataPlaneWorker
from .utils import ResearchPluginError
from .utils import ValidationError as ToolValidationError
from .execution import SandboxBackend, build_sandbox_backend
from .execution.ssh_rsync import SshRsyncSyncer
from .workspace import LocalWorkspace
from .services import (
    ClaimService,
    ExperimentService,
    FeedService,
    PermissionService,
    ProjectService,
    ResourceService,
    ReviewService,
    SandboxService,
    SynthesisService,
    WorkflowService,
)
from .services.sandbox_mgmt_keys import LocalMgmtKeyStore
from .state import (
    ActivityLogger,
    BaseStateStore,
    StateStore,
    ToolCallStore,
    monotonic_ms,
    rows_to_dicts,
)
from .state.blobs import BlobStore, LocalDirBlobStore
from .observability import StructuredLogger
from .services.workflow_gates import TERMINAL_STATUSES as EXPERIMENT_TERMINAL_STATUSES
from .domain.vocabulary import PROJECT_GRAPH_ROLES


@dataclass(frozen=True)
class ToolSpec:
    description: str
    input_model: type[ContractModel]
    handler: Callable[..., dict[str, Any]]

    def input_schema(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return schema

    def call(
        self,
        *,
        raw_arguments: dict[str, Any],
        internal_kwargs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = self.input_model.model_validate(raw_arguments)
        kwargs = request.model_dump()
        if internal_kwargs:
            kwargs.update(internal_kwargs)
        return self.handler(**kwargs)


def _contract_error_message(*, exc: PydanticValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(part) for part in first.get("loc", ())) or "input"
    error_type = first.get("type")
    if error_type == "missing":
        return f"{loc} is required"
    if error_type == "extra_forbidden":
        return f"unexpected field: {loc}"
    return f"{loc}: {first.get('msg', 'invalid value')}"


def _assert_tool_contracts_match_handlers(
    *, handlers: dict[str, Callable[..., dict[str, Any]]]
) -> None:
    handler_names = set(handlers)
    contract_names = set(TOOL_CONTRACTS)
    if handler_names == contract_names:
        return
    missing_handlers = sorted(contract_names - handler_names)
    missing_contracts = sorted(handler_names - contract_names)
    raise AssertionError(
        "tool handler/contract mismatch"
        f"; missing handlers: {', '.join(missing_handlers) or 'none'}"
        f"; missing contracts: {', '.join(missing_contracts) or 'none'}"
    )


class ResearchPluginApp:
    """Composes isolated components behind tool-call contracts."""

    def __init__(
        self,
        *,
        repo_root: Path,
        db_path: Path,
        execution_backend: SandboxBackend | None = None,
        rsync_syncer: SshRsyncSyncer | None = None,
        store: BaseStateStore | None = None,
        blobs: "BlobStore | None" = None,
        task_channel: Any | None = None,
    ) -> None:
        # The plane seam (cloud plan Phase 3): the record store knows nothing
        # about the checkout; local paths flow from the workspace and every
        # local-IO duty routes through the data-plane worker. This constructor
        # IS the local-mode composition — it binds both planes in one process.
        self.workspace = LocalWorkspace(repo_root=repo_root)
        # Store injection (cloud plan Phase 6): the dual-dialect contract
        # tests hand in a PostgresStateStore; absent that, local mode builds
        # its SQLite store at db_path exactly as before. The control profile
        # (Phase 8) injects a PostgresStateStore + S3BlobStore via the control
        # composition root rather than db_url plumbing through here.
        self.store = store if store is not None else StateStore(db_path=db_path)
        # Telemetry sinks are machine-local by construction: composition hands
        # them explicit paths (the control composition gets its own sinks).
        self.activity = ActivityLogger(repo_root=self.workspace.repo_root)
        # Full-fidelity tool-call recorder backing the debug analyzer. Isolated in
        # its own SQLite file so its churn never touches the state DB.
        self.tool_calls = ToolCallStore(
            db_path=self.workspace.research_dir / "tool_calls.sqlite"
        )
        # Structured cloud log stream (cloud plan Phase 9): one redacted JSON
        # line per tool call / HTTP request to stdout, in control mode only.
        # Dormant (disabled) in local mode, so behavior is byte-identical.
        self.structured_logger = StructuredLogger()
        self.permissions = PermissionService()
        # Content-addressed store for gated-artifact bytes (and figures and
        # parachute objects). Local mode roots it next to the state DB; the
        # control composition injects an S3BlobStore (Phase 8). Same protocol,
        # same contract tests, so the rest of the app is blob-impl-blind.
        self.blobs = blobs if blobs is not None else LocalDirBlobStore(
            root=self.workspace.research_dir / "blobs"
        )
        if execution_backend is None:
            execution_backend = build_sandbox_backend(
                repo_root=self.workspace.repo_root,
                activity=self._activity_hook,
            )
        self.execution_backend = execution_backend
        self.worker = LocalDataPlaneWorker(
            workspace=self.workspace,
            backend=execution_backend,
            rsync_syncer=rsync_syncer,
        )
        self.projects = ProjectService(store=self.store)
        self.claims = ClaimService(store=self.store)
        self.experiments = ExperimentService(
            store=self.store,
            blobs=self.blobs,
        )
        self.resources = ResourceService(
            store=self.store,
            permissions=self.permissions,
            workspace=self.workspace,
            blobs=self.blobs,
        )
        # One-time local upgrade: capture bytes for gated associations made
        # before byte capture existed (idempotent, skips present blobs).
        self.resources.backfill_gated_blobs()
        self.syntheses = SynthesisService(
            store=self.store,
            blobs=self.blobs,
        )
        self.reviews = ReviewService(
            store=self.store,
            permissions=self.permissions,
            experiments=self.experiments,
            syntheses=self.syntheses,
            blobs=self.blobs,
        )
        self.sandboxes = SandboxService(
            store=self.store,
            sandbox_backend=execution_backend,
            worker=self.worker,
            activity=self.activity,
            experiments=self.experiments,
            # Per-sandbox management keys (plan Phase 5): control-plane
            # custody — local mode roots them under .research_plugin/ beside
            # the rest of the control state.
            mgmt_keys=LocalMgmtKeyStore(
                root=self.workspace.research_dir / "mgmt_keys"
            ),
            # Decision 7's one shared blob store also holds parachute objects.
            blobs=self.blobs,
            # Split mode (Phase 8): the control composition injects an
            # HttpTaskChannel so control enqueues data-plane work to the daemon
            # over HTTP. None ⇒ the synchronous in-process channel (local mode).
            task_channel=task_channel,
        )
        self.workflow = WorkflowService(
            store=self.store,
            experiments=self.experiments,
            reviews=self.reviews,
            sandboxes=self.sandboxes,
            resources=self.resources,
            syntheses=self.syntheses,
        )
        # Feed (Feed_PRD.md) is a self-contained module: it owns its schema,
        # tools, HTTP routes, and UI, and nothing in the research workflow
        # depends on it. Constructed here purely as a composition-root wiring.
        self.feed = FeedService(
            store=self.store,
            workspace=self.workspace,
            blobs=self.blobs,
        )
        handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "workflow.status_and_next": self.workflow.status_and_next_agent,
            "project.create": self.projects.create,
            "project.update": self.projects.update,
            "project.get": self.projects.get,
            "project.current": self.current_project,
            "project.list": self.projects.list_projects,
            "claim.create": self.claims.create,
            "claim.list": self.claims.list_claims,
            "claim.update": self.claims.update,
            "experiment.create": self.experiments.create,
            "experiment.list": self.experiments.list_experiments_agent,
            "experiment.get_state": self.experiments.get_state_agent,
            "experiment.transition": self.experiments.transition,
            "reflection.create": self.reflection_create,
            "reflection.get": self.reflection_get,
            "reflection.list": self.reflection_list,
            "reflection.transition": self.reflection_transition,
            "resource.register_file": self.resources.register_file,
            "resource.associate": self.resources.associate,
            "resource.delete": self.resources.delete,
            "resource.list": self.resources.list_resources,
            "resource.resolve": self.resources.resolve,
            "review.request": self.reviews.request,
            "review.start": self.reviews.start,
            "review.submit": self.reviews.submit,
            "review.status": self.reviews.status,
            "sandbox.request": self.sandboxes.request,
            "sandbox.options": self.sandboxes.options,
            "sandbox.get": self.sandboxes.get,
            "sandbox.sync": self.sandboxes.sync,
            "sandbox.list": self.sandboxes.list_sandboxes,
            "sandbox.release": self.sandboxes.release,
            "sandbox.terminal": self.sandboxes.terminal,
            "sandbox.health": self.sandboxes.health,
            "feed.register": self.feed.register,
            "feed.post": self.feed.post,
            "feed.list": self.feed.list_posts,
        }
        _assert_tool_contracts_match_handlers(handlers=handlers)
        self._tools = {
            name: ToolSpec(contract.description, contract.input_model, handlers[name])
            for name, contract in TOOL_CONTRACTS.items()
        }
        # Plane annotation per tool (cloud plan §3.3): served so the stdlib-only
        # proxy can route from the catalog without importing contracts.
        self._tool_planes = {
            name: contract.plane for name, contract in TOOL_CONTRACTS.items()
        }

    def reflection_create(
        self,
        *,
        project_id: str,
        title: str = "",
        lenses: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self._external_reflection_state(
            self.syntheses.create(project_id=project_id, title=title, lenses=lenses or [])
        )

    def reflection_get(
        self, *, project_id: str, reflection_id: str
    ) -> dict[str, Any]:
        return self._external_reflection_state(
            self.syntheses.get_state(synthesis_id=reflection_id, project_id=project_id)
        )

    def reflection_list(self, *, project_id: str) -> dict[str, Any]:
        state = self.syntheses.list_syntheses(project_id=project_id)
        return {
            "count": state.get("count", len(state.get("syntheses", []))),
            "reflections": [
                self._external_reflection_state(item)
                for item in state.get("syntheses", [])
            ],
        }

    def reflection_transition(
        self, *, project_id: str, reflection_id: str, transition: str
    ) -> dict[str, Any]:
        internal_transition = (
            "submit_synthesis"
            if transition == "submit_reflection_artifacts"
            else transition
        )
        return self._external_reflection_state(
            self.syntheses.transition(
                project_id=project_id,
                synthesis_id=reflection_id,
                transition=internal_transition,
            )
        )

    def _external_reflection_state(self, state: dict[str, Any]) -> dict[str, Any]:
        output = dict(state)
        if output.get("status") == "synthesis_review":
            output["status"] = "reflection_review"
        if "allowed_transitions" in output:
            output["allowed_transitions"] = [
                self._external_reflection_transition(item)
                for item in output.get("allowed_transitions", [])
            ]
        return output

    def _external_reflection_transition(self, item: Any) -> Any:
        if not isinstance(item, dict):
            return item
        output = dict(item)
        if output.get("transition") == "submit_synthesis":
            output["transition"] = "submit_reflection_artifacts"
        if output.get("leads_to") == "synthesis_review":
            output["leads_to"] = "reflection_review"
        text_fields = ("requires", "description")
        for field in text_fields:
            if isinstance(output.get(field), str):
                output[field] = output[field].replace(
                    "synthesis_reviewer",
                    "reflection_reviewer",
                ).replace(
                    "submit_synthesis",
                    "submit_reflection_artifacts",
                )
        return output

    def current_project(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        """Project identity plus the small orientation block every agent sees.

        The base shape stays compatible (`exists`, `project`, `hint`). When a
        project exists, `at_a_glance` points to the latest reflection artifacts
        and names what project work is newer than that reflection.
        """
        current = self.projects.current(tenant_id=tenant_id)
        if not current.get("exists"):
            return current
        project = current.get("project") or {}
        project_id = str(project.get("id") or "")
        if not project_id:
            return current
        return {
            **current,
            "at_a_glance": self._project_at_a_glance(project_id=project_id),
        }

    def _project_at_a_glance(self, *, project_id: str) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            latest = self.syntheses.latest_published(conn=conn, project_id=project_id)
            open_wave = self.syntheses.open_synthesis(conn=conn, project_id=project_id)
            experiments = rows_to_dicts(
                rows=conn.execute(
                    """
                    SELECT id, name, intent, status, attempt_index, created_at, updated_at
                    FROM experiments
                    WHERE project_id = ?
                    ORDER BY created_at
                    """,
                    (project_id,),
                ).fetchall()
            )
            recent_claims = rows_to_dicts(
                rows=conn.execute(
                    """
                    SELECT c.id, c.statement, c.status, c.confidence
                    FROM claims c
                    LEFT JOIN events e
                      ON e.project_id = c.project_id
                     AND e.target_type = 'claim'
                     AND e.target_id = c.id
                     AND e.type IN ('claim.created', 'claim.updated')
                    WHERE c.project_id = ?
                    GROUP BY c.id
                    ORDER BY COALESCE(MAX(e.created_at), c.created_at) DESC, c.created_at DESC
                    LIMIT 5
                    """,
                    (project_id,),
                ).fetchall()
            )
            claim_events: list[dict[str, Any]] = []
            if latest is not None:
                publish_event = conn.execute(
                    """
                    SELECT id FROM events
                    WHERE project_id = ? AND type = 'synthesis.transitioned'
                      AND target_type = 'synthesis' AND target_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (project_id, latest.get("id")),
                ).fetchone()
                if publish_event is not None:
                    claim_events = rows_to_dicts(
                        rows=conn.execute(
                            """
                            SELECT id, type, target_id, payload_json, created_at
                            FROM events
                            WHERE project_id = ? AND target_type = 'claim'
                              AND type IN ('claim.created', 'claim.updated')
                              AND id > ?
                            ORDER BY id
                            """,
                            (project_id, publish_event["id"]),
                        ).fetchall()
                    )
                elif latest.get("published_at"):
                    claim_events = rows_to_dicts(
                        rows=conn.execute(
                            """
                            SELECT id, type, target_id, payload_json, created_at
                            FROM events
                            WHERE project_id = ? AND target_type = 'claim'
                              AND type IN ('claim.created', 'claim.updated')
                              AND created_at >= ?
                            ORDER BY id
                            """,
                            (project_id, latest.get("published_at")),
                        ).fetchall()
                    )
        finally:
            conn.close()

        terminal_statuses = set(EXPERIMENT_TERMINAL_STATUSES)
        terminal_experiments = [
            exp for exp in experiments if str(exp.get("status")) in terminal_statuses
        ]
        active_experiments = [
            exp for exp in experiments if str(exp.get("status")) not in terminal_statuses
        ]
        corpus = (latest or {}).get("corpus") or {}
        covered_ids = {
            str(exp.get("id"))
            for exp in corpus.get("terminal_experiments", [])
        }
        experiments_since_reflection = [
            exp for exp in terminal_experiments if str(exp.get("id")) not in covered_ids
        ]
        changed_claim_ids = [
            str(event.get("target_id"))
            for event in claim_events
            if event.get("target_id")
            and self._event_payload(event).get("source_synthesis_id") != (latest or {}).get("id")
        ]
        seen_claim_ids: set[str] = set()
        changed_claim_ids = [
            claim_id
            for claim_id in changed_claim_ids
            if not (claim_id in seen_claim_ids or seen_claim_ids.add(claim_id))
        ]

        project_reflection = None
        if latest is not None:
            graph = self._resource_link_for_role(
                synthesis=latest,
                roles=PROJECT_GRAPH_ROLES,
                label="Current project graph",
                canonical_role="project_graph",
            )
            reflection_doc = self._resource_link_for_role(
                synthesis=latest,
                roles=("reflection_doc", "synthesis_doc"),
                label="Latest reflection doc",
                canonical_role="reflection_doc",
            )
            project_reflection = {
                "reflection_id": latest.get("id"),
                "time": latest.get("published_at"),
                "reflection_doc_resource_id": (
                    reflection_doc.get("resource_id") if reflection_doc else None
                ),
                "project_graph_resource_id": graph.get("resource_id") if graph else None,
            }

        covered_count = len(
            covered_ids & {str(exp.get("id")) for exp in terminal_experiments}
        )
        return {
            "summary": self._at_a_glance_summary(
                latest=latest,
                terminal_count=len(terminal_experiments),
                covered_count=covered_count,
                experiments_since=len(experiments_since_reflection),
                claims_changed=len(changed_claim_ids),
            ),
            "recent": {
                "experiments": [
                    {
                        "id": exp.get("id"),
                        "name": exp.get("name"),
                        "status": exp.get("status"),
                    }
                    for exp in sorted(
                        experiments,
                        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
                        reverse=True,
                    )[:5]
                ],
                "claims": [
                    {
                        "id": claim.get("id"),
                        "status": claim.get("status"),
                        "confidence": claim.get("confidence"),
                        "statement": claim.get("statement"),
                    }
                    for claim in recent_claims
                ],
            },
            "project_reflection": project_reflection,
            "since_reflection": {
                "finished_experiment_ids": [
                    str(exp.get("id")) for exp in experiments_since_reflection
                ],
                "changed_claim_ids": changed_claim_ids,
                "active_experiment_ids": [
                    str(exp.get("id")) for exp in active_experiments
                ],
            },
            "open_reflection_id": open_wave.get("id") if open_wave else None,
        }

    def _event_payload(self, event: dict[str, Any]) -> dict[str, Any]:
        raw = event.get("payload_json")
        if not raw:
            return {}
        try:
            payload = json.loads(str(raw))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _resource_link_for_role(
        self,
        *,
        synthesis: dict[str, Any],
        roles: tuple[str, ...],
        label: str,
        canonical_role: str,
    ) -> dict[str, Any] | None:
        attempt = synthesis.get("attempt_index")
        candidates = [
            res
            for res in synthesis.get("resources", [])
            if res.get("association_role") in roles
            and res.get("association_attempt_index") == attempt
        ]
        if not candidates:
            return None
        role_rank = {role: index for index, role in enumerate(roles)}
        res = min(
            candidates,
            key=lambda item: (
                role_rank.get(str(item.get("association_role")), len(roles)),
                -(item.get("association_rowid") or 0),
            ),
        )
        return {
            "label": label,
            "kind": "resource",
            "role": canonical_role,
            "legacy_role": (
                res.get("association_role")
                if res.get("association_role") != canonical_role
                else None
            ),
            "resource_id": res.get("id"),
            "path": res.get("path"),
            "version_id": res.get("association_version_id"),
            "read_with": "resource.resolve",
            "read_args": {"resource_id": res.get("id"), "include_history": True},
        }

    def _at_a_glance_summary(
        self,
        *,
        latest: dict[str, Any] | None,
        terminal_count: int,
        covered_count: int,
        experiments_since: int,
        claims_changed: int,
    ) -> str:
        if latest is None:
            summary = (
                f"No published reflection; 0/{terminal_count} finished "
                f"experiments covered; {terminal_count} finished experiments since."
            )
            if terminal_count >= 3:
                summary += " New reflection recommended."
            return summary
        pieces = [f"Latest reflection covers {covered_count}/{terminal_count} finished experiments"]
        if experiments_since:
            pieces.append(f"{experiments_since} finished experiments since")
        if claims_changed:
            pieces.append(f"{claims_changed} claims changed since")
        if len(pieces) == 1:
            pieces.append("no newer experiment or claim changes detected")
        summary = "; ".join(pieces) + "."
        if experiments_since >= 3:
            summary += " New reflection recommended."
        return summary

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "description": spec.description,
                "inputSchema": spec.input_schema(),
                "plane": self._tool_planes[name],
            }
            for name, spec in self._tools.items()
        ]

    def shutdown(self) -> None:
        """Best-effort: stop background provisioning jobs and the sync poller."""
        try:
            self.sandboxes.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.execution_backend.shutdown()
        except Exception:  # noqa: BLE001
            pass

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        activity_source: str = "app",
        internal_kwargs: dict[str, Any] | None = None,
        telemetry_project_id: str | None = None,
    ) -> dict[str, Any]:
        arguments = arguments or {}
        telemetry_arguments = arguments
        if telemetry_project_id:
            telemetry_arguments = {
                **arguments,
                "project_id": telemetry_project_id,
            }
        started = monotonic_ms()
        try:
            if name not in self._tools:
                raise ResearchPluginError(f"unknown tool: {name}", details={"tool": name})
            self.permissions.reject_reviewer_mutation(
                tool_name=name,
                review_session_id=arguments.get("review_session_id"),
            )
            try:
                result = self._tools[name].call(
                    raw_arguments=arguments,
                    internal_kwargs=internal_kwargs,
                )
            except PydanticValidationError as exc:
                raise ToolValidationError(
                    _contract_error_message(exc=exc),
                    details={"tool": name, "errors": exc.errors()},
                ) from exc
            duration_ms = monotonic_ms() - started
            self.activity.tool_ok(
                source=activity_source,
                tool=name,
                arguments=telemetry_arguments,
                duration_ms=duration_ms,
                result=result,
            )
            self.tool_calls.record(
                tool=name,
                source=activity_source,
                status="ok",
                duration_ms=duration_ms,
                arguments=telemetry_arguments,
                result=result,
            )
            return result
        except ResearchPluginError as exc:
            duration_ms = monotonic_ms() - started
            self.activity.tool_error(
                source=activity_source,
                tool=name,
                arguments=telemetry_arguments,
                duration_ms=duration_ms,
                error=exc.message,
                error_code=exc.error_code,
            )
            self.tool_calls.record(
                tool=name,
                source=activity_source,
                status="error",
                duration_ms=duration_ms,
                arguments=telemetry_arguments,
                error=exc.message,
                error_code=exc.error_code,
            )
            raise
        except Exception as exc:
            duration_ms = monotonic_ms() - started
            self.activity.tool_error(
                source=activity_source,
                tool=name,
                arguments=telemetry_arguments,
                duration_ms=duration_ms,
                error=str(exc),
                error_code="unexpected",
            )
            self.tool_calls.record(
                tool=name,
                source=activity_source,
                status="error",
                duration_ms=duration_ms,
                arguments=telemetry_arguments,
                error=str(exc),
                error_code="unexpected",
            )
            raise

    def _activity_hook(self, event_type: str, payload: dict[str, Any]) -> None:
        """Bridge backend emit-style logging and ActivityLogger."""
        try:
            self.activity.emit(event_type=event_type, payload=payload)
        except Exception:  # noqa: BLE001
            pass
