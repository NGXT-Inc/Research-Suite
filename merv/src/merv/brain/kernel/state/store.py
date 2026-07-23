"""Record-store state management: dialect-neutral base + the SQLite dialect.

``BaseStateStore`` defines the contract the services were written against;
``StateStore`` (= ``SqliteStateStore``) is the local-mode SQLite dialect and
the historical default. The Postgres dialect for the cloud control plane
lives in ``dialects.py`` (cloud plan Phase 6).
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

from ..events import StoredEvent, freeze_json_object
from ..secret_tokens import hash_secret
from ..utils import NotFoundError, ValidationError
from ..utils import new_id
from ..utils import now_iso


class Row(Protocol):
    """Mapping-shaped database row shared by the SQLite and Postgres dialects."""

    def __getitem__(self, key: str) -> Any:
        ...

    def keys(self) -> Iterable[str]:
        ...


class ResultCursor(Protocol):
    """Cursor result surface used by record services."""

    def fetchone(self) -> Row | Mapping[str, Any] | None:
        ...

    def fetchall(self) -> list[Row | Mapping[str, Any]]:
        ...


class Connection(Protocol):
    """Small database connection surface exposed through ``BaseStateStore``."""

    def execute(
        self, sql: str, parameters: Sequence[Any] = ()
    ) -> ResultCursor:
        ...

    def __enter__(self) -> Connection:
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        ...

    def commit(self) -> None:
        ...

    def rollback(self) -> None:
        ...

    def close(self) -> None:
        ...


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  -- Per-project policy knobs (e.g. require_verified_reviews), JSON dict.
  settings_json TEXT NOT NULL DEFAULT '{}',
  -- Tenancy (cloud plan Phase 6): ownership lives on the project row; every
  -- other table reaches its tenant through project_id. The current private
  -- deployment uses the fixed 'local' tenant until real user auth lands.
  tenant_id TEXT NOT NULL DEFAULT 'local',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_members (
  -- Access layer for authenticated (hosted) mode: user_id is a Supabase
  -- auth.users UUID; a row grants full member access to the project. The
  -- local surface carries no user_id, so membership never filters it.
  project_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  added_at TEXT NOT NULL,
  PRIMARY KEY (project_id, user_id),
  FOREIGN KEY(project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS claims (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  statement TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  confidence TEXT NOT NULL DEFAULT 'medium',
  created_at TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS experiments (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  intent TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt_index INTEGER NOT NULL DEFAULT 1,
  revision_context TEXT NOT NULL DEFAULT '',
  conclusion TEXT NOT NULL DEFAULT '',
  mlflow_run_id TEXT NOT NULL DEFAULT '',
  mlflow_run_name TEXT NOT NULL DEFAULT '',
  mlflow_run_status TEXT NOT NULL DEFAULT '',
  mlflow_run_artifact_uri TEXT NOT NULL DEFAULT '',
  mlflow_run_created_at TEXT,
  mlflow_run_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS experiment_claims (
  experiment_id TEXT NOT NULL,
  claim_id TEXT NOT NULL,
  PRIMARY KEY(experiment_id, claim_id)
);

CREATE TABLE IF NOT EXISTS resources (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  path TEXT NOT NULL,
	  kind TEXT NOT NULL,
	  title TEXT NOT NULL DEFAULT '',
	  current_version_id TEXT,
	  version_token TEXT NOT NULL,
	  mtime_ns INTEGER NOT NULL,
	  size_bytes INTEGER NOT NULL,
  observed_at TEXT NOT NULL,
  git_commit TEXT,
  missing INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  created_by TEXT NOT NULL DEFAULT 'codex',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
	  UNIQUE(project_id, path),
	  FOREIGN KEY(project_id) REFERENCES projects(id)
	);

	CREATE TABLE IF NOT EXISTS resource_versions (
	  id TEXT PRIMARY KEY,
	  resource_id TEXT NOT NULL,
	  project_id TEXT NOT NULL,
	  path TEXT NOT NULL,
	  content_sha256 TEXT NOT NULL,
	  size_bytes INTEGER NOT NULL,
	  mtime_ns INTEGER NOT NULL,
	  observed_at TEXT NOT NULL,
	  content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
	  created_by TEXT NOT NULL DEFAULT 'codex',
	  created_at TEXT NOT NULL,
	  -- Explicit insertion-order column (cloud plan Phase 6): replaces SQLite
	  -- rowid ordering so the same queries run on Postgres. Service inserts set
	  -- it via next_created_seq(); the DEFAULT 0 only keeps legacy convergence
	  -- (ALTER TABLE ADD COLUMN) and raw test inserts valid.
	  created_seq INTEGER NOT NULL DEFAULT 0,
	  FOREIGN KEY(resource_id) REFERENCES resources(id),
	  FOREIGN KEY(project_id) REFERENCES projects(id)
	);

	CREATE TABLE IF NOT EXISTS resource_associations (
	  id TEXT PRIMARY KEY,
	  resource_id TEXT NOT NULL,
	  version_id TEXT,
	  target_type TEXT NOT NULL,
	  target_id TEXT NOT NULL,
	  role TEXT NOT NULL,
  attempt_index INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  -- Insertion-order column replacing rowid ordering (cloud plan Phase 6).
  -- An upsert keeps its original created_seq, exactly like rowid did.
  created_seq INTEGER NOT NULL DEFAULT 0,
  UNIQUE(resource_id, target_type, target_id, role, attempt_index),
	  FOREIGN KEY(resource_id) REFERENCES resources(id),
	  FOREIGN KEY(version_id) REFERENCES resource_versions(id)
	);

CREATE TABLE IF NOT EXISTS storage_objects (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  name TEXT NOT NULL,
  version INTEGER NOT NULL,
  kind TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
  namespace TEXT NOT NULL,
  status TEXT NOT NULL,
  upload_id TEXT,
  expires_at TEXT,
  created_by TEXT NOT NULL DEFAULT 'codex',
  producing_experiment_id TEXT NOT NULL DEFAULT '',
  producing_run TEXT NOT NULL DEFAULT '',
  source_uri TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_accessed_at TEXT,
  created_seq INTEGER NOT NULL DEFAULT 0,
  UNIQUE(project_id, name, version),
  FOREIGN KEY(project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS review_requests (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  role TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  -- Capability hardening (cloud plan Phase 7): the reviewer capability is
  -- stored HASHED (sha256 of the minted token), never in plaintext. The
  -- plaintext is returned once to the caller at request time; review.start
  -- resolves the request by hashing the presented token and comparing with a
  -- constant-time check. Replaces the pre-Phase-7 plaintext `capability`
  -- column (legacy DBs converge in _ensure_forward_schema).
  capability_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  target_snapshot_id TEXT NOT NULL,
  producer_session_id TEXT NOT NULL DEFAULT '',
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  -- Insertion-order column replacing rowid ordering (cloud plan Phase 6).
  created_seq INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS review_sessions (
  id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL,
  declared_agent TEXT NOT NULL DEFAULT '',
  caller_session_id TEXT NOT NULL DEFAULT '',
  -- Principal binding (cloud plan Phase 7): the authenticated tenant that
  -- started the session, so cross-tenant review hijacking is rejected at
  -- start. Local mode (single tenant, auth off) writes the 'local' tenant —
  -- a no-op. Empty on legacy rows that predate the column.
  tenant_id TEXT NOT NULL DEFAULT '',
  independence TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(request_id) REFERENCES review_requests(id)
);

CREATE TABLE IF NOT EXISTS reviews (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  target_snapshot_id TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  role TEXT NOT NULL,
  verdict TEXT NOT NULL,
  return_to TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  -- Researcher-facing TLDR (July 2026): 1-3 plain sentences, the first thing
  -- the human reads on the experiment page. Required on new submissions;
  -- empty on rows that predate the column (legacy DBs converge below).
  synopsis TEXT NOT NULL DEFAULT '',
  findings_json TEXT NOT NULL DEFAULT '[]',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  -- Insertion-order column replacing rowid ordering (cloud plan Phase 6).
  created_seq INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(project_id) REFERENCES projects(id),
  FOREIGN KEY(request_id) REFERENCES review_requests(id),
  FOREIGN KEY(session_id) REFERENCES review_sessions(id)
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  type TEXT NOT NULL,
  target_type TEXT NOT NULL DEFAULT '',
  target_id TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS reflections (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  attempt_index INTEGER NOT NULL DEFAULT 1,
  revision_context TEXT NOT NULL DEFAULT '',
  -- The declared reflection roster: 5 lenses (3 core + 2 wave-authored), each
  -- {id, title, charter, core, why_distinct}. JSON list, fixed at create.
  roster_json TEXT NOT NULL DEFAULT '[]',
  -- The corpus snapshot taken at create: terminal experiments (id + attempt +
  -- status) and claim statuses at that moment. The reflection review judges the
  -- story against this fixed corpus, and staleness is computed against it.
  corpus_json TEXT NOT NULL DEFAULT '{}',
  published_at TEXT,
  -- Version id of the project logic graph association at publish time, so the
  -- single living graph file still yields an immutable per-wave history.
  published_graph_version_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  -- Insertion-order column replacing rowid ordering (cloud plan Phase 6).
  created_seq INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS reflection_claim_changes (
  reflection_id TEXT NOT NULL,
  claim_id TEXT NOT NULL,
  op TEXT NOT NULL,
  claim_key TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY(reflection_id, claim_id),
  FOREIGN KEY(reflection_id) REFERENCES reflections(id),
  FOREIGN KEY(claim_id) REFERENCES claims(id)
);

CREATE TABLE IF NOT EXISTS reflection_experiments (
  reflection_id TEXT NOT NULL,
  experiment_id TEXT NOT NULL,
  proposal_key TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY(reflection_id, experiment_id),
  FOREIGN KEY(reflection_id) REFERENCES reflections(id),
  FOREIGN KEY(experiment_id) REFERENCES experiments(id)
);

CREATE TABLE IF NOT EXISTS sandboxes (
  sandbox_uid TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL DEFAULT 'local',
  sandbox_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'none',
  gpu TEXT NOT NULL DEFAULT '',
  cpu REAL NOT NULL DEFAULT 0,
  memory INTEGER NOT NULL DEFAULT 0,
  -- Compute provider that owns this sandbox (the backend's capabilities.name).
  -- Empty on rows that predate multi-provider support and means "the
  -- configured default backend" at read time.
  provider TEXT NOT NULL DEFAULT '',
  -- Provider-bundled machine SKU + datacenter, for backends (Lambda Labs) that
  -- procure a fixed instance type rather than composing cpu/memory. Empty for
  -- Modal, which sets gpu/cpu/memory above instead.
  instance_type TEXT NOT NULL DEFAULT '',
  region TEXT NOT NULL DEFAULT '',
  -- Provider price quote at provision (cloud plan Phase 7): captured from the
  -- catalog option (Lambda has it; Modal leaves 0). Recorded on the row AND
  -- appended to sandbox_generations so per-generation spend is reconstructable
  -- even though the row itself only retains its current generation.
  price_usd_per_hour REAL NOT NULL DEFAULT 0,
  time_limit INTEGER NOT NULL DEFAULT 0,
  ssh_host TEXT NOT NULL DEFAULT '',
  ssh_port INTEGER NOT NULL DEFAULT 0,
  ssh_user TEXT NOT NULL DEFAULT 'root',
  workdir TEXT NOT NULL DEFAULT '',
  sync_dir TEXT NOT NULL DEFAULT '',
  unsynced_dir TEXT NOT NULL DEFAULT '',
  sandbox_data_dir TEXT NOT NULL DEFAULT '',
  -- Management keypair reference (cloud plan Phase 5, fixed decision 4):
  -- non-empty when a control-plane management key was minted for this
  -- sandbox. A key-store reference (the sandbox_uid) — never key material.
  mgmt_key_ref TEXT NOT NULL DEFAULT '',
  -- User SSH key custody source: caller supplied an OpenSSH public key, or the
  -- local data plane used the managed fallback keypair.
  public_key_source TEXT NOT NULL DEFAULT 'managed',
  volume_name TEXT NOT NULL DEFAULT '',
  sandbox_name TEXT NOT NULL DEFAULT '',
  phase TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  provision_started_at TEXT,
  requested_at TEXT,
  expires_at TEXT,
  last_seen_at TEXT,
  idle_since TEXT,
  heartbeat_snapshot_json TEXT NOT NULL DEFAULT '{}',
  last_command_id TEXT NOT NULL DEFAULT '',
  last_command_text TEXT NOT NULL DEFAULT '',
  last_command_started_at TEXT,
  last_command_status TEXT NOT NULL DEFAULT '',
  last_command_exit_code INTEGER,
  last_command_finished_at TEXT,
  last_command_output_tail TEXT NOT NULL DEFAULT '',
  last_command_snapshot_at TEXT,
  terminated_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  -- Insertion-order column replacing rowid ordering (cloud plan Phase 6).
  created_seq INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS sandbox_attachments (
  sandbox_uid TEXT NOT NULL,
  experiment_id TEXT NOT NULL,
  attached_at TEXT NOT NULL,
  detached_at TEXT,
  FOREIGN KEY(sandbox_uid) REFERENCES sandboxes(sandbox_uid)
);

-- merv_run receipts observed on the box (July 2026). The sandbox filesystem is
-- the registry — .runs/<label>/ sentinel files written by the merv_run wrapper —
-- and this table is the brain's reconciled mirror of it, so run status
-- outlives both the agent session and the sandbox. finished_event_emitted
-- makes the run.finished event exactly-once across daemon restarts (flag and
-- event flip in one transaction).
CREATE TABLE IF NOT EXISTS sandbox_runs (
  sandbox_uid TEXT NOT NULL,
  label TEXT NOT NULL,
  command TEXT NOT NULL DEFAULT '',
  pid INTEGER,
  exit_code INTEGER,
  started_at TEXT NOT NULL DEFAULT '',
  finished_at TEXT NOT NULL DEFAULT '',
  first_seen_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_event_emitted INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (sandbox_uid, label),
  FOREIGN KEY(sandbox_uid) REFERENCES sandboxes(sandbox_uid)
);

-- Figures submitted alongside a markdown gated artifact (cloud plan Phase 2):
-- when a plan, report, or reflection_doc (legacy synthesis_doc) is associated, each resolvable relative image
-- link's bytes are captured to the blob store and recorded here, keyed by the
-- artifact's pinned version. Markdown lints check THIS mapping (submitted
-- figures), never the disk.
CREATE TABLE IF NOT EXISTS report_figures (
  report_version_id TEXT NOT NULL,
  link_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (report_version_id, link_path),
  FOREIGN KEY(report_version_id) REFERENCES resource_versions(id)
);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

-- Tenant records. The current private hosted-control deployment has no user
-- auth yet, but projects, quotas, budgets, and counters are already tenant
-- shaped so the real auth system can attach users later without reshaping
-- stored project data.
CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

-- Cost governance (cloud plan Phase 7). One quota row per tenant; every
-- column nullable = unlimited. Local mode's 'local' tenant has no row, so
-- QuotaService.check_admission is a no-op (unlimited) — byte-identical
-- behavior. Enforcement gates at the procurement choke point only when a
-- ceiling is set and exceeded.
CREATE TABLE IF NOT EXISTS tenant_quotas (
  tenant_id TEXT PRIMARY KEY,
  max_concurrent_sandboxes INTEGER,
  max_time_limit_seconds INTEGER,
  max_price_usd_per_hour REAL,
  gpu_hours_budget REAL,
  usd_budget REAL,
  blob_bytes_budget INTEGER
);

-- Per-generation sandbox spend ledger (cloud plan Phase 7). The sandboxes row
-- retains only its current generation, so it cannot reconstruct historical
-- spend; each provisioned generation appends a row here with the price the
-- provider quoted (Lambda has it; Modal leaves it 0/null). Reconstructable
-- spend = sum over rows of price_usd_per_hour * runtime. Dormant in local
-- mode (no quota to govern) but always recorded so the ledger is truthful.
CREATE TABLE IF NOT EXISTS sandbox_generations (
  id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL DEFAULT 'local',
  sandbox_id TEXT NOT NULL DEFAULT '',
  -- Owning compute provider (empty = pre-multi-provider row / default backend).
  provider TEXT NOT NULL DEFAULT '',
  instance_type TEXT NOT NULL DEFAULT '',
  gpu TEXT NOT NULL DEFAULT '',
  price_usd_per_hour REAL NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  created_seq INTEGER NOT NULL DEFAULT 0
);

-- Spend kill-switch (cloud plan Phase 9, risk 13). An operator-trippable
-- circuit breaker that refuses NEW sandbox provisioning when set, independent
-- of (and faster to act than) per-dimension budgets. ``scope = 'global'`` is a
-- platform-wide halt; ``scope = '<tenant_id>'`` halts one tenant. A row exists
-- only when the switch was tripped; absence = armed/off. Dormant in local mode
-- (no row, no tripping). Never carries secrets — just a reason string.
CREATE TABLE IF NOT EXISTS spend_kill_switches (
  scope TEXT PRIMARY KEY,
  tripped INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '',
  tripped_at TEXT
);

-- Literature review (July 2026, dev_docs/litreview_feature_plan.md). One
-- living sectioned document per project: kind='summary' is the General
-- Summary (at most one, enforced by the litreview_one_summary partial index
-- below; ensured lazily on first WRITE — reads never create it), kind='section'
-- are the dynamic theme sections. Rows are mutable envelopes; history is the
-- events table (full post-state per mutation, like claims). ``revision`` is
-- the per-section compare-and-swap counter; reorder bumps every row's
-- revision because position is section state.
CREATE TABLE IF NOT EXISTS litreview_sections (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('summary','section')),
  title TEXT NOT NULL,
  tldr TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL DEFAULT 0,
  revision INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL DEFAULT '',
  created_seq INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project_id, kind, title),
  FOREIGN KEY(project_id) REFERENCES projects(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS litreview_one_summary
  ON litreview_sections(project_id) WHERE kind = 'summary';

-- The papers ledger: every external paper the project has cited, deduplicated
-- per project by ``norm_key`` (arxiv:<id-sans-version> | doi:<casefolded> |
-- normalized URL). Metadata comes from the strict paper unfurler;
-- ``fetch_status`` records how ('fetched' beats 'manual' beats 'failed' and
-- is never downgraded).
CREATE TABLE IF NOT EXISTS papers (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  norm_key TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  authors_json TEXT NOT NULL DEFAULT '[]',
  year TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  source_kind TEXT NOT NULL CHECK (source_kind IN ('arxiv','doi','url')),
  fetch_status TEXT NOT NULL CHECK (fetch_status IN ('fetched','manual','failed')),
  created_by TEXT NOT NULL DEFAULT '',
  created_seq INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project_id, norm_key),
  FOREIGN KEY(project_id) REFERENCES projects(id)
);

-- Citation edges: paper -> lit-review section | experiment | claim. Same-
-- project integrity is enforced in the service write transaction (paper and
-- target are both looked up WHERE project_id = ?); deleting a section deletes
-- its links in the same transaction. The rendered References block is derived
-- from these rows and is never hand-edited.
CREATE TABLE IF NOT EXISTS paper_links (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  paper_id TEXT NOT NULL,
  target_type TEXT NOT NULL CHECK (target_type IN ('litreview_section','experiment','claim')),
  target_id TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(project_id, paper_id, target_type, target_id),
  FOREIGN KEY(project_id) REFERENCES projects(id),
  FOREIGN KEY(paper_id) REFERENCES papers(id)
);
"""


