#!/usr/bin/env python3
"""Rename existing MLflow experiments from the legacy rp/ namespace to merv/.

One-time deploy companion to the MLFLOW_NAMESPACE_PREFIX flip in
src/merv/brain/mlflow/tracking.py: new experiments are created as merv/<project>/<exp>,
and this script renames every existing `rp/...` experiment in place over the
MLflow REST API so name-based lookups keep resolving. Idempotent — a second run
finds nothing left to rename. `merv/...` name collisions (a re-run after a
partial rename created the new name) are reported and skipped, never clobbered.

Usage:
  python3 merv/scripts/migrate_mlflow_namespace.py [--dry-run] [--uri URI]

The tracking URI resolves like the backend's own reads: --uri wins, then
MERV_MLFLOW_SERVER_URI, then MERV_MLFLOW_TRACKING_URI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx

from merv.brain.mlflow.config import (
    resolve_mlflow_server_uri,
    resolve_mlflow_tracking_uri,
)

LEGACY_PREFIX = "rp/"
NEW_PREFIX = "merv/"
PAGE_SIZE = 200


def _search_legacy_experiments(*, client: httpx.Client, base: str) -> list[dict]:
    """Every experiment (any lifecycle stage) whose name starts with rp/."""
    experiments: list[dict] = []
    token = ""
    while True:
        payload: dict = {
            "max_results": PAGE_SIZE,
            "filter": f"name LIKE '{LEGACY_PREFIX}%'",
            "view_type": "ALL",
        }
        if token:
            payload["page_token"] = token
        response = client.post(f"{base}/api/2.0/mlflow/experiments/search", json=payload)
        response.raise_for_status()
        body = response.json()
        experiments.extend(body.get("experiments") or [])
        token = str(body.get("next_page_token") or "")
        if not token:
            return experiments


def _experiment_exists(*, client: httpx.Client, base: str, name: str) -> bool:
    response = client.get(
        f"{base}/api/2.0/mlflow/experiments/get-by-name", params={"experiment_name": name}
    )
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return True


def _rename(*, client: httpx.Client, base: str, experiment_id: str, new_name: str) -> None:
    response = client.post(
        f"{base}/api/2.0/mlflow/experiments/update",
        json={"experiment_id": experiment_id, "new_name": new_name},
    )
    response.raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the rename plan and exit")
    parser.add_argument("--uri", default="", help="MLflow tracking URI (overrides env resolution)")
    args = parser.parse_args()

    base = (
        args.uri.strip().rstrip("/")
        or resolve_mlflow_server_uri()
        or resolve_mlflow_tracking_uri()
    )
    if not base:
        print(
            "no MLflow URI: pass --uri or set MERV_MLFLOW_SERVER_URI / "
            "MERV_MLFLOW_TRACKING_URI",
            file=sys.stderr,
        )
        return 2

    renamed = skipped = failed = 0
    with httpx.Client(timeout=10.0) as client:
        legacy = _search_legacy_experiments(client=client, base=base)
        if not legacy:
            print(f"{base}: no {LEGACY_PREFIX}* experiments left — nothing to do")
            return 0
        print(f"{base}: {len(legacy)} {LEGACY_PREFIX}* experiment(s) to rename")
        for experiment in sorted(legacy, key=lambda e: str(e.get("name") or "")):
            experiment_id = str(experiment.get("experiment_id") or "")
            old_name = str(experiment.get("name") or "")
            new_name = NEW_PREFIX + old_name[len(LEGACY_PREFIX):]
            if args.dry_run:
                print(f"  would rename {old_name} -> {new_name}")
                continue
            if _experiment_exists(client=client, base=base, name=new_name):
                print(f"  SKIP {old_name}: {new_name} already exists — merge by hand")
                skipped += 1
                continue
            try:
                _rename(client=client, base=base, experiment_id=experiment_id, new_name=new_name)
            except httpx.HTTPError as exc:
                print(f"  FAIL {old_name}: {exc}", file=sys.stderr)
                failed += 1
                continue
            print(f"  renamed {old_name} -> {new_name}")
            renamed += 1
    if args.dry_run:
        return 0
    print(f"done: {renamed} renamed, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
