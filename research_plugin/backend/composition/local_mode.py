"""Local-mode composition (cloud plan Phase 8).

Exactly today's ResearchPluginApp: both planes in one process, SQLite,
LocalDirBlobStore, in-process task channel, provider creds from local .env.
http_server keeps its existing ResearchPluginApp / ProjectRouter construction
for the local path; this factory exists so the mode dispatch in main() has a
uniform local branch.
"""

from __future__ import annotations

from pathlib import Path

from ..app import ResearchPluginApp
from ..config import build_object_store, build_state_store
from ..storage.service import StorageLedgerService


def build_local_app(*, repo_root: Path, db_path: Path) -> ResearchPluginApp:
    """Today's single-process app."""
    store = build_state_store(db_path=db_path)
    objects = build_object_store(default_root=repo_root / ".research_plugin")
    storage = StorageLedgerService(store=store, objects=objects) if objects else None
    return ResearchPluginApp(
        repo_root=repo_root,
        db_path=db_path,
        store=store,
        storage=storage,
    )
