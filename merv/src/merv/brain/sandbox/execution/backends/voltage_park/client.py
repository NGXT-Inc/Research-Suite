"""Small stdlib client for the Voltage Park cloud API.

Coded against the live spec at ``/api/v1/openapi.json`` (July 2026).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ....sandbox_backend import BackendUnavailableError
from .config import VoltageParkCloudConfig


class VoltageParkClient:
    def __init__(
        self, *, config: VoltageParkCloudConfig | None = None, timeout: float = 60.0
    ) -> None:
        self.config = config or VoltageParkCloudConfig.from_env()
        self.timeout = timeout

    def list_instant_locations(self) -> list[dict[str, Any]]:
        raw = self._request("GET", "/virtual-machines/instant/locations")
        results = raw.get("results") if isinstance(raw, dict) else raw
        if not isinstance(results, list):
            raise BackendUnavailableError(
                "Voltage Park returned malformed instant locations data"
            )
        return [item for item in results if isinstance(item, dict)]

    def create_instant_vm(
        self,
        *,
        config_id: str,
        name: str,
        ssh_keys: list[str],
        cloud_init: dict[str, Any],
    ) -> str:
        raw = self._request(
            "POST",
            "/virtual-machines/instant",
            body={
                "config_id": config_id,
                "name": name,
                "ssh_keys": ssh_keys,
                "cloud_init": cloud_init,
            },
        )
        vm_id = str(raw.get("vm_id") or "") if isinstance(raw, dict) else ""
        if not vm_id:
            raise BackendUnavailableError("Voltage Park create returned no vm_id")
        return vm_id

    def list_vms(self) -> list[dict[str, Any]]:
        raw = self._request("GET", "/virtual-machines/")
        results = raw.get("results") if isinstance(raw, dict) else raw
        if not isinstance(results, list):
            raise BackendUnavailableError("Voltage Park returned malformed VM list data")
        return [item for item in results if isinstance(item, dict)]

    def get_vm(self, vm_id: str) -> dict[str, Any]:
        raw = self._request("GET", f"/virtual-machines/{vm_id}")
        if not isinstance(raw, dict):
            raise BackendUnavailableError("Voltage Park returned malformed VM data")
        return raw

    def delete_vm(self, vm_id: str) -> None:
        self._request("DELETE", f"/virtual-machines/{vm_id}")

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
                f"Voltage Park API {method} {path} failed with HTTP {exc.code}: {detail}",
                status=exc.code,
            ) from exc
        except URLError as exc:
            raise BackendUnavailableError(f"Voltage Park API is unreachable: {exc}") from exc
        except TimeoutError as exc:
            raise BackendUnavailableError("Voltage Park API request timed out") from exc
        try:
            return json.loads(payload) if payload else {}
        except json.JSONDecodeError as exc:
            raise BackendUnavailableError("Voltage Park API returned invalid JSON") from exc
