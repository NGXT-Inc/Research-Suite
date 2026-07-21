"""Hosted credential selection and silent login-session refresh."""

from __future__ import annotations

import json
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.request import Request


OpenUrl = Callable[..., Any]


class CredentialProvider:
    def __init__(
        self,
        *,
        api_key: str,
        control_url: Optional[str],
        config_path: Optional[Path],
        timeout_seconds: float,
        opener: OpenUrl,
    ) -> None:
        self.api_key = api_key
        self.control_url = control_url
        self.config_path = config_path
        self.timeout_seconds = timeout_seconds
        self._opener = opener
        self._session: Optional[dict[str, Any]] = None

    def headers(self, *, is_cloud: bool, client_version: str) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-RP-Client-Version": client_version,
        }
        bearer = self.bearer() if is_cloud else ""
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return headers

    def bearer(self) -> str:
        if self.api_key:
            return self.api_key
        session = self.session()
        if not session:
            return ""
        if float(session.get("expires_at") or 0) - time.time() < 300:
            session = self.refresh(session) or session
        return str(session.get("access_token") or "")

    def session(self) -> Optional[dict[str, Any]]:
        if self._session is None:
            try:
                raw = (
                    json.loads(self.config_path.read_text(encoding="utf-8"))
                    if self.config_path
                    else {}
                )
            except (OSError, ValueError):
                raw = {}
            self._session = raw if raw.get("access_token") else {}
        return self._session or None

    def refresh(self, session: dict[str, Any]) -> Optional[dict[str, Any]]:
        refresh_token = str(session.get("refresh_token") or "")
        if not refresh_token or not self.control_url:
            return None
        request = Request(
            f"{self.control_url}/api/sdk/auth/refresh",
            data=json.dumps({"refresh_token": refresh_token}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                fresh = json.loads(response.read().decode("utf-8"))
        except (
            Exception
        ):  # noqa: BLE001 - stale token still yields the login hint upstream
            return None
        updated = dict(session)
        updated.update(
            access_token=str(fresh.get("access_token") or ""),
            refresh_token=str(fresh.get("refresh_token") or refresh_token),
            expires_at=int(time.time()) + int(fresh.get("expires_in") or 3600),
        )
        self._session = updated
        if self.config_path is not None:
            with suppress(OSError):
                tmp = self.config_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(updated), encoding="utf-8")
                tmp.chmod(0o600)
                tmp.replace(self.config_path)
        return updated
