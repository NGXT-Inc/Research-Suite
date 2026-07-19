"""Small stdlib client for the Lambda Cloud API."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ....sandbox_backend import BackendUnavailableError
from .config import LambdaCloudConfig


class LambdaCloudClient:
    def __init__(self, *, config: LambdaCloudConfig | None = None, timeout: float = 30.0) -> None:
        self.config = config or LambdaCloudConfig.from_env()
        self.timeout = timeout

    def list_instance_types(self) -> dict[str, Any]:
        data = self._request("GET", "/instance-types")
        raw = data.get("data")
        if not isinstance(raw, dict):
            raise BackendUnavailableError("Lambda Cloud returned malformed instance-types data")
        return raw

    def list_instances(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/instances")
        raw = data.get("data")
        if not isinstance(raw, list):
            raise BackendUnavailableError("Lambda Cloud returned malformed instances data")
        return [item for item in raw if isinstance(item, dict)]

    def get_instance(self, instance_id: str) -> dict[str, Any]:
        data = self._request("GET", f"/instances/{instance_id}")
        raw = data.get("data")
        if not isinstance(raw, dict):
            raise BackendUnavailableError("Lambda Cloud returned malformed instance data")
        return raw

    def add_ssh_key(self, *, name: str, public_key: str) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/ssh-keys",
            body={"name": name, "public_key": public_key},
        )
        raw = data.get("data")
        if not isinstance(raw, dict):
            raise BackendUnavailableError("Lambda Cloud returned malformed SSH key data")
        return raw

    def list_ssh_keys(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/ssh-keys")
        raw = data.get("data")
        if not isinstance(raw, list):
            raise BackendUnavailableError("Lambda Cloud returned malformed SSH keys data")
        return [item for item in raw if isinstance(item, dict)]

    def delete_ssh_key(self, key_id: str) -> None:
        self._request("DELETE", f"/ssh-keys/{key_id}")

    def launch_instance(
        self,
        *,
        region_name: str,
        instance_type_name: str,
        ssh_key_name: str,
        name: str,
        user_data: str,
    ) -> str:
        data = self._request(
            "POST",
            "/instance-operations/launch",
            body={
                "region_name": region_name,
                "instance_type_name": instance_type_name,
                "ssh_key_names": [ssh_key_name],
                "file_system_names": [],
                "quantity": 1,
                "name": name,
                "hostname": name,
                "user_data": user_data,
            },
        )
        raw = data.get("data")
        if not isinstance(raw, dict):
            raise BackendUnavailableError("Lambda Cloud returned malformed launch data")
        ids = raw.get("instance_ids")
        if not isinstance(ids, list) or not ids or not isinstance(ids[0], str):
            raise BackendUnavailableError("Lambda Cloud launch returned no instance id")
        return ids[0]

    def terminate_instances(self, instance_ids: list[str]) -> list[dict[str, Any]]:
        data = self._request(
            "POST",
            "/instance-operations/terminate",
            body={"instance_ids": instance_ids},
        )
        raw = data.get("data")
        if not isinstance(raw, dict):
            raise BackendUnavailableError("Lambda Cloud returned malformed terminate data")
        terminated = raw.get("terminated_instances")
        if not isinstance(terminated, list):
            raise BackendUnavailableError("Lambda Cloud returned malformed terminated instances data")
        return [item for item in terminated if isinstance(item, dict)]

    def _request(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
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
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - fixed API URL from config
                body = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BackendUnavailableError(
                f"Lambda Cloud API {method} {path} failed with HTTP {exc.code}: {detail}",
                status=exc.code,
            ) from exc
        except URLError as exc:
            raise BackendUnavailableError(f"Lambda Cloud API is unreachable: {exc}") from exc
        except TimeoutError as exc:
            raise BackendUnavailableError("Lambda Cloud API request timed out") from exc
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            raise BackendUnavailableError("Lambda Cloud API returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise BackendUnavailableError("Lambda Cloud API returned a non-object response")
        return parsed
