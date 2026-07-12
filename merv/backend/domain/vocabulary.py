"""Shared domain vocabulary for records, gates, and external contracts.

This module is deliberately dependency-free. It gives services, contracts, and
transport adapters one place to agree on stable status vocabulary without
pulling policy services across feature boundaries. Artifact role vocabulary
lives with its owner in ``backend/artifacts/roles.py``.
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
REVIEW_VERDICT_VALUES = ("pass", "needs_changes", "fail")
REVIEW_VERDICTS = frozenset(REVIEW_VERDICT_VALUES)

CLAIM_STATUSES = frozenset(
    {"draft", "active", "supported", "weakened", "contradicted", "abandoned"}
)
CLAIM_CONFIDENCES = frozenset({"low", "medium", "high"})

EXPERIMENT_TERMINAL_STATUSES = frozenset({"complete", "failed", "abandoned"})
EXPERIMENT_ACTIVE_PROCESS_STATUSES = frozenset({"provisioning", "running"})

LOCAL_TENANT_ID = "local"
LOCAL_CLIENT_ID = "local"
