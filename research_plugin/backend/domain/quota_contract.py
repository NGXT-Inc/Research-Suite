"""Quota admission request contract shared by control-safe services."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AdmissionRequest:
    """The cost-relevant facts of a sandbox request, for quota admission.

    Decoupled from execution.SandboxRequest so the quota seam stays a pure
    record-layer concern. ``price_usd_per_hour`` is the quoted price for the
    chosen instance, or None when the provider has no price quote.
    """

    tenant_id: str
    time_limit_seconds: int
    price_usd_per_hour: float | None = None
