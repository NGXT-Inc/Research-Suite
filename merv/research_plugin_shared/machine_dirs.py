"""Machine-level (per-user home) state-directory names and resolution.

The sibling of ``project_dirs`` for home-scoped client state — ``client.json``,
``project_links.sqlite``, ``daemon_secret``. A machine that already has
``~/.research_plugin/`` keeps using it forever; a machine without one (fresh
installs included) keeps its client state under ``~/.merv/``. Resolution is
per-call and never cached: the legacy dir wins from the moment it exists, so
all consumers converge on one answer within a process. Stdlib-only: the
zero-install stdio proxy imports this package.
"""

from __future__ import annotations

from pathlib import Path

MACHINE_STATE_DIR = ".merv"
LEGACY_MACHINE_STATE_DIR = ".research_plugin"


def resolve_machine_state_dir(home: Path | None = None) -> Path:
    """The machine's client-state dir: the legacy dir if present, else ``.merv``.

    ``is_dir()`` (not ``exists()``) so a stray ``.research_plugin`` file can
    never hijack resolution, mirroring ``resolve_project_state_dir``.
    """
    base = Path(home) if home is not None else Path.home()
    legacy = base / LEGACY_MACHINE_STATE_DIR
    if legacy.is_dir():
        return legacy
    return base / MACHINE_STATE_DIR
