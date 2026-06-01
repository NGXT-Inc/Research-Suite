"""Service layer: per-domain mutation logic for the daemon.

Each *Service owns a slice of the durable model (projects, claims, experiments,
resources, reviews, sandboxes) and the rules that govern its transitions.
Services are composed by `backend.app.ResearchPluginApp` and dispatched through
the MCP tool surface.
"""

from .claims import ClaimService
from .experiments import ExperimentService
from .permissions import RESOURCE_ROLES, PermissionService
from .projects import ProjectService
from .resources import ResourceService
from .reviews import ReviewService
from .sandboxes import SandboxService
from .workflow import WorkflowService

__all__ = [
    "RESOURCE_ROLES",
    "ClaimService",
    "ExperimentService",
    "PermissionService",
    "ProjectService",
    "ResourceService",
    "ReviewService",
    "SandboxService",
    "WorkflowService",
]
