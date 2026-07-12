"""Record-plane service composition, independent of local data-plane wiring."""

from __future__ import annotations

from dataclasses import dataclass

from ..artifacts.pinned import PinnedStore
from ..artifacts.resources import ResourceService
from ..services.association_targets import AssociationTargets
from ..services.claims import ClaimService
from ..services.experiments import ExperimentService
from ..services.feed import FeedService
from ..services.graph_refs import GraphRefResolver
from ..services.permissions import PermissionService
from ..services.project_overview import ProjectOverviewService
from ..services.projects import ProjectService
from ..services.quotas import QuotaService
from ..services.reflection_tools import ReflectionToolService
from ..services.reviews import ReviewService
from ..services.reflections import ReflectionService
from ..state import BaseStateStore
from ..storage.blobs import BlobStore
from ..storage.service import objects_for_experiment
from ..utils import NotFoundError


@dataclass(frozen=True)
class RecordCore:
    permissions: PermissionService
    quotas: QuotaService
    projects: ProjectService
    claims: ClaimService
    experiments: ExperimentService
    resources: ResourceService
    graph_refs: GraphRefResolver
    reflection_waves: ReflectionService
    reflection_tools: ReflectionToolService
    reflections: ReflectionToolService
    project_overview: ProjectOverviewService
    reviews: ReviewService
    feed: FeedService


def build_record_core(*, store: BaseStateStore, blobs: BlobStore) -> RecordCore:
    """Build record services without workspace, worker, or execution objects."""
    permissions = PermissionService()
    quotas = QuotaService(store=store)
    projects = ProjectService(store=store)
    claims = ClaimService(store=store)
    # Research-core services read pinned artifact bytes through the
    # artifacts-owned facade; only artifacts/feed touch the blob store.
    pinned = PinnedStore(blobs=blobs)
    # Cross-module reads the import law forbids as direct edges are injected
    # here instead: research_core gets the object-storage ledger query;
    # artifacts gets research-core target resolution.
    experiments = ExperimentService(
        store=store, pinned=pinned, storage_objects_reader=objects_for_experiment
    )
    resources = ResourceService(
        store=store,
        permissions=permissions,
        blobs=blobs,
        association_targets=AssociationTargets(),
    )
    graph_refs = GraphRefResolver(store=store)
    reflection_waves = ReflectionService(
        store=store,
        claims=claims,
        experiment_writer=experiments,
        pinned=pinned,
    )
    reflection_tools = ReflectionToolService(reflections=reflection_waves)
    project_overview = ProjectOverviewService(
        store=store,
        projects=projects,
        reflections=reflection_waves,
    )
    reviews = ReviewService(
        store=store,
        permissions=permissions,
        experiments=experiments,
        reflections=reflection_waves,
        pinned=pinned,
    )
    feed = FeedService(store=store, blobs=blobs)
    return RecordCore(
        permissions=permissions,
        quotas=quotas,
        projects=projects,
        claims=claims,
        experiments=experiments,
        resources=resources,
        graph_refs=graph_refs,
        reflection_waves=reflection_waves,
        reflection_tools=reflection_tools,
        reflections=reflection_tools,
        project_overview=project_overview,
        reviews=reviews,
        feed=feed,
    )


def build_experiment_attachment_check(*, store: BaseStateStore):
    """Surface-owned validator handed to ``SandboxService``.

    The sandbox module treats attachment labels as opaque strings; only the
    surface knows a label happens to be an experiment id, so the composition
    injects the existence/scope check (docs/MODULE_BOUNDARIES.md, sandbox
    de-domaining). Raises the same NotFoundError the sandbox service used to.
    """

    def check(*, attachment_id: str, project_id: str) -> None:
        conn = store.connect()
        try:
            row = conn.execute(
                "SELECT project_id FROM experiments WHERE id = ?", (attachment_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None or row["project_id"] != project_id:
            raise NotFoundError(
                f"experiment not found in project {project_id}: {attachment_id}"
            )

    return check
