"""Proxy-local execution for split-mode data-plane tools.

The MCP proxy runs on the user's machine, so it can safely perform the local
file reads and validation required by the current architecture. Proxy-local and
shared helpers are imported lazily inside methods to keep startup light.
"""

from __future__ import annotations

import mimetypes
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable, Optional

from .routing import local_handler_identity


RSYNC_PULL_OUTPUTS_HINT = (
    "rsync -az --itemize-changes --no-links --no-devices --no-specials "
    '-e "ssh -i <key_path> -p <port> -o StrictHostKeyChecking=no '
    '-o UserKnownHostsFile=/dev/null" '
    "<user>@<host>:<remote-path> <local-destination>"
)


ControlApiPost = Callable[[str, dict[str, Any]], dict[str, Any]]
ControlToolCall = Callable[[str, dict[str, Any]], dict[str, Any]]
# Runtime-evaluated alias: typing.Optional, not `str | None` — the proxy must
# import under Apple CLT Python 3.9, where PEP 604 unions raise at runtime.
ProjectIdResolver = Callable[[], Optional[str]]


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

    def call_tool(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        control_facts: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        arguments = dict(arguments or {})
        identity = local_handler_identity(name)
        if identity.startswith("local."):
            handler = getattr(self, f"_{identity.split('.', 1)[1]}", None)
            if handler is not None:
                if control_facts is not None:
                    return handler(arguments=arguments, control_facts=control_facts)
                return handler(arguments=arguments)
        raise LocalDataPlaneError(
            f"tool is not served by the proxy data plane: {name}",
            details={"tool": name},
        )

    def _health(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        del arguments
        return {"ok": True, "mode": "proxy"}

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

    def _sandbox_get_enrichment(
        self,
        *,
        arguments: dict[str, Any],
        control_facts: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        facts = control_facts
        if facts is None:
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
        from .dataplane.sandbox_outputs import pull_sandbox_outputs

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
            experiment_id = str(
                sandbox.get("experiment_id") or args.get("experiment_id") or ""
            )
            sandbox_uid = str(
                sandbox.get("sandbox_uid") or args.get("sandbox_uid") or ""
            )
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
        result = pull_sandbox_outputs(
            repo_root=self.repo_root,
            sandbox=sandbox,
            paths=args.get("paths") or [],
            destination_path=str(args.get("destination_path") or ""),
            overwrite=bool(args.get("overwrite")),
        )
        from merv.shared.storage_guidance import STORAGE_RULE_OF_THUMB

        result["storage_guidance"] = str(STORAGE_RULE_OF_THUMB)
        result["rsync"] = RSYNC_PULL_OUTPUTS_HINT
        # Live-runs nudge rides along from the control sandbox.get lookup, so
        # pulling outputs from a box with a run still going is never silent.
        if sandbox.get("runs"):
            result["runs"] = sandbox["runs"]
        return result

    # feed.post is a control tool since the no-dataplane transition (Phase D.1):
    # media bytes travel over the agent's own `curl -T` against the token-bearer
    # PUT /api/feed/u/<token>, so the proxy forwards feed.post to /mcp unchanged
    # and carries no feed media handler.

    def _materialize_experiment_folders(
        self, *, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        from .dataplane.experiment_folders import materialize_experiment_folders

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
        return materialize_experiment_folders(
            repo_root=self.repo_root, experiments=experiments
        )

    def _upload_storage_file(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        from merv.shared.file_transfer import file_digest, upload_file_to_target

        project_id = self._project_id()
        file_path = self._resolve_local_path(path=self._required_arg(arguments, "path"))
        if not file_path.exists():
            raise LocalDataPlaneError(f"storage upload file not found: {file_path}")
        if not file_path.is_file():
            raise LocalDataPlaneError(f"storage upload path is not a file: {file_path}")
        sha256, size_bytes = file_digest(file_path)
        content_type = (
            str(arguments.get("content_type") or "")
            or mimetypes.guess_type(str(file_path))[0]
            or "application/octet-stream"
        )
        payload = {
            "project_id": project_id,
            "name": str(arguments.get("name") or "")
            or file_path.relative_to(self.repo_root).as_posix(),
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
        from merv.shared.file_transfer import download_target_to_file, file_digest

        project_id = self._project_id()
        target = self._resolve_local_path(path=self._required_arg(arguments, "path"))
        if target.exists() and not bool(arguments.get("overwrite")):
            raise LocalDataPlaneError(
                f"download target already exists; pass overwrite=true to replace: {target}"
            )
        resolved = self._control_tool_call(
            "storage.find",
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
            with suppress(FileNotFoundError):
                tmp.unlink()
        return {
            "object": obj,
            "path": str(target),
            "downloaded": True,
            "bytes_written": int(obj["size_bytes"]),
        }

    def _project_id(self) -> str:
        project_id = self._project_id_resolver()
        if isinstance(project_id, str) and project_id:
            return project_id
        raise LocalDataPlaneError(
            "no hosted project link found for repo; call the project tool with "
            'action="connect" to link this folder to a project',
            error_code="project_not_linked",
            details={"repo_root": str(self.repo_root)},
        )

    def _required_arg(self, arguments: dict[str, Any], key: str) -> str:
        value = arguments.get(key)
        if value is None or str(value) == "":
            raise LocalDataPlaneError(f"{key} is required")
        return str(value)

    def _resolve_local_path(self, *, path: str) -> Path:
        from .dataplane.repo_paths import resolve_repo_path

        _rel, full = resolve_repo_path(repo_root=self.repo_root, path=path)
        return full

    def _local_experiment_dir(self, *, experiment_id: str, name: str = "") -> Path:
        from .workspace import local_experiment_dir

        return local_experiment_dir(
            repo_root=self.repo_root, experiment_id=experiment_id, name=name
        )

    def _validate_sandbox_request(self, payload: dict[str, Any]) -> None:
        from merv.shared.tool_validation import validate_openssh_public_key

        try:
            validate_openssh_public_key(str(payload.get("public_key") or ""))
        except ValueError as exc:
            raise LocalDataPlaneError(str(exc)) from exc
