"""Shape TensorDock locations into the agent selection menu.

TensorDock composes machines (GPU model + count, vCPU, RAM, storage) per
host, so the menu synthesizes bundled shapes: one option per location, GPU
model, and count (1x plus the location's max) with a sane default
vCPU/RAM/storage. The option's ``instance_type`` encodes ``<count>x-<v0Name>``
and the location id rides in ``regions``.

MANDATORY filter: only locations whose GPU reports
``network_features.dedicated_ip_available`` are offered — port-mapped hosts
cannot serve the direct-SSH sandbox contract.
"""

from __future__ import annotations

import re
from typing import Any

from .._values import _float_or_zero, _int_or_zero, _norm


# Deploy shape defaults per GPU (scaled by count, clipped to location maxima).
DEFAULT_VCPUS_PER_GPU = 8
DEFAULT_RAM_GB_PER_GPU = 32
DEFAULT_STORAGE_GB = 100  # TensorDock's minimum
# RAM must land on one of TensorDock's allowed steps.
RAM_GB_STEPS = (2, 4, 6, 8, 10, 16, 32, 48, 64, 80, 96, 112, 128, 144, 160, 176, 192, 208, 224, 240, 256, 512)

_INSTANCE_TYPE_RE = re.compile(r"^(\d+)x-(.+)$")


def parse_instance_type(instance_type: str) -> tuple[int, str] | None:
    """'2x-h100-sxm5-80gb' -> (2, 'h100-sxm5-80gb'); None when malformed."""
    match = _INSTANCE_TYPE_RE.match(instance_type.strip())
    if not match:
        return None
    return int(match.group(1)), match.group(2)


def deploy_shape(option: dict[str, Any]) -> dict[str, int]:
    """The vcpu/ram/storage the synthesized option deploys with."""
    return {
        "vcpu_count": int(option["vcpus"]),
        "ram_gb": int(option["memory_gib"]),
        "storage_gb": int(option["storage_gib"]),
    }


def to_agent_options(
    locations: list[dict[str, Any]],
    *,
    gpu: str | None = None,
    region: str | None = None,
    only_available: bool = True,
) -> list[dict[str, Any]]:
    gpu_filter = _norm(gpu)
    region_filter = _norm(region)
    options: list[dict[str, Any]] = []
    for location in locations:
        location_id = str(location.get("id") or "")
        if region_filter and region_filter != location_id.lower():
            continue
        place = ", ".join(
            str(part)
            for part in (location.get("city"), location.get("country"))
            if part
        )
        for entry in location.get("gpus", []) or []:
            if not isinstance(entry, dict):
                continue
            features = (
                entry.get("network_features")
                if isinstance(entry.get("network_features"), dict)
                else {}
            )
            # Port-mapped-only hosts are unusable for us: no dedicated IP, no
            # direct SSH contract.
            if not features.get("dedicated_ip_available"):
                continue
            v0_name = str(entry.get("v0Name") or "")
            display = str(entry.get("displayName") or v0_name)
            max_count = _int_or_zero(entry.get("max_count"))
            available = max_count > 0
            if only_available and not available:
                continue
            if gpu_filter and gpu_filter not in _norm(display) and gpu_filter not in _norm(v0_name):
                continue
            resources = (
                entry.get("resources") if isinstance(entry.get("resources"), dict) else {}
            )
            pricing = (
                entry.get("pricing") if isinstance(entry.get("pricing"), dict) else {}
            )
            for count in sorted({1, max_count} if max_count else {1}):
                if count < 1 or count > max(max_count, 1):
                    continue
                vcpus = min(
                    DEFAULT_VCPUS_PER_GPU * count,
                    _int_or_zero(resources.get("max_vcpus")) or DEFAULT_VCPUS_PER_GPU * count,
                )
                vcpus = max(vcpus - (vcpus % 2), 2)  # vCPU steps of 2
                ram = _ram_step(
                    min(
                        DEFAULT_RAM_GB_PER_GPU * count,
                        _int_or_zero(resources.get("max_ram_gb"))
                        or DEFAULT_RAM_GB_PER_GPU * count,
                    )
                )
                storage_cap = _int_or_zero(resources.get("max_storage_gb"))
                if storage_cap and storage_cap < DEFAULT_STORAGE_GB:
                    continue  # cannot meet the 100 GB minimum here
                price = (
                    _float_or_zero(entry.get("price_per_hr")) * count
                    + _float_or_zero(pricing.get("per_vcpu_hr")) * vcpus
                    + _float_or_zero(pricing.get("per_gb_ram_hr")) * ram
                    + _float_or_zero(pricing.get("per_gb_storage_hr")) * DEFAULT_STORAGE_GB
                )
                options.append(
                    {
                        "instance_type": f"{count}x-{v0_name}",
                        "gpu": _gpu_label(v0_name),
                        "gpu_description": f"{count}x {display}" + (f" @ {place}" if place else ""),
                        "gpu_count": count,
                        "vcpus": vcpus,
                        "memory_gib": ram,
                        "storage_gib": DEFAULT_STORAGE_GB,
                        "price_usd_per_hour": round(price, 4),
                        "regions": [location_id],
                        "available": available,
                    }
                )
    options.sort(key=lambda o: (o["price_usd_per_hour"], o["instance_type"]))
    return options


def find_option(
    options: list[dict[str, Any]],
    *,
    instance_type: str,
    region: str | None = None,
) -> dict[str, Any] | None:
    wanted = _norm(instance_type)
    for option in options:
        if _norm(option.get("instance_type")) != wanted:
            continue
        if region and region not in (option.get("regions") or []):
            continue
        return option
    return None


def _ram_step(ram_gb: int) -> int:
    """Largest allowed RAM step at or below the requested amount."""
    allowed = [step for step in RAM_GB_STEPS if step <= max(ram_gb, RAM_GB_STEPS[0])]
    return allowed[-1] if allowed else RAM_GB_STEPS[0]


def _gpu_label(v0_name: str) -> str:
    """Short GPU label, e.g. 'H100' from 'h100-sxm5-80gb'."""
    return v0_name.split("-")[0].upper() if v0_name else ""
