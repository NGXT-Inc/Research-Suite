"""Ray Jobs API adapters."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ...errors import BackendUnavailableError


class RayClient(Protocol):
    supports_local_working_dir_upload: bool

    def submit_job(
        self, *, entrypoint: str, runtime_env: dict[str, Any], metadata: dict[str, str]
    ) -> str: ...

    def get_job_status(self, *, runtime_job_id: str) -> str: ...

    def get_job_logs(self, *, runtime_job_id: str) -> str: ...

    def stop_job(self, *, runtime_job_id: str) -> bool: ...

    def list_jobs(self) -> dict[str, Any]: ...

    def health(self) -> dict[str, Any]: ...


@dataclass(kw_only=True)
class RayRestJobClient:
    """Small REST client for the Ray Jobs API."""

    address: str = "http://127.0.0.1:8265"
    timeout: float = 10.0
    supports_local_working_dir_upload: bool = False

    def submit_job(
        self, *, entrypoint: str, runtime_env: dict[str, Any], metadata: dict[str, str]
    ) -> str:
        payload = {
            "entrypoint": entrypoint,
            "runtime_env": runtime_env,
            "metadata": metadata,
        }
        response = self._request(method="POST", path="/api/jobs/", payload=payload)
        return str(response.get("job_id") or response["submission_id"])

    def get_job_status(self, *, runtime_job_id: str) -> str:
        return str(self._request(method="GET", path=f"/api/jobs/{runtime_job_id}")["status"])

    def get_job_logs(self, *, runtime_job_id: str) -> str:
        logs = str(
            self._request(method="GET", path=f"/api/jobs/{runtime_job_id}/logs").get("logs", "")
        )
        if logs:
            return logs
        info = self._request(method="GET", path=f"/api/jobs/{runtime_job_id}")
        return str(info.get("message", ""))

    def stop_job(self, *, runtime_job_id: str) -> bool:
        response = self._request(method="POST", path=f"/api/jobs/{runtime_job_id}/stop", payload={})
        return bool(response.get("stopped", False))

    def list_jobs(self) -> dict[str, Any]:
        return self._request(method="GET", path="/api/jobs/")

    def health(self) -> dict[str, Any]:
        try:
            return {"ok": True, "name": "ray", "address": self.address, "jobs": self.list_jobs()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "name": "ray", "address": self.address, "error": str(exc)}

    def _request(
        self, *, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self.address.rstrip("/") + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BackendUnavailableError(
                f"Ray Jobs API HTTP {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise BackendUnavailableError(
                f"Ray Jobs API unavailable at {self.address}: {exc.reason}"
            ) from exc
        return json.loads(raw) if raw else {}


class RaySdkJobClient:
    """Ray SDK adapter. Prefer this for local working_dir uploads."""

    def __init__(self, *, address: str = "http://127.0.0.1:8265") -> None:
        try:
            from ray.job_submission import JobSubmissionClient
        except ImportError as exc:
            raise BackendUnavailableError("ray SDK is not installed") from exc

        self.address = address
        self.client = JobSubmissionClient(address)
        self.supports_local_working_dir_upload = True

    def submit_job(
        self, *, entrypoint: str, runtime_env: dict[str, Any], metadata: dict[str, str]
    ) -> str:
        return str(
            self.client.submit_job(entrypoint=entrypoint, runtime_env=runtime_env, metadata=metadata)
        )

    def get_job_status(self, *, runtime_job_id: str) -> str:
        return str(self.client.get_job_status(runtime_job_id))

    def get_job_logs(self, *, runtime_job_id: str) -> str:
        return str(self.client.get_job_logs(runtime_job_id))

    def stop_job(self, *, runtime_job_id: str) -> bool:
        return bool(self.client.stop_job(runtime_job_id))

    def list_jobs(self) -> dict[str, Any]:
        jobs = self.client.list_jobs()
        return (
            {str(key): _to_jsonable(value=value) for key, value in jobs.items()}
            if isinstance(jobs, dict)
            else {"jobs": _to_jsonable(value=jobs)}
        )

    def health(self) -> dict[str, Any]:
        try:
            return {"ok": True, "name": "ray", "address": self.address, "jobs": self.list_jobs()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "name": "ray", "address": self.address, "error": str(exc)}


def _to_jsonable(*, value: Any) -> Any:
    if hasattr(value, "dict"):
        return value.dict()
    if hasattr(value, "__dict__"):
        return {
            key: _to_jsonable(value=val)
            for key, val in value.__dict__.items()
            if not key.startswith("_")
        }
    if isinstance(value, dict):
        return {str(key): _to_jsonable(value=val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(value=item) for item in value]
    return str(value)
