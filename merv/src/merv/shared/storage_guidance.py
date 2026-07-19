"""Agent-facing guidance for durable heavy-file storage."""

from __future__ import annotations

from typing import Any

STORAGE_RULE_OF_THUMB = (
    "Use storage for files that are too large or noisy for repo resources, "
    "or expensive to regenerate: checkpoints/models, precious datasets, "
    "dataset shards, parquet/archive outputs, generated caches that must "
    "survive, and logs/traces over about 10 MB."
)

STORAGE_USE_FOR = (
    "checkpoints and trained model weights worth reusing",
    "precious datasets, dataset shards, parquet/archive outputs, and cache directories",
    "long logs or traces over about 10 MB that a reviewer may need",
    "generated intermediates that must survive sandbox release but do not belong in git",
)

RESOURCE_USE_FOR = (
    "plan.md, report.md, graph.json, scripts, configs, and small retained result files",
    "metrics TSV/JSON, summarized logs, and plots referenced by the report",
    "dataset notes such as data.md with sources, filters, row counts, schema, and caveats",
)

EPHEMERAL_USE_FOR = (
    "regenerable package caches, scratch downloads, and temporary preprocessing outputs",
    "large intermediate files that are not needed after the sandbox run",
)


def storage_guidance(*, enabled: bool = True) -> dict[str, Any]:
    guidance = {
        "enabled": bool(enabled),
        "rule_of_thumb": STORAGE_RULE_OF_THUMB,
        "use_storage_for": list(STORAGE_USE_FOR),
        "keep_as_resources": list(RESOURCE_USE_FOR),
        "leave_ephemeral": list(EPHEMERAL_USE_FOR),
    }
    if not enabled:
        guidance["disabled_note"] = (
            "Storage is not enabled on this backend; copy the small files you need "
            "into the local experiment folder and expect large sandbox-only files "
            "to be lost at release unless another durable path is available."
        )
    return guidance
