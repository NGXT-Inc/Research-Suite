"""Daemon (slim local data-plane) composition (cloud plan Phase 8, §3.4).

A user-machine process that holds the SSH keys, conn files, and repo↔project
links, and moves bytes for the cloud. It runs:

- a ``LocalDataPlaneWorker`` (rsync push/pull, conn files, dashboards),
- an ``HttpControlPlaneClient`` upstream to the cloud (bearer token from the
  0600 token file),
- a ``DaemonTaskLoop`` long-polling the cloud for data-plane tasks
  (initial_push | final_pull | conn_refresh | teardown | parachute_restore),
- an auto-sync loop that asks the cloud "my running sandboxes + lease grants"
  and rsyncs each (the HTTP ControlPlaneView),
- a small loopback HTTP surface for the proxy (local data-plane tool subset +
  GET /local/route for the repo→project mapping + local UI byte endpoints).

Fail-fast (§3.4): a daemon without RESEARCH_PLUGIN_CONTROL_URL refuses to
start — no silent 127.0.0.1 fallback. The cloud never dials in; this process is
the only initiator. Provider SDKs are NOT imported at module load (modal is
already lazily imported; the daemon profile drops it entirely).
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..contracts import AGGREGATE_TOOL_NAMES, DATA_PLANE_TOOL_NAMES, static_tool_catalog
from ..control_client import HttpControlPlaneClient
from ..dataplane import LocalDataPlaneWorker
from ..dataplane.http_channel import DaemonTaskLoop
from ..dataplane.project_links import ProjectLinks
from ..dataplane.remote_view import HttpControlPlaneView
from ..execution import build_sandbox_backend
from ..services.artifacts import markdown_image_links, markdown_image_targets
from ..services.permissions import GATED_ROLE_BYTE_CAPS
from ..services.resources import ResourceService
from ..utils import ValidationError
from ..workspace import LocalWorkspace


IMPLEMENTED_DATA_TOOL_NAMES = frozenset({"resource.register_file", "resource.associate"})


def _ensure_loopback_secret(*, root: Path) -> str:
    """A local auth secret for the daemon's loopback surface (plan Phase 8,
    risk 11). The daemon holds the cloud token + the user private keys behind a
    loopback HTTP surface; a per-daemon secret (0600 file) keeps another local
    process from driving it. Minimal but real; unix-socket bind is the Phase 9
    hardening upgrade.
    """
    import secrets

    path = root / "daemon_secret"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        pass
    return token


class DaemonServer:
    """A running data-plane daemon: worker + task loop + auto-sync loop.

    Holds the worker (local IO), the control client (cloud upstream), and the
    two background loops. ``start``/``stop`` own the loop lifecycle. The
    loopback FastAPI surface is built by the caller (http_server) from this.
    """

    def __init__(
        self,
        *,
        worker: LocalDataPlaneWorker,
        control: HttpControlPlaneClient,
        task_loop: DaemonTaskLoop,
        view: HttpControlPlaneView,
        project_links: ProjectLinks,
        loopback_secret: str,
        auto_sync_interval_seconds: float = 5.0,
    ) -> None:
        self.worker = worker
        self.control = control
        self.task_loop = task_loop
        self.view = view
        # Daemon-local repo_root→project_id mapping; the proxy resolves identity
        # through this so repo_root never crosses to the cloud (§3.2).
        self.project_links = project_links
        # Local auth secret for the loopback surface (risk 11).
        self.loopback_secret = loopback_secret
        self._auto_sync_interval = auto_sync_interval_seconds
        self._auto_sync_stop = threading.Event()
        self._auto_sync_thread: threading.Thread | None = None

    def start(self) -> None:
        self.task_loop.start()
        self._auto_sync_thread = threading.Thread(
            target=self._auto_sync_loop, name="daemon-auto-sync", daemon=True
        )
        self._auto_sync_thread.start()

    def stop(self) -> None:
        self._auto_sync_stop.set()
        self.task_loop.stop()
        if self._auto_sync_thread is not None:
            self._auto_sync_thread.join(timeout=2.0)

    def list_tools(self) -> list[dict[str, Any]]:
        """The local MCP catalog: data-plane tools plus aggregate enrichers."""
        allowed = IMPLEMENTED_DATA_TOOL_NAMES | AGGREGATE_TOOL_NAMES
        return [tool for tool in static_tool_catalog() if tool.get("name") in allowed]

    def call_tool(
        self,
        *,
        name: str,
        arguments: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a proxy-routed data-plane tool.

        Local file observation happens here; only repo-relative paths,
        content hashes, and gated artifact bytes cross to the control plane.
        """
        arguments = dict(arguments or {})
        context = dict(context or {})
        if name == "sandbox.health":
            cloud_ok = True
            try:
                self.control.list_tools()
            except Exception:  # noqa: BLE001
                cloud_ok = False
            return {
                "ok": True,
                "mode": "daemon",
                "cloud_reachable": cloud_ok,
            }
        if name == "resource.register_file":
            return self._register_resource_files(arguments=arguments, context=context)
        if name == "resource.associate":
            return self._associate_resource(arguments=arguments, context=context)
        if name not in (DATA_PLANE_TOOL_NAMES | AGGREGATE_TOOL_NAMES):
            raise ValidationError(
                f"tool is not served by the data plane: {name}",
                details={"tool": name},
            )
        if name in AGGREGATE_TOOL_NAMES:
            # Aggregate daemon half: contribute only machine-local enrichment.
            # No enrichment yet is a neutral result; the cloud row remains the
            # primary answer after proxy merge.
            return {}
        raise ValidationError(
            f"{name} is not implemented by this data-plane daemon",
            details={"tool": name, "error_code": "data_plane_tool_unimplemented"},
        )

    def _register_resource_files(
        self, *, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        repo_root = self._repo_root_from_context(context=context)
        project_id = self._project_id(arguments=arguments, repo_root=repo_root)
        kind = str(arguments.get("kind") or "other")
        title = str(arguments.get("title") or "")
        created_by = str(arguments.get("created_by") or "codex")
        paths = arguments.get("paths")
        if paths:
            if not isinstance(paths, list):
                raise ValidationError("paths must be a list")
            resources = [
                self._submit_resource_observation(
                    repo_root=repo_root,
                    project_id=project_id,
                    path=str(path),
                    kind=kind,
                    title=title,
                    created_by=created_by,
                )
                for path in paths
            ]
            return {"synced": resources, "count": len(resources)}
        path = arguments.get("path")
        if not path:
            raise ValidationError(
                "resource.register_file requires 'path' (a single file) or 'paths' (a batch)"
            )
        return self._submit_resource_observation(
            repo_root=repo_root,
            project_id=project_id,
            path=str(path),
            kind=kind,
            title=title,
            created_by=created_by,
        )

    def _associate_resource(
        self, *, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        repo_root = self._repo_root_from_context(context=context)
        project_id = self._project_id(arguments=arguments, repo_root=repo_root)
        resource_id = self._required_arg(arguments, "resource_id")
        role = self._required_arg(arguments, "role")
        intent = {
            "project_id": project_id,
            "resource_id": resource_id,
            "target_type": self._required_arg(arguments, "target_type"),
            "target_id": self._required_arg(arguments, "target_id"),
            "role": role,
        }
        validation = self.control.validate_resource_association(intent)
        resource = validation.get("resource") or {}
        path = str(resource.get("path") or "")
        if not path:
            raise ValidationError(f"resource has no path: {resource_id}")
        self._submit_resource_observation(
            repo_root=repo_root,
            project_id=project_id,
            path=path,
            kind=str(resource.get("kind") or "other"),
            title=str(resource.get("title") or ""),
            created_by=str(resource.get("created_by") or "codex"),
        )
        payload: dict[str, Any] = {
            **intent,
        }
        if role in GATED_ROLE_BYTE_CAPS:
            rel_path, file_path = self._resolve_repo_file(repo_root=repo_root, path=path)
            size = file_path.stat().st_size
            cap = GATED_ROLE_BYTE_CAPS[role]
            if size > cap:
                raise ValidationError(
                    f"{rel_path} is {size} bytes; the maximum for a role-{role!r} "
                    f"artifact is {cap} bytes — slim the file before associating "
                    "(move raw data/outputs elsewhere and reference them)",
                    details={"path": rel_path, "role": role, "size_bytes": size, "max_bytes": cap},
                )
            data = file_path.read_bytes()
            markdown_text = data.decode("utf-8", errors="replace")
            if role in {"report", "reflection_doc", "synthesis_doc"}:
                self._reject_absolute_markdown_image_targets(
                    markdown_rel_path=rel_path, markdown_text=markdown_text
                )
            payload["blob"] = {
                "data_b64": base64.b64encode(data).decode("ascii"),
                "content_type": mimetypes.guess_type(rel_path)[0] or "application/octet-stream",
            }
            if role in {"report", "reflection_doc", "synthesis_doc"}:
                payload["figures"] = self._submitted_figures(
                    repo_root=repo_root,
                    markdown_rel_path=rel_path,
                    markdown_text=markdown_text,
                )
        return self.control.submit_resource_association(payload)

    def _submit_resource_observation(
        self,
        *,
        repo_root: Path,
        project_id: str,
        path: str,
        kind: str,
        title: str,
        created_by: str,
    ) -> dict[str, Any]:
        rel_path, file_path = self._resolve_repo_file(repo_root=repo_root, path=path)
        stat = file_path.stat()
        return self.control.submit_resource_observation(
            {
                "project_id": project_id,
                "path": rel_path,
                "kind": kind,
                "title": title,
                "created_by": created_by,
                "mtime_ns": stat.st_mtime_ns,
                "ctime_ns": stat.st_ctime_ns,
                "size_bytes": stat.st_size,
                "content_sha256": self._content_sha256(file_path),
                "content_type": mimetypes.guess_type(rel_path)[0] or "application/octet-stream",
            }
        )

    def _submitted_figures(
        self, *, repo_root: Path, markdown_rel_path: str, markdown_text: str
    ) -> list[dict[str, Any]]:
        markdown_dir = (repo_root / markdown_rel_path).parent
        figures: list[dict[str, Any]] = []
        for link in markdown_image_links(markdown_text):
            resolved = (markdown_dir / link).resolve()
            try:
                resolved.relative_to(repo_root)
            except ValueError as exc:
                raise ValidationError(
                    f"report image link escapes the repo: {link}",
                    details={"link": link, "resource": markdown_rel_path},
                ) from exc
            if not resolved.is_file():
                continue
            size = resolved.stat().st_size
            if size > ResourceService.FIGURE_MAX_BYTES:
                continue
            data = resolved.read_bytes()
            figures.append(
                {
                    "link_path": link,
                    "data_b64": base64.b64encode(data).decode("ascii"),
                    "content_type": mimetypes.guess_type(link)[0] or "application/octet-stream",
                    "size_bytes": size,
                }
            )
        return figures

    def _reject_absolute_markdown_image_targets(
        self, *, markdown_rel_path: str, markdown_text: str
    ) -> None:
        for target in markdown_image_targets(markdown_text):
            if target.startswith("/") or os.path.isabs(target):
                raise ValidationError(
                    f"markdown image link must be repo-relative: {target}",
                    details={"link": target, "resource": markdown_rel_path},
                )

    def _repo_root_from_context(self, *, context: dict[str, Any]) -> Path:
        repo_root = str(context.get("repo_root") or "")
        if not repo_root:
            raise ValidationError("repo_root context is required for data-plane tools")
        return Path(repo_root).expanduser().resolve()

    def _project_id(self, *, arguments: dict[str, Any], repo_root: Path) -> str:
        project_id = str(arguments.get("project_id") or "")
        if project_id:
            return project_id
        linked = self.project_links.project_for_repo(repo_root=str(repo_root))
        if linked:
            return linked
        raise ValidationError(
            "project_id is required until this repo is linked to a project",
            details={"repo_root": str(repo_root)},
        )

    def _required_arg(self, arguments: dict[str, Any], key: str) -> str:
        value = arguments.get(key)
        if value is None or str(value) == "":
            raise ValidationError(f"{key} is required")
        return str(value)

    def _resolve_repo_file(self, *, repo_root: Path, path: str) -> tuple[str, Path]:
        if not path:
            raise ValidationError("path is required")
        if os.path.isabs(path):
            raise ValidationError("resource paths must be repo-relative")
        rel = Path(path)
        if any(part == ".." for part in rel.parts):
            raise ValidationError("resource path may not contain '..'")
        if rel.parts and rel.parts[0] == ".research_plugin":
            raise ValidationError("resource path may not point inside .research_plugin")
        full = (repo_root / rel).resolve()
        try:
            full.relative_to(repo_root)
        except ValueError as exc:
            raise ValidationError("resource path escapes repo root") from exc
        if not full.exists():
            raise ValidationError(f"resource file does not exist: {path}")
        if not full.is_file():
            raise ValidationError("v0.0001 resources must be files")
        return rel.as_posix(), full

    def _content_sha256(self, file_path: Path) -> str:
        digest = hashlib.sha256()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _auto_sync_loop(self) -> None:
        # Mirror SandboxDaemons._auto_sync_loop, but the targets come from the
        # cloud over HTTP (the ControlPlaneView poll, plan Phase 4) and the sync
        # completion is reported back over HTTP. A row leased to another client
        # is simply absent from the targets.
        while not self._auto_sync_stop.wait(self._auto_sync_interval):
            try:
                targets = self.view.sync_targets()
            except Exception:  # noqa: BLE001 — a cloud blip must not kill the loop
                continue
            for target in targets:
                try:
                    self.worker.sync_pull(session=target["session"], skip_if_busy=True)
                except Exception:  # noqa: BLE001 — per-target best-effort
                    continue


def build_daemon_executor(*, worker: LocalDataPlaneWorker, control: HttpControlPlaneClient):
    """The daemon's task dispatch: (type, payload, deadline) -> result.

    Same worker-dispatch shape as InProcessTaskChannel._execute, but reads
    JSON payloads. parachute_restore downloads bytes from a presigned GET URL
    (the in-process channel carried the bytes inline); everything else carries
    serializable session/row dicts already.
    """
    from urllib.request import urlopen

    def _relativize(result: Any) -> Any:
        # The cloud receives the daemon's task RESULT over the wire; a machine
        # path must never enter a cloud-bound row (§3.2). The daemon knows its
        # own repo_root, so it relativizes local_dir before acking — the cloud
        # then stores the logical spelling, not an absolute checkout path.
        if isinstance(result, dict) and result.get("local_dir"):
            result = dict(result)
            result["local_dir"] = worker.repo_relative(result["local_dir"])
        return result

    def execute(task_type: str, payload: dict[str, Any], deadline: str | None) -> Any:
        if task_type == "initial_push":
            return _relativize(worker.push_initial(
                session=payload["session"], name=str(payload.get("name") or "")
            ))
        if task_type == "final_pull":
            return _relativize(worker.final_pull(
                session=payload["session"],
                name=str(payload.get("name") or ""),
                deadline=deadline,
            ))
        if task_type == "conn_refresh":
            return worker.sandbox_enrichment(
                row=payload["row"], name=str(payload.get("name") or "")
            )
        if task_type == "teardown":
            sandbox_id = payload.get("sandbox_id")
            if sandbox_id is not None:
                worker.stop_dashboards(sandbox_id=str(sandbox_id))
            worker.remove_conn_file(experiment_id=str(payload["experiment_id"]))
            return None
        if task_type == "parachute_restore":
            # The cloud hands a presigned GET URL (S3); the daemon downloads the
            # tar and unpacks it through the worker's normal sync-path semantics.
            url = str(payload.get("get_url") or "")
            if not url:
                raise ValidationError("parachute_restore task has no get_url")
            with urlopen(url, timeout=120) as response:  # noqa: S310 — presigned URL from the control plane
                data = response.read()
            return worker.restore_parachute(
                experiment_id=str(payload["experiment_id"]),
                data=data,
                name=str(payload.get("name") or ""),
            )
        raise ValidationError(f"unknown task type: {task_type}")

    return execute


def build_daemon_server(
    *,
    control_url: str | None,
    token: str | None = None,
    workspace_root: Path | None = None,
    client_id: str | None = None,
    env: Mapping[str, str] | None = None,
) -> DaemonServer:
    """Build the data-plane daemon, fail-fast on a missing control URL.

    ``workspace_root`` defaults to ~/.research_plugin for the daemon's own
    machine-local state (keys, conn files, sandbox_local.sqlite). ``client_id``
    is the stable per-daemon lease-holder id; absent, the worker mints one.
    """
    if not control_url:
        raise ValidationError(
            "daemon mode requires RESEARCH_PLUGIN_CONTROL_URL (the cloud control "
            "plane URL); refusing to start with no upstream (cloud plan §3.4)",
            details={"mode": "daemon"},
        )
    root = workspace_root or (Path.home() / ".research_plugin")
    workspace = LocalWorkspace(repo_root=root)
    # The provider SDK is needed only for dashboards; build the backend lazily
    # via the standard factory (modal stays a lazy import). A daemon that never
    # opens a dashboard never pays the import.
    backend = build_sandbox_backend(repo_root=root, activity=lambda *_a, **_k: None)
    worker = LocalDataPlaneWorker(workspace=workspace, backend=backend)
    resolved_client_id = client_id or worker.client_id()
    control = HttpControlPlaneClient(base_url=control_url, token=token)
    view = HttpControlPlaneView(
        control=control, worker=worker, client_id=resolved_client_id
    )
    executor = build_daemon_executor(worker=worker, control=control)

    def poll(wait_seconds: float) -> dict[str, Any] | None:
        return view.poll_task(wait_seconds=wait_seconds)

    def ack(*, task_id: str, ok: bool, result: Any = None, error: str | None = None) -> None:
        view.ack_task(task_id=task_id, ok=ok, result=result, error=error)

    task_loop = DaemonTaskLoop(poll=poll, ack=ack, executor=executor)
    project_links = ProjectLinks(db_path=root / "project_links.sqlite")
    loopback_secret = _ensure_loopback_secret(root=root)
    return DaemonServer(
        worker=worker,
        control=control,
        task_loop=task_loop,
        view=view,
        project_links=project_links,
        loopback_secret=loopback_secret,
    )
