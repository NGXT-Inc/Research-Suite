"""Small stdlib client for the TensorDock v2 API.

The v2 API is JSON:API-flavored: writes wrap attributes in a ``data``
envelope, and reads answer either enveloped or bare — ``_unwrap`` accepts
both shapes.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ....sandbox_backend import BackendUnavailableError
from .config import TensorDockCloudConfig


class TensorDockClient:
    def __init__(
        self, *, config: TensorDockCloudConfig | None = None, timeout: float = 60.0
    ) -> None:
        self.config = config or TensorDockCloudConfig.from_env()
        self.timeout = timeout

    def list_locations(self) -> list[dict[str, Any]]:
        raw = _unwrap(self._request("GET", "/locations"))
        locations = raw.get("locations") if isinstance(raw, dict) else raw
        if not isinstance(locations, list):
            raise BackendUnavailableError("TensorDock returned malformed locations data")
        return [item for item in locations if isinstance(item, dict)]

    def create_instance(
        self,
        *,
        name: str,
        image: str,
        location_id: str,
        vcpu_count: int,
        ram_gb: int,
        storage_gb: int,
        gpus: dict[str, dict[str, int]],
        ssh_key: str,
        cloud_init: dict[str, Any],
    ) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/instances",
            body={
                "data": {
                    "type": "virtualmachine",
                    "attributes": {
                        "name": name,
                        "type": "virtualmachine",
                        "image": image,
                        "resources": {
                            "vcpu_count": vcpu_count,
                            "ram_gb": ram_gb,
                            "storage_gb": storage_gb,
                            "gpus": gpus,
                        },
                        "location_id": location_id,
                        # Port-mapped hosts are unusable for direct SSH; the
                        # catalog only offers dedicated-IP-capable locations.
                        "useDedicatedIp": True,
                        "ssh_key": ssh_key,
                        "cloud_init": cloud_init,
                    },
                }
            },
        )
        instance = _unwrap(data)
        if not isinstance(instance, dict) or not instance.get("id"):
            raise BackendUnavailableError("TensorDock create returned no instance id")
        return instance

    def list_instances(self) -> list[dict[str, Any]]:
        raw = _unwrap(self._request("GET", "/instances"))
        instances = raw.get("instances") if isinstance(raw, dict) else raw
        if not isinstance(instances, list):
            raise BackendUnavailableError("TensorDock returned malformed instances data")
        return [item for item in instances if isinstance(item, dict)]

    def get_instance(self, instance_id: str) -> dict[str, Any]:
        raw = _unwrap(self._request("GET", f"/instances/{instance_id}"))
        if not isinstance(raw, dict):
            raise BackendUnavailableError("TensorDock returned malformed instance data")
        return raw

    def delete_instance(self, instance_id: str) -> None:
        self._request("DELETE", f"/instances/{instance_id}")

    def _request(
        self, method: str, path: str, *, body: dict[str, Any] | None = None
    ) -> Any:
        url = f"{self.config.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.config.token}",
                "Content-Type": "application/json",
                "User-Agent": "merv/0.0013",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - fixed API URL from config
                payload = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BackendUnavailableError(
                f"TensorDock API {method} {path} failed with HTTP {exc.code}: {detail}",
                status=exc.code,
            ) from exc
        except URLError as exc:
            raise BackendUnavailableError(f"TensorDock API is unreachable: {exc}") from exc
        except TimeoutError as exc:
            raise BackendUnavailableError("TensorDock API request timed out") from exc
        try:
            return json.loads(payload) if payload else {}
        except json.JSONDecodeError as exc:
            raise BackendUnavailableError("TensorDock API returned invalid JSON") from exc


def _unwrap(payload: Any) -> Any:
    """Strip the JSON:API ``data`` envelope (and nested ``attributes``) if present."""
    if isinstance(payload, dict) and "data" in payload:
        payload = payload["data"]
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("attributes"), dict)
        and ("id" in payload or "type" in payload)
    ):
        return {**payload["attributes"], "id": payload.get("id"), "type": payload.get("type")}
    return payload
