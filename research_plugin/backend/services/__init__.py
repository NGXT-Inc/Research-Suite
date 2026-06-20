"""Service layer: per-domain mutation logic for the daemon.

Each *Service owns a slice of the durable model (projects, claims, experiments,
resources, reviews, sandboxes) and the rules that govern its transitions.
Services are composed by `backend.app.ResearchPluginApp` and dispatched through
the MCP tool surface.
"""

from .claims import ClaimService
from .experiments import ExperimentService
from .feed import FeedService
from .permissions import PermissionService
from .projects import ProjectService
from .resources import ResourceService
from .reviews import ReviewService
from .sandboxes import SandboxService
from .syntheses import SynthesisService
from .workflow import WorkflowService

__all__ = [
    "ClaimService",
    "ExperimentService",
    "FeedService",
    "PermissionService",
    "ProjectService",
    "ResourceService",
    "ReviewService",
    "SandboxService",
    "SynthesisService",
    "WorkflowService",
]
