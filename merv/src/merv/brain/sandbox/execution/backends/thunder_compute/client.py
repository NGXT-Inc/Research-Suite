"""Small stdlib client for the Thunder Compute API."""

from __future__ import annotations

from typing import Any

from .._http import bearer_json_headers, request_json
from ....sandbox_backend import BackendUnavailableError
from .config import ThunderCloudConfig


class ThunderComputeClient:
    def __init__(self, *, config: ThunderCloudConfig | None = None, timeout: float = 30.0) -> None:
        self.config = config or ThunderCloudConfig.from_env()
        self.timeout = timeout

    def list_specs(self) -> dict[str, Any]:
        data = self._request("GET", "/specs")
        raw = data.get("specs")
        if not isinstance(raw, dict):
            raise BackendUnavailableError("Thunder Compute returned malformed specs data")
        return raw

    def pricing(self) -> dict[str, Any]:
        data = self._request("GET", "/pricing")
        raw = data.get("pricing")
        if not isinstance(raw, dict):
            raise BackendUnavailableError("Thunder Compute returned malformed pricing data")
        return raw

    def list_instances(self) -> dict[str, dict[str, Any]]:
        data = self._request("GET", "/instances/list")
        instances: dict[str, dict[str, Any]] = {}
        for key, item in data.items():
            if isinstance(item, dict):
                row = dict(item)
                row.setdefault("id", str(key))
                instances[str(key)] = row
        return instances

    def create_instance(
        self,
        *,
        cpu_cores: int,
        disk_size_gb: int,
        gpu_type: str,
        mode: str,
        num_gpus: int,
        template: str,
        public_key: str,
    ) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/instances/create",
            body={
                "cpu_cores": int(cpu_cores),
                "disk_size_gb": int(disk_size_gb),
                "gpu_type": gpu_type,
                "mode": mode,
                "num_gpus": int(num_gpus),
                "template": template,
                "public_key": public_key,
            },
        )
        if not isinstance(data.get("identifier"), int) or not data.get("uuid"):
            raise BackendUnavailableError("Thunder Compute create returned no instance identifier")
        return data

    def delete_instance(self, instance_id: str) -> None:
        self._request("POST", f"/instances/{instance_id}/delete")

    def _request(
        self, method: str, path: str, *, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return request_json(
            provider="Thunder Compute",
            method=method,
            base_url=self.config.base_url,
            path=path,
            body=body,
            headers=bearer_json_headers(self.config.api_key, "merv/0.0005"),
            timeout=self.timeout,
            require_object=True,
            report_http_status=False,
        )
