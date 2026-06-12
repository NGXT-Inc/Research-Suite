"""Provider-neutral remote directory contract for SSH sandboxes.

The remote layout has exactly one synced location: the experiment's own
folder, ``<root>/<name>`` (``$RP_EXPERIMENT_DIR`` inside the VM). It
is pushed wholesale at provisioning and mirrored back to the local repo while
the sandbox lives. Everything else on the VM stays on the VM — there is no
"unsynced" directory concept, just "outside the experiment folder".

Runtime telemetry (MLflow/TensorBoard state, command transcripts) is authored
by the sandbox itself and lives OUTSIDE the experiment folder, under
``<root>/.research_plugin_sessions/<experiment_id>``; a dedicated pull lands
it in a daemon-owned local dir so it never pollutes the experiment's mirror.
"""

from __future__ import annotations

import posixpath

from ..workspace import safe_experiment_dirname


DEFAULT_REMOTE_ROOT = "/workspace"
# Conventional VM-local home for datasets, caches, and other heavy files the
# agent does not want in the repo (``$RP_DATASET_DIR``). Never synced.
DEFAULT_DATA_DIR = "/workspace/data"
SESSIONS_DIRNAME = ".research_plugin_sessions"
ARTIFACTS_TO_KEEP_DIRNAME = "artifacts_to_keep"


def remote_experiment_dir(
    *, experiment_id: str, name: str = "", root: str = DEFAULT_REMOTE_ROOT
) -> str:
    """The one synced folder on the VM for this experiment.

    Named after the experiment's short unique name so the remote layout
    matches ``experiments/<name>/`` locally; rows that predate the name
    requirement fall back to the experiment id.
    """
    return posixpath.join(
        root.rstrip("/") or "/", safe_experiment_dirname(name.strip() or experiment_id)
    )


def remote_sessions_dir(*, experiment_id: str, root: str = DEFAULT_REMOTE_ROOT) -> str:
    """Where the VM writes its own telemetry — outside the experiment folder."""
    return posixpath.join(
        root.rstrip("/") or "/", SESSIONS_DIRNAME, safe_experiment_dirname(experiment_id)
    )


def remote_root_of(experiment_dir: str) -> str:
    """Recover the remote root from a stored per-experiment dir."""
    return posixpath.dirname(experiment_dir.rstrip("/")) or DEFAULT_REMOTE_ROOT


def sync_hint() -> str:
    return (
        "Work inside the experiment folder ($RP_EXPERIMENT_DIR): it is the "
        "only directory that syncs back to the local repo. Keep datasets, "
        "caches, checkpoints, and anything else you do not want carried into "
        "the repo OUTSIDE the folder (e.g. $RP_DATASET_DIR) — nothing outside "
        "it is ever synced. Put deliberate large final artifacts in "
        "$RP_EXPERIMENT_DIR/artifacts_to_keep, which syncs with a 5 GB "
        "per-file cap instead of the usual 100 MB."
    )
