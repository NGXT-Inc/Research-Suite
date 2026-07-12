from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from backend.composition import build_local_server
from backend.execution.backends.fake import FakeSandboxBackend
from backend.state import StateStore
from backend.storage.blobs import LocalDirBlobStore
from backend.tools.contracts import (
    DATA_PLANE_TOOL_NAMES,
    available_tool_names,
    static_tool_catalog,
)
from backend.utils import NotFoundError, ValidationError
from mcp_server.local_data_plane import LocalDataPlane, LocalDataPlaneError


DEFAULT_PUBLIC_KEY = "ssh-ed25519 " + ("A" * 48) + " test-brain@local"


class TestBrain:
    """Unified localhost brain plus an in-process proxy-local data plane.

    Tests keep the old repo_root/db_path construction convenience, while the
    record/lifecycle brain is the production ControlApp path built by
    build_local_server. Data-plane tools are routed through LocalDataPlane, the
    same code the stdio MCP proxy uses on a caller machine.
    """

    __test__ = False

    def __init__(
        self,
        *,
        repo_root: Path,
        db_path: Path,
        execution_backend: Any | None = None,
        store: Any | None = None,
        blobs: Any | None = None,
        storage: Any | None = None,
        task_channel: Any | None = None,
        mlflow_tracking: Any | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.db_path = Path(db_path).expanduser().resolve()
        self.workspace = SimpleNamespace(repo_root=self.repo_root)
        self._active_project_id: str | None = None
        self._store = store if store is not None else StateStore(db_path=self.db_path)
        self._blobs = (
            blobs
            if blobs is not None
            else LocalDirBlobStore(root=self.db_path.parent / "blobs")
        )
        self.server = build_local_server(
            state_dir=self._brain_root(),
            env={} if env is None else env,
            execution_backend=(
                execution_backend
                if execution_backend is not None
                else FakeSandboxBackend()
            ),
            store=self._store,
            blobs=self._blobs,
            storage=storage,
            task_channel=task_channel,
            mlflow_tracking=mlflow_tracking,
        )
        self._app = self.server.app
        self.task_channel = self.server.task_channel
        self.fastapi_app = self.server.fastapi_app
        self._client = TestClient(self.fastapi_app)
        self._data_plane = LocalDataPlane(
            repo_root=self.repo_root,
            project_id_resolver=self._resolve_project_id,
            control_api_post=self._control_api_post,
            control_tool_call=self._control_tool_call,
        )

    def _brain_root(self) -> Path:
        if self.db_path.parent.name == ".research_plugin":
            return self.db_path.parent.parent
        return self.db_path.parent

    def __getattr__(self, name: str) -> Any:
        return getattr(self._app, name)

    def current_project(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        return self._app.current_project(tenant_id=tenant_id)

    def list_tools(self) -> list[dict[str, Any]]:
        return static_tool_catalog(
            tool_names=available_tool_names(storage_enabled=self.storage is not None)
        )

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        activity_source: str = "app",
        internal_kwargs: dict[str, Any] | None = None,
        telemetry_project_id: str | None = None,
    ) -> dict[str, Any]:
        args = dict(arguments or {})
        if name in DATA_PLANE_TOOL_NAMES:
            if name == "sandbox.request":
                args.setdefault("public_key", DEFAULT_PUBLIC_KEY)
            with self._project_scope(args.get("project_id")):
                try:
                    return self._data_plane.call_tool(name=name, arguments=args)
                except LocalDataPlaneError as exc:
                    raise ValidationError(exc.message, details=exc.details) from exc
        return self._app.call_tool(
            name=name,
            arguments=args,
            activity_source=activity_source,
            internal_kwargs=internal_kwargs,
            telemetry_project_id=telemetry_project_id,
        )

    @contextlib.contextmanager
    def _project_scope(self, project_id: Any):
        previous = self._active_project_id
        self._active_project_id = str(project_id) if project_id else None
        try:
            yield
        finally:
            self._active_project_id = previous

    def _resolve_project_id(self) -> str | None:
        if self._active_project_id:
            return self._active_project_id
        projects = self._app.projects.list_projects()["projects"]
        if len(projects) == 1:
            return str(projects[0]["id"])
        return None

    def _control_tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._app.call_tool(name=name, arguments=arguments)

    def _control_api_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(path, json=payload)
        if response.status_code < 400:
            body = response.json()
            return body if isinstance(body, dict) else {}
        try:
            body = response.json()
        except ValueError:
            body = {"detail": response.text}
        detail = str(body.get("detail") or body.get("message") or response.text)
        details = {
            key: value
            for key, value in body.items()
            if key not in {"detail", "message", "error_code"}
        }
        if response.status_code == 404:
            raise NotFoundError(detail, details=details)
        raise ValidationError(detail, details=details)

    def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            self._client.close()
        with contextlib.suppress(Exception):
            self.server.shutdown()


__all__ = ["DEFAULT_PUBLIC_KEY", "TestBrain"]
