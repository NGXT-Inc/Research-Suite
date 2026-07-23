"""Record-plane service composition, independent of local data-plane wiring."""

from __future__ import annotations

from dataclasses import dataclass

from ...artifacts.resources import ResourceService
from ...research_core.association_targets import AssociationTargets
from ...research_core.claims import ClaimService
from ...research_core.experiments import ExperimentService
from ...feed.feed import FeedService
from ...feed.feed_unfurl import AllowlistedPaperUnfurl, NetworkLinkUnfurl
from ...research_core.graph_refs import GraphRefResolver
from ...research_core.literature import LiteratureService
from ..permissions import PermissionService
from ...research_core.projects import ProjectService
from ...sandbox.quotas import QuotaService
from ...research_core.reviews import ReviewService
from ...research_core.reflections import ReflectionService
from ...kernel.state import BaseStateStore
from ...kernel.ports.blob_store import EvidenceBlobStore


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
    reviews: ReviewService
    feed: FeedService
    literature: LiteratureService


def build_record_core(*, store: BaseStateStore, blobs: EvidenceBlobStore) -> RecordCore:
    """Build record services without workspace, worker, or execution objects."""
    permissions = PermissionService()
    quotas = QuotaService(store=store)
    projects = ProjectService(store=store)
    claims = ClaimService(store=store)
    # Artifacts receives the narrow Research-owned association target resolver.
    resources = ResourceService(
        store=store,
        blobs=blobs,
        association_targets=AssociationTargets(store=store),
    )
    experiments = ExperimentService(
        store=store,
        evidence_reader=resources,
    )
    graph_refs = GraphRefResolver(store=store)
    reflection_waves = ReflectionService(
        store=store,
        claims=claims,
        experiment_writer=experiments,
        evidence_reader=resources,
    )
    reviews = ReviewService(
        store=store,
        experiments=experiments,
        reflections=reflection_waves,
        evidence_reader=resources,
    )
    feed = FeedService(store=store, blobs=blobs, link_unfurl=NetworkLinkUnfurl())
    literature = LiteratureService(store=store, unfurl=AllowlistedPaperUnfurl())
    return RecordCore(
        permissions=permissions,
        quotas=quotas,
        projects=projects,
        claims=claims,
        experiments=experiments,
        resources=resources,
        graph_refs=graph_refs,
        reflection_waves=reflection_waves,
        reviews=reviews,
        feed=feed,
        literature=literature,
    )
