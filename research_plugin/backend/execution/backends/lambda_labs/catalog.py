"""Shape Lambda Cloud `/instance-types` data into selection catalogs.

One place owns the messy mapping from Lambda's raw API payload to the two
shapes the rest of the app consumes:

  - ``summarize_instance_types`` — the rich, filterable shape the HTTP route and
    ``ComputeService.lambda_available_gpus`` return to the UI.
  - ``to_agent_options`` — a flat menu the agent picks from when it must choose a
    bundled machine SKU for ``sandbox.request``.

Both the ``ComputeService`` and the Lambda sandbox backend call in here so the
catalog logic never drifts between the UI and the agent surfaces.
"""

from __future__ import annotations

from typing import Any


def summarize_instance_types(
    raw_types: dict[str, Any],
    *,
    region: str | None = None,
    gpu: str | None = None,
    instance_type: str | None = None,
    min_gpus: int | None = None,
    only_available: bool = True,
) -> dict[str, Any]:
    """Filter + normalize Lambda instance types into the rich catalog shape."""

    region_filter = _norm(region)
    gpu_filter = _norm(gpu)
    instance_type_filter = _norm(instance_type)
    min_gpu_count = int(min_gpus) if min_gpus is not None else None

    entries: list[dict[str, Any]] = []
    for name, item in sorted(raw_types.items()):
        if not isinstance(item, dict):
            continue
        instance = item.get("instance_type") or {}
        if not isinstance(instance, dict):
            continue
        entry_name = str(instance.get("name") or name)
        specs = instance.get("specs") if isinstance(instance.get("specs"), dict) else {}
        regions = [
            {
                "name": str(region_item.get("name") or ""),
                "description": str(region_item.get("description") or ""),
            }
            for region_item in item.get("regions_with_capacity_available", [])
            if isinstance(region_item, dict) and region_item.get("name")
        ]
        region_names = {str(item["name"]).lower() for item in regions}
        gpu_description = str(instance.get("gpu_description") or "")
        gpus = _int_or_zero(specs.get("gpus"))
        available = bool(regions)

        if only_available and not available:
            continue
        if region_filter and region_filter not in region_names:
            continue
        if gpu_filter and gpu_filter not in _norm(gpu_description) and gpu_filter not in _norm(entry_name):
            continue
        if instance_type_filter and instance_type_filter != _norm(entry_name):
            continue
        if min_gpu_count is not None and gpus < min_gpu_count:
            continue

        price_cents = _int_or_zero(instance.get("price_cents_per_hour"))
        entries.append(
            {
                "name": entry_name,
                "description": str(instance.get("description") or ""),
                "gpu_description": gpu_description,
                "price_cents_per_hour": price_cents,
                "price_usd_per_hour": price_cents / 100.0,
                "specs": {
                    "vcpus": _int_or_zero(specs.get("vcpus")),
                    "memory_gib": _int_or_zero(specs.get("memory_gib")),
                    "storage_gib": _int_or_zero(specs.get("storage_gib")),
                    "gpus": gpus,
                },
                "regions_with_capacity_available": regions,
                "available": available,
            }
        )

    all_regions = sorted(
        {
            region_item["name"]
            for entry in entries
            for region_item in entry["regions_with_capacity_available"]
        }
    )
    return {
        "provider": "lambda_labs",
        "count": len(entries),
        "regions": all_regions,
        "instance_types": entries,
    }


def to_agent_options(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the rich catalog into a compact menu the agent chooses from.

    Sorted cheapest-first so the agent (and the user reading the menu) sees the
    least expensive viable machine at the top.
    """

    options: list[dict[str, Any]] = []
    for entry in summary.get("instance_types", []):
        specs = entry.get("specs") or {}
        options.append(
            {
                "instance_type": entry.get("name"),
                "gpu": _gpu_label(entry.get("gpu_description") or "", entry.get("name") or ""),
                "gpu_description": entry.get("gpu_description") or "",
                "gpu_count": _int_or_zero(specs.get("gpus")),
                "vcpus": _int_or_zero(specs.get("vcpus")),
                "memory_gib": _int_or_zero(specs.get("memory_gib")),
                "storage_gib": _int_or_zero(specs.get("storage_gib")),
                "price_usd_per_hour": entry.get("price_usd_per_hour", 0.0),
                "regions": [r["name"] for r in entry.get("regions_with_capacity_available", [])],
                "available": bool(entry.get("available")),
            }
        )
    options.sort(key=lambda o: (o["price_usd_per_hour"], o["instance_type"] or ""))
    return options


def find_option(summary: dict[str, Any], *, instance_type: str) -> dict[str, Any] | None:
    """Return the flat menu entry for one instance type, or None if absent."""
    wanted = _norm(instance_type)
    for option in to_agent_options(summary):
        if _norm(option.get("instance_type")) == wanted:
            return option
    return None


def _gpu_label(gpu_description: str, name: str) -> str:
    """Best-effort short GPU label, e.g. 'H100' from 'H100 (80 GB SXM5)'."""
    text = gpu_description.strip()
    if text:
        return text.split("(")[0].strip() or text
    # Fall back to the SKU name's GPU token, e.g. gpu_1x_h100_sxm5 -> H100.
    parts = [p for p in name.lower().split("_") if p]
    for part in parts:
        if any(ch.isdigit() for ch in part) and any(ch.isalpha() for ch in part):
            return part.upper()
    return ""


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