# Ordered migration ledger. SCHEMA above stays the CREATE-IF-NOT-EXISTS
# baseline for fresh databases; one-time or destructive DDL goes here and is
# applied exactly once per database, recorded in schema_migrations. The
# introspective helpers (_ensure_columns/_drop_columns) remain the SQLite
# legacy-convergence path for pre-ledger databases; NEW schema changes should
# be ledger migrations, not new introspective branches.
MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    # The defunct `jobs` table predates the sandbox model. Dropping it lived in
    # the every-boot SCHEMA constant; destructive DDL belongs in the ledger.
    (1, "drop_legacy_jobs_table", "DROP TABLE IF EXISTS jobs"),
    # Existing hosted Postgres control stores predate sandboxes.tenant_id; fresh
    # schemas have it, and this idempotent migration backfills existing rows.
    (2, "add_sandbox_tenant_id", ""),
    # Slice-3 (June 2026): idle-reaper heartbeat columns. Fresh schemas have
    # them; this idempotently adds them to existing SQLite + Postgres stores.
    (3, "add_sandbox_heartbeat_columns", ""),
    # Slice-2 (June 2026): the sandbox gets its own identity. Existing hosted
    # Postgres stores keyed sandboxes by experiment_id; this swaps the primary
    # key to sandbox_uid and opens the sandbox_attachments relation. Must run
    # before the mgmt-key/attachment migrations below (they read sandbox_uid).
    # SQLite already reaches this shape in _ensure_forward_schema, so the
    # handler is a guarded no-op there and on every fresh schema.
    (4, "migrate_sandbox_uid_identity", ""),
    # Slice-4 (June 2026): one experiment may own multiple sandbox rows.
    (5, "drop_sandboxes_experiment_unique", ""),
    # Slice-5 (June 2026): management keys follow the sandbox, not the
    # experiment; legacy non-empty refs are left as fallback refs.
    (6, "backfill_sandbox_mgmt_key_refs", ""),
    # Slice-5 (June 2026): attachment history can contain multiple
    # close-then-open rows for the same sandbox/experiment pair.
    (7, "allow_sandbox_attachment_history", ""),
    # Slice-6 (June 2026): sandbox rows are machine state only; experiment
    # relationships live in sandbox_attachments.
    (8, "drop_sandboxes_experiment_id", ""),
    # Slice-6 follow-up: MLflow is centralized and no longer archived through
    # sandbox release/daemon paths.
    (9, "drop_metrics_snapshots", "DROP TABLE IF EXISTS metrics_snapshots"),
    # Storage simplification: `missing` is no longer a storage object status.
    # Old rows are unavailable to agents, so keep them visible only through
    # expired/history views instead of preserving a removed state.
    (
        10,
        "normalize_storage_missing_status",
        "UPDATE storage_objects SET status = 'expired' WHERE status = 'missing'",
    ),
    # Review policy (July 2026): per-project settings dict backing knobs like
    # require_verified_reviews. Fresh schemas have the column; this backfills.
    (11, "add_project_settings_json", ""),
    # MLflow tracking (July 2026): fresh schemas have these columns, but hosted
    # Postgres stores that predate the feature need an explicit ledger step.
    (12, "add_experiment_mlflow_run_columns", ""),
    # Researcher synopsis (July 2026): fresh schemas have the column; this
    # backfills hosted Postgres stores that predate the requirement.
    (13, "add_review_synopsis", ""),
    # Daemon diet Phase 4b: sandbox.get must report whether the authorized
    # user SSH key came from the caller or the managed fallback.
    (14, "add_sandbox_public_key_source", ""),
    # Product-name alignment Phase 5: the reflection-wave table was formerly
    # named syntheses. Row ids and payload keys keep their legacy spelling.
    (15, "rename_syntheses_to_reflections", ""),
    # The whole last_command_* snapshot family reached the fresh-create SCHEMA
    # without a migration, so migrated deployments lacked all eight columns
    # (found when the sandbox signal ETag 500ed on production Postgres).
    (16, "add_sandbox_last_command_columns", ""),
    # Hard stop removed (July 2026): a published reflection can no longer stop
    # the project — winding down is the researcher's call, made outside the
    # workflow. Reactivate projects stopped under the old contract; the legacy
    # hard_stop_* columns stay behind in old databases, inert.
    (
        17,
        "reactivate_hard_stopped_projects",
        "UPDATE projects SET status = 'active' WHERE status = 'stopped'",
    ),
    # Multi-provider sandboxes (July 2026): rows record their owning compute
    # provider. Existing rows keep '' = "the configured default backend".
    (18, "add_sandbox_provider_columns", ""),
    # Synthesis -> reflection unification (July 2026): the wave entity is a
    # reflection everywhere (only the consolidation PHASE keeps the name
    # `synthesizing`), and the external names become the internal names — the
    # projection layer is deleted. Renames the two wave-relation tables (and
    # their synthesis_id columns), rewrites the persisted status/target_type/
    # event vocabulary, and rewrites review snapshot ids so passing reviews
    # keep satisfying their gates.
    (19, "unify_synthesis_to_reflection", ""),
    # Literature review (July 2026): three new tables + the at-most-one-summary
    # partial index. The table entries dispatch to handlers that execute the
    # SCHEMA-extracted DDL (_schema_table_ddl), so ledger and SCHEMA cannot
    # drift; each entry stays one statement.
    (20, "add_litreview_sections", ""),
    (21, "add_litreview_papers", ""),
    (22, "add_litreview_paper_links", ""),
    (
        23,
        "add_litreview_summary_unique_index",
        "CREATE UNIQUE INDEX IF NOT EXISTS litreview_one_summary\n"
        "  ON litreview_sections(project_id) WHERE kind = 'summary'",
    ),
)


