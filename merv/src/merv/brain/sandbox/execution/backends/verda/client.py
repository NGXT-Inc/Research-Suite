"""Small stdlib client for the Verda (DataCrunch) API.

Auth is OAuth2 client-credentials: the client mints a bearer token lazily,
caches it until shortly before expiry, and refreshes-and-replays once on 401.
"""

from __future__ import annotations

import time
from typing import Any

from .._http import request_json
from ....sandbox_backend import BackendUnavailableError
from .config import VerdaCloudConfig


# Refresh this many seconds before the token's stated expiry.
TOKEN_EXPIRY_SLACK_SECONDS = 60.0


class VerdaClient:
    def __init__(
        self, *, config: VerdaCloudConfig | None = None, timeout: float = 60.0
    ) -> None:
        self.config = config or VerdaCloudConfig.from_env()
        self.timeout = timeout
        self._token = ""
        self._token_expires_at = 0.0

    def list_instance_types(self) -> list[dict[str, Any]]:
        raw = self._request("GET", "/v1/instance-types")
        if not isinstance(raw, list):
            raise BackendUnavailableError("Verda returned malformed instance-types data")
        return [item for item in raw if isinstance(item, dict)]

    def list_availability(self) -> list[dict[str, Any]]:
        raw = self._request("GET", "/v1/instance-availability")
        if not isinstance(raw, list):
            raise BackendUnavailableError("Verda returned malformed availability data")
        return [item for item in raw if isinstance(item, dict)]

    def add_ssh_key(self, *, name: str, key: str) -> str:
        raw = self._request("POST", "/v1/ssh-keys", body={"name": name, "key": key})
        if not isinstance(raw, str) or not raw:
            raise BackendUnavailableError("Verda returned no SSH key id")
        return raw

    def list_ssh_keys(self) -> list[dict[str, Any]]:
        raw = self._request("GET", "/v1/ssh-keys")
        if not isinstance(raw, list):
            raise BackendUnavailableError("Verda returned malformed SSH keys data")
        return [item for item in raw if isinstance(item, dict)]

    def delete_ssh_key(self, key_id: str) -> None:
        self._request("DELETE", f"/v1/ssh-keys/{key_id}")

    def add_script(self, *, name: str, script: str) -> str:
        raw = self._request(
            "POST", "/v1/scripts", body={"name": name, "script": script}
        )
        if not isinstance(raw, str) or not raw:
            raise BackendUnavailableError("Verda returned no startup script id")
        return raw

    def list_scripts(self) -> list[dict[str, Any]]:
        raw = self._request("GET", "/v1/scripts")
        if not isinstance(raw, list):
            raise BackendUnavailableError("Verda returned malformed scripts data")
        return [item for item in raw if isinstance(item, dict)]

    def delete_script(self, script_id: str) -> None:
        self._request("DELETE", f"/v1/scripts/{script_id}")

    def deploy_instance(
        self,
        *,
        instance_type: str,
        image: str,
        hostname: str,
        description: str,
        location_code: str,
        ssh_key_ids: list[str],
        startup_script_id: str,
    ) -> str:
        raw = self._request(
            "POST",
            "/v1/instances",
            body={
                "instance_type": instance_type,
                "image": image,
                "hostname": hostname,
                "description": description,
                "location_code": location_code,
                "ssh_key_ids": ssh_key_ids,
                "startup_script_id": startup_script_id,
            },
        )
        # 202 body is the bare instance id as a JSON string.
        if not isinstance(raw, str) or not raw:
            raise BackendUnavailableError("Verda deploy returned no instance id")
        return raw

    def get_instance(self, instance_id: str) -> dict[str, Any]:
        raw = self._request("GET", f"/v1/instances/{instance_id}")
        if not isinstance(raw, dict):
            raise BackendUnavailableError("Verda returned malformed instance data")
        return raw

    def list_instances(self) -> list[dict[str, Any]]:
        raw = self._request("GET", "/v1/instances")
        if not isinstance(raw, list):
            raise BackendUnavailableError("Verda returned malformed instances data")
        return [item for item in raw if isinstance(item, dict)]

    def perform_action(self, *, instance_id: str, action: str) -> None:
        self._request(
            "PUT", "/v1/instances", body={"id": instance_id, "action": action}
        )

    # ---------- auth ----------

    def _bearer_token(self, *, force: bool = False) -> str:
        if force or not self._token or time.monotonic() >= self._token_expires_at:
            payload = self._raw_request(
                "POST",
                "/v1/oauth2/token",
                body={
                    "grant_type": "client_credentials",
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                },
                token="",
            )
            if not isinstance(payload, dict) or not payload.get("access_token"):
                raise BackendUnavailableError("Verda OAuth2 returned no access token")
            self._token = str(payload["access_token"])
            expires_in = float(payload.get("expires_in") or 0.0)
            self._token_expires_at = time.monotonic() + max(
                expires_in - TOKEN_EXPIRY_SLACK_SECONDS, 30.0
            )
        return self._token

    def _request(
        self, method: str, path: str, *, body: dict[str, Any] | None = None
    ) -> Any:
        try:
            return self._raw_request(method, path, body=body, token=self._bearer_token())
        except BackendUnavailableError as exc:
            if exc.status != 401:
                raise
        # Expired/revoked token: mint a fresh one and replay once.
        return self._raw_request(
            method, path, body=body, token=self._bearer_token(force=True)
        )

    def _raw_request(
        self, method: str, path: str, *, body: dict[str, Any] | None, token: str
    ) -> Any:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "merv/0.0013",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return request_json(
            provider="Verda",
            method=method,
            base_url=self.config.base_url,
            path=path,
            body=body,
            headers=headers,
            timeout=self.timeout,
        )
