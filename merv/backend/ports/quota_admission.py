"""Port for sandbox procurement admission checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class AdmissionRequest:
    """The cost-relevant facts of a sandbox request, for quota admission.

    Decoupled from execution.SandboxRequest so the quota seam stays a pure
    record-layer concern. ``price_usd_per_hour`` is the quoted price for the
    chosen instance and ``gpu_count`` is its accelerator count; None means the
    provider could not resolve that fact.
    """

    tenant_id: str
    time_limit_seconds: int
    price_usd_per_hour: float | None = None
    gpu_count: int | None = None
    sandbox_uid: str = ""


class QuotaAdmission(Protocol):
    """Admits or denies fresh sandbox procurement."""

    def check_admission(self, *, request: AdmissionRequest) -> None:
        ...

    def reserve_provisioning(
        self,
        *,
        request: AdmissionRequest,
        reservation: Callable[[Any], None],
    ) -> None:
        """Atomically admit and persist the caller's provisioning reservation."""
        ...

    def check_lifetime_extension(
        self,
        *,
        tenant_id: str,
        total_time_limit_seconds: int,
        price_usd_per_hour: float | None = None,
        gpu_count: int | None = None,
        sandbox_uid: str = "",
        remaining_time_limit_seconds: int = 0,
        reservation: Callable[[Any], None] | None = None,
    ) -> None:
        ...
