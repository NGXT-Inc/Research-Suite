"""Artifacts module: resource records, versions, and evidence.

Owns repo-file resource identity and associations (``resources``), the
pinned-bytes rule, and public evidence contracts (``ports``). Shared role
vocabulary and markdown parsing live below both planes in ``merv.shared``.
"""

from __future__ import annotations

from .resources import ResourceService

__all__ = ["ResourceService"]
