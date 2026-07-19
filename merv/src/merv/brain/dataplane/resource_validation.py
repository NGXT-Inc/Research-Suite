"""Local preflight lint for repo-file resources."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..research_core.domain.artifacts import plan_sections_missing, report_problems
from ..research_core.domain.graph_lint import graph_problems
from ..artifacts.markdown_images import (
    MARKDOWN_FIGURE_ROLES,
    markdown_image_links,
)
from ..artifacts.roles import (
    GATED_ROLE_BYTE_CAPS,
    LEGACY_REFLECTION_DOC_ROLE,
    PROJECT_GRAPH_ROLE,
    REFLECTION_LENS_DOC_ROLES,
    RESOURCE_ROLES,
)
from ..research_core.domain.reflection_artifacts import (
    change_spec_structure_problems,
    reflection_doc_problems,
    reflection_lens_doc_problems,
)
from ..kernel.utils import NotFoundError, ValidationError
from .repo_paths import resolve_repo_path
from .resource_artifacts import (
    figure_link_problem,
    reject_absolute_markdown_image_targets,
)


def validate_local_resource_artifact(
    *, repo_root: Path, path: str, role: str
) -> dict[str, Any]:
    """Lint the current local file before register/associate mutates state."""
    repo_root = Path(repo_root).resolve()
    role = str(role or "").strip()
    problems: list[str] = []
    if role not in RESOURCE_ROLES:
        problems.append(f"unknown resource role: {role}")

    rel_path = str(path or "")
    size_bytes = 0
    try:
        rel_path, file_path = resolve_repo_path(
            repo_root=repo_root, path=path, subject="resource path"
        )
        if not file_path.exists():
            raise NotFoundError(f"resource file does not exist: {path}")
        if not file_path.is_file():
            raise ValidationError("v0.0001 resources must be files")
        # Stat before reading: non-gated roles (results, models) are routinely
        # huge and only need the size, matching read_for_association.
        size_bytes = file_path.stat().st_size
    except (OSError, NotFoundError, ValidationError) as exc:
        return _result(
            path=rel_path,
            role=role,
            size_bytes=size_bytes,
            max_bytes=GATED_ROLE_BYTE_CAPS.get(role),
            problems=[*problems, str(exc)],
        )

    max_bytes = GATED_ROLE_BYTE_CAPS.get(role)
    if max_bytes is None:
        return _result(
            path=rel_path,
            role=role,
            size_bytes=size_bytes,
            max_bytes=max_bytes,
            problems=problems,
        )
    if size_bytes > max_bytes:
        # Over-cap parity with the gate: association refuses on size before
        # reading content, so content lints are noise here too.
        problems.append(
            f"{rel_path} is {size_bytes} bytes; the maximum for a role-{role!r} "
            f"artifact is {max_bytes} bytes"
        )
        return _result(
            path=rel_path,
            role=role,
            size_bytes=size_bytes,
            max_bytes=max_bytes,
            problems=problems,
        )

    text = file_path.read_bytes().decode("utf-8", errors="replace")
    if role in MARKDOWN_FIGURE_ROLES:
        try:
            reject_absolute_markdown_image_targets(
                markdown_rel_path=rel_path, markdown_text=text
            )
        except ValidationError as exc:
            problems.append(str(exc))

    if role == "plan":
        missing = plan_sections_missing(text)
        if missing:
            problems.append("missing required sections: " + ", ".join(missing))
        for link in markdown_image_links(text):
            problem = figure_link_problem(
                repo_root=repo_root, markdown_rel_path=rel_path, link=link
            )
            if problem:
                problems.append(problem)
    elif role == "report":
        # exhibit_path is deliberately absent here: at associate time no
        # exhibit exists yet — only the submit_results gate supplies it.
        problems.extend(
            report_problems(
                text,
                figure_problem=lambda link: figure_link_problem(
                    repo_root=repo_root, markdown_rel_path=rel_path, link=link
                ),
            )
        )
    elif role in {"graph", PROJECT_GRAPH_ROLE}:
        problems.extend(graph_problems(text))
    elif role in {"reflection_doc", LEGACY_REFLECTION_DOC_ROLE}:
        problems.extend(reflection_doc_problems(text))
        for link in markdown_image_links(text):
            problem = figure_link_problem(
                repo_root=repo_root, markdown_rel_path=rel_path, link=link
            )
            if problem:
                problems.append(problem)
    elif role in REFLECTION_LENS_DOC_ROLES:
        problems.extend(reflection_lens_doc_problems(text))
    elif role == "change_spec":
        # Structure only: claim/experiment existence and active-experiment
        # caps need canonical state and are checked at the transition gate.
        problems.extend(change_spec_structure_problems(text))

    return _result(
        path=rel_path,
        role=role,
        size_bytes=size_bytes,
        max_bytes=max_bytes,
        problems=problems,
    )


def _result(
    *,
    path: str,
    role: str,
    size_bytes: int,
    max_bytes: int | None,
    problems: list[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": not problems,
        "path": path,
        "role": role,
        "gated": max_bytes is not None,
        "size_bytes": size_bytes,
        "problems": problems,
    }
    if max_bytes is not None:
        result["max_bytes"] = max_bytes
    return result
