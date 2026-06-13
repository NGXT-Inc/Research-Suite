"""The daemon loopback HTTP surface (cloud plan Phase 8, §3.3/§3.4).

The proxy talks to the daemon for the data-plane tool subset and to resolve the
repo_root to project_id mapping (so repo_root never crosses to the cloud). A
deliberately small surface:

- GET /local/route (repo_root query) resolves the project_id for a checkout
  from the daemon-local project_links; the proxy caches it and sends an
  explicit project_id on cloud calls.
- GET /health: daemon liveness plus cloud reachability (feeds sandbox.health).
- POST /local/link registers a repo_root to project_id mapping (the proxy calls
  it once the cloud has minted a project).

The surface is gated by a local daemon auth secret (risk 11): the daemon holds
the cloud token and the user private keys, so a bare loopback bind is not
enough. A unix-socket bind is the Phase 9 upgrade.

The data-plane tool EXECUTION (register_file/associate reading bytes locally and
submitting to the cloud record half; request/sync driving the worker) is the
cross-process record-forwarding seam the plan's section 3.1 names
(resource_submit_artifact). The split-mode smoke exercises it through a
shared-store two-app wiring; a true multi-process record-forwarding daemon is
finished in Phase 9.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

from . import __version__
from .control_client import ControlPlaneUnreachableError
from .utils import ResearchPluginError, ValidationError


def create_daemon_loopback_app(*, daemon: Any) -> FastAPI:
    http = FastAPI(title="Research Plugin Daemon (loopback)", version=__version__)
    secret = daemon.loopback_secret

    def _check_secret(authorization: str | None) -> None:
        # Local auth secret (risk 11). The proxy sends it as a bearer; a missing
        # or wrong secret is refused so another local process cannot drive the
        # credential-holding daemon.
        token = ""
        if authorization and authorization[:7].lower() == "bearer ":
            token = authorization[7:].strip()
        if not secret or token != secret:
            raise _Unauthorized()

    @http.exception_handler(ResearchPluginError)
    async def _research_error(_request: Request, exc: ResearchPluginError) -> JSONResponse:
        return JSONResponse(
            {"detail": exc.message, "error_code": exc.error_code, **exc.details},
            status_code=400,
        )

    @http.exception_handler(_Unauthorized)
    async def _unauth(_request: Request, _exc: "_Unauthorized") -> JSONResponse:
        return JSONResponse(
            {"detail": "daemon loopback secret required", "error_code": "unauthorized"},
            status_code=401,
        )

    @http.get("/health")
    def health(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _check_secret(authorization)
        cloud_ok = True
        try:
            daemon.control.list_tools()
        except ControlPlaneUnreachableError:
            cloud_ok = False
        except Exception:  # noqa: BLE001
            cloud_ok = False
        return {
            "ok": True,
            "version": __version__,
            "mode": "daemon",
            "cloud_reachable": cloud_ok,
        }

    @http.get("/local/route")
    def local_route(
        repo_root: str = Query(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _check_secret(authorization)
        if not repo_root:
            raise ValidationError("repo_root is required", details={"field": "repo_root"})
        project_id = daemon.project_links.project_for_repo(repo_root=repo_root)
        return {"repo_root": repo_root, "project_id": project_id, "exists": project_id is not None}

    @http.post("/local/link")
    async def local_link(
        request: Request, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _check_secret(authorization)
        body = await request.json()
        repo_root = str((body or {}).get("repo_root") or "")
        project_id = str((body or {}).get("project_id") or "")
        if not repo_root or not project_id:
            raise ValidationError("repo_root and project_id are required")
        daemon.project_links.link(repo_root=repo_root, project_id=project_id)
        return {"linked": True, "repo_root": repo_root, "project_id": project_id}

    return http


class _Unauthorized(Exception):
    """Loopback secret missing or wrong (mapped to 401)."""
