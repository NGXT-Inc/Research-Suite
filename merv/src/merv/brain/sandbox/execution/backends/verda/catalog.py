"""Shape Verda instance types + availability into the agent selection menu.

``/v1/instance-types`` carries specs and $/hr (as strings);
``/v1/instance-availability`` maps location -> deployable types. Note the
billing quirk surfaced in the menu: Verda bills in 10-minute increments,
rounded up.
"""

from __future__ import annotations

from typing import Any

from .._values import _float_or_zero, _int_or_zero, _norm


def to_agent_options(
    instance_types: list[dict[str, Any]],
    availability: list[dict[str, Any]],
    *,
    gpu: str | None = None,
    region: str | None = None,
    only_available: bool = True,
) -> list[dict[str, Any]]:
    locations_by_type: dict[str, list[str]] = {}
    for entry in availability:
        location = str(entry.get("location_code") or "")
        for name in entry.get("availabilities", []) or []:
            locations_by_type.setdefault(str(name), []).append(location)

    gpu_filter = _norm(gpu)
    region_filter = _norm(region)
    options: list[dict[str, Any]] = []
    for item in instance_types:
        name = str(item.get("instance_type") or "")
        if not name:
            continue
        regions = sorted(locations_by_type.get(name, []))
        available = bool(regions)
        model = str(item.get("model") or "")
        gpu_obj = item.get("gpu") if isinstance(item.get("gpu"), dict) else {}
        cpu_obj = item.get("cpu") if isinstance(item.get("cpu"), dict) else {}
        memory_obj = item.get("memory") if isinstance(item.get("memory"), dict) else {}
        if only_available and not available:
            continue
        if region_filter and region_filter not in {r.lower() for r in regions}:
            continue
        if gpu_filter and gpu_filter not in _norm(model) and gpu_filter not in _norm(name):
            continue
        options.append(
            {
                "instance_type": name,
                "gpu": model,
                "gpu_description": str(gpu_obj.get("description") or model),
                "gpu_count": _int_or_zero(gpu_obj.get("number_of_gpus")),
                "vcpus": _int_or_zero(cpu_obj.get("number_of_cores")),
                "memory_gib": _int_or_zero(memory_obj.get("size_in_gigabytes")),
                "storage_gib": 0,  # Verda OS volumes are sized at deploy, not by SKU
                "price_usd_per_hour": _float_or_zero(item.get("price_per_hour")),
                "regions": regions,
                "available": available,
            }
        )
    options.sort(key=lambda o: (o["price_usd_per_hour"], o["instance_type"]))
    return options


def find_option(
    options: list[dict[str, Any]], *, instance_type: str
) -> dict[str, Any] | None:
    wanted = _norm(instance_type)
    for option in options:
        if _norm(option.get("instance_type")) == wanted:
            return option
    return None
