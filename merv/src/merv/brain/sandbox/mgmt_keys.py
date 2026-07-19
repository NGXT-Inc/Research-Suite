"""Local management key custody adapter.

The service layer consumes the neutral ``MgmtKeyStore`` port; this module is
the local filesystem implementation used by local-mode composition. Keys live
under ``mgmt_keys/<sandbox_uid>/`` in the local brain state root (de-nested
``~/.merv/brain`` fresh; a legacy nested ``.research_plugin/`` layout keeps
its paths — see ``composition/brain_dirs``). A cloud
control plane can provide a different implementation behind the same port
(see ``managed_mgmt_keys``). Custody adapters are sandbox machinery, so they
live in the sandbox module beside the backend port's other collaborators.
"""

from __future__ import annotations

from pathlib import Path

from .sandbox_support import _safe_name
from .ssh_keys import ensure_ed25519_keypair


class LocalMgmtKeyStore:
    """Management keys on the control plane's local disk, keyed by sandbox_uid."""

    def __init__(self, *, root: Path) -> None:
        self.root = root

    def key_path(self, *, sandbox_uid: str) -> Path:
        return self.root / _safe_name(sandbox_uid) / "key"

    def ensure(self, *, sandbox_uid: str) -> str:
        return ensure_ed25519_keypair(
            key_path=self.key_path(sandbox_uid=sandbox_uid),
            comment=f"research-plugin-mgmt-{sandbox_uid}",
            missing_action="mint the sandbox management key",
            failure_subject="sandbox management key",
        )

    def remove(self, *, sandbox_uid: str) -> None:
        key_path = self.key_path(sandbox_uid=sandbox_uid)
        for path in (key_path, key_path.with_suffix(".pub")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            key_path.parent.rmdir()
        except OSError:
            pass
