"""Pure execution policy: command, path, and environment validation.

No SQLite, no event logging, no app-level errors.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, Mapping

from .errors import BackendPermissionError, BackendValidationError
from .types import JobSpec


SENSITIVE_ENV_MARKERS: frozenset[str] = frozenset(
    {"SECRET", "TOKEN", "PASSWORD", "PRIVATE", "CREDENTIAL", "KEY"}
)
SAFE_ENV_NAMES: frozenset[str] = frozenset({"TOKENIZERS_PARALLELISM"})

ALLOWED_EXECUTABLES: frozenset[str] = frozenset({"python", "python3", "pytest", "uv"})

FORBIDDEN_SHELL_TOKENS: tuple[str, ...] = (";", "&&", "||", "|", "`", "$(", ">", "<")


class JobExecutionPolicy:
    """Validates job specs before a backend sees them."""

    def __init__(self, *, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()

    def validate(
        self,
        *,
        command: str,
        cwd: str,
        expected_outputs: list[str] | None,
        env: dict[str, str] | None,
        backend_hints: Mapping[str, Any] | None,
        metadata: Mapping[str, str] | None = None,
    ) -> JobSpec:
        expected_outputs = list(expected_outputs or [])
        env = dict(env or {})
        backend_hints = dict(backend_hints or {})
        metadata = dict(metadata or {})

        self._validate_command(command)
        rel_cwd = self._validate_relative_dir(cwd)
        normalised_outputs = tuple(self._validate_relative_path(p) for p in expected_outputs)
        self._validate_env(env)

        return JobSpec(
            command=command,
            cwd=rel_cwd,
            env=env,
            expected_outputs=normalised_outputs,
            backend_hints=backend_hints,
            metadata=metadata,
        )

    def _validate_command(self, command: str) -> None:
        if not command or not command.strip():
            raise BackendValidationError("command is required")
        if any(token in command for token in FORBIDDEN_SHELL_TOKENS):
            raise BackendPermissionError(
                "job command contains shell control syntax that is not allowed"
            )
        parts = shlex.split(command)
        if not parts:
            raise BackendValidationError("command is required")
        executable = Path(parts[0]).name
        if executable not in ALLOWED_EXECUTABLES:
            raise BackendPermissionError(f"job command executable is not allowed: {executable}")

    def _validate_relative_dir(self, path: str) -> str:
        rel = self._validate_relative_path(path)
        full = self.repo_root / rel
        if not full.exists():
            raise BackendValidationError(f"cwd does not exist: {rel}")
        if not full.is_dir():
            raise BackendValidationError(f"cwd is not a directory: {rel}")
        return rel

    def _validate_relative_path(self, path: str) -> str:
        if not path:
            raise BackendValidationError("path is required")
        rel = Path(path)
        if rel.is_absolute() or any(part == ".." for part in rel.parts):
            raise BackendValidationError("paths must be repo-relative and may not contain '..'")
        full = (self.repo_root / rel).resolve()
        try:
            full.relative_to(self.repo_root)
        except ValueError as exc:
            raise BackendValidationError("path escapes repo root") from exc
        return rel.as_posix()

    def _validate_env(self, env: dict[str, str]) -> None:
        for key in env:
            upper = key.upper()
            if upper in SAFE_ENV_NAMES:
                continue
            parts = [part for part in re.split(r"[^A-Z0-9]+", upper) if part]
            if any(marker in parts for marker in SENSITIVE_ENV_MARKERS):
                raise BackendPermissionError(
                    f"job env var appears sensitive and is not allowed: {key}"
                )
