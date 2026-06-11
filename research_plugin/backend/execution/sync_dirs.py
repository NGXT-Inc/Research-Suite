"""Provider-neutral remote directory contract for SSH sandboxes."""

from __future__ import annotations


DEFAULT_SYNC_DIR = "/workspace/synced"
DEFAULT_UNSYNCED_DIR = "/workspace/unsynced"
ARTIFACTS_TO_KEEP_DIRNAME = "artifacts_to_keep"


def sync_hint() -> str:
    return (
        "Use /workspace/synced for code, logs, metrics, and small outputs that "
        "should be rsynced back to the local experiment folder. Use "
        "/workspace/unsynced for datasets, caches, temporary checkpoints, and "
        "large scratch files. Put deliberate large final artifacts under "
        "/workspace/synced/artifacts_to_keep."
    )
