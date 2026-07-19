"""Port for sandbox procurement admission checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


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


class QuotaAdmission(Protocol):
    """Admits or denies fresh sandbox procurement."""

    def check_admission(self, *, request: AdmissionRequest) -> None:
        ...

    def check_lifetime_extension(
        self,
        *,
        tenant_id: str,
        total_time_limit_seconds: int,
        price_usd_per_hour: float | None = None,
    ) -> None:
        ...
