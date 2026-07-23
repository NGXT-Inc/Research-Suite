"""Project-scoped API-key lifecycle and verification policy.

One ``mk_`` key binds one project immutably; the presented secret is returned
once at mint and only its digest is stored. There is no local/cloud profile:
the key carries project + audience + (stored, unenforced) ceilings, nothing
else. ``verify_secret`` reads the database fresh on every call so a revoke is
effective immediately (INV-4).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Protocol

from ..kernel.secret_tokens import hash_secret, mint_secret, secret_digest_matches
from ..kernel.utils import NotFoundError, ValidationError, new_id, now_iso, parse_iso

PROJECT_KEY_PREFIX = "mk_"


@dataclass(frozen=True, slots=True)
class ProjectKeyRecord:
    id: str
    secret_digest: str
    owner_user_id: str
    tenant_id: str
    project_id: str
    audience: str | None
    oauth_family_id: str | None
    created_at: str
    expires_at: str | None
    revoked_at: str | None
    parent_key_id: str | None
    sandbox_seconds_ceiling: int | None
    blob_bytes_ceiling: int | None


class ProjectKeyRepository(Protocol):
    def project_tenant(self, *, project_id: str) -> str: ...
    def insert(self, *, record: ProjectKeyRecord) -> None: ...
    def rotate(self, *, record: ProjectKeyRecord, revoked_at: str) -> bool: ...
    def revoke_lineage(
        self, *, key_id: str, project_id: str, owner_user_id: str, revoked_at: str
    ) -> bool: ...
    def by_digest(self, *, digest: str) -> ProjectKeyRecord | None: ...
    def by_id(self, *, key_id: str) -> ProjectKeyRecord | None: ...
    def list_for_owner(
        self, *, project_id: str, owner_user_id: str
    ) -> list[ProjectKeyRecord]: ...
    def revoke(
        self, *, key_id: str, project_id: str, owner_user_id: str, revoked_at: str
    ) -> ProjectKeyRecord | None: ...


class ProjectKeyLookup(Protocol):
    def verify_secret(self, *, secret: str) -> ProjectKeyRecord | None: ...


class ProjectKeyControl(ProjectKeyLookup, Protocol):
    def create(self, **kwargs: object) -> dict[str, object]: ...
    def rotate(self, **kwargs: object) -> dict[str, object]: ...
    def revoke_lineage(self, **kwargs: object) -> dict[str, object]: ...
    def list(self, **kwargs: object) -> dict[str, object]: ...
    def revoke(self, **kwargs: object) -> dict[str, object]: ...


class ProjectKeys:
    """Public facade for mint/list/revoke plus uncached secret lookup."""

    def __init__(self, *, repository: ProjectKeyRepository) -> None:
        self._repository = repository

    def create(
        self,
        *,
        project_id: str,
        owner_user_id: str,
        expires_at: str | None = None,
        parent_key_id: str | None = None,
        sandbox_seconds_ceiling: int | None = None,
        blob_bytes_ceiling: int | None = None,
        audience: str | None = None,
        oauth_family_id: str | None = None,
    ) -> dict[str, object]:
        record, secret = self._new_record(
            project_id=project_id,
            owner_user_id=owner_user_id,
            expires_at=expires_at,
            parent_key_id=parent_key_id,
            sandbox_seconds_ceiling=sandbox_seconds_ceiling,
            blob_bytes_ceiling=blob_bytes_ceiling,
            audience=audience,
            oauth_family_id=oauth_family_id,
        )
        self._repository.insert(record=record)
        return {"key": _public_record(record), "secret": secret}

    def rotate(
        self,
        *,
        project_id: str,
        owner_user_id: str,
        parent_key_id: str,
        expires_at: str | None = None,
        sandbox_seconds_ceiling: int | None = None,
        blob_bytes_ceiling: int | None = None,
        audience: str | None = None,
        oauth_family_id: str | None = None,
    ) -> dict[str, object]:
        """Atomically revoke one active parent while inserting its child."""
        record, secret = self._new_record(
            project_id=project_id,
            owner_user_id=owner_user_id,
            expires_at=expires_at,
            parent_key_id=_required(parent_key_id, field="parent_key_id"),
            sandbox_seconds_ceiling=sandbox_seconds_ceiling,
            blob_bytes_ceiling=blob_bytes_ceiling,
            audience=audience,
            oauth_family_id=oauth_family_id,
        )
        if not self._repository.rotate(record=record, revoked_at=now_iso()):
            raise NotFoundError(f"project key not found: {parent_key_id}")
        return {"key": _public_record(record), "secret": secret}

    def _new_record(
        self,
        *,
        project_id: str,
        owner_user_id: str,
        expires_at: str | None,
        parent_key_id: str | None,
        sandbox_seconds_ceiling: int | None,
        blob_bytes_ceiling: int | None,
        audience: str | None,
        oauth_family_id: str | None,
    ) -> tuple[ProjectKeyRecord, str]:
        project_id = _required(project_id, field="project_id")
        owner_user_id = _required(owner_user_id, field="owner_user_id")
        expires_at = _expiry(expires_at)
        sandbox_seconds_ceiling = _ceiling(
            sandbox_seconds_ceiling, field="sandbox_seconds_ceiling"
        )
        blob_bytes_ceiling = _ceiling(blob_bytes_ceiling, field="blob_bytes_ceiling")
        parent_key_id = str(parent_key_id or "").strip() or None
        if parent_key_id:
            parent = self._repository.by_id(key_id=parent_key_id)
            if (
                parent is None
                or parent.project_id != project_id
                or parent.owner_user_id != owner_user_id
            ):
                raise NotFoundError(f"project key not found: {parent_key_id}")
        secret = mint_secret(prefix=PROJECT_KEY_PREFIX, nbytes=32)
        record = ProjectKeyRecord(
            id=new_id(prefix="mkey"),
            secret_digest=hash_secret(secret),
            owner_user_id=owner_user_id,
            tenant_id=self._repository.project_tenant(project_id=project_id),
            project_id=project_id,
            audience=str(audience or "").strip() or None,
            oauth_family_id=str(oauth_family_id or "").strip() or None,
            created_at=now_iso(),
            expires_at=expires_at,
            revoked_at=None,
            parent_key_id=parent_key_id,
            sandbox_seconds_ceiling=sandbox_seconds_ceiling,
            blob_bytes_ceiling=blob_bytes_ceiling,
        )
        return record, secret

    def list(self, *, project_id: str, owner_user_id: str) -> dict[str, object]:
        return {
            "keys": [
                _public_record(record)
                for record in self._repository.list_for_owner(
                    project_id=_required(project_id, field="project_id"),
                    owner_user_id=_required(owner_user_id, field="owner_user_id"),
                )
            ]
        }

    def revoke(
        self, *, project_id: str, key_id: str, owner_user_id: str
    ) -> dict[str, object]:
        record = self._repository.revoke(
            project_id=_required(project_id, field="project_id"),
            key_id=_required(key_id, field="key_id"),
            owner_user_id=_required(owner_user_id, field="owner_user_id"),
            revoked_at=now_iso(),
        )
        if record is None:
            raise NotFoundError(f"project key not found: {key_id}")
        return {"key": _public_record(record)}

    def revoke_lineage(
        self, *, project_id: str, key_id: str, owner_user_id: str
    ) -> dict[str, object]:
        """Revoke one key and every rotation descendant in its grant lineage."""
        project_id = _required(project_id, field="project_id")
        key_id = _required(key_id, field="key_id")
        owner_user_id = _required(owner_user_id, field="owner_user_id")
        if not self._repository.revoke_lineage(
            project_id=project_id,
            key_id=key_id,
            owner_user_id=owner_user_id,
            revoked_at=now_iso(),
        ):
            raise NotFoundError(f"project key not found: {key_id}")
        return {"revoked": True, "root_key_id": key_id}

    def verify_secret(self, *, secret: str) -> ProjectKeyRecord | None:
        """Resolve one bearer with a fresh database read on every call."""
        digest = hash_secret(secret)
        record = self._repository.by_digest(digest=digest)
        if not secret_digest_matches(
            stored_digest=record.secret_digest if record is not None else None,
            presented_digest=digest,
        ):
            return None
        if record is None or record.revoked_at:
            return None
        expiry = parse_iso(record.expires_at)
        if record.expires_at and expiry is None:
            return None
        if expiry is not None and expiry <= datetime.now(UTC):
            return None
        return record


def _public_record(record: ProjectKeyRecord) -> dict[str, object]:
    result = asdict(record)
    result.pop("secret_digest")
    result.pop("audience")
    result.pop("oauth_family_id")
    return result


def _required(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValidationError(f"{field} is required", details={"field": field})
    return text


def _ceiling(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be a nonnegative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be a nonnegative integer") from exc
    if parsed < 0:
        raise ValidationError(f"{field} must be a nonnegative integer")
    return parsed


def _expiry(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = parse_iso(text)
    if parsed is None:
        raise ValidationError(
            "expires_at must be an ISO-8601 timestamp", details={"field": "expires_at"}
        )
    if parsed <= datetime.now(UTC):
        raise ValidationError(
            "expires_at must be in the future", details={"field": "expires_at"}
        )
    return text


__all__ = [
    "PROJECT_KEY_PREFIX",
    "ProjectKeyControl",
    "ProjectKeyLookup",
    "ProjectKeyRecord",
    "ProjectKeys",
]
