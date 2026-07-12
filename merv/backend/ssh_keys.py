"""Local OpenSSH keypair helpers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .utils import ValidationError


def ensure_ed25519_keypair(
    *,
    key_path: Path,
    comment: str,
    missing_action: str,
    failure_subject: str,
) -> str:
    """Ensure an ed25519 keypair exists and return the public key."""
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
                comment,
                "-f",
                str(key_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ValidationError(
            f"ssh-keygen is required to {missing_action} but was not found"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise ValidationError(
            f"failed to generate {failure_subject}: {exc.stderr or exc.stdout or exc}"
        ) from exc
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return pub_path.read_text().strip()
