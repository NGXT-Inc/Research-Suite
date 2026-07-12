"""Ports used by workflow orientation services."""

from __future__ import annotations

from typing import Any, Protocol


class ExperimentWorkflowReader(Protocol):
    """Experiment read operations required by workflow orientation."""

    def get_state(
        self, *, experiment_id: str, project_id: str | None = None, conn: Any = None
    ) -> dict[str, Any]:
        ...

    def validator_problems(
        self, *, conn: Any, experiment_id: str, name: str
    ) -> list[str]:
        ...


class ReviewWorkflowReader(Protocol):
    """Review read operations required by workflow orientation."""

    def gate_state(
        self, *, conn: Any, target_type: str, target_id: str, role: str
    ) -> dict[str, Any]:
        ...

    def open_request(
        self, *, conn: Any, target_type: str, target_id: str, role: str
    ) -> dict[str, Any] | None:
        ...


class SandboxWorkflowReader(Protocol):
    """Sandbox read operations required by workflow orientation."""

    def sandboxes_for_experiment(
        self, *, conn: Any, experiment_id: str
    ) -> list[dict[str, Any]]:
        ...

    def sandboxes_for_project(
        self, *, conn: Any, project_id: str
    ) -> list[dict[str, Any]]:
        ...


class ReflectionWorkflowReader(Protocol):
    """Reflection-wave read operations required by workflow orientation."""

    def open_reflection(self, *, conn: Any, project_id: str) -> dict[str, Any] | None:
        ...

    def reflection_signal(
        self, *, project_id: str, conn: Any = None
    ) -> dict[str, Any]:
        ...
