"""Small stdlib client for the Hyperstack (NexGen Cloud) Infrahub API."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ....sandbox.sandbox_backend import BackendUnavailableError
from .config import HyperstackCloudConfig


class HyperstackClient:
    def __init__(
        self, *, config: HyperstackCloudConfig | None = None, timeout: float = 60.0
    ) -> None:
        self.config = config or HyperstackCloudConfig.from_env()
        self.timeout = timeout

    def list_flavors(self, *, region: str | None = None) -> list[dict[str, Any]]:
        path = "/core/flavors" + (f"?region={region}" if region else "")
        raw = self._request("GET", path).get("data")
        if not isinstance(raw, list):
            raise BackendUnavailableError("Hyperstack returned malformed flavors data")
        return [item for item in raw if isinstance(item, dict)]

    def get_pricebook(self) -> list[dict[str, Any]]:
        # Non-standard envelope: /pricebook returns a bare JSON array.
        raw = self._request("GET", "/pricebook", bare=True)
        if not isinstance(raw, list):
            raise BackendUnavailableError("Hyperstack returned malformed pricebook data")
        return [item for item in raw if isinstance(item, dict)]

    def import_keypair(
        self, *, name: str, environment_name: str, public_key: str
    ) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/core/keypairs",
            body={
                "name": name,
                "environment_name": environment_name,
                "public_key": public_key,
            },
        )
        raw = data.get("keypair")
        if not isinstance(raw, dict):
            raise BackendUnavailableError("Hyperstack returned malformed keypair data")
        return raw

    def list_keypairs(self) -> list[dict[str, Any]]:
        raw = self._request("GET", "/core/keypairs").get("keypairs")
        if not isinstance(raw, list):
            raise BackendUnavailableError("Hyperstack returned malformed keypairs data")
        return [item for item in raw if isinstance(item, dict)]

    def delete_keypair(self, keypair_id: int | str) -> None:
        self._request("DELETE", f"/core/keypair/{keypair_id}")

    def create_vm(
        self,
        *,
        name: str,
        environment_name: str,
        image_name: str,
        flavor_name: str,
        key_name: str,
        user_data: str,
        security_rules: list[dict[str, Any]],
    ) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/core/virtual-machines",
            body={
                "name": name,
                "environment_name": environment_name,
                "image_name": image_name,
                "flavor_name": flavor_name,
                "key_name": key_name,
                "count": 1,
                "assign_floating_ip": True,
                "user_data": user_data,
                "security_rules": security_rules,
            },
        )
        instances = data.get("instances")
        if not isinstance(instances, list) or not instances or not isinstance(instances[0], dict):
            raise BackendUnavailableError("Hyperstack create returned no instance")
        return instances[0]

    def list_vms(self) -> list[dict[str, Any]]:
        raw = self._request("GET", "/core/virtual-machines").get("instances")
        if not isinstance(raw, list):
            raise BackendUnavailableError("Hyperstack returned malformed instances data")
        return [item for item in raw if isinstance(item, dict)]

    def get_vm(self, vm_id: str) -> dict[str, Any]:
        raw = self._request("GET", f"/core/virtual-machines/{vm_id}").get("instance")
        if not isinstance(raw, dict):
            raise BackendUnavailableError("Hyperstack returned malformed instance data")
        return raw

    def delete_vm(self, vm_id: str) -> None:
        self._request("DELETE", f"/core/virtual-machines/{vm_id}")

    def _request(
        self, method: str, path: str, *, body: dict[str, Any] | None = None, bare: bool = False
    ) -> Any:
        url = f"{self.config.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                # Hyperstack authenticates with a bare `api_key` header.
                "api_key": self.config.api_key,
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
                f"Hyperstack API {method} {path} failed with HTTP {exc.code}: {detail}",
                status=exc.code,
            ) from exc
        except URLError as exc:
            raise BackendUnavailableError(f"Hyperstack API is unreachable: {exc}") from exc
        except TimeoutError as exc:
            raise BackendUnavailableError("Hyperstack API request timed out") from exc
        try:
            parsed = json.loads(payload) if payload else {}
        except json.JSONDecodeError as exc:
            raise BackendUnavailableError("Hyperstack API returned invalid JSON") from exc
        if bare:
            return parsed
        if not isinstance(parsed, dict):
            raise BackendUnavailableError("Hyperstack API returned a non-object response")
        return parsed
