"""Pure markdown artifact lint for experiment plan and report resources."""

from __future__ import annotations

import re
from collections.abc import Callable

from ...artifacts.markdown_images import markdown_image_links


# --- Plan schema (PRD-style) -------------------------------------------------
# plan.md is the face of the experiment in the UI and the artifact the design
# reviewer evaluates. We enforce a small REQUIRED spine — the minimum that makes
# a plan readable (Summary), motivated (Objective & hypothesis), and judgeable
# (Evaluation) — and leave Method/Outputs/Risks to the design reviewer's
# judgment. See skills/research-workflow/plan-template.md.
#
# Each entry is (canonical_name, match_key): a plan heading satisfies the
# section when its normalized text starts with match_key. The lint is
# deliberately dumb (heading present + non-empty body); whether the content is
# *sufficient* is the design reviewer's call, not the linter's.
REQUIRED_PLAN_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Summary", "summary"),
    ("Objective & hypothesis", "objective"),
    ("Evaluation", "evaluation"),
)

# --- Results report schema ---------------------------------------------------
# report.md is the face of the *executed* experiment: the artifact the
# experiment reviewer grades and the UI spotlights once results exist. Same
# philosophy as the plan spine — the lint enforces shape (sections present,
# short, figures resolve, the system metrics exhibit referenced when one is
# pinned), and the experiment reviewer judges substance. Metric numbers are
# NOT linted: the system-authored metrics exhibit is the record, and the
# report interprets it. See skills/research-workflow/report-template.md.
REQUIRED_REPORT_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Summary", "summary"),
    ("Results", "results"),
    ("Deviations from plan", "deviations"),
    ("Conclusion", "conclusion"),
)

# Brevity is structural: the report is the executive layer; raw numbers, logs,
# and large tables belong in linked result resources. 16 KB gives enough room
# for curated metrics and figure captions without turning the report into a dump.
MAX_REPORT_BYTES = 16_000

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$", re.MULTILINE)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _normalize_heading(text: str) -> str:
    """Lowercase, expand '&' to 'and', collapse to space-separated words."""
    text = text.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _sections_missing(
    text: str, required: tuple[tuple[str, str], ...]
) -> list[str]:
    """Return the canonical names of REQUIRED sections that are absent or
    empty. A section counts as present when its heading exists and the body
    beneath it — up to the next same-or-higher-level heading — contains
    non-whitespace text. HTML comments are stripped first, so they neither count
    as content nor register as headings; template guidance therefore lives in
    comments precisely so an unfilled section reads as empty here."""
    text = _HTML_COMMENT_RE.sub("", text)
    headings = [
        (m.start(), len(m.group(1)), _normalize_heading(m.group(2)), m.end())
        for m in _HEADING_RE.finditer(text)
    ]
    missing: list[str] = []
    for canonical, key in required:
        idx = next((i for i, h in enumerate(headings) if h[2].startswith(key)), None)
        if idx is None:
            missing.append(canonical)
            continue
        level, body_start = headings[idx][1], headings[idx][3]
        body_end = len(text)
        for nxt_start, nxt_level, _, _ in headings[idx + 1:]:
            if nxt_level <= level:
                body_end = nxt_start
                break
        if not text[body_start:body_end].strip():
            missing.append(canonical)
    return missing


def plan_sections_missing(plan_text: str) -> list[str]:
    return _sections_missing(plan_text, REQUIRED_PLAN_SECTIONS)


def report_sections_missing(report_text: str) -> list[str]:
    return _sections_missing(report_text, REQUIRED_REPORT_SECTIONS)


def report_figure_links(report_text: str) -> list[str]:
    """Compatibility wrapper for callers/tests that still use report wording."""
    return markdown_image_links(report_text)


def report_problems(
    report_text: str,
    *,
    figure_problem: Callable[[str], str | None] | None = None,
    exhibit_path: str | None = None,
) -> list[str]:
    """Everything wrong with a results report, in one pass, so the agent can
    fix all of it in a single revision instead of peeling errors one by one.

    Checks: required spine sections; the brevity ceiling; via the
    ``figure_problem`` callback, that every relative image link has submitted
    figure content (a report whose figures weren't submitted renders broken in
    the UI and is unreviewable); and — when ``exhibit_path`` names a pinned
    system metrics exhibit — that the report references it. The server does
    not police the shape of agent-written numbers: the exhibit is the record,
    and the report is graded on how it answers around it."""
    problems: list[str] = []
    missing = report_sections_missing(report_text)
    if missing:
        problems.append("missing required sections: " + ", ".join(missing))
    if exhibit_path:
        # Strip HTML comments first so template guidance naming the exhibit
        # does not satisfy the reference check.
        basename = exhibit_path.rsplit("/", 1)[-1]
        if basename not in _HTML_COMMENT_RE.sub("", report_text):
            problems.append(
                f"the report must reference the system metrics exhibit "
                f"({exhibit_path}): it is the authoritative record of this "
                "attempt's runs and result files — write the Results section "
                "around it and cite it by name"
            )
    size = len(report_text.encode("utf-8"))
    if size > MAX_REPORT_BYTES:
        problems.append(
            f"report is {size} bytes; keep it under {MAX_REPORT_BYTES} — move raw "
            "numbers and logs into result resources and link them instead"
        )
    if figure_problem is not None:
        for target in report_figure_links(report_text):
            problem = figure_problem(target)
            if problem:
                problems.append(problem)
    return problems
