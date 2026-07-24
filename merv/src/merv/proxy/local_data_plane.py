"""Proxy-local execution for split-mode data-plane tools.

The MCP proxy runs on the user's machine, so it can safely perform the local
file reads and validation required by the current architecture. Proxy-local and
shared helpers are imported lazily inside methods to keep startup light.
"""

from __future__ import annotations

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
