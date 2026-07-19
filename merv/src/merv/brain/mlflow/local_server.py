"""Managed local MLflow process for the local backend."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

import httpx

from .tracking import CentralMlflowService


@dataclass
class LocalMlflowServer:
    """Start and stop the backend-owned MLflow server in local mode."""

    root: Path
    host: str = "127.0.0.1"
    preferred_port: int = 5000
    startup_timeout_s: float = 20.0
    process: subprocess.Popen[bytes] | None = field(default=None, init=False)
    _log_handle: BinaryIO | None = field(default=None, init=False)
    _url: str = field(default="", init=False)

    def start(self) -> CentralMlflowService:
        configured = CentralMlflowService.from_env()
        if configured.tracking_uri or configured.mode == "external":
            return configured

        self.root.mkdir(parents=True, exist_ok=True)
        artifact_root = self.root / "artifacts"
        artifact_root.mkdir(parents=True, exist_ok=True)
        db_path = self.root / "mlflow.db"
        port = self._choose_port()
        self._url = f"http://{self.host}:{port}"
        log_path = self.root / "mlflow.log"
        command = [
            sys.executable,
            "-m",
            "mlflow",
            "server",
            "--host",
            self.host,
            "--port",
            str(port),
            "--backend-store-uri",
            f"sqlite:///{db_path}",
            "--artifacts-destination",
            artifact_root.resolve().as_uri(),
            "--serve-artifacts",
        ]
        try:
            self._log_handle = log_path.open("ab", buffering=0)
            self.process = subprocess.Popen(
                command,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError:
            self._close_log()
            return self._failed_service(log_path)
        if not self._wait_until_ready():
            self.stop()
            return self._failed_service(log_path)
        return CentralMlflowService(
            mode="managed",
            tracking_uri=self._url,
            server_uri=self._url,
            dashboard_url=self._url,
            health_check=self.is_alive,
        )

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            self._signal_process(process, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._signal_process(process, signal.SIGKILL)
                with suppress(Exception):
                    process.wait(timeout=5)
        self._close_log()

    def _signal_process(
        self, process: subprocess.Popen[bytes], sig: signal.Signals
    ) -> None:
        try:
            os.killpg(process.pid, sig)
        except OSError:
            with suppress(Exception):
                process.send_signal(sig)

    def is_alive(self) -> bool:
        process = self.process
        return bool(process is not None and process.poll() is None)

    def _wait_until_ready(self) -> bool:
        deadline = time.monotonic() + self.startup_timeout_s
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                return False
            try:
                response = httpx.get(f"{self._url}/health", timeout=0.5)
                if response.status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        return False

    def _choose_port(self) -> int:
        if self.preferred_port > 0 and self._can_bind(self.preferred_port):
            return self.preferred_port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((self.host, 0))
            return int(sock.getsockname()[1])

    def _can_bind(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((self.host, port))
            except OSError:
                return False
            return True

    def _close_log(self) -> None:
        if self._log_handle is not None:
            with suppress(Exception):
                self._log_handle.close()
            self._log_handle = None

    def _failed_service(self, log_path: Path) -> CentralMlflowService:
        return CentralMlflowService(
            mode="managed",
            note=f"Managed MLflow failed to start; see {log_path}.",
        )
