"""Service layer: per-domain mutation logic for the daemon.

Each *Service owns a slice of the durable model (projects, claims, experiments,
resources, reviews, jobs) and the rules that govern its transitions. Services
are composed by `backend.app.ResearchPluginApp` and dispatched through the
MCP tool surface.
"""

from .claims import ClaimService
from .experiments import ExperimentService
from .jobs import JobService
from .permissions import RESOURCE_ROLES, PermissionService
from .projects import ProjectService
from .resources import ResourceService
from .reviews import ReviewService
from .workflow import WorkflowService

__all__ = [
    "RESOURCE_ROLES",
    "ClaimService",
    "ExperimentService",
    "JobService",
    "PermissionService",
    "ProjectService",
    "ResourceService",
    "ReviewService",
    "WorkflowService",
]
