"""Shared domain vocabulary for records, gates, and external contracts.

This module is deliberately dependency-free. It gives services, contracts, and
transport adapters one place to agree on stable role/status vocabulary without
pulling policy services across feature boundaries.
"""

from __future__ import annotations


REVIEW_ROLES = frozenset(
    {
        "design_reviewer",
        "experiment_reviewer",
        "reflection_reviewer",
        "human",
        "automated_check",
    }
)
REVIEW_VERDICTS = frozenset({"pass", "needs_changes", "fail"})

CLAIM_STATUSES = frozenset(
    {"draft", "active", "supported", "weakened", "contradicted", "abandoned"}
)
CLAIM_CONFIDENCES = frozenset({"low", "medium", "high"})

EXPERIMENT_TERMINAL_STATUSES = frozenset({"complete", "failed", "abandoned"})
EXPERIMENT_ACTIVE_PROCESS_STATUSES = frozenset({"provisioning", "running"})

RESOURCE_TARGET_TYPES = frozenset({"experiment", "reflection", "claim", "review", "attempt"})

LOCAL_TENANT_ID = "local"
LOCAL_CLIENT_ID = "local"

PROJECT_GRAPH_ROLE = "project_graph"
LEGACY_PROJECT_GRAPH_ROLE = "graph"
PROJECT_GRAPH_ROLES = (PROJECT_GRAPH_ROLE, LEGACY_PROJECT_GRAPH_ROLE)

REFLECTION_LENS_DOC_ROLE = "reflection_lens_doc"
LEGACY_REFLECTION_LENS_DOC_ROLE = "reflection"
REFLECTION_LENS_DOC_ROLES = (
    REFLECTION_LENS_DOC_ROLE,
    LEGACY_REFLECTION_LENS_DOC_ROLE,
)

LEGACY_REFLECTION_DOC_ROLE = "synthesis_doc"
LEGACY_PROPOSALS_ROLE = "proposals"
LEGACY_RESOURCE_ROLES = frozenset(
    {
        LEGACY_REFLECTION_LENS_DOC_ROLE,
        LEGACY_REFLECTION_DOC_ROLE,
        LEGACY_PROPOSALS_ROLE,
    }
)

RESOURCE_ROLES = frozenset(
    {
        "plan",
        "input",
        "code",
        "config",
        "result",
        "report",
        "graph",
        PROJECT_GRAPH_ROLE,
        REFLECTION_LENS_DOC_ROLE,
        "reflection_doc",
        "change_spec",
        "note",
        "model",
        "other",
    }
)

# Gated roles: the artifacts workflow gates lint. Associating one of these
# captures the file's bytes into the blob store (size-capped), pinning the
# association to immutable content.
GATED_ROLE_BYTE_CAPS: dict[str, int] = {
    "plan": 16_000,
    "report": 16_000,
    "graph": 16_000,
    PROJECT_GRAPH_ROLE: 16_000,
    REFLECTION_LENS_DOC_ROLE: 16_000,
    "reflection_doc": 16_000,
    # Legacy alias accepted for waves created before the rename.
    "synthesis_doc": 16_000,
    "change_spec": 16_000,
    "proposals": 16_000,
    # Legacy alias accepted for per-lens docs created before the rename.
    "reflection": 16_000,
}
GATED_ROLES = frozenset(GATED_ROLE_BYTE_CAPS)
