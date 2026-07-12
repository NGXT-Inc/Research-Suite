"""Heavy-file object store providers."""

from __future__ import annotations

from .s3_object_store import S3CompatibleObjectStore

__all__ = ["S3CompatibleObjectStore"]
