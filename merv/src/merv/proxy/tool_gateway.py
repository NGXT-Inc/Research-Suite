"""Manifest-driven tool execution across the brain and local data plane."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from merv.shared.client_config import HOSTED_CONTROL_URL, LOCAL_BRAIN_URL

from .errors import UpstreamError
from .features import storage_feature_enabled
from .local_data_plane import LocalDataPlane, LocalDataPlaneError
from .routing import (
    ToolRoute,
    merge_enriched_control,
    public_catalog_tool,
    route_from_manifest,
    route_target,
    shipped_manifest,
)


LONG_VERB_TIMEOUT_SECONDS = 90.0
LONG_VERBS = {"sandbox.request"}


class ToolGateway:
    """Execution mixin composed by the stdio MCP server."""

    def _list_tools(self) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        catalog, _complete = self._catalog_tools()
        for _is_cloud, tool in catalog:
            # Literal retained only for brains predating the manifest visibility flag.
            if tool.get("hidden") or tool.get("name") == "project.list":
                continue
            shaped = self._with_hidden_project_scope(tool=tool)
            merged[shaped["name"]] = shaped
        return list(merged.values())

    def _catalog_tools(self) -> tuple[list[tuple[bool, dict[str, Any]]], bool]:
        tools: list[tuple[bool, dict[str, Any]]] = []
        complete = True
        try:
            body = self._http.get(
                url=f"{self._require_control_url()}/mcp/tools", is_cloud=True
            )
        except UpstreamError:
            complete = False
        else:
            tools.extend(
                (True, tool)
                for tool in body.get("tools") or []
                if isinstance(tool, dict)
            )
        tools.extend((False, tool) for tool in self._local_tool_catalog())
        return tools, complete

    def _call_tool(self, *, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        target = route_target(
            name=name, arguments=arguments, route=self._tool_meta(name=name)
        )
        if target == "project-current":
            return self._current_project()
        if target == "project-connect":
            return self._connect_project(arguments)
        if target == "project-overview":
            project_id = self._resolve_project_id()
            return (
                self._call_cloud_raw(
                    name=name, arguments={**arguments, "project_id": project_id}
                )
                if project_id
                else self._current_project()
            )
        if target == "control-raw":
            return self._call_cloud_raw(name=name, arguments=arguments)
        if target == "local":
            return self._call_local_data(name=name, arguments=arguments)
        if target == "enriched":
            return self._call_local_enriched_control(name=name, arguments=arguments)
        return self._call_cloud(name=name, arguments=arguments)

    def _call_cloud(self, *, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        args = self._call_arguments(arguments=arguments)
        if self._tool_meta(name=name).project_scoped:
            args["project_id"] = self._resolve_project_id_required()
        return self._call_cloud_raw(name=name, arguments=args)

    def _call_cloud_raw(
        self, *, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        body = self._http.post(
            url=f"{self._require_control_url()}/mcp/call",
            payload={"name": name, "arguments": arguments},
            is_cloud=True,
            timeout=self._timeout_for(name=name, arguments=arguments),
        )
        return self._result_dict(body=body)

    def _call_control_api(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._http.post(
            url=f"{self._require_control_url()}{path}",
            payload=payload,
            is_cloud=True,
            timeout=self.config.timeout_seconds,
        )

    def _call_local_data(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        control_facts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            kwargs: dict[str, Any] = {
                "name": name,
                "arguments": self._call_arguments(arguments=arguments),
            }
            if control_facts is not None:
                kwargs["control_facts"] = control_facts
            return self._local_executor().call_tool(**kwargs)
        except LocalDataPlaneError as exc:
            raise UpstreamError(
                exc.message,
                error_code=exc.error_code,
                details=exc.details,
            ) from exc
        except Exception as exc:
            details = getattr(exc, "details", {})
            raise UpstreamError(
                str(exc),
                error_code=str(getattr(exc, "error_code", "") or "validation_error"),
                details=details if isinstance(details, dict) else {},
            ) from exc

    def _call_local_enriched_control(
        self, *, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if self._tool_meta(name=name).local_handler_identity == "local.health":
            return self._enriched_health()
        cloud, cloud_error = self._best_effort(self._call_cloud, name, arguments)
        # Reuse a successful read. After a failed read, retain the legacy retry so
        # degraded responses report both control- and data-plane failures exactly.
        local_kwargs = {"control_facts": cloud} if cloud_error is None else {}
        local, local_error = self._best_effort(
            self._call_local_data, name, arguments, **local_kwargs
        )
        return merge_enriched_control(
            cloud=cloud,
            local=local,
            cloud_error=cloud_error,
            local_error=local_error,
        )

    @staticmethod
    def _best_effort(call: Any, name: str, arguments: dict[str, Any], **kwargs: Any):
        try:
            return call(name=name, arguments=arguments, **kwargs), None
        except UpstreamError as exc:
            return {}, {"error": exc.message, "error_code": exc.error_code}

    def _enriched_health(self) -> dict[str, Any]:
        cloud_ok, cloud_detail = self._probe(is_cloud=True)
        return {
            "ok": bool(cloud_ok),
            "data_plane": {"reachable": True, "mode": "proxy"},
            "control_plane": {
                "reachable": cloud_ok,
                "configured": bool(self.config.control_url),
                **cloud_detail,
            },
        }

    def _probe(self, *, is_cloud: bool) -> tuple[bool, dict[str, Any]]:
        if not self.config.control_url:
            return False, {"error_code": "not_configured"}
        try:
            self._http.get(url=f"{self.config.control_url}/health", is_cloud=is_cloud)
            return True, {}
        except UpstreamError as exc:
            return False, {"error": exc.message, "error_code": exc.error_code}

    def _current_project(self) -> dict[str, Any]:
        return self._project_scope.current(cloud_call=self._call_cloud)

    def _resolve_project_id(self) -> str | None:
        return self._project_scope.resolve()

    def _resolve_project_id_required(self) -> str:
        return self._resolve_project_id() or self._project_scope.require()

    def _connect_project(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._project_scope.connect(
            arguments=arguments, cloud_call_raw=self._call_cloud_raw
        )

    def _tool_meta(self, *, name: str) -> ToolRoute:
        if self._tool_cache is None:
            self._tool_cache = {
                tool["name"]: route_from_manifest(tool) for tool in shipped_manifest()
            }
        if name in self._tool_cache:
            return self._tool_cache[name]
        catalog, _complete = self._catalog_tools()
        return next(
            (
                route_from_manifest(tool)
                for _cloud, tool in catalog
                if tool.get("name") == name
            ),
            ToolRoute(),
        )

    def _local_tool_catalog(self) -> list[dict[str, Any]]:
        storage_enabled = storage_feature_enabled()
        return [
            public_catalog_tool(tool)
            for tool in shipped_manifest()
            if (
                tool.get("plane") == "data"
                or tool.get("executionStrategy") == "control-plus-local-enrichment"
            )
            and (
                storage_enabled or "storage" not in tool.get("featureRequirements", ())
            )
        ]

    def _local_executor(self) -> LocalDataPlane:
        if self._local_data_plane is None:
            self._local_data_plane = LocalDataPlane(
                repo_root=self.config.repo_root,
                project_id_resolver=self._resolve_project_id,
                control_api_post=self._call_control_api,
                control_tool_call=lambda tool, args: self._call_cloud(
                    name=tool, arguments=args
                ),
            )
        return self._local_data_plane

    def _links(self) -> Any:
        return self._project_scope.links

    @staticmethod
    def _call_arguments(*, arguments: dict[str, Any]) -> dict[str, Any]:
        args = dict(arguments)
        args.pop("project_id", None)
        return args

    @staticmethod
    def _result_dict(*, body: dict[str, Any]) -> dict[str, Any]:
        result = body.get("result")
        if not isinstance(result, dict):
            raise UpstreamError(
                "upstream returned an invalid /mcp/call payload",
                error_code="daemon_bad_response",
                details={"payload": body},
            )
        return result

    def _timeout_for(
        self,
        *,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> float:
        if name == "sandbox.runs":
            try:
                wait = float((arguments or {}).get("wait_seconds") or 0)
            except (TypeError, ValueError):
                wait = 0.0
            return max(self.config.timeout_seconds, wait + 30.0)
        return (
            LONG_VERB_TIMEOUT_SECONDS
            if name in LONG_VERBS
            else self.config.timeout_seconds
        )

    def _require_control_url(self) -> str:
        url = (self.config.control_url or "").strip().rstrip("/")
        if not url:
            raise UpstreamError(
                "control_url is required; set MERV_CONTROL_URL to "
                f"the hosted brain ({HOSTED_CONTROL_URL}) or to "
                f"{LOCAL_BRAIN_URL} for a local brain",
                error_code="cloud_unreachable",
            )
        return url

    def _with_hidden_project_scope(
        self,
        *,
        tool: dict[str, Any],
    ) -> dict[str, Any]:
        scoped = deepcopy(tool)
        scoped.pop("plane", None)
        name = str(scoped.get("name") or "")
        if self._tool_meta(name=name).scope_strategy == "caller-selected":
            return scoped
        schema = scoped.get("inputSchema")
        if not isinstance(schema, dict):
            return scoped
        properties = schema.get("properties")
        if isinstance(properties, dict):
            properties.pop("project_id", None)
        required = schema.get("required")
        if isinstance(required, list):
            schema["required"] = [field for field in required if field != "project_id"]
        return scoped
