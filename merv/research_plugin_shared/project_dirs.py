"""Per-project checkout state-directory names and resolution.

A checkout that already contains ``.research_plugin/`` keeps using it
forever; a checkout without one (fresh clones included) keeps its state
(activity log, sandbox keys, pulled session telemetry) under ``.merv/``.
This module is the single owner of both names — every path builder and
exclusion filter derives from it. Resolution is per-call and never cached:
the legacy dir wins from the moment it exists, so all consumers converge on
one answer within a process; do not cache resolved paths across calls.
Stdlib-only: the zero-install stdio proxy imports this package.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_STATE_DIR = ".merv"
LEGACY_PROJECT_STATE_DIR = ".research_plugin"
PROJECT_STATE_DIR_NAMES = (PROJECT_STATE_DIR, LEGACY_PROJECT_STATE_DIR)


def resolve_project_state_dir(repo_root: Path) -> Path:
    """The checkout's state dir: the legacy dir if present, else ``.merv``.

    ``is_dir()`` (not ``exists()``) so a stray ``.research_plugin`` file can
    never hijack resolution; when both dirs exist the legacy one wins so
    pre-v0.0013 projects never silently split their state.
    """
    root = Path(repo_root)
    legacy = root / LEGACY_PROJECT_STATE_DIR
    if legacy.is_dir():
        return legacy
    return root / PROJECT_STATE_DIR


def ensure_project_state_dir(repo_root: Path) -> Path:
    """Resolve the state dir, create it, and make it self-ignoring.

    Drops a ``.gitignore`` containing ``*`` inside the dir when absent so
    checkout state — sandbox private keys above all — can never be staged,
    regardless of the repo's own ignore rules.
    """
    state = resolve_project_state_dir(repo_root)
    state.mkdir(parents=True, exist_ok=True)
    ignore = state / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    return state
