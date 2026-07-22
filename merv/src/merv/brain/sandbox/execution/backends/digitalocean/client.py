"""Small stdlib client for the DigitalOcean API."""

from __future__ import annotations

from typing import Any

from .._http import bearer_json_headers, request_json
from ....sandbox_backend import BackendUnavailableError
from .config import DigitalOceanCloudConfig


class DigitalOceanClient:
    def __init__(
        self, *, config: DigitalOceanCloudConfig | None = None, timeout: float = 60.0
    ) -> None:
        self.config = config or DigitalOceanCloudConfig.from_env()
        self.timeout = timeout

    def list_sizes(self) -> list[dict[str, Any]]:
        raw = self._request("GET", "/sizes?per_page=200").get("sizes")
        if not isinstance(raw, list):
            raise BackendUnavailableError("DigitalOcean returned malformed sizes data")
        return [item for item in raw if isinstance(item, dict)]

    def create_ssh_key(self, *, name: str, public_key: str) -> dict[str, Any]:
        data = self._request(
            "POST", "/account/keys", body={"name": name, "public_key": public_key}
        )
        raw = data.get("ssh_key")
        if not isinstance(raw, dict):
            raise BackendUnavailableError("DigitalOcean returned malformed SSH key data")
        return raw

    def list_ssh_keys(self) -> list[dict[str, Any]]:
        raw = self._request("GET", "/account/keys?per_page=200").get("ssh_keys")
        if not isinstance(raw, list):
            raise BackendUnavailableError("DigitalOcean returned malformed SSH keys data")
        return [item for item in raw if isinstance(item, dict)]

    def delete_ssh_key(self, key_id: int | str) -> None:
        self._request("DELETE", f"/account/keys/{key_id}")

    def create_droplet(
        self,
        *,
        name: str,
        region: str,
        size: str,
        image: str,
        ssh_key_ids: list[int | str],
        user_data: str,
    ) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/droplets",
            body={
                "name": name,
                "region": region,
                "size": size,
                "image": image,
                "ssh_keys": ssh_key_ids,
                "user_data": user_data,
                "tags": ["merv-sandbox"],
            },
        )
        raw = data.get("droplet")
        if not isinstance(raw, dict) or not raw.get("id"):
            raise BackendUnavailableError("DigitalOcean create returned no droplet")
        return raw

    def list_droplets(self) -> list[dict[str, Any]]:
        raw = self._request(
            "GET", "/droplets?per_page=200&tag_name=merv-sandbox"
        ).get("droplets")
        if not isinstance(raw, list):
            raise BackendUnavailableError("DigitalOcean returned malformed droplets data")
        return [item for item in raw if isinstance(item, dict)]

    def get_droplet(self, droplet_id: str) -> dict[str, Any]:
        raw = self._request("GET", f"/droplets/{droplet_id}").get("droplet")
        if not isinstance(raw, dict):
            raise BackendUnavailableError("DigitalOcean returned malformed droplet data")
        return raw

    def delete_droplet(self, droplet_id: str) -> None:
        self._request("DELETE", f"/droplets/{droplet_id}")

    def _request(
        self, method: str, path: str, *, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return request_json(
            provider="DigitalOcean",
            method=method,
            base_url=self.config.base_url,
            path=path,
            body=body,
            headers=bearer_json_headers(self.config.token, "merv/0.0013"),
            timeout=self.timeout,
            require_object=True,
        )
