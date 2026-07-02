"""Daemon (slim local data-plane) composition.

A user-machine process that holds the SSH keys, conn files, and repo↔project
links. It runs:

- a ``LocalDataPlaneWorker`` (keys, conn files, local paths),
- an ``HttpControlPlaneClient`` upstream to the cloud,
- a ``DaemonTaskLoop`` long-polling the cloud for data-plane tasks
  (conn_refresh | teardown),
- a small loopback HTTP surface for the proxy (local data-plane tool subset +
  GET /local/route for the repo→project mapping + local UI byte endpoints).

Fail-fast (§3.4): a daemon without RESEARCH_PLUGIN_CONTROL_URL refuses to
start — no silent 127.0.0.1 fallback. The cloud never dials in; this process is
the only initiator. Provider SDKs are NOT imported at module load (modal is
already lazily imported; the daemon profile drops it entirely).
"""

from __future__ import annotations

import base64
import mimetypes
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..tools.contracts import AGGREGATE_TOOL_NAMES, DATA_PLANE_TOOL_NAMES, static_tool_catalog
from ..control.control_client import HttpControlPlaneClient
from ..dataplane import LocalDataPlaneWorker
from ..dataplane.feed_images import LocalFeedImageReader
from ..dataplane.http_channel import DaemonTaskLoop
from ..dataplane.project_links import ProjectLinks
from ..dataplane.remote_view import HttpControlPlaneView
from ..dataplane.resource_artifacts import LocalResourceArtifactReader
from ..dataplane.resource_observer import LocalResourceObserver
from ..dataplane.resource_validation import validate_local_resource_artifact
from ..dataplane.results_tsv import merge_results_tsv
from ..dataplane.experiment_folders import materialize_experiment_folders
from ..secret_tokens import mint_secret
from ..services.sandbox import sandbox_views
from ..storage.file_transfer import (
    download_target_to_file,
    file_digest,
    upload_file_to_target,
)
from ..utils import ValidationError, new_id
from ..workspace import LocalWorkspace


def _ensure_loopback_secret(*, root: Path) -> str:
    path = root / "daemon_secret"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            _lock_secret_file(path)
            return existing
    except OSError:
        pass
    token = mint_secret(prefix="", nbytes=32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token)
        _lock_secret_file(path)
    except OSError as exc:
        raise ValidationError(
            f"cannot persist daemon loopback secret at {path}",
            details={"path": str(path)},
        ) from exc
    return token


def _lock_secret_file(path: Path) -> None:
    try:
        path.chmod(0o600)
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        raise ValidationError(
            f"cannot secure daemon loopback secret at {path}",
            details={"path": str(path)},
        ) from exc
    if mode & 0o077:
        raise ValidationError(
            f"daemon loopback secret must not be group/world-readable: {path}",
            details={"path": str(path), "mode": oct(mode)},
        )


