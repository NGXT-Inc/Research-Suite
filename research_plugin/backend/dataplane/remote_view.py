"""The daemon's HTTP window onto the control plane.

In-process, ``InProcessTaskChannel`` dispatches tasks. In split mode the daemon
talks to the cloud over HTTP: ``HttpControlPlaneView`` long-polls
``/api/daemon/tasks`` for data-plane work and posts acks back. The cloud never
dials in — every call here is daemon-initiated.
"""

from __future__ import annotations

import json
from typing import Any
from urllib import error as urllib_error
from urllib.request import Request, urlopen

from ..control.control_client import ControlPlaneUnreachableError, HttpControlPlaneClient


class HttpControlPlaneView:
    """Daemon-side HTTP client for task long-poll/ack and control views.

    Reuses the control client's base_url; the task poll uses a longer
    per-request timeout than ordinary tool calls because it is a long poll the
    cloud holds open.
    """

    def __init__(
        self,
        *,
        control: HttpControlPlaneClient,
        worker: Any,
        client_id: str,
        poll_timeout_seconds: float = 35.0,
    ) -> None:
        self._control = control
        self._worker = worker
        self._client_id = client_id
        self._poll_timeout = poll_timeout_seconds

    # ---- task long-poll + ack ----

    def poll_task(self, wait_seconds: float) -> dict[str, Any] | None:
        try:
            body = self._request(
                method="GET",
                path=(
                    f"/api/daemon/tasks?client_id={self._client_id}"
                    f"&wait={int(wait_seconds)}"
                ),
                timeout=max(self._poll_timeout, wait_seconds + 5.0),
            )
        except ControlPlaneUnreachableError:
            return None
        task = body.get("task")
        return task if isinstance(task, dict) else None

    def ack_task(
        self, *, task_id: str, ok: bool, result: Any = None, error: str | None = None
    ) -> None:
        payload: dict[str, Any] = {"ok": ok}
        if result is not None:
            payload["result"] = result
        if error is not None:
            payload["error"] = error
        self._request(method="POST", path=f"/api/daemon/tasks/{task_id}/ack", body=payload)

    # ---- transport (reuses the control client's base) ----

    def _request(
        self,
        *,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        url = self._control.base_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        # Version/compat handshake (cloud plan Phase 9): stamp the daemon's
        # version so the control plane can reject below-floor clients with an
        # actionable upgrade error. Sourced from the package version.
        from .. import __version__ as _client_version
        from ..version import CLIENT_VERSION_HEADER

        headers[CLIENT_VERSION_HEADER] = _client_version
        req = Request(url, data=data, method=method, headers=headers)
        try:
            with urlopen(req, timeout=timeout or self._control.timeout_seconds) as response:
                raw = response.read()
        except urllib_error.URLError as exc:
            raise ControlPlaneUnreachableError(
                f"control plane unreachable at {self._control.base_url}",
                details={"path": path},
            ) from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
