"""Tool-call dispatch, contract validation, and telemetry.

The dispatcher is intentionally independent of local workspace and data-plane
construction. Composition roots provide handlers and telemetry sinks; this
module owns the external tool contract machinery shared by local and control
apps.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError as PydanticValidationError

from .contracts import ContractModel, TOOL_CONTRACTS, static_tool_catalog
from ..state.activity import monotonic_ms
from ..utils import ResearchPluginError
from ..utils import ValidationError as ToolValidationError


@dataclass(frozen=True)
class ToolSpec:
    input_model: type[ContractModel]
    handler: Callable[..., dict[str, Any]]

    def call(
        self,
        *,
        raw_arguments: dict[str, Any],
        internal_kwargs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = self.input_model.model_validate(raw_arguments)
        kwargs = request.model_dump()
        if internal_kwargs:
            kwargs.update(internal_kwargs)
        return self.handler(**kwargs)


class ToolPermissionPolicy(Protocol):
    """Permission surface used by the tool dispatcher."""

    def reject_reviewer_mutation(
        self, *, tool_name: str, review_session_id: str | None
    ) -> None:
        ...


def _contract_error_message(*, exc: PydanticValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(part) for part in first.get("loc", ())) or "input"
    error_type = first.get("type")
    if error_type == "missing":
        return f"{loc} is required"
    if error_type == "extra_forbidden":
        return f"unexpected field: {loc}"
    return f"{loc}: {first.get('msg', 'invalid value')}"


def _assert_tool_contracts_match_handlers(
    *,
    handlers: dict[str, Callable[..., dict[str, Any]]],
    tool_names: set[str],
) -> None:
    handler_names = set(handlers)
    unknown_tools = sorted(tool_names - set(TOOL_CONTRACTS))
    if unknown_tools:
        raise AssertionError(f"unknown tool contracts: {', '.join(unknown_tools)}")
    if handler_names == tool_names:
        return
    missing_handlers = sorted(tool_names - handler_names)
    missing_contracts = sorted(handler_names - tool_names)
    raise AssertionError(
        "tool handler/contract mismatch"
        f"; missing handlers: {', '.join(missing_handlers) or 'none'}"
        f"; missing contracts: {', '.join(missing_contracts) or 'none'}"
    )


class ToolDispatcher:
    """Contract-checked tool dispatcher with activity/tool-call telemetry."""

    def __init__(
        self,
        *,
        handlers: dict[str, Callable[..., dict[str, Any]]],
        permissions: ToolPermissionPolicy,
        activity: Any,
        tool_calls: Any,
        tool_names: Iterable[str] | None = None,
    ) -> None:
        selected_tool_names = (
            set(TOOL_CONTRACTS) if tool_names is None else set(tool_names)
        )
        _assert_tool_contracts_match_handlers(
            handlers=handlers,
            tool_names=selected_tool_names,
        )
        self.permissions = permissions
        self.activity = activity
        self.tool_calls = tool_calls
        self._tool_names = frozenset(selected_tool_names)
        self._tools = {
            name: ToolSpec(contract.input_model, handlers[name])
            for name, contract in TOOL_CONTRACTS.items()
            if name in self._tool_names
        }

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            tool
            for tool in static_tool_catalog(tool_names=set(self._tool_names))
            if tool.get("name") in self._tool_names
        ]

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        activity_source: str = "app",
        internal_kwargs: dict[str, Any] | None = None,
        telemetry_project_id: str | None = None,
    ) -> dict[str, Any]:
        arguments = arguments or {}
        telemetry_arguments = arguments
        if telemetry_project_id:
            telemetry_arguments = {
                **arguments,
                "project_id": telemetry_project_id,
            }
        started = monotonic_ms()
        try:
            if name not in self._tools:
                raise ResearchPluginError(f"unknown tool: {name}", details={"tool": name})
            self.permissions.reject_reviewer_mutation(
                tool_name=name,
                review_session_id=arguments.get("review_session_id"),
            )
            try:
                result = self._tools[name].call(
                    raw_arguments=arguments,
                    internal_kwargs=internal_kwargs,
                )
            except PydanticValidationError as exc:
                raise ToolValidationError(
                    _contract_error_message(exc=exc),
                    details={"tool": name, "errors": exc.errors()},
                ) from exc
            duration_ms = monotonic_ms() - started
            self.activity.tool_ok(
                source=activity_source,
                tool=name,
                arguments=telemetry_arguments,
                duration_ms=duration_ms,
                result=result,
            )
            self.tool_calls.record(
                tool=name,
                source=activity_source,
                status="ok",
                duration_ms=duration_ms,
                arguments=telemetry_arguments,
                result=result,
            )
            return result
        except ResearchPluginError as exc:
            duration_ms = monotonic_ms() - started
            self.activity.tool_error(
                source=activity_source,
                tool=name,
                arguments=telemetry_arguments,
                duration_ms=duration_ms,
                error=exc.message,
                error_code=exc.error_code,
            )
            self.tool_calls.record(
                tool=name,
                source=activity_source,
                status="error",
                duration_ms=duration_ms,
                arguments=telemetry_arguments,
                error=exc.message,
                error_code=exc.error_code,
            )
            raise
        except Exception as exc:
            duration_ms = monotonic_ms() - started
            self.activity.tool_error(
                source=activity_source,
                tool=name,
                arguments=telemetry_arguments,
                duration_ms=duration_ms,
                error=str(exc),
                error_code="unexpected",
            )
            self.tool_calls.record(
                tool=name,
                source=activity_source,
                status="error",
                duration_ms=duration_ms,
                arguments=telemetry_arguments,
                error=str(exc),
                error_code="unexpected",
            )
            raise
