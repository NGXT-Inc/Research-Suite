"""Managed-secret management key custody adapter.

Control deployments should receive management SSH keys from their orchestrator
or secret manager. This adapter uses a mounted private key and configured public
key, and never generates or deletes key material itself.
"""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

from ..utils import ValidationError


class MountedMgmtKeyStore:
    """Management key custody backed by an externally managed mounted secret."""

    def __init__(self, *, private_key_path: Path, public_key: str | None = None) -> None:
        self._private_key_path = private_key_path.expanduser()
        self._private_key_digest = self._read_private_key_digest()
        self._public_key = self._normalize_public_key(
            public_key if public_key is not None else self._read_adjacent_public_key()
        )

    def key_path(self, *, sandbox_uid: str) -> Path:
        del sandbox_uid  # one mounted key serves every sandbox
        self._assert_private_key_unchanged()
        return self._private_key_path

    def ensure(self, *, sandbox_uid: str) -> str:
        del sandbox_uid  # one mounted key serves every sandbox
        self._assert_private_key_unchanged()
        return self._public_key

    def remove(self, *, sandbox_uid: str) -> None:
        del sandbox_uid
        # The orchestrator/secret manager owns this key's lifecycle.
        return None

    def _read_adjacent_public_key(self) -> str:
        public_path = Path(f"{self._private_key_path}.pub")
        try:
            return public_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValidationError(
                "configured management private key needs either "
                "RESEARCH_PLUGIN_MGMT_PUBLIC_KEY or an adjacent .pub file",
                details={"public_key_path": str(public_path)},
            ) from exc

    def _read_private_key_digest(self) -> str:
        try:
            key_stat = self._private_key_path.stat()
        except OSError as exc:
            raise ValidationError(
                "configured management private key does not exist",
                details={"path": str(self._private_key_path)},
            ) from exc
        mode = stat.S_IMODE(key_stat.st_mode)
        if mode & 0o077:
            raise ValidationError(
                "configured management private key permissions are too open "
                "(expected 0600 or stricter)",
                details={"path": str(self._private_key_path), "mode": oct(mode)},
            )
        try:
            data = self._private_key_path.read_bytes()
        except OSError as exc:
            raise ValidationError(
                "configured management private key is not readable",
                details={"path": str(self._private_key_path)},
            ) from exc
        if b"PRIVATE KEY" not in data:
            raise ValidationError(
                "configured management private key does not look like a private key",
                details={"path": str(self._private_key_path)},
            )
        return hashlib.sha256(data).hexdigest()

    def _assert_private_key_unchanged(self) -> None:
        if self._read_private_key_digest() != self._private_key_digest:
            raise ValidationError(
                "configured management private key changed after control startup; "
                "drain live sandboxes or restart control before rotating it",
                details={"path": str(self._private_key_path)},
            )

    @staticmethod
    def _normalize_public_key(public_key: str) -> str:
        value = public_key.strip()
        if not value:
            raise ValidationError("configured management public key is empty")
        if not value.startswith(("ssh-", "ecdsa-", "sk-ssh-")):
            raise ValidationError(
                "configured management public key does not look like an OpenSSH key"
            )
        return value
