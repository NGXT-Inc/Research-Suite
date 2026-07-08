"""Proxy-local execution for split-mode data-plane tools.

The MCP proxy runs on the user's machine, so it can safely perform the local
file reads and validation that used to require a long-running daemon. Static
imports stay stdlib-only; backend data-plane helpers are imported lazily inside
methods so the proxy package's import discipline remains unchanged.
"""

from __future__ import annotations

import base64
import importlib
import mimetypes
import uuid
from pathlib import Path
from typing import Any, Callable


RSYNC_PULL_OUTPUTS_HINT = (
    'rsync -az --itemize-changes --no-links --no-devices --no-specials '
    '-e "ssh -i <key_path> -p <port> -o StrictHostKeyChecking=no '
    '-o UserKnownHostsFile=/dev/null" '
    "<user>@<host>:<remote-path> <local-destination>"
)


ControlApiPost = Callable[[str, dict[str, Any]], dict[str, Any]]
ControlToolCall = Callable[[str, dict[str, Any]], dict[str, Any]]
ProjectIdResolver = Callable[[], str | None]


class LocalDataPlaneError(Exception):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "validation_error",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.details = details or {}


class LocalDataPlane:
    def __init__(
        self,
        *,
        repo_root: Path,
        project_id_resolver: ProjectIdResolver,
        control_api_post: ControlApiPost,
        control_tool_call: ControlToolCall,
    ) -> None:
        self.repo_root = Path(repo_root).expanduser().resolve()
        self._project_id_resolver = project_id_resolver
        self._control_api_post = control_api_post
        self._control_tool_call = control_tool_call

    def call_tool(self, *, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        arguments = dict(arguments or {})
        if name == "sandbox.health":
            return {"ok": True, "mode": "proxy"}
        if name == "resource.register_file":
            return self._register_resource_files(arguments=arguments)
        if name == "resource.validate":
            return self._validate_resource_file(arguments=arguments)
        if name == "resource.associate":
            return self._associate_resource(arguments=arguments)
        if name == "resource.associate_batch":
            return self._associate_resource_batch(arguments=arguments)
        if name == "experiment.materialize_folders":
            return self._materialize_experiment_folders(arguments=arguments)
        if name == "feed.post":
            return self._post_feed(arguments=arguments)
        if name == "storage.upload_file":
            return self._upload_storage_file(arguments=arguments)
        if name == "storage.download_file":
            return self._download_storage_file(arguments=arguments)
        if name == "sandbox.request":
            return self._request_sandbox(arguments=arguments)
        if name == "sandbox.attach":
            return self._attach_sandbox(arguments=arguments)
        if name == "sandbox.pull_outputs":
            return self._pull_sandbox_outputs(arguments=arguments)
        if name == "sandbox.get":
            return self._sandbox_get_enrichment(arguments=arguments)
        raise LocalDataPlaneError(
            f"tool is not served by the proxy data plane: {name}",
            details={"tool": name},
        )

    def _request_sandbox(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = self._project_id()
        public_key = str(arguments.get("public_key") or "").strip()
        if not public_key:
            raise LocalDataPlaneError(
                "split-mode sandbox.request requires public_key because the "
                "long-running local daemon no longer mints user SSH keys. "
                "Generate an ed25519 keypair on this machine and pass the "
                "single-line .pub contents as public_key.",
                error_code="public_key_required",
            )
        payload = dict(arguments)
        payload["project_id"] = project_id
        self._validate_sandbox_request(payload)
        return self._control_api_post("/api/data-plane/sandboxes/request", payload)

    def _attach_sandbox(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = dict(arguments)
        payload["project_id"] = self._project_id()
        payload.setdefault("public_key", "")
        return self._control_api_post("/api/data-plane/sandboxes/attach", payload)

    def _sandbox_get_enrichment(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        args = dict(arguments)
        args["project_id"] = self._project_id()
        facts = self._control_tool_call("sandbox.get", args)
        sandbox_uid = str(facts.get("sandbox_uid") or "")
        experiment_id = str(facts.get("experiment_id") or sandbox_uid)
        name = f"sandbox-{sandbox_uid[:12]}" if sandbox_uid else ""
        local_dir = ""
        if experiment_id:
            local_dir = str(
                self._local_experiment_dir(experiment_id=experiment_id, name=name)
            )
        return {"local_dir": local_dir}

    def _pull_sandbox_outputs(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        args = dict(arguments)
        project_id = self._project_id()
        sandbox_args = {
            "project_id": project_id,
            "experiment_id": args.get("experiment_id"),
            "sandbox_uid": args.get("sandbox_uid"),
        }
        sandbox = self._control_tool_call("sandbox.get", sandbox_args)
        key_path = str(args.get("key_path") or "").strip()
        if key_path:
            sandbox = dict(sandbox)
            ssh = dict(sandbox.get("ssh") or {})
            ssh["key_path"] = key_path
            sandbox["ssh"] = ssh
        if "local_experiment_dir" not in sandbox:
            sandbox = dict(sandbox)
            experiment_id = str(sandbox.get("experiment_id") or args.get("experiment_id") or "")
            sandbox_uid = str(sandbox.get("sandbox_uid") or args.get("sandbox_uid") or "")
            if experiment_id or sandbox_uid:
                sandbox["local_experiment_dir"] = str(
                    self._local_experiment_dir(
                        experiment_id=experiment_id or sandbox_uid,
                        name=(
                            f"sandbox-{sandbox_uid[:12]}"
                            if sandbox_uid and sandbox_uid != experiment_id
                            else ""
                        ),
                    )
                )
        pull_sandbox_outputs = _import_attr(
            "backend.dataplane.sandbox_outputs",
            "pull_sandbox_outputs",
        )
        result = pull_sandbox_outputs(
            repo_root=self.repo_root,
            sandbox=sandbox,
            paths=args.get("paths") or [],
            destination_path=str(args.get("destination_path") or ""),
            overwrite=bool(args.get("overwrite")),
        )
        result["storage_guidance"] = self._storage_guidance()
        result["rsync"] = RSYNC_PULL_OUTPUTS_HINT
        # Live-runs nudge rides along from the control sandbox.get lookup, so
        # pulling outputs from a box with a run still going is never silent.
        if sandbox.get("runs"):
            result["runs"] = sandbox["runs"]
        return result

    def _post_feed(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "project_id": self._project_id(),
            "handle": self._required_arg(arguments, "handle"),
            "text": self._required_arg(arguments, "text"),
        }
        for key in ("url", "ref", "kind", "in_reply_to"):
            if arguments.get(key) is not None:
                payload[key] = arguments.get(key)
        self._control_api_post(
            "/api/data-plane/feed/validate-post",
            {
                key: payload[key]
                for key in ("project_id", "handle", "text", "ref", "kind", "in_reply_to")
                if key in payload
            },
        )
        image_path = str(arguments.get("image_path") or "")
        html_path = str(arguments.get("html_path") or "")
        if image_path and html_path:
            raise LocalDataPlaneError(
                "a post may carry an image or an embed, not both"
            )
        if image_path:
            payload["image"] = self._feed_image_payload(image_path=image_path)
        if html_path:
            payload["html"] = self._feed_embed_payload(html_path=html_path)
        return self._control_api_post("/api/data-plane/feed/post", payload)

    def _feed_image_payload(self, *, image_path: str) -> dict[str, Any]:
        reader_cls = _import_attr("backend.dataplane.feed_images", "LocalFeedImageReader")
        image = reader_cls(repo_root=self.repo_root).read_image(path=image_path)
        return {
            "path": str(image["path"]),
            "data_b64": base64.b64encode(image["data"]).decode("ascii"),
        }

    def _feed_embed_payload(self, *, html_path: str) -> dict[str, Any]:
        reader_cls = _import_attr("backend.dataplane.feed_embeds", "LocalFeedEmbedReader")
        embed = reader_cls(repo_root=self.repo_root).read_embed(path=html_path)
        return {
            "path": str(embed["path"]),
            "data_b64": base64.b64encode(embed["data"]).decode("ascii"),
        }

    def _register_resource_files(
        self, *, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        project_id = self._project_id()
        kind = str(arguments.get("kind") or "other")
        title = str(arguments.get("title") or "")
        created_by = str(arguments.get("created_by") or "codex")
        paths = arguments.get("paths")
        if paths:
            if not isinstance(paths, list):
                raise LocalDataPlaneError("paths must be a list")
            resources = [
                self._submit_resource_observation(
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
            raise LocalDataPlaneError(
                "resource.register_file requires 'path' (a single file) or 'paths' (a batch)"
            )
        return self._submit_resource_observation(
            project_id=project_id,
            path=str(path),
            kind=kind,
            title=title,
            created_by=created_by,
        )

    def _validate_resource_file(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        self._project_id()
        validate_local_resource_artifact = _import_attr(
            "backend.dataplane.resource_validation",
            "validate_local_resource_artifact",
        )
        return validate_local_resource_artifact(
            repo_root=self.repo_root,
            path=self._required_arg(arguments, "path"),
            role=self._required_arg(arguments, "role"),
        )

    def _associate_resource(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = self._project_id()
        resource_id = self._required_arg(arguments, "resource_id")
        role = self._required_arg(arguments, "role")
        intent = {
            "project_id": project_id,
            "resource_id": resource_id,
            "target_type": self._required_arg(arguments, "target_type"),
            "target_id": self._required_arg(arguments, "target_id"),
            "role": role,
        }
        validation = self._control_api_post(
            "/api/data-plane/resources/validate-association", intent
        )
        resource = validation.get("resource") or {}
        path = str(resource.get("path") or "")
        if not path:
            raise LocalDataPlaneError(f"resource has no path: {resource_id}")
        self._submit_resource_observation(
            project_id=project_id,
            path=path,
            kind=str(resource.get("kind") or "other"),
            title=str(resource.get("title") or ""),
            created_by=str(resource.get("created_by") or "codex"),
        )
        payload: dict[str, Any] = {**intent}
        reader_cls = _import_attr(
            "backend.dataplane.resource_artifacts",
            "LocalResourceArtifactReader",
        )
        artifact = reader_cls(repo_root=self.repo_root).read_for_association(
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
        return self._control_api_post("/api/data-plane/resources/associate", payload)

    def _associate_resource_batch(
        self, *, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        associations = arguments.get("associations")
        if not isinstance(associations, list) or not associations:
            raise LocalDataPlaneError("associations must be a non-empty list")
        applied = [
            self._associate_resource(arguments=dict(association))
            for association in associations
        ]
        return {"associations": applied, "count": len(applied)}

    def _materialize_experiment_folders(
        self, *, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        project_id = self._project_id()
        experiment_id = str(arguments.get("experiment_id") or "").strip()
        status = arguments.get("status", "planned")
        if experiment_id:
            experiments = [
                self._control_tool_call(
                    "experiment.get_state",
                    {"project_id": project_id, "experiment_id": experiment_id},
                )
            ]
        else:
            listed = self._control_tool_call(
                "experiment.list", {"project_id": project_id}
            )
            raw_experiments = listed.get("experiments")
            if not isinstance(raw_experiments, list):
                raise LocalDataPlaneError("experiment.list returned an invalid payload")
            experiments = [
                experiment
                for experiment in raw_experiments
                if isinstance(experiment, dict)
                and (status is None or experiment.get("status") == status)
            ]
        materialize = _import_attr(
            "backend.dataplane.experiment_folders",
            "materialize_experiment_folders",
        )
        return materialize(repo_root=self.repo_root, experiments=experiments)

    def _upload_storage_file(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = self._project_id()
        file_path = self._resolve_local_path(path=self._required_arg(arguments, "path"))
        if not file_path.exists():
            raise LocalDataPlaneError(f"storage upload file not found: {file_path}")
        if not file_path.is_file():
            raise LocalDataPlaneError(f"storage upload path is not a file: {file_path}")
        file_digest = _import_attr("backend.storage.file_transfer", "file_digest")
        upload_file_to_target = _import_attr(
            "backend.storage.file_transfer", "upload_file_to_target"
        )
        sha256, size_bytes = file_digest(file_path)
        content_type = (
            str(arguments.get("content_type") or "")
            or mimetypes.guess_type(str(file_path))[0]
            or "application/octet-stream"
        )
        payload = {
            "project_id": project_id,
            "name": str(arguments.get("name") or "")
            or self._default_storage_name(path=file_path),
            "kind": self._required_arg(arguments, "kind"),
            "sha256": sha256,
            "size_bytes": size_bytes,
            "content_type": content_type,
            "producing_experiment_id": str(arguments.get("producing_experiment_id") or ""),
            "producing_run": str(arguments.get("producing_run") or ""),
            "source_uri": str(arguments.get("source_uri") or ""),
            "notes": str(arguments.get("notes") or ""),
        }
        registered = self._control_tool_call("storage.put_object", payload)
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
        completed = self._control_tool_call(
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

    def _download_storage_file(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = self._project_id()
        target = self._resolve_local_path(path=self._required_arg(arguments, "path"))
        if target.exists() and not bool(arguments.get("overwrite")):
            raise LocalDataPlaneError(
                f"download target already exists; pass overwrite=true to replace: {target}"
            )
        resolved = self._control_tool_call(
            "storage.resolve",
            {
                "project_id": project_id,
                "object_id": arguments.get("object_id"),
                "name": arguments.get("name"),
                "version": arguments.get("version"),
                "include_download": True,
            },
        )
        obj = resolved["object"]
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp-{uuid.uuid4().hex}")
        file_digest = _import_attr("backend.storage.file_transfer", "file_digest")
        download_target_to_file = _import_attr(
            "backend.storage.file_transfer", "download_target_to_file"
        )
        try:
            download_target_to_file(download=resolved["download"], path=tmp)
            sha256, size_bytes = file_digest(tmp)
            if sha256 != str(obj["content_sha256"]):
                raise LocalDataPlaneError(
                    "downloaded storage object checksum mismatch: "
                    f"{sha256} != {obj['content_sha256']}"
                )
            if size_bytes != int(obj["size_bytes"]):
                raise LocalDataPlaneError(
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

    def _submit_resource_observation(
        self,
        *,
        project_id: str,
        path: str,
        kind: str,
        title: str,
        created_by: str,
    ) -> dict[str, Any]:
        observer_cls = _import_attr(
            "backend.dataplane.resource_observer",
            "LocalResourceObserver",
        )
        observation = observer_cls(repo_root=self.repo_root).observe_file(
            path=path,
            kind=kind,
            title=title,
            created_by=created_by,
        )
        return self._control_api_post(
            "/api/data-plane/resources/observe",
            {"project_id": project_id, **observation},
        )

    def _project_id(self) -> str:
        project_id = self._project_id_resolver()
        if isinstance(project_id, str) and project_id:
            return project_id
        raise LocalDataPlaneError(
            "no hosted project link found for repo; run "
            "research-plugin-client link --project-id <project_id>",
            error_code="project_not_linked",
            details={"repo_root": str(self.repo_root)},
        )

    def _required_arg(self, arguments: dict[str, Any], key: str) -> str:
        value = arguments.get(key)
        if value is None or str(value) == "":
            raise LocalDataPlaneError(f"{key} is required")
        return str(value)

    def _resolve_local_path(self, *, path: str) -> Path:
        resolve_repo_path = _import_attr("backend.dataplane.repo_paths", "resolve_repo_path")
        _rel, full = resolve_repo_path(repo_root=self.repo_root, path=path)
        return full

    def _default_storage_name(self, *, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.repo_root.resolve()).as_posix()
        except ValueError:
            return path.name

    def _local_experiment_dir(self, *, experiment_id: str, name: str = "") -> Path:
        local_experiment_dir = _import_attr("backend.workspace", "local_experiment_dir")
        return local_experiment_dir(
            repo_root=self.repo_root, experiment_id=experiment_id, name=name
        )

    def _validate_sandbox_request(self, payload: dict[str, Any]) -> None:
        input_cls = _import_attr("backend.tools.contracts", "SandboxRequestInput")
        input_cls.model_validate(payload)

    def _storage_guidance(self) -> str:
        return str(
            _import_attr(
                "backend.domain.storage_guidance",
                "STORAGE_RULE_OF_THUMB",
            )
        )


def _import_attr(module_name: str, attr: str) -> Any:
    module = importlib.import_module(module_name)
    return getattr(module, attr)
