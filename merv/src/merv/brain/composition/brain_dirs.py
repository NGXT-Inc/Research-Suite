"""Local brain state-directory resolution: ``.merv`` fresh, legacy-wins.

The brain-side sibling of ``src/merv/shared/project_dirs.py``. Two
layers resolve per call and are never cached:

* the home staging root — ``~/.merv/brain`` unless the legacy brain state
  exists, in which case ``~/.research_plugin/brain`` wins forever;
* the layout inside a staging root — fresh roots hold ``state.sqlite`` /
  ``blobs/`` / ``mgmt_keys/`` directly (de-nested), while a root whose
  ``.research_plugin/state.sqlite`` exists keeps the pre-v0.0014 nested
  layout verbatim.

``state.sqlite`` is the sentinel on both layers: any brain that ever booted
created it, so its presence marks real legacy state and a stray empty
``.research_plugin`` dir cannot hijack resolution. No migration ever moves
files between layouts.
"""

from __future__ import annotations

from pathlib import Path

from merv.shared.machine_dirs import (
    LEGACY_MACHINE_STATE_DIR,
    MACHINE_STATE_DIR,
)

BRAIN_DIRNAME = "brain"
BRAIN_STATE_SENTINEL = "state.sqlite"
LEGACY_BRAIN_SUBDIR = LEGACY_MACHINE_STATE_DIR


def resolve_brain_state_root(staging: Path) -> Path:
    """Where a staging root keeps ``state.sqlite``/``blobs``/``mgmt_keys``.

    The legacy nested ``<staging>/.research_plugin/`` wins when its state file
    exists; every other root (fresh installs included) is de-nested.
    """
    root = Path(staging)
    legacy = root / LEGACY_BRAIN_SUBDIR
    if (legacy / BRAIN_STATE_SENTINEL).is_file():
        return legacy
    return root


def resolve_local_brain_staging(home: Path | None = None) -> Path:
    """The default local brain staging root under the user's home.

    Deliberately independent of ``resolve_machine_state_dir``: a machine may
    keep legacy *client* state while having never run a local brain, and such
    a machine gets a fresh ``~/.merv/brain``. Only real legacy brain state
    (its ``state.sqlite``) pins the legacy root.
    """
    base = Path(home) if home is not None else Path.home()
    legacy = base / LEGACY_MACHINE_STATE_DIR / BRAIN_DIRNAME
    if (legacy / LEGACY_BRAIN_SUBDIR / BRAIN_STATE_SENTINEL).is_file():
        return legacy
    return base / MACHINE_STATE_DIR / BRAIN_DIRNAME
