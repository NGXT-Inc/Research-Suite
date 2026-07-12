"""Small stdlib client for the Thunder Compute API."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ....sandbox.sandbox_backend import BackendUnavailableError
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
        url = f"{self.config.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "merv/0.0005",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                response_body = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BackendUnavailableError(
                f"Thunder Compute API {method} {path} failed with HTTP {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise BackendUnavailableError(f"Thunder Compute API is unreachable: {exc}") from exc
        except TimeoutError as exc:
            raise BackendUnavailableError("Thunder Compute API request timed out") from exc
        try:
            parsed = json.loads(response_body) if response_body else {}
        except json.JSONDecodeError as exc:
            raise BackendUnavailableError("Thunder Compute API returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise BackendUnavailableError("Thunder Compute API returned a non-object response")
        return parsed
