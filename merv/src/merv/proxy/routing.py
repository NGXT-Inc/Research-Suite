"""Pure manifest-driven tool routing decisions for the stdio proxy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional


RouteTarget = Literal[
    "control",
    "control-raw",
    "local",
    "enriched",
    "project-current",
    "project-connect",
    "project-overview",
]


@dataclass(frozen=True)
class ToolRoute:
    execution_strategy: str = "control"
    scope_strategy: str = "none"
    handler_identity: str = ""
    local_handler_identity: str = ""
    plane: str = "control"
    # Compatibility constructor field for older tests/extensions that created
    # `_ToolMeta(project_scoped=True)` directly.
    project_scoped: bool = False


def route_target(
    *, name: str, arguments: dict[str, Any], route: ToolRoute
) -> RouteTarget:
    del name
    if route.handler_identity == "operations.project":
        action = str(arguments.get("action") or "")
        if action == "current":
            return "project-current"
        if action == "connect":
            return "project-connect"
        if action == "overview":
            return "project-overview"
        return "control-raw"
    if (
        route.execution_strategy == "control-plus-local-enrichment"
        or route.plane == "aggregate"
    ):
        return "enriched"
    if (
        route.execution_strategy in {"local", "local-orchestration"}
        or route.plane == "data"
    ):
        return "local"
    return "control"


def route_from_manifest(tool: dict[str, Any]) -> ToolRoute:
    schema = tool.get("inputSchema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    scope = tool.get("scopeStrategy")
    if not isinstance(scope, str):
        scope = (
            "linked-project"
            if isinstance(properties, dict) and "project_id" in properties
            else "none"
        )
    return ToolRoute(
        execution_strategy=str(
            tool.get("executionStrategy") or tool.get("plane") or "control"
        ),
        scope_strategy=scope,
        handler_identity=str(tool.get("handlerIdentity") or tool.get("name") or ""),
        local_handler_identity=str(tool.get("localHandlerIdentity") or ""),
        plane=str(tool.get("plane") or "control"),
        project_scoped=scope == "linked-project",
    )


def public_catalog_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Strip private manifest fields while preserving the legacy wire shape."""
    keys = ("name", "description", "inputSchema", "plane", "hidden")
    return {key: tool[key] for key in keys if key in tool}


def merge_enriched_control(
    *,
    cloud: dict[str, Any],
    local: dict[str, Any],
    cloud_error: dict[str, Any] | None,
    local_error: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge control facts with the proxy-local fields exposed on the wire."""
    merged = dict(cloud)
    if any(local.get(key) for key in ("command", "raw_command", "key_path")):
        ssh = dict(merged.get("ssh") or {})
        ssh.update(
            (key, local[key])
            for key in ("command", "raw_command", "key_path")
            if local.get(key)
        )
        merged["ssh"] = ssh
    if local.get("local_dir"):
        merged["local_experiment_dir"] = local["local_dir"]
    if cloud_error:
        merged["control_plane"] = cloud_error
    if local_error:
        merged["data_plane"] = local_error
    return {key: value for key, value in merged.items() if not key.startswith("_")}


@lru_cache(maxsize=1)
def shipped_manifest() -> tuple[dict[str, Any], ...]:
    path = Path(__file__).with_name("_tool_manifest.json")
    return tuple(json.loads(path.read_text(encoding="utf-8"))["tools"])


def shipped_route(name: str) -> ToolRoute:
    for tool in shipped_manifest():
        if tool.get("name") == name:
            return route_from_manifest(tool)
    return ToolRoute()


def local_handler_identity(name: str) -> str:
    route = shipped_route(name)
    return route.local_handler_identity or (
        route.handler_identity if route.handler_identity.startswith("local.") else ""
    )
