"""Local-mode composition (cloud plan Phase 8).

Exactly today's ResearchPluginApp: both planes in one process, SQLite,
LocalDirBlobStore, in-process task channel, provider creds from local .env.
This is the default mode forever and must stay byte-identical — so this builder
is a thin, deliberately boring pass-through. http_server keeps its existing
ResearchPluginApp / ProjectRouter construction for the local path; this factory
exists so the mode dispatch in main() has a uniform local branch.
"""

from __future__ import annotations

from pathlib import Path

from ..app import ResearchPluginApp


def build_local_app(*, repo_root: Path, db_path: Path) -> ResearchPluginApp:
    """Today's single-process app — no behavior change of any kind."""
    return ResearchPluginApp(repo_root=repo_root, db_path=db_path)
