"""Compute-provider discovery helpers."""

from __future__ import annotations

from typing import Any, Protocol

from ..execution.backends.lambda_labs import LambdaCloudClient
from ..execution.backends.lambda_labs.catalog import summarize_instance_types


class LambdaInstanceTypeClient(Protocol):
    def list_instance_types(self) -> dict[str, Any]: ...


class ComputeService:
    def __init__(self, *, lambda_client: LambdaInstanceTypeClient | None = None) -> None:
        self.lambda_client = lambda_client

    def lambda_available_gpus(
        self,
        *,
        region: str | None = None,
        gpu: str | None = None,
        instance_type: str | None = None,
        min_gpus: int | None = None,
        only_available: bool = True,
    ) -> dict[str, Any]:
        client = self.lambda_client or LambdaCloudClient()
        raw_types = client.list_instance_types()
        summary = summarize_instance_types(
            raw_types,
            region=region,
            gpu=gpu,
            instance_type=instance_type,
            min_gpus=min_gpus,
            only_available=only_available,
        )
        return {
            "provider": summary["provider"],
            "filters": {
                "region": region,
                "gpu": gpu,
                "instance_type": instance_type,
                "min_gpus": min_gpus,
                "only_available": only_available,
            },
            "count": summary["count"],
            "regions": summary["regions"],
            "instance_types": summary["instance_types"],
        }
