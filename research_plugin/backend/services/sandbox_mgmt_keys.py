"""Per-sandbox management SSH keypairs (cloud plan Phase 5, fixed decision 4).

The control plane mints one ed25519 keypair per sandbox at provision time and
authorizes its public key at bootstrap alongside the user key. Transcript
reads, metrics sampling, reaping, and the expiry parachute ride the management
key, so none of them ever depend on the user's machine; the user key stays
data-plane-only (rsync, the sbx dispatcher, dashboard tunnels) and never
leaves it.

``LocalMgmtKeyStore`` is the local-mode implementation: keys live under
``.research_plugin/mgmt_keys/<experiment_id>/`` — control-plane property that
merely shares the machine with the data plane while both planes run in one
process (the cloud control plane keeps them in its own secret store from
Phase 8, behind this same protocol). Private key material never appears in
rows, events, or logs: the ``sandboxes.mgmt_key_ref`` column records only the
store reference (the experiment id) so control knows a key exists.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..utils import ValidationError
from ..sandbox_support import _safe_name


class LocalMgmtKeyStore:
    """Management keys on the control plane's local disk (local mode)."""

    def __init__(self, *, root: Path) -> None:
        self.root = root

    def key_path(self, *, experiment_id: str) -> Path:
        return self.root / _safe_name(experiment_id) / "key"

    def ensure(self, *, experiment_id: str) -> str:
        key_path = self.key_path(experiment_id=experiment_id)
        pub_path = key_path.with_suffix(".pub")
        if key_path.exists() and pub_path.exists():
            return pub_path.read_text().strip()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        # Remove a half-written pair before regenerating (mirrors the
        # data-plane user-key path).
        for path in (key_path, pub_path):
            if path.exists():
                path.unlink()
        try:
            subprocess.run(
                [
                    "ssh-keygen", "-t", "ed25519", "-N", "", "-q",
                    "-C", f"research-plugin-mgmt-{experiment_id}",
                    "-f", str(key_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise ValidationError(
                "ssh-keygen is required to mint the sandbox management key "
                "but was not found"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise ValidationError(
                f"failed to generate sandbox management key: "
                f"{exc.stderr or exc.stdout or exc}"
            ) from exc
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        return pub_path.read_text().strip()

    def remove(self, *, experiment_id: str) -> None:
        key_path = self.key_path(experiment_id=experiment_id)
        for path in (key_path, key_path.with_suffix(".pub")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            key_path.parent.rmdir()
        except OSError:
            pass
