"""Full-fidelity tool-call recorder for the debug analyzer.

The activity log (`activity.py`) is a *bounded visibility feed*: it summarizes
arguments to IDs and caps results at 16 KB so it never balloons. That makes it
useless for the one thing debugging a context blow-up actually needs — reading
the *raw* request and response of a specific call.

`ToolCallStore` fills that gap. It keeps the FULL arguments and result of the
last `max_rows` tool calls in a dedicated SQLite file, so the UI can:

  - rank tools by how much they send back (with avg / p50 / p95 / max), and
  - drill into any single call and read the exact JSON the agent received.

Bounded by construction: a row cap (oldest evicted first) plus a per-payload
char cap (giant payloads stored as a truncation marker, while the recorded
size stays exact). It is local debug data, isolated in its own DB so churn and
the occasional `clear()` never touch the precious state DB.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from ..utils import now_iso
from .activity import SENSITIVE_KEYS, jsonable, payload_chars


DEFAULT_MAX_ROWS = 1500
# Per-payload display cap. Sizes (sent/received chars) are always recorded in
# full; only the stored *text* is capped, and only for the rare giant payload —
# 256 K chars is far more than anyone reads in a JSON viewer.
DEFAULT_MAX_PAYLOAD_CHARS = 256 * 1024

SORTABLE_COLUMNS = frozenset({"ts", "received_chars", "sent_chars", "duration_ms", "tool"})


class ToolCallStore:
    """SQLite-backed ring of full tool-call I/O, plus query/aggregate helpers."""

    def __init__(
        self,
        *,
        db_path: Path,
        max_rows: int = DEFAULT_MAX_ROWS,
        max_payload_chars: int = DEFAULT_MAX_PAYLOAD_CHARS,
        enabled: bool = True,
    ) -> None:
        self.db_path = db_path
        self.max_rows = max_rows
        self.max_payload_chars = max_payload_chars
        self.enabled = enabled
        self._lock = threading.Lock()
        if self.enabled:
            self._init_db()

    # ---------- write path ----------

    def record(
        self,
        *,
        tool: str,
        source: str,
        status: str,
        duration_ms: int,
        arguments: dict[str, Any],
        result: Any | None = None,
        error: str = "",
        error_code: str = "",
    ) -> None:
        """Persist one tool call with its full I/O. Never raises (telemetry)."""
        if not self.enabled:
            return
        try:
            # Redact bearer-like secrets before they hit disk (cloud plan Phase
            # 7). This store keeps full I/O, so review.request results and
            # review.start args would otherwise persist one-time capabilities.
            arguments = _redact_sensitive(arguments)
            result = _redact_sensitive(result)
            # Derive scope + entity target from the args so the UI can render a
            # project-scoped feed and an entity chip without re-parsing payloads.
            project_id = str(arguments.get("project_id") or "") if isinstance(arguments, dict) else ""
            target_type, target_id = self._target_of(arguments)
            args_text, args_trunc, sent_chars = self._encode(arguments)
            if status == "error":
                result_text = error or ""
                result_trunc = 0
                received_chars = len(result_text)
            else:
                result_text, result_trunc, received_chars = self._encode(result)
            with self._lock, self._db() as conn:
                conn.execute(
                    """
                    INSERT INTO tool_calls
                      (ts, tool, source, status, duration_ms, sent_chars, received_chars,
                       args_json, result_json, error_code, args_truncated, result_truncated,
                       project_id, target_type, target_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now_iso(), tool, source, status, int(duration_ms or 0),
                        sent_chars, received_chars, args_text, result_text,
                        error_code, args_trunc, result_trunc,
                        project_id, target_type, target_id,
                    ),
                )
                conn.execute(
                    "DELETE FROM tool_calls WHERE id <= "
                    "(SELECT MAX(id) FROM tool_calls) - ?",
                    (self.max_rows,),
                )
        except Exception:  # noqa: BLE001 — debug recording must never break a call
            pass

    @staticmethod
    def _target_of(arguments: Any) -> tuple[str | None, str | None]:
        """The entity an MCP call acts on, for the activity chip. Mirrors the
        UI's precedence; ``project_id`` is intentionally NOT a target — the feed
        is project-scoped, so a project chip would be redundant on every row."""
        if not isinstance(arguments, dict):
            return None, None
        if arguments.get("experiment_id"):
            return "experiment", str(arguments["experiment_id"])
        if arguments.get("claim_id"):
            return "claim", str(arguments["claim_id"])
        if arguments.get("resource_id"):
            return "resource", str(arguments["resource_id"])
        review = arguments.get("review_id") or arguments.get("request_id")
        if review:
            return "review", str(review)
        return None, None

    # ---------- read path ----------

    def stats(
        self,
        *,
        minutes: int | None = None,
        source: str | None = None,
        status: str | None = None,
        tool: str | None = None,
        project_id: str | None = None,
        project_ids: set[str] | list[str] | tuple[str, ...] | None = None,
        limit: int = 200,
        sort: str = "ts",
        order: str = "desc",
    ) -> dict[str, Any]:
        """Per-tool aggregate + a filtered/sorted slice of individual calls.

        Filters (time window, source, status, tool substring) are applied first;
        the per-tool aggregate and the call rows both describe that same filtered
        set. Aggregates include avg / p50 / p95 / max received so a tool that is
        *occasionally* huge is distinguishable from one that is *consistently*
        large. Call rows are lightweight (no payloads) — fetch a single call's
        full I/O with `get()`.
        """
        limit = max(1, min(int(limit), 2000))
        sort = sort if sort in SORTABLE_COLUMNS else "ts"
        reverse = str(order).lower() != "asc"
        empty_totals = {"calls": 0, "sent_chars": 0, "received_chars": 0, "error_calls": 0}
        base = {
            "calls": [],
            "by_tool": [],
            "totals": dict(empty_totals),
            "coverage": {"calls": 0, "stored": 0, "oldest_ts": None, "newest_ts": None, "capped": False},
            "filter": {"minutes": minutes, "source": source, "status": status, "tool": tool, "project_id": project_id},
        }
        if not self.enabled or not self.db_path.exists():
            return base

        where, params = self._build_where(
            minutes=minutes,
            source=source,
            status=status,
            tool=tool,
            project_id=project_id,
            project_ids=project_ids,
        )
        # Coverage describes ring eviction for THIS view's universe. The ring is
        # a single global capacity, so fullness is judged on the whole ring
        # (`ring`), but the displayed "stored" count and the eviction-window
        # check use the source+project-scoped rows (`scoped`) so a scoped view
        # does not report the all-source/all-project ring total.
        src_where, src_params = self._build_where(
            minutes=None,
            source=source,
            status=None,
            tool=None,
            project_id=project_id,
            project_ids=project_ids,
        )
        with self._lock, self._db() as conn:
            rows = conn.execute(
                f"SELECT id, ts, tool, source, status, duration_ms, sent_chars, "
                f"received_chars, error_code, project_id, target_type, target_id "
                f"FROM tool_calls{where}",
                params,
            ).fetchall()
            ring = conn.execute("SELECT COUNT(*) AS n FROM tool_calls").fetchone()
            scoped = conn.execute(
                f"SELECT COUNT(*) AS n, MIN(ts) AS o FROM tool_calls{src_where}",
                src_params,
            ).fetchone()

        calls = [dict(r) for r in rows]
        totals = dict(empty_totals)
        agg: dict[str, dict[str, Any]] = {}
        for c in calls:
            totals["calls"] += 1
            totals["sent_chars"] += c["sent_chars"]
            totals["received_chars"] += c["received_chars"]
            if c["status"] == "error":
                totals["error_calls"] += 1
            self._accumulate(agg, c)
        by_tool = sorted(
            (self._finalize_bucket(b) for b in agg.values()),
            key=lambda b: b["received_chars"],
            reverse=True,
        )

        calls.sort(key=lambda c: (c.get(sort) if c.get(sort) is not None else 0), reverse=reverse)
        oldest = min((c["ts"] for c in calls), default=None)
        newest = max((c["ts"] for c in calls), default=None)
        capped = self._coverage_capped(ring=ring, scoped=scoped, minutes=minutes)
        return {
            "calls": calls[:limit],
            "by_tool": by_tool,
            "totals": totals,
            "coverage": {
                "calls": totals["calls"],
                "stored": scoped["n"] if scoped else 0,
                "oldest_ts": oldest,
                "newest_ts": newest,
                "capped": capped,
            },
            "filter": {"minutes": minutes, "source": source, "status": status, "tool": tool},
        }

    def get(
        self,
        *,
        call_id: int,
        project_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        """Return one call's full record, with args/result parsed back to JSON."""
        if not self.enabled or not self.db_path.exists():
            return None
        if project_ids is not None:
            allowed = {str(pid) for pid in project_ids if str(pid)}
            if not allowed:
                return None
            placeholders = ", ".join("?" for _ in allowed)
            query = (
                "SELECT * FROM tool_calls WHERE id = ? "
                f"AND project_id IN ({placeholders})"
            )
            params: tuple[Any, ...] = (call_id, *sorted(allowed))
        else:
            query = "SELECT * FROM tool_calls WHERE id = ?"
            params = (call_id,)
        with self._lock, self._db() as conn:
            row = conn.execute(query, params).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["args"] = _safe_load(record.pop("args_json", ""))
        if record.get("status") == "error":
            # Errors store a plain message, not JSON.
            record["result"] = record.pop("result_json", "") or ""
        else:
            record["result"] = _safe_load(record.pop("result_json", ""))
        record["args_truncated"] = bool(record.get("args_truncated"))
        record["result_truncated"] = bool(record.get("result_truncated"))
        return record

    def clear(
        self,
        *,
        project_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Drop all recorded calls. Returns how many were removed."""
        if not self.enabled or not self.db_path.exists():
            return {"cleared": 0}
        if project_ids is not None:
            allowed = {str(pid) for pid in project_ids if str(pid)}
            if not allowed:
                return {"cleared": 0}
            placeholders = ", ".join("?" for _ in allowed)
            where = f" WHERE project_id IN ({placeholders})"
            params: tuple[Any, ...] = tuple(sorted(allowed))
        else:
            where = ""
            params = ()
        with self._lock, self._db() as conn:
            before = conn.execute(
                f"SELECT COUNT(*) AS n FROM tool_calls{where}", params
            ).fetchone()["n"]
            conn.execute(f"DELETE FROM tool_calls{where}", params)
        return {"cleared": before}

    # ---------- internals ----------

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """A connection that commits on success and always closes.

        `with sqlite3.connect(...)` only manages the transaction, not the handle,
        so without this the per-call connections would leak file descriptors.
        """
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_calls (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts TEXT NOT NULL,
                  tool TEXT NOT NULL,
                  source TEXT NOT NULL DEFAULT 'mcp',
                  status TEXT NOT NULL DEFAULT 'ok',
                  duration_ms INTEGER NOT NULL DEFAULT 0,
                  sent_chars INTEGER NOT NULL DEFAULT 0,
                  received_chars INTEGER NOT NULL DEFAULT 0,
                  args_json TEXT NOT NULL DEFAULT '',
                  result_json TEXT NOT NULL DEFAULT '',
                  error_code TEXT NOT NULL DEFAULT '',
                  args_truncated INTEGER NOT NULL DEFAULT 0,
                  result_truncated INTEGER NOT NULL DEFAULT 0,
                  project_id TEXT NOT NULL DEFAULT '',
                  target_type TEXT,
                  target_id TEXT
                )
                """
            )
            # Migrate DBs created before project scoping / entity-target chips.
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(tool_calls)")}
            added = []
            if "project_id" not in cols:
                conn.execute("ALTER TABLE tool_calls ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
                added.append("project_id")
            if "target_type" not in cols:
                conn.execute("ALTER TABLE tool_calls ADD COLUMN target_type TEXT")
                added.append("target_type")
            if "target_id" not in cols:
                conn.execute("ALTER TABLE tool_calls ADD COLUMN target_id TEXT")
                added.append("target_id")
            if added:
                self._backfill_scope(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_ts ON tool_calls(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls(tool)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_project ON tool_calls(project_id)")

    @staticmethod
    def _backfill_scope(conn: sqlite3.Connection) -> None:
        """Populate project_id / target_* on rows that predate those columns by
        reading the values back out of the stored args_json. Best-effort: needs
        the SQLite JSON1 extension; new rows are populated directly by record()."""
        try:
            conn.execute(
                "UPDATE tool_calls SET project_id = COALESCE(json_extract(args_json, '$.project_id'), '') "
                "WHERE project_id = ''"
            )
            for target_type, expr in (
                ("experiment", "json_extract(args_json, '$.experiment_id')"),
                ("claim", "json_extract(args_json, '$.claim_id')"),
                ("resource", "json_extract(args_json, '$.resource_id')"),
                ("review", "COALESCE(json_extract(args_json, '$.review_id'), json_extract(args_json, '$.request_id'))"),
            ):
                conn.execute(
                    f"UPDATE tool_calls SET target_type = ?, target_id = {expr} "
                    f"WHERE target_type IS NULL AND {expr} IS NOT NULL",
                    (target_type,),
                )
        except sqlite3.Error:
            pass

    def _encode(self, value: Any) -> tuple[str, int, int]:
        """Serialize a payload for storage. Returns (text, truncated, full_chars).

        `full_chars` is the true serialized size — recorded even when the stored
        text is replaced by a truncation marker, so size stats stay exact.
        """
        chars = payload_chars(value=value)
        try:
            text = json.dumps(jsonable(value=value), sort_keys=True)
        except (TypeError, ValueError):
            text = json.dumps(str(value))
        if len(text) > self.max_payload_chars:
            marker = json.dumps(
                {"_truncated": True, "_chars": chars, "preview": text[: self.max_payload_chars]}
            )
            return marker, 1, chars
        return text, 0, chars

    @staticmethod
    def _build_where(
        *,
        minutes: int | None,
        source: str | None,
        status: str | None,
        tool: str | None,
        project_id: str | None = None,
        project_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if minutes and minutes > 0:
            cutoff = (datetime.now(tz=UTC) - timedelta(minutes=minutes)).replace(microsecond=0)
            clauses.append("ts >= ?")
            params.append(cutoff.isoformat().replace("+00:00", "Z"))
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        elif project_ids is not None:
            allowed = sorted({str(pid) for pid in project_ids if str(pid)})
            if allowed:
                clauses.append(f"project_id IN ({', '.join('?' for _ in allowed)})")
                params.extend(allowed)
            else:
                clauses.append("1 = 0")
        if source and source != "all":
            clauses.append("source = ?")
            params.append(source)
        if status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        if tool:
            clauses.append("tool LIKE ?")
            params.append(f"%{tool}%")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    @staticmethod
    def _accumulate(agg: dict[str, dict[str, Any]], call: dict[str, Any]) -> None:
        bucket = agg.setdefault(
            call["tool"],
            {
                "tool": call["tool"],
                "calls": 0,
                "error_calls": 0,
                "sent_chars": 0,
                "received_chars": 0,
                "max_received_chars": 0,
                "max_sent_chars": 0,
                "_dur_sum": 0,
                "max_duration_ms": 0,
                "_recv_samples": [],
                "last_ts": None,
            },
        )
        bucket["calls"] += 1
        bucket["error_calls"] += 1 if call["status"] == "error" else 0
        bucket["sent_chars"] += call["sent_chars"]
        bucket["received_chars"] += call["received_chars"]
        bucket["max_received_chars"] = max(bucket["max_received_chars"], call["received_chars"])
        bucket["max_sent_chars"] = max(bucket["max_sent_chars"], call["sent_chars"])
        bucket["_dur_sum"] += call["duration_ms"]
        bucket["max_duration_ms"] = max(bucket["max_duration_ms"], call["duration_ms"])
        bucket["_recv_samples"].append(call["received_chars"])
        if bucket["last_ts"] is None or call["ts"] > bucket["last_ts"]:
            bucket["last_ts"] = call["ts"]

    @staticmethod
    def _finalize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
        count = bucket["calls"] or 1
        samples = sorted(bucket.pop("_recv_samples"))
        bucket["avg_received_chars"] = round(bucket["received_chars"] / count)
        bucket["avg_sent_chars"] = round(bucket["sent_chars"] / count)
        bucket["avg_duration_ms"] = round(bucket.pop("_dur_sum") / count)
        bucket["p50_received_chars"] = _percentile(samples, 50)
        bucket["p95_received_chars"] = _percentile(samples, 95)
        return bucket

    def _coverage_capped(
        self, *, ring: sqlite3.Row | None, scoped: sqlite3.Row | None, minutes: int | None
    ) -> bool:
        """True when the requested window may extend past evicted history.

        Ring fullness is global (eviction drops the oldest rows of any source),
        but the eviction-window test uses the source-scoped oldest timestamp so
        the answer reflects the filtered view's universe.
        """
        if ring is None or ring["n"] < self.max_rows:
            return False
        if not minutes or minutes <= 0:
            return True  # "all", but the ring is full → older calls were evicted
        cutoff = datetime.now(tz=UTC) - timedelta(minutes=minutes)
        oldest = _parse_ts(scoped["o"]) if scoped else None
        # Ring is full AND the oldest still-stored matching call is inside the
        # window → there may be matching calls that were already evicted.
        return oldest is None or oldest >= cutoff


def _percentile(sorted_values: list[int], q: int) -> int:
    if not sorted_values:
        return 0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = max(1, -(-q * len(sorted_values) // 100))  # ceil(q/100 * n)
    return sorted_values[min(rank, len(sorted_values)) - 1]


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _safe_load(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]" if key in SENSITIVE_KEYS else _redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive(item) for item in value)
    return value
