"""Record-store state management: dialect-neutral base + the SQLite dialect.

``BaseStateStore`` defines the contract the services were written against;
``StateStore`` (= ``SqliteStateStore``) is the local-mode SQLite dialect and
the historical default. The Postgres dialect for the cloud control plane
lives in ``dialects.py`` (cloud plan Phase 6).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..utils import NotFoundError, ValidationError
from ..utils import new_id
from ..utils import now_iso


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  -- Tenancy (cloud plan Phase 6): ownership lives on the project row; every
  -- other table reaches its tenant through project_id. Local mode is the
  -- fixed 'local' tenant. Denormalized per-table tenant columns and
  -- enforcement land with Phase 7's auth, not before.
  tenant_id TEXT NOT NULL DEFAULT 'local',
  created_at TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS syntheses (
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
  -- status) and claim statuses at that moment. The synthesis review judges the
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

CREATE TABLE IF NOT EXISTS sandboxes (
  experiment_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  sandbox_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'none',
  gpu TEXT NOT NULL DEFAULT '',
  cpu REAL NOT NULL DEFAULT 0,
  memory INTEGER NOT NULL DEFAULT 0,
  -- Provider-bundled machine SKU + datacenter, for backends (Lambda Labs) that
  -- procure a fixed instance type rather than composing cpu/memory. Empty for
  -- Modal, which sets gpu/cpu/memory above instead.
  instance_type TEXT NOT NULL DEFAULT '',
  region TEXT NOT NULL DEFAULT '',
  -- Provider price quote at provision (cloud plan Phase 7): captured from the
  -- catalog option (Lambda has it; Modal leaves 0). Recorded on the row AND
  -- appended to sandbox_generations so per-generation spend is reconstructable
  -- even though the row itself is upsert-overwritten per experiment.
  price_usd_per_hour REAL NOT NULL DEFAULT 0,
  time_limit INTEGER NOT NULL DEFAULT 0,
  ssh_host TEXT NOT NULL DEFAULT '',
  ssh_port INTEGER NOT NULL DEFAULT 0,
  ssh_user TEXT NOT NULL DEFAULT 'root',
  workdir TEXT NOT NULL DEFAULT '',
  sync_dir TEXT NOT NULL DEFAULT '',
  unsynced_dir TEXT NOT NULL DEFAULT '',
  sandbox_data_dir TEXT NOT NULL DEFAULT '',
  -- Files delivered by the initial experiment-folder push (-1 = unknown).
  initial_pushed INTEGER NOT NULL DEFAULT -1,
  -- Management keypair reference (cloud plan Phase 5, fixed decision 4):
  -- non-empty when a control-plane management key was minted for this
  -- sandbox. A key-store reference (the experiment id) — never key material.
  mgmt_key_ref TEXT NOT NULL DEFAULT '',
  -- Expiry parachute record (cloud plan Phase 5, fixed decision 5): set when
  -- a reap/release whose final pull failed uploaded the experiment dir to
  -- the blob store over the management channel. State machine:
  -- '' (none) → 'uploaded' → 'restored' | 'failed'. The object key is
  -- namespace/sha256 in the blob store; expires_at is the TTL backstop.
  parachute_state TEXT NOT NULL DEFAULT '',
  parachute_object_key TEXT NOT NULL DEFAULT '',
  parachute_sha256 TEXT NOT NULL DEFAULT '',
  parachute_size_bytes INTEGER NOT NULL DEFAULT 0,
  parachute_expires_at TEXT,
  volume_name TEXT NOT NULL DEFAULT '',
  -- Observability dashboards exposed inside the sandbox (MLflow at 5000,
  -- TensorBoard at 6006), surfaced to the user as provider URLs (Modal HTTPS
  -- tunnels) or daemon-owned local SSH forwards (Lambda Labs). JSON object
  -- keyed by dashboard name. Empty '{}' when no dashboards were exposed.
  dashboards_json TEXT NOT NULL DEFAULT '{}',
  sandbox_name TEXT NOT NULL DEFAULT '',
  phase TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  provision_started_at TEXT,
  requested_at TEXT,
  expires_at TEXT,
  last_seen_at TEXT,
  terminated_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  -- Insertion-order column replacing rowid ordering (cloud plan Phase 6).
  created_seq INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(project_id) REFERENCES projects(id)
);

-- Figures submitted alongside a report (cloud plan Phase 2): when a report is
-- associated, each resolvable relative image link's bytes are captured to the
-- blob store and recorded here, keyed by the report's pinned version. The
-- report lint checks THIS mapping (submitted figures), never the disk.
CREATE TABLE IF NOT EXISTS report_figures (
  report_version_id TEXT NOT NULL,
  link_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (report_version_id, link_path),
  FOREIGN KEY(report_version_id) REFERENCES resource_versions(id)
);

-- Sync leases (cloud plan Phase 4, fixed decision 8): the exclusive
-- per-experiment byte-movement authority. Cloud-held — the only safe
-- multi-client coordinator — with TTL + takeover; every sandbox sync/push/
-- final-pull is authorized by the experiment's lease and its completion
-- report is validated against the lease id. One row per experiment: the
-- current holder. Expired rows are takeover-able in place.
CREATE TABLE IF NOT EXISTS sync_leases (
  experiment_id TEXT PRIMARY KEY,
  lease_id TEXT NOT NULL,
  holder_client_id TEXT NOT NULL,
  ttl_seconds INTEGER NOT NULL,
  expires_at TEXT NOT NULL,
  renewed_at TEXT NOT NULL
);

-- MLflow metrics snapshots as control-plane records (cloud plan Phase 5):
-- reviews and the UI read metrics without the user machine online. One row
-- per experiment — the latest snapshot, mirroring the daemon's local file
-- cache (which is kept as-is). snapshot_json is the full extracted record
-- (captured_at + source + experiments/runs/metrics).
CREATE TABLE IF NOT EXISTS metrics_snapshots (
  experiment_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  snapshot_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

-- Identity (cloud plan Phase 7, §3.2). Dormant in local mode: the implicit
-- 'local' tenant needs no row here and auth is off on loopback, so these
-- tables stay empty until the control plane provisions tenants out of band.
-- A tenant owns projects (projects.tenant_id) and, through them, every
-- project-scoped record. Bearer tokens are minted per tenant and resolved to
-- a Principal by AuthService (services/identity.py).
CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

-- API bearer tokens (cloud plan Phase 7). The PK is the sha256 hash of the
-- token — the plaintext is shown once at mint and never stored (see
-- services/identity.py for why a fast hash is correct for high-entropy bearer
-- secrets). expires_at/revoked_at are nullable: NULL means "no expiry" /
-- "not revoked". Lookups are by hash with a constant-time compare.
CREATE TABLE IF NOT EXISTS api_tokens (
  token_hash TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  expires_at TEXT,
  revoked_at TEXT
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
  blob_bytes_budget INTEGER
);

-- Per-generation sandbox spend ledger (cloud plan Phase 7). The sandboxes row
-- is upsert-overwritten per experiment, so it cannot reconstruct historical
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
  instance_type TEXT NOT NULL DEFAULT '',
  gpu TEXT NOT NULL DEFAULT '',
  price_usd_per_hour REAL NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  created_seq INTEGER NOT NULL DEFAULT 0
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
)


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

    def connect(self) -> sqlite3.Connection:
        raise NotImplementedError

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        raise NotImplementedError

    def _apply_migrations(self, *, conn: sqlite3.Connection) -> None:
        """Apply unapplied ledger migrations in order, recording each."""
        applied = {
            int(row["version"])
            for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        for version, name, statement in MIGRATIONS:
            if version in applied:
                continue
            conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, now_iso()),
            )

    def require_project_id(
        self,
        *,
        conn: sqlite3.Connection,
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
        conn: sqlite3.Connection,
        project_id: str,
        event_type: str,
        target_type: str = "",
        target_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO events (project_id, type, target_type, target_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, event_type, target_type, target_id, json.dumps(payload or {}, sort_keys=True), now_iso()),
        )

    def recent_events(self, *, project_id: str | None, limit: int = 100) -> dict[str, Any]:
        conn = self.connect()
        try:
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
        finally:
            conn.close()


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
            conn.executescript(SCHEMA)
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
            columns={"tenant_id": "TEXT NOT NULL DEFAULT 'local'"},
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
        # Cloud-split Phase 7: review sessions bind to the authenticated tenant
        # (single 'local' tenant in local mode); legacy rows predate the column.
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
                "phase": "TEXT NOT NULL DEFAULT ''",
                "detail": "TEXT NOT NULL DEFAULT ''",
                "error": "TEXT NOT NULL DEFAULT ''",
                "provision_started_at": "TEXT",
                "sandbox_data_dir": "TEXT NOT NULL DEFAULT ''",
                "sync_dir": "TEXT NOT NULL DEFAULT ''",
                "unsynced_dir": "TEXT NOT NULL DEFAULT ''",
                # Phase 1 observability dashboards: MLflow + TensorBoard URLs
                # surfaced from the in-sandbox servers through provider URLs or
                # daemon-owned local SSH forwards. JSON object keyed by dashboard
                # name; '{}' on older rows and sandboxes where none were exposed.
                "dashboards_json": "TEXT NOT NULL DEFAULT '{}'",
                # Lambda-default (June 2026): provider-bundled machine SKU +
                # datacenter for backends that procure a fixed instance type.
                "instance_type": "TEXT NOT NULL DEFAULT ''",
                "region": "TEXT NOT NULL DEFAULT ''",
                # Cloud-split Phase 7 (June 2026): provider price quote captured
                # at provision for cost governance. 0 on rows that predate it.
                "price_usd_per_hour": "REAL NOT NULL DEFAULT 0",
                # Experiment-folder sync (June 2026): how many files the initial
                # push delivered to the sandbox. -1 = unknown (pre-change rows
                # or provisioning still in flight); 0 is meaningful — the local
                # experiment folder had nothing eligible to push.
                "initial_pushed": "INTEGER NOT NULL DEFAULT -1",
                # Cloud-split Phase 5 (June 2026): management keypair reference
                # — non-empty when a control-plane management key exists for
                # this sandbox. Never key material.
                "mgmt_key_ref": "TEXT NOT NULL DEFAULT ''",
                # Cloud-split Phase 5 (June 2026): expiry-parachute record —
                # the blob-store object a failed final pull was rescued to.
                "parachute_state": "TEXT NOT NULL DEFAULT ''",
                "parachute_object_key": "TEXT NOT NULL DEFAULT ''",
                "parachute_sha256": "TEXT NOT NULL DEFAULT ''",
                "parachute_size_bytes": "INTEGER NOT NULL DEFAULT 0",
                "parachute_expires_at": "TEXT",
            },
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
        # (.research_plugin/dataplane_state.sqlite). Both columns were always
        # derivable, so no value migration is needed.
        self._drop_columns(
            conn=conn,
            table="sandboxes",
            columns=("key_path", "local_sync_dir"),
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
            "syntheses",
            "sandboxes",
        ):
            added = self._ensure_columns(
                conn=conn,
                table=table,
                columns={"created_seq": "INTEGER NOT NULL DEFAULT 0"},
            )
            if "created_seq" in added:
                conn.execute(f"UPDATE {table} SET created_seq = rowid")

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
                            hashlib.sha256(plaintext.encode("utf-8")).hexdigest(),
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


def next_created_seq(*, conn: sqlite3.Connection, table: str) -> int:
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


def row_to_dict(*, row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Plain dict from a row of either dialect (sqlite3.Row or mapping)."""
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def rows_to_dicts(*, rows: list[sqlite3.Row] | list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [row_to_dict(row=row) or {} for row in rows]
