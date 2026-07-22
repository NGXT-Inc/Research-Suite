"""Shape Voltage Park instant-deploy presets into the agent selection menu.

Each option is one deploy preset (its uuid IS the ``instance_type`` passed
back on create); ``regions`` are the location uuids offering it. Windows
presets are excluded — the bootstrap needs an Ubuntu-family image.
"""

from __future__ import annotations

from typing import Any

from .._values import _float_or_zero, _int_or_zero, _norm, find_option


def to_agent_options(
    locations: list[dict[str, Any]],
    *,
    gpu: str | None = None,
    region: str | None = None,
    only_available: bool = True,
) -> list[dict[str, Any]]:
    gpu_filter = _norm(gpu)
    region_filter = _norm(region)
    merged: dict[str, dict[str, Any]] = {}
    for location in locations:
        location_id = str(location.get("id") or "")
        if region_filter and region_filter != location_id.lower():
            continue
        for preset in location.get("available_presets", []) or []:
            if not isinstance(preset, dict):
                continue
            operating_system = str(preset.get("operating_system") or "")
            if operating_system.startswith("Windows"):
                continue
            preset_id = str(preset.get("id") or "")
            if not preset_id:
                continue
            resources = (
                preset.get("resources") if isinstance(preset.get("resources"), dict) else {}
            )
            gpus = resources.get("gpus") if isinstance(resources.get("gpus"), dict) else {}
            gpu_names = sorted(gpus)
            label = _gpu_label(gpu_names[0]) if gpu_names else ""
            if gpu_filter and gpu_filter not in _norm(label) and gpu_filter not in _norm(
                " ".join(gpu_names)
            ):
                continue
            available_vms = _int_or_zero(preset.get("available_vms"))
            option = merged.setdefault(
                preset_id,
                {
                    "instance_type": preset_id,
                    "gpu": label,
                    "gpu_description": ", ".join(gpu_names) + f" ({operating_system})",
                    "gpu_count": sum(
                        _int_or_zero((spec or {}).get("count")) for spec in gpus.values()
                    ),
                    "vcpus": _int_or_zero(resources.get("vcpu_count")),
                    "memory_gib": _int_or_zero(resources.get("ram_gb")),
                    "storage_gib": _int_or_zero(resources.get("storage_gb")),
                    # Compute + storage hourly rates arrive as strings.
                    "price_usd_per_hour": _float_or_zero(preset.get("compute_rate_hourly"))
                    + _float_or_zero(preset.get("storage_rate_hourly")),
                    "regions": [],
                    "available": False,
                },
            )
            if available_vms > 0:
                option["available"] = True
                if location_id and location_id not in option["regions"]:
                    option["regions"].append(location_id)
    options = [
        option
        for option in merged.values()
        if option["available"] or not only_available
    ]
    for option in options:
        option["regions"].sort()
    options.sort(key=lambda o: (o["price_usd_per_hour"], o["instance_type"]))
    return options


def _gpu_label(name: str) -> str:
    """Short GPU label, e.g. 'H100' from 'h100-sxm5-80gb'."""
    return name.split("-")[0].upper() if name else ""
