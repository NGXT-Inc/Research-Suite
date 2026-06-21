"""Local management key custody adapter.

The service layer consumes the neutral ``MgmtKeyStore`` port; this module is
the local filesystem implementation used by local-mode composition. Keys live
under ``.research_plugin/mgmt_keys/<experiment_id>/`` in local mode. A cloud
control plane can provide a different implementation behind the same port.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..sandbox_support import _safe_name
from ..utils import ValidationError


class LocalMgmtKeyStore:
    """Management keys on the control plane's local disk."""

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
        for path in (key_path, pub_path):
            if path.exists():
                path.unlink()
        try:
            subprocess.run(
                [
                    "ssh-keygen",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-q",
                    "-C",
                    f"research-plugin-mgmt-{experiment_id}",
                    "-f",
                    str(key_path),
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
                "failed to generate sandbox management key: "
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
