"""Artifact role and association-target vocabulary.

The artifacts module owns what a resource may be called (roles), what it may
attach to (target types), and which roles pin submitted bytes (gated roles).
This module is deliberately dependency-free so research_core gates, surface
adapters, and the data plane can all share it without pulling services across
feature boundaries.
"""

from __future__ import annotations

from typing import Any


RESOURCE_TARGET_TYPES = frozenset({"experiment", "reflection", "claim", "review", "attempt"})

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

# System-authored role: the metrics exhibit is generated and pinned by the
# backend at submit_results. It is deliberately NOT in RESOURCE_ROLES — agents
# cannot associate (and so cannot replace) it; only the system pin path may.
EXHIBIT_ROLE = "exhibit"
SYSTEM_CREATED_BY = "system"

# Result-file metric sources: associating a small JSON result file with role
# 'result' pins its bytes so the metrics exhibit can ingest the numbers.
# Opportunistic — larger or non-JSON result files associate exactly as before.
METRIC_RESULT_MAX_BYTES = 16_000


def metric_result_capture_cap(*, role: str, path: str) -> int | None:
    """Byte cap when a role-'result' association pins metric-file bytes."""
    if role != "result":
        return None
    normalized = str(path).replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    if not name.endswith(".json"):
        return None
    if name in {"metrics.json", "results.json"} or "/results/" in f"/{normalized}":
        return METRIC_RESULT_MAX_BYTES
    return None


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


# Association target-type aliasing: records created before the reflection
# rename store 'synthesis'; the external contract says 'reflection'.
_INTERNAL_SYNTHESIS = "synthesis"
_EXTERNAL_REFLECTION = "reflection"


def external_reflection_target_type(target_type: Any) -> Any:
    """Internal 'synthesis' -> external 'reflection'; pass through all else."""
    return _EXTERNAL_REFLECTION if target_type == _INTERNAL_SYNTHESIS else target_type


def internal_synthesis_target_type(target_type: Any) -> Any:
    """External 'reflection' -> internal 'synthesis'; pass through all else."""
    return _INTERNAL_SYNTHESIS if target_type == _EXTERNAL_REFLECTION else target_type
