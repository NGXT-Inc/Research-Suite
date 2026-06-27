"""Port for sandbox data-plane work."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class SandboxWorker(Protocol):
    """Local sandbox duties the services call through the data-plane seam."""

    def repo_relative(self, path: str | Path) -> str: ...

    def capture_metrics_fallback(
        self, *, experiment_id: str, name: str = ""
    ) -> dict[str, Any] | None: ...

    def capture_metrics_snapshot(
        self, *, row: dict[str, Any], name: str = ""
    ) -> dict[str, Any] | None: ...

    def local_experiment_dir(self, *, experiment_id: str, name: str = "") -> Path: ...

    def pulled_mlflow_db_path(self, *, experiment_id: str, name: str = "") -> Path: ...

    def ensure_keypair(self, *, experiment_id: str) -> tuple[str, Path]: ...

    def sandbox_enrichment(
        self,
        *,
        row: dict[str, Any],
        name: str = "",
        use_sandbox_uid_command: bool = True,
    ) -> dict[str, Any]: ...
