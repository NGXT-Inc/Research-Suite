"""Shape Hyperstack flavors + pricebook into the agent selection menu.

``/core/flavors`` groups flavors by GPU model and region and already carries
``stock_available``; ``/pricebook`` maps flavor name -> $/hr. One place joins
them into the flat, cheapest-first option shape the app's catalogs share.
"""

from __future__ import annotations

from typing import Any

from .._values import _float_or_zero, _int_or_zero, _norm, find_option


def to_agent_options(
    flavor_groups: list[dict[str, Any]],
    pricebook: list[dict[str, Any]],
    *,
    gpu: str | None = None,
    region: str | None = None,
    only_available: bool = True,
) -> list[dict[str, Any]]:
    prices = {
        str(entry.get("name") or ""): _float_or_zero(entry.get("value"))
        for entry in pricebook
    }
    gpu_filter = _norm(gpu)
    region_filter = _norm(region)
    options: list[dict[str, Any]] = []
    for group in flavor_groups:
        group_gpu = str(group.get("gpu") or "")
        for flavor in group.get("flavors", []) or []:
            if not isinstance(flavor, dict):
                continue
            name = str(flavor.get("name") or "")
            flavor_region = str(flavor.get("region_name") or group.get("region_name") or "")
            flavor_gpu = str(flavor.get("gpu") or group_gpu)
            available = bool(flavor.get("stock_available"))
            if only_available and not available:
                continue
            if region_filter and region_filter != _norm(flavor_region):
                continue
            if gpu_filter and gpu_filter not in _norm(flavor_gpu) and gpu_filter not in _norm(name):
                continue
            options.append(
                {
                    "instance_type": name,
                    "gpu": _gpu_label(flavor_gpu),
                    "gpu_description": flavor_gpu,
                    "gpu_count": _int_or_zero(flavor.get("gpu_count")),
                    "vcpus": _int_or_zero(flavor.get("cpu")),
                    "memory_gib": _int_or_zero(flavor.get("ram")),
                    "storage_gib": _int_or_zero(flavor.get("disk"))
                    + _int_or_zero(flavor.get("ephemeral")),
                    "price_usd_per_hour": prices.get(name, 0.0),
                    "regions": [flavor_region] if flavor_region else [],
                    "available": available,
                }
            )
    options.sort(key=lambda o: (o["price_usd_per_hour"], o["instance_type"]))
    return options


def _gpu_label(gpu_description: str) -> str:
    """Short GPU label, e.g. 'H100' from 'H100-80G-PCIe'."""
    text = gpu_description.strip()
    return text.split("-")[0].strip() if text else ""
