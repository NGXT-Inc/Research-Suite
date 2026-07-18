"""Writer ports used by reflection materialization."""

from __future__ import annotations

from typing import Any, Protocol


class ReflectionClaimWriter(Protocol):
    """Claim writes triggered by a reviewed reflection change spec."""

    def create_from_reflection(
        self,
        *,
        conn: Any,
        project_id: str,
        reflection_id: str,
        statement: str,
        scope: str,
        status: str,
        confidence: str,
        rationale: str,
    ) -> str:
        ...

    def update_from_reflection(
        self,
        *,
        conn: Any,
        project_id: str,
        reflection_id: str,
        claim_id: str,
        statement: str | None = None,
        scope: str | None = None,
        status: str | None = None,
        confidence: str | None = None,
        rationale: str,
    ) -> str:
        ...


class ReflectionExperimentWriter(Protocol):
    """Experiment writes triggered by a reviewed reflection change spec."""

    def create_from_reflection(
        self,
        *,
        conn: Any,
        project_id: str,
        reflection_id: str,
        name: str,
        intent: str,
        claim_ids: list[str],
        proposal_key: str,
        parallelism: str,
    ) -> str:
        ...

