"""Shape Thunder Compute specs into the provider-neutral hardware menu."""

from __future__ import annotations

from typing import Any

from .._values import _int_or_zero, _norm


def summarize_specs(
    specs: dict[str, Any],
    *,
    pricing: dict[str, Any] | None = None,
    template: str,
    gpu: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    gpu_filter = _norm(gpu)
    mode_filter = _norm(mode)
    prices = pricing or {}
    options: list[dict[str, Any]] = []

    for name, raw in sorted(specs.items()):
        if not isinstance(raw, dict):
            continue
        gpu_type = _gpu_type_from_name(name)
        display = str(raw.get("displayName") or gpu_type.upper())
        spec_mode = str(raw.get("mode") or "")
        if mode_filter and mode_filter != _norm(spec_mode):
            continue
        haystack = " ".join((name, gpu_type, display)).lower()
        if gpu_filter and gpu_filter not in haystack:
            continue
        vcpu_options = sorted(
            {
                parsed
                for item in raw.get("vcpuOptions", []) or []
                for parsed in [_int_or_zero(item)]
                if parsed > 0
            }
        )
        if not vcpu_options:
            continue
        storage = raw.get("storageGB") if isinstance(raw.get("storageGB"), dict) else {}
        storage_min = _int_or_zero(storage.get("min")) or 100
        gpu_count = _int_or_zero(raw.get("gpuCount"))
        ram_per_vcpu = _int_or_zero(raw.get("ramPerVCPUGiB"))
        vcpus = vcpu_options[0]
        options.append(
            {
                "instance_type": name,
                "gpu": display,
                "gpu_type": gpu_type,
                "gpu_count": gpu_count,
                "vram_gb": _int_or_zero(raw.get("vramGB")),
                "mode": spec_mode,
                "vcpus": vcpus,
                "vcpu_options": vcpu_options,
                "memory_gib": vcpus * ram_per_vcpu if ram_per_vcpu else 0,
                "storage_gib": storage_min,
                "storage_options_gib": {
                    "min": storage_min,
                    "max": _int_or_zero(storage.get("max")),
                },
                "template": template,
                "price_usd_per_hour": _price_for(name=name, gpu_type=gpu_type, prices=prices),
                "available": True,
            }
        )

    options.sort(
        key=lambda item: (
            float(item.get("price_usd_per_hour") or 0.0),
            int(item.get("gpu_count") or 0),
            int(item.get("vcpus") or 0),
            str(item.get("instance_type") or ""),
        )
    )
    return {
        "provider": "thunder_compute",
        "count": len(options),
        "regions": [],
        "instance_types": options,
    }


def find_option(summary: dict[str, Any], *, instance_type: str) -> dict[str, Any] | None:
    wanted = _norm(instance_type)
    for option in summary.get("instance_types", []):
        if _norm(option.get("instance_type")) == wanted:
            return option
    return None


def _gpu_type_from_name(name: str) -> str:
    marker = "_x"
    if marker in name:
        return name.split(marker, 1)[0]
    return name.split("_", 1)[0]


def _price_for(*, name: str, gpu_type: str, prices: dict[str, Any]) -> float:
    for key in (name, gpu_type):
        try:
            return float(prices.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0