EXPERIMENT_MLFLOW_COLUMNS: dict[str, str] = {
    "mlflow_run_id": "TEXT NOT NULL DEFAULT ''",
    "mlflow_run_name": "TEXT NOT NULL DEFAULT ''",
    "mlflow_run_status": "TEXT NOT NULL DEFAULT ''",
    "mlflow_run_artifact_uri": "TEXT NOT NULL DEFAULT ''",
    "mlflow_run_created_at": "TEXT",
    "mlflow_run_error": "TEXT NOT NULL DEFAULT ''",
}


# Rebuild shape for the legacy `resources` table whose UNIQUE was on `path`
# alone. SQLite cannot drop a column-level UNIQUE in place, so we copy into this
# shape (UNIQUE on project_id + path) and swap. Kept in sync with the resources
# block in SCHEMA above.
_RESOURCES_REBUILD_DDL = """
CREATE TABLE resources_migrate (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  path TEXT NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  current_version_id TEXT,
  version_token TEXT NOT NULL,
  mtime_ns INTEGER NOT NULL,
  size_bytes INTEGER NOT NULL,
  observed_at TEXT NOT NULL,
  git_commit TEXT,
  missing INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  created_by TEXT NOT NULL DEFAULT 'codex',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project_id, path),
  FOREIGN KEY(project_id) REFERENCES projects(id)
);
"""


# Rebuild shape for the legacy `review_requests` table whose `capability`
# column carried a column-level UNIQUE (cloud plan Phase 7). SQLite cannot drop
# such a column in place, so copy into this shape — `capability_hash` replaces
# `capability` — and swap. No UNIQUE on capability_hash here: empty-string
# placeholders during the row-by-row rehash would collide under it; fresh DBs
# get the UNIQUE constraint from the SCHEMA constant. Kept in sync with the
# review_requests block in SCHEMA above (minus that one constraint).
_REVIEW_REQUESTS_REBUILD_DDL = """
CREATE TABLE review_requests_migrate (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  role TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  capability_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  target_snapshot_id TEXT NOT NULL,
  producer_session_id TEXT NOT NULL DEFAULT '',
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  created_seq INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(project_id) REFERENCES projects(id)
);
"""


def _schema_table_ddl(*, table: str, name: str | None = None) -> str:
    """Extract one CREATE TABLE block from SCHEMA for SQLite rebuilds."""
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);",
        SCHEMA,
        re.DOTALL,
    )
    if match is None:
        raise RuntimeError(f"table not found in schema: {table}")
    ddl = match.group(0)
    if name is not None:
        ddl = ddl.replace(
            f"CREATE TABLE IF NOT EXISTS {table}",
            f"CREATE TABLE {name}",
            1,
        )
    return ddl


