"""Writer ports used by synthesis materialization."""

from __future__ import annotations

from typing import Any, Protocol


class SynthesisClaimWriter(Protocol):
    """Claim writes triggered by a reviewed synthesis change spec."""

    def create_from_synthesis(
        self,
        *,
        conn: Any,
        project_id: str,
        synthesis_id: str,
        statement: str,
        scope: str,
        status: str,
        confidence: str,
        rationale: str,
    ) -> str:
        ...

    def update_from_synthesis(
        self,
        *,
        conn: Any,
        project_id: str,
        synthesis_id: str,
        claim_id: str,
        statement: str | None = None,
        scope: str | None = None,
        status: str | None = None,
        confidence: str | None = None,
        rationale: str,
    ) -> str:
        ...


class SynthesisExperimentWriter(Protocol):
    """Experiment writes triggered by a reviewed synthesis change spec."""

    def create_from_synthesis(
        self,
        *,
        conn: Any,
        project_id: str,
        synthesis_id: str,
        name: str,
        intent: str,
        claim_ids: list[str],
        proposal_key: str,
        parallelism: str,
    ) -> str:
        ...


class SynthesisProjectWriter(Protocol):
    """Project writes triggered by a reviewed synthesis change spec."""

    def stop_from_synthesis(
        self,
        *,
        conn: Any,
        project_id: str,
        synthesis_id: str,
        rationale: str,
    ) -> None:
        ...
