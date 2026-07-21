"""Record-plane service composition, independent of local data-plane wiring."""

from __future__ import annotations

from dataclasses import dataclass

from ...artifacts.pinned import PinnedStore
from ...artifacts.resources import ResourceService
from ...research_core.association_targets import AssociationTargets
from ...research_core.claims import ClaimService
from ...research_core.experiments import ExperimentService
from ...feed.feed import FeedService
from ...research_core.graph_refs import GraphRefResolver
from ..permissions import PermissionService
from ...research_core.projects import ProjectService
from ...sandbox.quotas import QuotaService
from ...research_core.reflection_tools import ReflectionToolService
from ...research_core.reviews import ReviewService
from ...research_core.reflections import ReflectionService
from ...kernel.state import BaseStateStore
from ...kernel.ports.blob_store import EvidenceBlobStore
from ...object_storage.service import objects_for_experiment


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
    reviews: ReviewService
    feed: FeedService


def build_record_core(*, store: BaseStateStore, blobs: EvidenceBlobStore) -> RecordCore:
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
    reviews = ReviewService(
        store=store,
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
        reviews=reviews,
        feed=feed,
    )
