"""Key-principal sandbox control path (no-dataplane Phase C).

A project-scoped ``mk_`` key reaches sandbox request/attach/pull_outputs over
the CONTROL path — a cloud agent has no local proxy. This is an ADDED path
beside the proxy's local data plane (the execution_strategy is not flipped until
Phase D), so local agents are unaffected. Project-shared (ruling 7): scope is
the key's project via the existing project_id equality + membership boundary in
``ProjectAuthorizer`` — there is NO per-principal ownership. The facade paths run
WITHOUT local_dir enrichment; attach never installs the caller's key.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from ....kernel.utils import ValidationError
from ...tools.contracts import TOOL_MANIFEST

# The sandbox data-plane tools an mk_ key is served over control.
KEY_SANDBOX_CONTROL_TOOLS = frozenset(
    {"sandbox.request", "sandbox.attach", "sandbox.pull_outputs"}
)


def serve_key_sandbox(
    *,
    sandboxes: Any,
    projects: Any,
    name: str,
    arguments: dict[str, Any],
    principal: Any,
) -> dict[str, Any]:
    """Serve a sandbox data-plane tool over control for an mk_ key."""
    projects.require_member(project_id=arguments.get("project_id"), principal=principal)
    try:
        request = TOOL_MANIFEST[name].input_model.model_validate(arguments)
    except PydanticValidationError as exc:
        raise ValidationError(
            "invalid tool arguments", details={"tool": name, "errors": exc.errors()}
        ) from exc
    if name == "sandbox.request":
        return sandboxes.request(
            experiment_id=request.experiment_id,
            project_id=request.project_id,
            gpu=request.gpu,
            cpu=request.cpu,
            memory=request.memory,
            time_limit=request.time_limit,
            instance_type=request.instance_type,
            region=request.region,
            provider=request.provider,
            public_key=request.public_key,
            include_data_plane_enrichment=False,
            additional=request.additional,
            provisioning_user_id=str(getattr(principal, "user_id", "") or ""),
            provisioning_key_id=str(getattr(principal, "key_id", "") or ""),
        )
    if name == "sandbox.attach":
        return sandboxes.attach(
            experiment_id=request.experiment_id,
            project_id=request.project_id,
            sandbox_uid=request.sandbox_uid,
            include_data_plane_enrichment=False,
        )
    return sandboxes.pull_outputs_command(
        experiment_id=request.experiment_id,
        project_id=request.project_id,
        sandbox_uid=request.sandbox_uid,
        paths=request.paths,
    )


__all__ = ["KEY_SANDBOX_CONTROL_TOOLS", "serve_key_sandbox"]