class DaemonServer:
    """A running data-plane daemon: worker + task loop + loopback surface.

    Holds the worker (local IO), the control client (cloud upstream), and the
    background task loop. ``start``/``stop`` own the loop lifecycle. The
    loopback FastAPI surface is built by the caller (http_server) from this.
    """

    def __init__(
        self,
        *,
        worker: LocalDataPlaneWorker,
        control: HttpControlPlaneClient,
        task_loop: DaemonTaskLoop,
        project_links: ProjectLinks,
        loopback_secret: str,
    ) -> None:
        self.worker = worker
        self.control = control
        self.task_loop = task_loop
        # Daemon-local repo_root→project_id mapping; the proxy resolves identity
        # through this so repo_root never crosses to the cloud (§3.2).
        self.project_links = project_links
        # Local auth secret for the loopback surface (risk 11).
        self.loopback_secret = loopback_secret

    def start(self) -> None:
        self.task_loop.start()

    def stop(self) -> None:
        self.task_loop.stop()

    def list_tools(self) -> list[dict[str, Any]]:
        """The local MCP catalog: data-plane tools plus aggregate enrichers."""
        allowed = DATA_PLANE_TOOL_NAMES | AGGREGATE_TOOL_NAMES
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
        if name == "resource.validate":
            return self._validate_resource_file(arguments=arguments, context=context)
        if name == "resource.associate":
            return self._associate_resource(arguments=arguments, context=context)
        if name == "resource.associate_batch":
            return self._associate_resource_batch(arguments=arguments, context=context)
        if name == "experiment.materialize_folders":
            return self._materialize_experiment_folders(arguments=arguments, context=context)
        if name == "results.merge_tsv":
            return self._merge_results_tsv(arguments=arguments, context=context)
        if name == "feed.post":
            return self._post_feed(arguments=arguments, context=context)
        if name == "storage.upload_file":
            return self._upload_storage_file(arguments=arguments, context=context)
        if name == "storage.download_file":
            return self._download_storage_file(arguments=arguments, context=context)
        if name == "sandbox.request":
            return self._request_sandbox(arguments=arguments, context=context)
        if name == "sandbox.attach":
            return self._attach_sandbox(arguments=arguments, context=context)
        if name == "sandbox.get":
            return self._sandbox_get_enrichment(arguments=arguments, context=context)
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

    def _request_sandbox(
        self, *, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        _repo_root, project_id = self._linked_scope(context=context)
        experiment_id = str(arguments.get("experiment_id") or "").strip()
        requested_uid = str(arguments.get("sandbox_uid") or uuid.uuid4().hex)
        public_key, _key_path = self.worker.ensure_keypair(experiment_id=requested_uid)
        payload = dict(arguments)
        payload["project_id"] = project_id
        if experiment_id:
            payload["experiment_id"] = experiment_id
        payload["sandbox_uid"] = requested_uid
        payload["public_key"] = public_key
        facts = self.control.request_sandbox(payload)
        sandbox_uid = str(facts.get("sandbox_uid") or "")
        facts.pop("_experiment_name", None)
        name = f"sandbox-{sandbox_uid[:12]}" if sandbox_uid else ""
        return self._merge_sandbox_enrichment(
            facts=facts,
            name=name,
            use_sandbox_uid_command=True,
        )

    def _attach_sandbox(
        self, *, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        _repo_root, project_id = self._linked_scope(context=context)
        experiment_id = self._required_arg(arguments, "experiment_id")
        payload = dict(arguments)
        payload["project_id"] = project_id
        payload["experiment_id"] = experiment_id
        payload.setdefault("public_key", "")
        facts = self.control.attach_sandbox(payload)
        sandbox_uid = str(facts.get("sandbox_uid") or "")
        facts.pop("_experiment_name", None)
        facts.pop("_use_sandbox_uid_command", None)
        name = f"sandbox-{sandbox_uid[:12]}" if sandbox_uid else ""
        return self._merge_sandbox_enrichment(
            facts=facts,
            name=name,
            use_sandbox_uid_command=True,
        )

    def _post_feed(
        self, *, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        repo_root, project_id = self._linked_scope(context=context)
        payload: dict[str, Any] = {
            "project_id": project_id,
            "handle": self._required_arg(arguments, "handle"),
            "text": self._required_arg(arguments, "text"),
        }
        for key in ("url", "ref", "kind"):
            if arguments.get(key) is not None:
                payload[key] = arguments.get(key)
        self.control.validate_feed_post(
            {
                key: payload[key]
                for key in ("project_id", "handle", "text", "ref", "kind")
                if key in payload
            }
        )
        image_path = str(arguments.get("image_path") or "")
        if image_path:
            payload["image"] = self._feed_image_payload(
                repo_root=repo_root, image_path=image_path
            )
        return self.control.submit_feed_post(payload)

    def _feed_image_payload(self, *, repo_root: Path, image_path: str) -> dict[str, Any]:
        image = LocalFeedImageReader(repo_root=repo_root).read_image(path=image_path)
        return {
            "path": str(image["path"]),
            "data_b64": base64.b64encode(image["data"]).decode("ascii"),
        }

    def _upload_storage_file(
        self, *, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        repo_root, project_id = self._linked_scope(context=context)
        file_path = self._resolve_local_path(
            repo_root=repo_root, path=self._required_arg(arguments, "path")
        )
        if not file_path.exists():
            raise ValidationError(f"storage upload file not found: {file_path}")
        if not file_path.is_file():
            raise ValidationError(f"storage upload path is not a file: {file_path}")
        sha256, size_bytes = file_digest(file_path)
        content_type = (
            str(arguments.get("content_type") or "")
            or mimetypes.guess_type(str(file_path))[0]
            or "application/octet-stream"
        )
        payload = {
            "project_id": project_id,
            "path": str(file_path),
            "name": str(arguments.get("name") or "")
            or self._default_storage_name(repo_root=repo_root, path=file_path),
            "kind": self._required_arg(arguments, "kind"),
            "sha256": sha256,
            "size_bytes": size_bytes,
            "content_type": content_type,
            "producing_experiment_id": str(
                arguments.get("producing_experiment_id") or ""
            ),
            "producing_run": str(arguments.get("producing_run") or ""),
            "source_uri": str(arguments.get("source_uri") or ""),
            "notes": str(arguments.get("notes") or ""),
        }
        registered = self.control.call(
            "storage.put_object",
            {key: value for key, value in payload.items() if key != "path"},
        )
        result: dict[str, Any] = {
            key: value for key, value in registered.items() if key != "upload"
        }
        result["path"] = str(file_path)
        result["sha256"] = sha256
        result["size_bytes"] = size_bytes
        result["uploaded"] = False
        upload = registered.get("upload")
        if not upload:
            return result
        completed_parts = upload_file_to_target(
            upload=upload,
            file_path=file_path,
            size_bytes=size_bytes,
            content_type=content_type,
        )
        completed = self.control.call(
            "storage.complete_upload",
            {
                "project_id": project_id,
                "upload_id": str(upload["upload_id"]),
                "parts": completed_parts,
            },
        )
        result["object"] = completed
        result["uploaded"] = True
        return result

    def _download_storage_file(
        self, *, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        repo_root, project_id = self._linked_scope(context=context)
        target = self._resolve_local_path(
            repo_root=repo_root, path=self._required_arg(arguments, "path")
        )
        if target.exists() and not bool(arguments.get("overwrite")):
            raise ValidationError(
                f"download target already exists; pass overwrite=true to replace: {target}"
            )
        payload = {
            "project_id": project_id,
            "object_id": arguments.get("object_id"),
            "name": arguments.get("name"),
            "version": arguments.get("version"),
            "include_download": True,
        }
        resolved = self.control.call("storage.resolve", payload)
        obj = resolved["object"]
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp-{new_id(prefix='download')}")
        try:
            download_target_to_file(download=resolved["download"], path=tmp)
            sha256, size_bytes = file_digest(tmp)
            if sha256 != str(obj["content_sha256"]):
                raise ValidationError(
                    "downloaded storage object checksum mismatch: "
                    f"{sha256} != {obj['content_sha256']}"
                )
            if size_bytes != int(obj["size_bytes"]):
                raise ValidationError(
                    "downloaded storage object size mismatch: "
                    f"{size_bytes} != {obj['size_bytes']} bytes"
                )
            tmp.replace(target)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        return {
            "object": obj,
            "path": str(target),
            "downloaded": True,
            "bytes_written": int(obj["size_bytes"]),
        }

    def _sandbox_get_enrichment(
        self, *, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        _repo_root, project_id = self._linked_scope(context=context)
        args = dict(arguments)
        args["project_id"] = project_id
        facts = self.control.call("sandbox.get", args)
        sandbox_uid = str(facts.get("sandbox_uid") or "")
        facts.pop("_experiment_name", None)
        name = f"sandbox-{sandbox_uid[:12]}" if sandbox_uid else ""
        enrichment = self._sandbox_enrichment(
            facts=facts,
            name=name,
            use_sandbox_uid_command=True,
        )
        return {
            "command": enrichment.get("command", ""),
            "raw_command": enrichment.get("raw_command", ""),
            "local_dir": enrichment.get("local_dir", ""),
            "key_path": enrichment.get("key_path", ""),
        }

    def _merge_sandbox_enrichment(
        self,
        *,
        facts: dict[str, Any],
        name: str,
        use_sandbox_uid_command: bool = True,
    ) -> dict[str, Any]:
        enrichment = self._sandbox_enrichment(
            facts=facts,
            name=name,
            use_sandbox_uid_command=use_sandbox_uid_command,
        )
        return sandbox_views.merge_agent_view(facts=facts, enrichment=enrichment)

    def _sandbox_enrichment(
        self,
        *,
        facts: dict[str, Any],
        name: str,
        use_sandbox_uid_command: bool = True,
    ) -> dict[str, Any]:
        return self.worker.sandbox_enrichment(
            row=self._sandbox_row_from_facts(facts=facts),
            name=name,
            use_sandbox_uid_command=use_sandbox_uid_command,
        )

    def _sandbox_row_from_facts(self, *, facts: dict[str, Any]) -> dict[str, Any]:
        ssh = facts.get("ssh") if isinstance(facts.get("ssh"), dict) else {}
        return {
            "experiment_id": facts.get("experiment_id", ""),
            "sandbox_uid": facts.get("sandbox_uid", ""),
            "project_id": facts.get("project_id", ""),
            "sandbox_id": facts.get("sandbox_id", ""),
            "status": facts.get("status", ""),
            "ssh_host": ssh.get("host", ""),
            "ssh_port": ssh.get("port") or 0,
            "ssh_user": ssh.get("user") or "root",
            "workdir": facts.get("workdir", ""),
            "sync_dir": facts.get("experiment_dir", ""),
            "sandbox_data_dir": facts.get("data_dir", ""),
        }

    def _register_resource_files(
        self, *, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        repo_root, project_id = self._linked_scope(context=context)
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
            return {"resources": resources, "count": len(resources)}
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
        repo_root, project_id = self._linked_scope(context=context)
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
        artifact = LocalResourceArtifactReader(repo_root=repo_root).read_for_association(
            path=path, role=role
        )
        content_bytes = artifact.get("content_bytes")
        if content_bytes is not None:
            payload["blob"] = {
                "data_b64": base64.b64encode(content_bytes).decode("ascii"),
                "content_type": str(
                    artifact.get("content_type") or "application/octet-stream"
                ),
            }
            figures = artifact.get("figures") or []
            if figures:
                payload["figures"] = [
                    {
                        "link_path": str(figure.get("link_path") or ""),
                        "data_b64": base64.b64encode(figure["data"]).decode("ascii"),
                        "content_type": str(
                            figure.get("content_type") or "application/octet-stream"
                        ),
                        "size_bytes": int(figure.get("size_bytes") or 0),
                    }
                    for figure in figures
                    if isinstance(figure.get("data"), bytes)
                ]
        return self.control.submit_resource_association(payload)

    def _validate_resource_file(
        self, *, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        repo_root, _project_id = self._linked_scope(context=context)
        return validate_local_resource_artifact(
            repo_root=repo_root,
            path=self._required_arg(arguments, "path"),
            role=self._required_arg(arguments, "role"),
        )

    def _associate_resource_batch(
        self, *, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        associations = arguments.get("associations")
        if not isinstance(associations, list) or not associations:
            raise ValidationError("associations must be a non-empty list")
        applied = [
            self._associate_resource(arguments=dict(association), context=context)
            for association in associations
        ]
        return {"associations": applied, "count": len(applied)}

    def _merge_results_tsv(
        self, *, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        repo_root, _project_id = self._linked_scope(context=context)
        key_columns = arguments.get("key_columns") or []
        if not isinstance(key_columns, list):
            raise ValidationError("key_columns must be a list")
        return merge_results_tsv(
            repo_root=repo_root,
            source_path=self._required_arg(arguments, "source_path"),
            target_path=self._required_arg(arguments, "target_path"),
            key_columns=[str(column) for column in key_columns],
            dry_run=bool(arguments.get("dry_run") or False),
        )

    def _materialize_experiment_folders(
        self, *, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        repo_root, project_id = self._linked_scope(context=context)
        experiment_id = str(arguments.get("experiment_id") or "").strip()
        status = arguments.get("status", "planned")
        if experiment_id:
            experiments = [
                self.control.call(
                    "experiment.get_state",
                    {
                        "project_id": project_id,
                        "experiment_id": experiment_id,
                    },
                )
            ]
        else:
            listed = self.control.call("experiment.list", {"project_id": project_id})
            raw_experiments = listed.get("experiments")
            if not isinstance(raw_experiments, list):
                raise ValidationError("experiment.list returned an invalid payload")
            experiments = [
                experiment
                for experiment in raw_experiments
                if isinstance(experiment, dict)
                and (status is None or experiment.get("status") == status)
            ]
        return materialize_experiment_folders(
            repo_root=repo_root,
            experiments=experiments,
        )

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
        observation = LocalResourceObserver(repo_root=repo_root).observe_file(
            path=path,
            kind=kind,
            title=title,
            created_by=created_by,
        )
        return self.control.submit_resource_observation(
            {"project_id": project_id, **observation}
        )

    def _repo_root_from_context(self, *, context: dict[str, Any]) -> Path:
        repo_root = str(context.get("repo_root") or "")
        if not repo_root:
            raise ValidationError("repo_root context is required for data-plane tools")
        return Path(repo_root).expanduser().resolve()

    def _linked_scope(self, *, context: dict[str, Any]) -> tuple[Path, str]:
        repo_root = self._repo_root_from_context(context=context)
        return repo_root, self._project_id(repo_root=repo_root)

    def _project_id(self, *, repo_root: Path) -> str:
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

    def _resolve_local_path(self, *, repo_root: Path, path: str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = repo_root / candidate
        return candidate

    def _default_storage_name(self, *, repo_root: Path, path: Path) -> str:
        try:
            return path.resolve().relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            return path.name

def build_daemon_executor(*, worker: LocalDataPlaneWorker):
    """The daemon's task dispatch: (type, payload, deadline) -> result.

    Same worker-dispatch shape as InProcessTaskChannel._execute, but reads
    JSON payloads from the control-plane queue.
    """
    def execute(task_type: str, payload: dict[str, Any], deadline: str | None) -> Any:
        del deadline
        if task_type == "conn_refresh":
            return worker.sandbox_enrichment(
                row=payload["row"],
                name=str(payload.get("name") or ""),
                use_sandbox_uid_command=bool(
                    payload.get("use_sandbox_uid_command", True)
                ),
            )
        if task_type == "teardown":
            worker.remove_conn_file(
                experiment_id=str(payload["experiment_id"]),
                sandbox_uid=str(payload.get("sandbox_uid") or ""),
                remove_experiment_alias=bool(
                    payload.get("remove_experiment_alias", True)
                ),
            )
            return None
        raise ValidationError(f"unknown task type: {task_type}")

    return execute


def build_daemon_server(
    *,
    control_url: str | None,
    workspace_root: Path | None = None,
    client_id: str | None = None,
    env: Mapping[str, str] | None = None,
) -> DaemonServer:
    """Build the data-plane daemon, fail-fast on a missing control URL.

    ``workspace_root`` defaults to ~/.research_plugin for the daemon's own
    machine-local state (keys, conn files, sandbox_local.sqlite). ``client_id``
    is the stable per-daemon id; absent, the worker mints one.
    """
    if not control_url:
        raise ValidationError(
            "daemon mode requires RESEARCH_PLUGIN_CONTROL_URL (the cloud control "
            "plane URL); refusing to start with no upstream (cloud plan §3.4)",
            details={"mode": "daemon"},
        )
    root = workspace_root or (Path.home() / ".research_plugin")
    workspace = LocalWorkspace(repo_root=root)
    worker = LocalDataPlaneWorker(workspace=workspace)
    resolved_client_id = client_id or worker.client_id()
    control = HttpControlPlaneClient(base_url=control_url)
    view = HttpControlPlaneView(
        control=control, worker=worker, client_id=resolved_client_id
    )
    executor = build_daemon_executor(worker=worker)

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
        project_links=project_links,
        loopback_secret=loopback_secret,
    )