class BaseStateStore:
    """Dialect-neutral record-store contract and shared persistence helpers.

    The dialect seam (cloud plan Phase 6): subclasses own connections and
    transaction semantics, but must present the same surface the services
    were written against — ``connect()`` returns a connection whose
    ``execute`` accepts ``?`` placeholders and whose rows are mappings
    (``row["col"]`` + ``.keys()``), and ``transaction()`` yields such a
    connection under single-writer semantics. Everything here is plain SQL
    that runs unchanged on both dialects.
    """

    def connect(self) -> Connection:
        raise NotImplementedError

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        raise NotImplementedError

    def _apply_migrations(self, *, conn: Connection) -> None:
        """Apply unapplied ledger migrations in order, recording each."""
        applied = {
            int(row["version"])
            for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        for version, name, statement in MIGRATIONS:
            if version in applied:
                continue
            if name == "add_sandbox_tenant_id":
                self._ensure_sandbox_tenant_id(conn=conn)
            elif name == "add_sandbox_heartbeat_columns":
                self._ensure_sandbox_heartbeat_columns(conn=conn)
            elif name == "migrate_sandbox_uid_identity":
                self._migrate_sandbox_uid_identity(conn=conn)
            elif name == "drop_sandboxes_experiment_unique":
                self._drop_sandboxes_experiment_unique(conn=conn)
            elif name == "backfill_sandbox_mgmt_key_refs":
                self._backfill_sandbox_mgmt_key_refs(conn=conn)
            elif name == "allow_sandbox_attachment_history":
                self._allow_sandbox_attachment_history(conn=conn)
            elif name == "drop_sandboxes_experiment_id":
                self._drop_sandboxes_experiment_id(conn=conn)
            elif name == "add_project_settings_json":
                self._ensure_project_settings_json(conn=conn)
            elif name == "add_experiment_mlflow_run_columns":
                self._ensure_experiment_mlflow_columns(conn=conn)
            elif name == "add_review_synopsis":
                self._ensure_review_synopsis(conn=conn)
            elif name == "add_sandbox_public_key_source":
                self._ensure_sandbox_public_key_source(conn=conn)
            elif name == "rename_syntheses_to_reflections":
                self._rename_syntheses_to_reflections(conn=conn)
            elif name == "add_sandbox_last_command_columns":
                self._ensure_sandbox_last_command_columns(conn=conn)
            elif name == "add_sandbox_provider_columns":
                self._ensure_sandbox_provider_columns(conn=conn)
            elif name == "unify_synthesis_to_reflection":
                self._unify_synthesis_to_reflection(conn=conn)
            elif name == "add_litreview_sections":
                conn.execute(_schema_table_ddl(table="litreview_sections"))
            elif name == "add_litreview_papers":
                conn.execute(_schema_table_ddl(table="papers"))
            elif name == "add_litreview_paper_links":
                conn.execute(_schema_table_ddl(table="paper_links"))
            else:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, now_iso()),
            )

    def _ensure_project_settings_json(self, *, conn: Connection) -> None:
        if not self._has_column(conn=conn, table="projects", column="settings_json"):
            conn.execute(
                "ALTER TABLE projects ADD COLUMN settings_json TEXT NOT NULL DEFAULT '{}'"
            )

    def _ensure_experiment_mlflow_columns(self, *, conn: Connection) -> None:
        for column, ddl in EXPERIMENT_MLFLOW_COLUMNS.items():
            if not self._has_column(conn=conn, table="experiments", column=column):
                conn.execute(f"ALTER TABLE experiments ADD COLUMN {column} {ddl}")

    def _ensure_review_synopsis(self, *, conn: Connection) -> None:
        if not self._has_column(conn=conn, table="reviews", column="synopsis"):
            conn.execute(
                "ALTER TABLE reviews ADD COLUMN synopsis TEXT NOT NULL DEFAULT ''"
            )

    def _ensure_sandbox_public_key_source(self, *, conn: Connection) -> None:
        if not self._has_column(conn=conn, table="sandboxes", column="public_key_source"):
            conn.execute(
                "ALTER TABLE sandboxes ADD COLUMN public_key_source TEXT NOT NULL DEFAULT 'managed'"
            )

    # Mirrors the SCHEMA block exactly; adding a ninth last_command_* column
    # there means extending this map too.
    SANDBOX_LAST_COMMAND_COLUMNS = {
        "last_command_id": "TEXT NOT NULL DEFAULT ''",
        "last_command_text": "TEXT NOT NULL DEFAULT ''",
        "last_command_started_at": "TEXT",
        "last_command_status": "TEXT NOT NULL DEFAULT ''",
        "last_command_exit_code": "INTEGER",
        "last_command_finished_at": "TEXT",
        "last_command_output_tail": "TEXT NOT NULL DEFAULT ''",
        "last_command_snapshot_at": "TEXT",
    }

    def _ensure_sandbox_last_command_columns(self, *, conn: Connection) -> None:
        for column, ddl in self.SANDBOX_LAST_COMMAND_COLUMNS.items():
            if not self._has_column(conn=conn, table="sandboxes", column=column):
                conn.execute(f"ALTER TABLE sandboxes ADD COLUMN {column} {ddl}")

    def _ensure_sandbox_provider_columns(self, *, conn: Connection) -> None:
        """Idempotently add the owning-provider column to both sandbox tables.

        Backfill is the empty string: '' means "the configured default
        backend" at read time, so pre-multi-provider rows keep working.
        """
        for table in ("sandboxes", "sandbox_generations"):
            if not self._has_column(conn=conn, table=table, column="provider"):
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN provider TEXT NOT NULL DEFAULT ''"
                )

    def _rename_syntheses_to_reflections(self, *, conn: Connection) -> None:
        if self._has_table(conn=conn, table="reflections"):
            return
        if self._has_table(conn=conn, table="syntheses"):
            conn.execute("ALTER TABLE syntheses RENAME TO reflections")

    def _rename_synthesis_wave_tables(self, *, conn: Connection) -> None:
        """Move legacy synthesis_* wave-relation tables to reflection_*.

        Runs BEFORE the SCHEMA create in both stores' _initialize (like
        _rename_syntheses_to_reflections) so old data is renamed into place
        rather than stranded beside fresh empty reflection_* tables; guarded
        and idempotent, so the ledger handler can call it again safely.
        """
        for old, new in (
            ("synthesis_claim_changes", "reflection_claim_changes"),
            ("synthesis_experiments", "reflection_experiments"),
        ):
            if not self._has_table(conn=conn, table=new) and self._has_table(conn=conn, table=old):
                conn.execute(f"ALTER TABLE {old} RENAME TO {new}")
            if self._has_table(conn=conn, table=new) and self._has_column(
                conn=conn, table=new, column="synthesis_id"
            ):
                conn.execute(f"ALTER TABLE {new} RENAME COLUMN synthesis_id TO reflection_id")

    def _unify_synthesis_to_reflection(self, *, conn: Connection) -> None:
        """Retire the synthesis wave vocabulary from persisted state.

        Fresh schemas already carry the reflection_* shapes, so every step is
        guarded or a naturally idempotent UPDATE. The snapshot-id rewrites are
        surgical (prefix swap + pipe-delimited status segment) so resource
        tokens embedding legacy roles like synthesis_doc stay byte-identical —
        those must keep matching the association rows they pinned.
        """
        self._rename_synthesis_wave_tables(conn=conn)
        conn.execute(
            "UPDATE reflections SET status = 'reflection_review' WHERE status = 'synthesis_review'"
        )
        # Events history: type prefix, target_type, and the known payload
        # vocabulary (statuses, the transition verb, claim provenance keys).
        # String-level JSON rewrites are deliberate — the payload shapes are
        # known and `synthesizing` (the phase, which stays) matches none of
        # the patterns.
        conn.execute(
            "UPDATE events SET type = 'reflection.' || SUBSTR(type, LENGTH('synthesis.') + 1) "
            "WHERE type LIKE ?",
            ("synthesis.%",),
        )
        conn.execute("UPDATE events SET target_type = 'reflection' WHERE target_type = 'synthesis'")
        conn.execute(
            "UPDATE events SET payload_json = REPLACE(REPLACE(REPLACE(payload_json, "
            "'synthesis_review', 'reflection_review'), "
            "'submit_synthesis', 'submit_reflection_artifacts'), "
            "'source_synthesis_id', 'source_reflection_id') "
            "WHERE payload_json LIKE ?",
            ("%synthesis%",),
        )
        # Reviews and their capabilities: the persisted target_type plus the
        # byte-compared snapshot ids (`synthesis|<id>|synthesis_review|...`),
        # so a pass recorded before the rename still satisfies its gate.
        for table in ("reviews", "review_requests"):
            conn.execute(
                f"UPDATE {table} SET target_snapshot_id = "
                "'reflection' || SUBSTR(target_snapshot_id, LENGTH('synthesis') + 1) "
                "WHERE target_snapshot_id LIKE ?",
                ("synthesis|%",),
            )
            conn.execute(
                f"UPDATE {table} SET target_snapshot_id = "
                "REPLACE(target_snapshot_id, '|synthesis_review|', '|reflection_review|') "
                "WHERE target_snapshot_id LIKE ?",
                ("%|synthesis_review|%",),
            )
            conn.execute(
                f"UPDATE {table} SET target_type = 'reflection' WHERE target_type = 'synthesis'"
            )
        conn.execute(
            "UPDATE resource_associations SET target_type = 'reflection' "
            "WHERE target_type = 'synthesis'"
        )

    def _ensure_sandbox_tenant_id(self, *, conn: Connection) -> None:
        if not self._has_column(conn=conn, table="sandboxes", column="tenant_id"):
            conn.execute(
                "ALTER TABLE sandboxes ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'local'"
            )
        conn.execute(
            """
            UPDATE sandboxes
            SET tenant_id = COALESCE(
              (SELECT tenant_id FROM projects WHERE projects.id = sandboxes.project_id),
              tenant_id,
              'local'
            )
            WHERE project_id != ''
            """
        )

    def _ensure_sandbox_heartbeat_columns(self, *, conn: Connection) -> None:
        """Idempotently add the idle-reaper columns to existing SQLite/Postgres."""
        for column, ddl in (
            ("idle_since", "TEXT"),
            ("heartbeat_snapshot_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if not self._has_column(conn=conn, table="sandboxes", column=column):
                conn.execute(f"ALTER TABLE sandboxes ADD COLUMN {column} {ddl}")

    def _drop_sandboxes_experiment_unique(self, *, conn: Connection) -> None:
        conn.execute(
            "ALTER TABLE sandboxes DROP CONSTRAINT IF EXISTS sandboxes_experiment_id_key"
        )

    def _backfill_sandbox_mgmt_key_refs(self, *, conn: Connection) -> None:
        if not self._has_column(conn=conn, table="sandboxes", column="mgmt_key_ref"):
            conn.execute(
                "ALTER TABLE sandboxes ADD COLUMN mgmt_key_ref TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            """
            UPDATE sandboxes
            SET mgmt_key_ref = sandbox_uid
            WHERE COALESCE(mgmt_key_ref, '') = '' AND COALESCE(sandbox_uid, '') != ''
            """
        )

    def _allow_sandbox_attachment_history(self, *, conn: Connection) -> None:
        conn.execute(
            "ALTER TABLE sandbox_attachments DROP CONSTRAINT IF EXISTS sandbox_attachments_pkey"
        )

    def _drop_sandboxes_experiment_id(self, *, conn: Connection) -> None:
        """Drop the legacy Postgres sandbox experiment_id column after backfill."""
        if not self._has_column(conn=conn, table="sandboxes", column="experiment_id"):
            return
        self._backfill_sandbox_attachments(conn=conn)
        conn.execute("ALTER TABLE sandboxes DROP COLUMN IF EXISTS experiment_id")

    def _migrate_sandbox_uid_identity(self, *, conn: Connection) -> None:
        """Repoint an experiment_id-keyed sandboxes table onto sandbox_uid.

        The decoupling refactor makes sandbox_uid the primary key and opens the
        sandbox_attachments relation. Fresh schemas already have that shape and
        SQLite reaches it in _ensure_forward_schema, so the guard makes this a
        no-op there; the real work upgrades a hosted Postgres store that predates
        the refactor. Idempotent: every step is guarded or IF-EXISTS, and the PK
        swap only commits once (a partial run re-converges on the next boot).
        """
        if self._sandboxes_uid_is_pk(conn=conn):
            return
        # No legacy sandboxes table yet (a fresh database, before its
        # schema-create) — there is nothing to upgrade; the schema-create builds
        # the final sandbox_uid-keyed shape directly.
        if not self._has_column(conn=conn, table="sandboxes", column="experiment_id"):
            return
        if not self._has_column(conn=conn, table="sandboxes", column="sandbox_uid"):
            conn.execute("ALTER TABLE sandboxes ADD COLUMN sandbox_uid TEXT")
        # experiment_id was the legacy primary key, so it addresses each row.
        for row in conn.execute(
            "SELECT experiment_id FROM sandboxes WHERE COALESCE(sandbox_uid, '') = ''"
        ).fetchall():
            conn.execute(
                "UPDATE sandboxes SET sandbox_uid = ? WHERE experiment_id = ?",
                (uuid.uuid4().hex, row["experiment_id"]),
            )
        conn.execute("ALTER TABLE sandboxes DROP CONSTRAINT IF EXISTS sandboxes_pkey")
        conn.execute("ALTER TABLE sandboxes ADD PRIMARY KEY (sandbox_uid)")
        # Open one attachment per surviving sandbox (closed if already terminated).
        self._backfill_sandbox_attachments(conn=conn)

    def _sandboxes_uid_is_pk(self, *, conn: Connection) -> bool:
        """True once sandbox_uid is the sandboxes primary key (fresh or upgraded)."""
        try:
            rows = conn.execute("PRAGMA table_info(sandboxes)").fetchall()
            if rows:
                return any(
                    str(row["name"]) == "sandbox_uid" and int(row["pk"] or 0) > 0
                    for row in rows
                )
        except Exception:  # noqa: BLE001 - Postgres has no PRAGMA
            pass
        row = conn.execute(
            """
            SELECT 1
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.table_schema = 'public'
              AND tc.table_name = 'sandboxes'
              AND tc.constraint_type = 'PRIMARY KEY'
              AND kcu.column_name = 'sandbox_uid'
            """
        ).fetchone()
        return row is not None

    def _backfill_sandbox_attachments(self, *, conn: Connection) -> None:
        """Open the forward relation for legacy/un-attached rows; a no-op once filled.

        Dialect-neutral so both the SQLite forward-schema rebuild and the Postgres
        identity migration share it.
        """
        conn.execute(_schema_table_ddl(table="sandbox_attachments"))
        if not self._has_column(conn=conn, table="sandboxes", column="experiment_id"):
            return
        # Only rows still missing their attachment — so re-runs after the first
        # upgrade do no work, while a partial upgrade still gets finished.
        rows = conn.execute(
            """
            SELECT sandbox_uid, experiment_id, requested_at, created_at, updated_at,
                   terminated_at, status
            FROM sandboxes
            WHERE COALESCE(sandbox_uid, '') != ''
              AND NOT EXISTS (
                SELECT 1 FROM sandbox_attachments a
                WHERE a.sandbox_uid = sandboxes.sandbox_uid
                  AND a.experiment_id = sandboxes.experiment_id
              )
            """
        ).fetchall()
        for row in rows:
            attached_at = (
                row["requested_at"]
                or row["created_at"]
                or row["updated_at"]
                or now_iso()
            )
            detached_at = None
            if row["terminated_at"] or row["status"] in {"terminated", "failed"}:
                detached_at = row["terminated_at"] or row["updated_at"] or attached_at
            conn.execute(
                """
                INSERT INTO sandbox_attachments (
                  sandbox_uid, experiment_id, attached_at, detached_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (row["sandbox_uid"], row["experiment_id"], attached_at, detached_at),
            )

    def _has_column(self, *, conn: Connection, table: str, column: str) -> bool:
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            if rows:
                return any(str(row["name"]) == column for row in rows)
        except Exception:  # noqa: BLE001 - Postgres has no PRAGMA
            pass
        row = conn.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ? AND column_name = ?
            """,
            (table, column),
        ).fetchone()
        return row is not None

    def _has_table(self, *, conn: Connection, table: str) -> bool:
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            return row is not None
        except Exception:  # noqa: BLE001 - Postgres has no sqlite_master
            pass
        row = conn.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = ?
            """,
            (table,),
        ).fetchone()
        return row is not None

    def require_project_id(
        self,
        *,
        conn: Connection,
        project_id: str | None,
        tenant_id: str | None = None,
    ) -> str:
        """Resolve and existence-check a project id, optionally tenant-scoped.

        Tenancy enforcement (cloud plan Phase 7): when ``tenant_id`` is given,
        the lookup is scoped to that tenant — a project owned by another tenant
        reads as not-found, so cross-tenant access is denied at the record
        layer. The default (``tenant_id`` unset) is today's behavior exactly, so
        every existing call site is unchanged and local mode (single implicit
        'local' tenant) never threads a tenant.
        """
        if not project_id:
            raise ValidationError("project_id is required")
        if tenant_id is None:
            row = conn.execute(
                "SELECT id FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM projects WHERE id = ? AND tenant_id = ?",
                (project_id, tenant_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"project not found: {project_id}")
        return project_id

    def record_event(
        self,
        *,
        conn: Connection,
        project_id: str,
        event_type: str,
        target_type: str = "",
        target_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> StoredEvent:
        created_at = now_iso()
        payload_json = json.dumps(payload or {}, sort_keys=True)
        row = conn.execute(
            """
            INSERT INTO events (project_id, type, target_type, target_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                project_id,
                event_type,
                target_type,
                target_id,
                payload_json,
                created_at,
            ),
        ).fetchone()
        if row is None:  # pragma: no cover - both supported dialects return it
            raise RuntimeError("event insert did not return an id")
        canonical_payload = json.loads(payload_json)
        return StoredEvent(
            id=int(row["id"]),
            project_id=project_id,
            type=event_type,
            target_type=target_type,
            target_id=target_id,
            payload=freeze_json_object(canonical_payload),
            created_at=created_at,
        )

    def events_since(
        self, *, project_id: str | None, after_id: int, limit: int = 500
    ) -> dict[str, Any]:
        """Ascending tail of the append-only events table — the SSE cursor read."""
        with closing(self.connect()) as conn:
            project_id = self.require_project_id(conn=conn, project_id=project_id)
            rows = conn.execute(
                """
                SELECT id, project_id, type, target_type, target_id, payload_json, created_at
                FROM events
                WHERE project_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (project_id, int(after_id), max(1, min(int(limit), 500))),
            ).fetchall()
            events = []
            for row in rows:
                item = row_to_dict(row=row) or {}
                item["payload"] = json.loads(str(item.pop("payload_json", "{}")))
                events.append(item)
            return {"events": events}

    def add_project_member(self, *, project_id: str, user_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO project_members (project_id, user_id, added_at)
                VALUES (?, ?, ?)
                ON CONFLICT (project_id, user_id) DO NOTHING
                """,
                (project_id, user_id, now_iso()),
            )

    def remove_project_member(self, *, project_id: str, user_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM project_members WHERE project_id = ? AND user_id = ?",
                (project_id, user_id),
            )

    def is_project_member(self, *, project_id: str, user_id: str) -> bool:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM project_members WHERE project_id = ? AND user_id = ?",
                (project_id, user_id),
            ).fetchone()
            return row is not None

    def list_project_members(self, *, project_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT user_id, added_at FROM project_members WHERE project_id = ? ORDER BY added_at",
                (project_id,),
            ).fetchall()
            return [row_to_dict(row=row) or {} for row in rows]

    def project_event_signal(self, *, project_id: str | None) -> str:
        """Monotonic per-project signal for the append-only event stream."""
        with closing(self.connect()) as conn:
            project_id = self.require_project_id(conn=conn, project_id=project_id)
            row = conn.execute(
                """
                SELECT COALESCE(MAX(id), 0) AS max_id, COUNT(*) AS count
                FROM events
                WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
            if row is None:
                return "0:0"
            return f"{int(row['max_id'] or 0)}:{int(row['count'] or 0)}"

    def project_sandbox_signal(self, *, project_id: str | None) -> str:
        """Change signal for a project's sandbox rows (no event-table proxy).

        Sandbox lifecycle mutations — provision, status, heartbeat, command,
        terminate — every one bumps ``updated_at`` (see repository) but,
        unlike claims/experiments/reviews, do NOT append an event, so the event
        signal can't stand in for them. Digest each row's identity plus the
        fields the sandbox_list_view surfaces: it changes iff that payload would.
        Cheap — a few rows, a handful of columns, no per-row view rendering.
        """
        with closing(self.connect()) as conn:
            project_id = self.require_project_id(conn=conn, project_id=project_id)
            rows = conn.execute(
                """
                SELECT sandbox_uid, status, updated_at, last_seen_at,
                       last_command_snapshot_at, terminated_at
                FROM sandboxes
                WHERE project_id = ?
                ORDER BY sandbox_uid
                """,
                (project_id,),
            ).fetchall()
            digest = "\n".join(
                "|".join(
                    str(row[column] or "")
                    for column in (
                        "sandbox_uid", "status", "updated_at",
                        "last_seen_at", "last_command_snapshot_at", "terminated_at",
                    )
                )
                for row in rows
            )
            return f"{len(rows)}:{digest}"

    def tenant_event_count(self, *, tenant_id: str) -> int:
        """Count durable project events for one tenant."""
        with closing(self.connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM events e
                JOIN projects p ON p.id = e.project_id
                WHERE p.tenant_id = ?
                """,
                (tenant_id,),
            ).fetchone()
        return int(row["n"]) if row is not None else 0

    def recent_events(self, *, project_id: str | None, limit: int = 100) -> dict[str, Any]:
        with closing(self.connect()) as conn:
            project_id = self.require_project_id(conn=conn, project_id=project_id)
            rows = conn.execute(
                """
                SELECT id, project_id, type, target_type, target_id, payload_json, created_at
                FROM events
                WHERE project_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (project_id, max(1, min(int(limit), 500))),
            ).fetchall()
            events = []
            for row in rows:
                item = row_to_dict(row=row) or {}
                item["payload"] = json.loads(str(item.pop("payload_json", "{}")))
                events.append(item)
            return {"events": events}


class StateStore(BaseStateStore):
    """The SQLite dialect — local mode's store, and the historical default.

    Records only — the store does not know where the repository checkout
    lives. Local paths belong to the data plane (``LocalWorkspace`` and the
    ``DataPlaneWorker``), so the same record layer can serve a cloud DB.
    The Postgres dialect lives in ``dialects.PostgresStateStore``; the name
    ``StateStore`` stays on the SQLite class so every existing call site and
    test keeps working unchanged (``SqliteStateStore`` is an alias).
    """

    def __init__(self, *, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL lets the background reconcile poller read while a submit writes,
        # instead of readers and writers blocking each other (rollback-journal
        # mode upgrades a read lock to write and returns SQLITE_BUSY immediately,
        # which surfaced as "database is locked" on concurrent submits).
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            # IMMEDIATE acquires the write lock up front so busy_timeout governs
            # the wait. A DEFERRED BEGIN takes a read lock first and then fails
            # instantly when it can't upgrade under contention.
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        self._migrate_resources_unique()
        self._migrate_capability_hash()
        conn = self.connect()
        try:
            self._rename_syntheses_to_reflections(conn=conn)
            self._rename_synthesis_wave_tables(conn=conn)
            conn.executescript(SCHEMA)  # IF NOT EXISTS — safe to race
            # The column probes and migration ledger below are check-then-act;
            # hold the write lock across them so two processes booting the same
            # upgrade can't both run one ALTER (executescript autocommits).
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_forward_schema(conn=conn)
            self._apply_migrations(conn=conn)
            row = conn.execute("SELECT id FROM projects LIMIT 1").fetchone()
            if row is None:
                project_id = new_id(prefix="proj")
                conn.execute(
                    "INSERT INTO projects (id, name, summary, created_at) VALUES (?, ?, ?, ?)",
                    (project_id, "Local Research Project", "", now_iso()),
                )
                self.record_event(
                    conn=conn,
                    project_id=project_id,
                    event_type="project.created",
                    target_type="project",
                    target_id=project_id,
                    payload={"name": "Local Research Project"},
                )
            conn.commit()
        finally:
            conn.close()

    def _ensure_forward_schema(self, *, conn: sqlite3.Connection) -> None:
        # Cloud-split Phase 6 (June 2026): tenancy column — projects carry
        # ownership; local mode is the fixed 'local' tenant (which is also the
        # column default, so older rows converge to it).
        self._ensure_columns(
            conn=conn,
            table="projects",
            columns={
                "tenant_id": "TEXT NOT NULL DEFAULT 'local'",
                "status": "TEXT NOT NULL DEFAULT 'active'",
            },
        )
        # Experiments now persist the accepted conclusion on `complete`; older
        # databases predate the column. Named experiments (June 2026): the
        # short unique name doubles as the experiment folder name; empty on
        # rows that predate the requirement (their folders stay id-named).
        self._ensure_columns(
            conn=conn,
            table="experiments",
            columns={
                "conclusion": "TEXT NOT NULL DEFAULT ''",
                "name": "TEXT NOT NULL DEFAULT ''",
                # MLflow run identity (July 2026): best-effort run created by
                # the control plane when an experiment enters `running`.
                **EXPERIMENT_MLFLOW_COLUMNS,
            },
        )
        self._ensure_columns(
            conn=conn,
            table="resources",
            columns={"deleted": "INTEGER NOT NULL DEFAULT 0"},
        )
        # Stage-routed rejections (June 2026): experiment reviews record which
        # stage a rejection sent the experiment back to ('planned' or
        # 'running'); empty on passes and on rows that predate the column.
        self._ensure_columns(
            conn=conn,
            table="reviews",
            columns={"return_to": "TEXT NOT NULL DEFAULT ''"},
        )
        # Review sessions keep a tenant column so future auth can scope review
        # starts without reshaping legacy rows.
        self._ensure_columns(
            conn=conn,
            table="review_sessions",
            columns={"tenant_id": "TEXT NOT NULL DEFAULT ''"},
        )
        # Async provisioning (June 2026): sandboxes gained a provisioning/failed
        # lifecycle with progress + error fields. Older DBs predate these columns.
        self._ensure_columns(
            conn=conn,
            table="sandboxes",
            columns={
                "sandbox_name": "TEXT NOT NULL DEFAULT ''",
                "tenant_id": "TEXT NOT NULL DEFAULT 'local'",
                "phase": "TEXT NOT NULL DEFAULT ''",
                "detail": "TEXT NOT NULL DEFAULT ''",
                "error": "TEXT NOT NULL DEFAULT ''",
                "provision_started_at": "TEXT",
                "sandbox_data_dir": "TEXT NOT NULL DEFAULT ''",
                "sync_dir": "TEXT NOT NULL DEFAULT ''",
                "unsynced_dir": "TEXT NOT NULL DEFAULT ''",
                # Lambda-default (June 2026): provider-bundled machine SKU +
                # datacenter for backends that procure a fixed instance type.
                "instance_type": "TEXT NOT NULL DEFAULT ''",
                "region": "TEXT NOT NULL DEFAULT ''",
                # Cloud-split Phase 7 (June 2026): provider price quote captured
                # at provision for cost governance. 0 on rows that predate it.
                "price_usd_per_hour": "REAL NOT NULL DEFAULT 0",
                # Cloud-split Phase 5 (June 2026): management keypair reference
                # — non-empty when a control-plane management key exists for
                # this sandbox. Never key material.
                "mgmt_key_ref": "TEXT NOT NULL DEFAULT ''",
                "public_key_source": "TEXT NOT NULL DEFAULT 'managed'",
                # Command status snapshot (July 2026): populated by
                # sandbox.terminal from rec.sh transcript markers so agents
                # keep the last known command state even if a later transcript
                # SSH read is unavailable.
                "last_command_id": "TEXT NOT NULL DEFAULT ''",
                "last_command_text": "TEXT NOT NULL DEFAULT ''",
                "last_command_started_at": "TEXT",
                "last_command_status": "TEXT NOT NULL DEFAULT ''",
                "last_command_exit_code": "INTEGER",
                "last_command_finished_at": "TEXT",
                "last_command_output_tail": "TEXT NOT NULL DEFAULT ''",
                "last_command_snapshot_at": "TEXT",
            },
        )
        conn.execute(
            """
            UPDATE sandboxes
            SET tenant_id = COALESCE(
              (SELECT tenant_id FROM projects WHERE projects.id = sandboxes.project_id),
              tenant_id,
              'local'
            )
            WHERE project_id != ''
            """
        )
        # The shadow-git unplug (May 2026) dropped these columns from the
        # SCHEMA constant, but pre-existing databases still have them. The
        # `snapshot_status` column is NOT NULL with no default, so any INSERT
        # via the new code raises sqlite3.IntegrityError. Drop them on boot
        # so old DBs match the new INSERT shape. Idempotent — _drop_columns
        # is a no-op when the columns are already gone.
        self._drop_columns(
            conn=conn,
            table="resource_versions",
            columns=("snapshot_status", "git_path", "git_commit"),
        )
        # Cloud-split Phase 3 (June 2026): machine-local values left the
        # cloud-bound sandboxes row — the per-experiment SSH key path and the
        # local sync dir live in the data-plane worker's local store now
        # (dataplane_state.sqlite under the checkout state dir). Both columns
        # were always derivable, so no value migration is needed.
        self._drop_columns(
            conn=conn,
            table="sandboxes",
            columns=("key_path", "local_sync_dir"),
        )
        # Slice-1 (June 2026): the automatic experiment-folder push was removed,
        # so the per-sandbox initial_pushed file count no longer exists.
        self._drop_columns(
            conn=conn,
            table="sandboxes",
            columns=("initial_pushed",),
        )
        # Cloud-split Phase 6 (June 2026): explicit insertion-order columns
        # replace `ORDER BY rowid` so the same queries run on the Postgres
        # dialect (which has no rowid). Legacy rows backfill created_seq from
        # their rowid — the exact order the old queries observed — once, when
        # the column is first added; new writes set it via next_created_seq().
        for table in (
            "resource_versions",
            "resource_associations",
            "review_requests",
            "reviews",
            "reflections",
            "sandboxes",
        ):
            added = self._ensure_columns(
                conn=conn,
                table=table,
                columns={"created_seq": "INTEGER NOT NULL DEFAULT 0"},
            )
            if "created_seq" in added:
                conn.execute(f"UPDATE {table} SET created_seq = rowid")
        # Slice-2 (June 2026): sandbox rows now have their own durable id;
        # public 1:1 behavior is preserved by registry primary selection.
        self._migrate_sandbox_identity(conn=conn)
        self._backfill_sandbox_attachments(conn=conn)
        self._drop_sandboxes_experiment_id(conn=conn)
        # Cloud-split Phase 9 (June 2026): the per-tenant USD spend budget. The
        # GPU-hour budget shipped in Phase 7; USD is its sibling. Nullable =
        # unlimited; pre-Phase-9 quota rows predate the column.
        self._ensure_columns(
            conn=conn,
            table="tenant_quotas",
            columns={"usd_budget": "REAL"},
        )

    def _migrate_sandbox_identity(self, *, conn: sqlite3.Connection) -> None:
        """Rebuild sandboxes when legacy SQLite has experiment_id as the PK."""
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sandboxes'"
        ).fetchone()
        if table is None:
            return
        columns = conn.execute("PRAGMA table_info(sandboxes)").fetchall()
        uid_pk = any(
            str(row["name"]) == "sandbox_uid" and int(row["pk"] or 0) > 0
            for row in columns
        )
        if uid_pk:
            return
        conn.execute("DROP TABLE IF EXISTS sandbox_attachments")
        conn.execute(_schema_table_ddl(table="sandboxes", name="sandboxes_migrate"))
        source_column_list = [str(row["name"]) for row in columns]
        source_columns = set(source_column_list)
        target_columns = [
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(sandboxes_migrate)").fetchall()
        ]
        copy_columns = [
            column
            for column in target_columns
            if column != "sandbox_uid" and column in source_columns
        ]
        insert_columns = ", ".join(["sandbox_uid", *copy_columns])
        placeholders = ", ".join("?" for _ in ["sandbox_uid", *copy_columns])
        attachments: list[tuple[str, str, str, str | None]] = []
        select_columns = ", ".join(source_column_list)
        for row in conn.execute(f"SELECT {select_columns} FROM sandboxes").fetchall():
            row_uid = uuid.uuid4().hex
            conn.execute(
                f"INSERT INTO sandboxes_migrate ({insert_columns}) VALUES ({placeholders})",
                [row_uid, *[row[column] for column in copy_columns]],
            )
            if "experiment_id" in source_columns and row["experiment_id"]:
                attached_at = (
                    (row["requested_at"] if "requested_at" in source_columns else None)
                    or (row["created_at"] if "created_at" in source_columns else None)
                    or (row["updated_at"] if "updated_at" in source_columns else None)
                    or now_iso()
                )
                detached_at = None
                terminated_at = (
                    row["terminated_at"] if "terminated_at" in source_columns else None
                )
                status = row["status"] if "status" in source_columns else ""
                if terminated_at or status in {"terminated", "failed"}:
                    detached_at = (
                        terminated_at
                        or (row["updated_at"] if "updated_at" in source_columns else None)
                        or attached_at
                    )
                attachments.append(
                    (row_uid, row["experiment_id"], attached_at, detached_at)
                )
        conn.execute("DROP TABLE sandboxes")
        conn.execute("ALTER TABLE sandboxes_migrate RENAME TO sandboxes")
        conn.execute(_schema_table_ddl(table="sandbox_attachments"))
        for sandbox_uid, experiment_id, attached_at, detached_at in attachments:
            conn.execute(
                """
                INSERT INTO sandbox_attachments (
                  sandbox_uid, experiment_id, attached_at, detached_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (sandbox_uid, experiment_id, attached_at, detached_at),
            )

    def _drop_sandboxes_experiment_unique(self, *, conn: sqlite3.Connection) -> None:
        """Rebuild sandboxes when SQLite still has UNIQUE(experiment_id)."""
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sandboxes'"
        ).fetchone()
        if table is None or not self._sandboxes_has_experiment_unique(conn=conn):
            return
        self._backfill_sandbox_attachments(conn=conn)
        attachments_exist = (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sandbox_attachments'"
            ).fetchone()
            is not None
        )
        if attachments_exist:
            conn.execute("DROP TABLE IF EXISTS sandbox_attachments_migrate")
            conn.execute(
                """
                CREATE TEMP TABLE sandbox_attachments_migrate AS
                SELECT sandbox_uid, experiment_id, attached_at, detached_at
                FROM sandbox_attachments
                """
            )
            conn.execute("DROP TABLE sandbox_attachments")
        conn.execute(_schema_table_ddl(table="sandboxes", name="sandboxes_migrate"))
        source_columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(sandboxes)").fetchall()
        }
        target_columns = [
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(sandboxes_migrate)").fetchall()
            if str(row["name"]) in source_columns
        ]
        if target_columns:
            columns = ", ".join(target_columns)
            conn.execute(
                f"INSERT INTO sandboxes_migrate ({columns}) SELECT {columns} FROM sandboxes"
            )
        conn.execute("DROP TABLE sandboxes")
        conn.execute("ALTER TABLE sandboxes_migrate RENAME TO sandboxes")
        conn.execute(_schema_table_ddl(table="sandbox_attachments"))
        if attachments_exist:
            conn.execute(
                """
                INSERT OR IGNORE INTO sandbox_attachments (
                  sandbox_uid, experiment_id, attached_at, detached_at
                )
                SELECT sandbox_uid, experiment_id, attached_at, detached_at
                FROM sandbox_attachments_migrate
                """
            )
            conn.execute("DROP TABLE sandbox_attachments_migrate")
        self._backfill_sandbox_attachments(conn=conn)

    def _drop_sandboxes_experiment_id(self, *, conn: sqlite3.Connection) -> None:
        """Rebuild sandboxes without the legacy experiment_id column."""
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sandboxes'"
        ).fetchone()
        if table is None or not self._has_column(
            conn=conn, table="sandboxes", column="experiment_id"
        ):
            return
        self._backfill_sandbox_attachments(conn=conn)
        attachments_exist = (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sandbox_attachments'"
            ).fetchone()
            is not None
        )
        if attachments_exist:
            conn.execute("DROP TABLE IF EXISTS sandbox_attachments_migrate")
            conn.execute(
                """
                CREATE TEMP TABLE sandbox_attachments_migrate AS
                SELECT sandbox_uid, experiment_id, attached_at, detached_at
                FROM sandbox_attachments
                """
            )
            conn.execute("DROP TABLE sandbox_attachments")
        conn.execute(_schema_table_ddl(table="sandboxes", name="sandboxes_migrate"))
        source_columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(sandboxes)").fetchall()
        }
        target_columns = [
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(sandboxes_migrate)").fetchall()
            if str(row["name"]) in source_columns
        ]
        if target_columns:
            columns = ", ".join(target_columns)
            conn.execute(
                f"INSERT INTO sandboxes_migrate ({columns}) SELECT {columns} FROM sandboxes"
            )
        conn.execute("DROP TABLE sandboxes")
        conn.execute("ALTER TABLE sandboxes_migrate RENAME TO sandboxes")
        conn.execute(_schema_table_ddl(table="sandbox_attachments"))
        if attachments_exist:
            conn.execute(
                """
                INSERT OR IGNORE INTO sandbox_attachments (
                  sandbox_uid, experiment_id, attached_at, detached_at
                )
                SELECT sandbox_uid, experiment_id, attached_at, detached_at
                FROM sandbox_attachments_migrate
                """
            )
            conn.execute("DROP TABLE sandbox_attachments_migrate")

    def _sandboxes_has_experiment_unique(self, *, conn: sqlite3.Connection) -> bool:
        for idx in conn.execute("PRAGMA index_list(sandboxes)").fetchall():
            if not idx["unique"]:
                continue
            columns = [
                str(info["name"])
                for info in conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()
            ]
            if columns == ["experiment_id"]:
                return True
        return False

    def _allow_sandbox_attachment_history(self, *, conn: sqlite3.Connection) -> None:
        """Rebuild sandbox_attachments when SQLite still keys only the pair."""
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sandbox_attachments'"
        ).fetchone()
        if table is None:
            conn.execute(_schema_table_ddl(table="sandbox_attachments"))
            return
        if self._sandbox_attachment_pk_columns(conn=conn) == []:
            return
        conn.execute("DROP TABLE IF EXISTS sandbox_attachments_migrate")
        conn.execute(_schema_table_ddl(table="sandbox_attachments", name="sandbox_attachments_migrate"))
        conn.execute(
            """
            INSERT OR IGNORE INTO sandbox_attachments_migrate (
              sandbox_uid, experiment_id, attached_at, detached_at
            )
            SELECT sandbox_uid, experiment_id, attached_at, detached_at
            FROM sandbox_attachments
            """
        )
        conn.execute("DROP TABLE sandbox_attachments")
        conn.execute("ALTER TABLE sandbox_attachments_migrate RENAME TO sandbox_attachments")

    def _sandbox_attachment_pk_columns(self, *, conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute("PRAGMA table_info(sandbox_attachments)").fetchall()
        return [
            str(row["name"])
            for row in sorted(rows, key=lambda row: int(row["pk"] or 0))
            if int(row["pk"] or 0) > 0
        ]

    def _backfill_sandbox_attachments(self, *, conn: sqlite3.Connection) -> None:
        """Open the forward relation for legacy/un-attached rows; a no-op once filled."""
        conn.execute(_schema_table_ddl(table="sandbox_attachments"))
        if not self._has_column(conn=conn, table="sandboxes", column="experiment_id"):
            return
        # Only rows still missing their attachment — so re-runs after the first
        # upgrade do no work, while a partial upgrade still gets finished.
        rows = conn.execute(
            """
            SELECT sandbox_uid, experiment_id, requested_at, created_at, updated_at,
                   terminated_at, status
            FROM sandboxes
            WHERE COALESCE(sandbox_uid, '') != ''
              AND NOT EXISTS (
                SELECT 1 FROM sandbox_attachments a
                WHERE a.sandbox_uid = sandboxes.sandbox_uid
                  AND a.experiment_id = sandboxes.experiment_id
              )
            """
        ).fetchall()
        for row in rows:
            attached_at = (
                row["requested_at"]
                or row["created_at"]
                or row["updated_at"]
                or now_iso()
            )
            detached_at = None
            if row["terminated_at"] or row["status"] in {"terminated", "failed"}:
                detached_at = row["terminated_at"] or row["updated_at"] or attached_at
            conn.execute(
                """
                INSERT INTO sandbox_attachments (
                  sandbox_uid, experiment_id, attached_at, detached_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (row["sandbox_uid"], row["experiment_id"], attached_at, detached_at),
            )

    def _migrate_capability_hash(self) -> None:
        """Migrate review_requests.capability (plaintext) → capability_hash.

        Pre-Phase-7 databases stored the minted capability in plaintext under a
        column-level UNIQUE `capability` column. Phase 7 stores its sha256
        instead. SQLite cannot DROP a column carrying a column-level UNIQUE in
        place, so — exactly like _migrate_resources_unique — the table is
        rebuilt into the new shape (own connection, foreign_keys toggled off so
        the review_sessions/reviews FKs to review_requests(id) don't block the
        DROP/RENAME): `capability_hash` replaces `capability`, backfilled with
        the sha256 of the existing plaintext so already-issued tokens still
        resolve. A request whose plaintext was empty converges to the
        empty-string hash, which no presented token matches — voided, must be
        re-requested (documented acceptable cost). No-op on fresh DBs (the table
        does not exist yet) and once `capability` is already gone.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'review_requests'"
            ).fetchone()
            if table is None:
                return
            cols = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(review_requests)").fetchall()
            }
            if "capability" not in cols:
                return
            seq_expr = "created_seq" if "created_seq" in cols else "rowid"
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(_REVIEW_REQUESTS_REBUILD_DDL)
                conn.execute(
                    f"""
                    INSERT INTO review_requests_migrate (
                      id, project_id, target_type, target_id, role, reason,
                      capability_hash, status, target_snapshot_id,
                      producer_session_id, expires_at, created_at, created_seq
                    )
                    SELECT
                      id, project_id, target_type, target_id, role, reason,
                      '', status, target_snapshot_id, producer_session_id,
                      expires_at, created_at, {seq_expr}
                    FROM review_requests
                    """
                )
                # SQLite has no portable sha256(); rehash row-by-row in Python.
                for row in conn.execute(
                    "SELECT id, capability FROM review_requests"
                ).fetchall():
                    plaintext = str(row["capability"] or "")
                    conn.execute(
                        "UPDATE review_requests_migrate SET capability_hash = ? WHERE id = ?",
                        (
                            hash_secret(plaintext),
                            row["id"],
                        ),
                    )
                conn.execute("DROP TABLE review_requests")
                conn.execute(
                    "ALTER TABLE review_requests_migrate RENAME TO review_requests"
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.execute("PRAGMA foreign_keys = ON")
        finally:
            conn.close()

    def _ensure_columns(
        self,
        *,
        conn: sqlite3.Connection,
        table: str,
        columns: dict[str, str],
    ) -> set[str]:
        """Add missing columns; returns the names actually added."""
        existing = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        added: set[str] = set()
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                added.add(name)
        return added

    def _drop_columns(
        self,
        *,
        conn: sqlite3.Connection,
        table: str,
        columns: tuple[str, ...],
    ) -> None:
        """Drop columns that no longer appear in the live schema.

        Requires SQLite ≥ 3.35 for `ALTER TABLE ... DROP COLUMN`. Idempotent:
        a column already absent is silently skipped.
        """
        existing = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name in columns:
            if name in existing:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {name}")

    def _migrate_resources_unique(self) -> None:
        """Re-key `resources` uniqueness from `path` to `(project_id, path)`.

        The original schema declared `path TEXT NOT NULL UNIQUE`, which blocked
        two projects from registering the same repo-relative file. SQLite cannot
        drop a column-level UNIQUE in place, so detect the legacy autoindex and
        rebuild. No-op once the (project_id, path) unique index already exists
        (fresh databases get the new shape directly from SCHEMA).
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            if not self._resources_needs_unique_migration(conn=conn):
                return
            # PRAGMA foreign_keys cannot change inside a transaction; toggle it
            # off around the rebuild so DROP/RENAME don't trip referential checks
            # against resource_versions / resource_associations (both key on id).
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Single statement, so execute() (not executescript(), which would
                # force-commit our BEGIN and break the rebuild's atomicity).
                conn.execute(_RESOURCES_REBUILD_DDL)
                conn.execute(
                    """
                    INSERT INTO resources_migrate (
                      id, project_id, path, kind, title, current_version_id,
                      version_token, mtime_ns, size_bytes, observed_at, git_commit,
                      missing, deleted, created_by, created_at, updated_at
                    )
                    SELECT
                      id, project_id, path, kind, title, current_version_id,
                      version_token, mtime_ns, size_bytes, observed_at, git_commit,
                      missing, 0, created_by, created_at, updated_at
                    FROM resources
                    """
                )
                conn.execute("DROP TABLE resources")
                conn.execute("ALTER TABLE resources_migrate RENAME TO resources")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.execute("PRAGMA foreign_keys = ON")
        finally:
            conn.close()

    def _resources_needs_unique_migration(self, *, conn: sqlite3.Connection) -> bool:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'resources'"
        ).fetchone()
        if table is None:
            return False
        has_path_only = False
        has_project_path = False
        for idx in conn.execute("PRAGMA index_list(resources)").fetchall():
            if not idx["unique"]:
                continue
            cols = [
                str(info["name"])
                for info in conn.execute(
                    f"PRAGMA index_info({idx['name']})"
                ).fetchall()
            ]
            if cols == ["path"]:
                has_path_only = True
            elif cols == ["project_id", "path"]:
                has_project_path = True
        return has_path_only and not has_project_path


# Alias for composition code that wants to name the dialect explicitly; the
# primary name stays on the class so call sites and reprs are unchanged.
SqliteStateStore = StateStore


def next_created_seq(*, conn: Connection, table: str) -> int:
    """The next insertion-order value for ``table`` (see created_seq columns).

    MAX+1 inside the caller's open write transaction is race-free under the
    store's single-writer semantics: SQLite's BEGIN IMMEDIATE holds the write
    lock, and the Postgres dialect's transaction() holds the advisory lock,
    so no two writers compute the same value.
    """
    row = conn.execute(
        f"SELECT COALESCE(MAX(created_seq), 0) + 1 AS next_seq FROM {table}"
    ).fetchone()
    return int(row["next_seq"])


def row_to_dict(*, row: Row | Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Plain dict from a row of either dialect (sqlite3.Row or mapping)."""
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def rows_to_dicts(
    *, rows: Iterable[Row | Mapping[str, Any]]
) -> list[dict[str, Any]]:
    return [row_to_dict(row=row) or {} for row in rows]
