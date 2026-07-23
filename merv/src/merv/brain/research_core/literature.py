"""Literature review service: one living sectioned document + a papers ledger.

Sections are mutable envelopes with per-row compare-and-swap revisions; the
events table (full post-state per mutation) is the document's history, like
claims. Papers are deduplicated per project by a normalized identity key and
linked to sections/experiments/claims through paper_links; the rendered
References block is derived from those rows and never hand-edited.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from contextlib import closing
from typing import Any, Protocol

from ..kernel.state.store import BaseStateStore, next_created_seq, rows_to_dicts
from ..kernel.utils import NotFoundError, ValidationError, new_id, now_iso

SUMMARY_TITLE = "General Summary"
MAX_SECTIONS = 64
MAX_BODY_BYTES = 16_000
MAX_TITLE_CHARS = 200
MAX_TLDR_CHARS = 500
MAX_NOTE_CHARS = 300
PAPER_LINK_TARGET_TYPES = ("litreview_section", "experiment", "claim")
_TARGET_TABLES = {
    "litreview_section": "litreview_sections",
    "experiment": "experiments",
    "claim": "claims",
}

# New-style (2107.03374) and legacy (cond-mat/0703470, cs.LG/0112017) arXiv
# ids, with an optional version suffix that identity ignores.
_ARXIV_ID_RE = re.compile(
    r"^(?:(\d{4}\.\d{4,5})|([a-z-]+(?:\.[A-Za-z]{2})?/\d{7}))(?:v\d+)?$"
)
_ARXIV_URL_RE = re.compile(
    r"^https?://(?:www\.|export\.)?(?:arxiv|ar5iv)\.org/"
    r"(?:abs|pdf|html|format|e-print)/([^?#]+?)(?:\.pdf)?/?(?:$|[?#])",
    re.IGNORECASE,
)
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
_DOI_URL_RE = re.compile(
    r"^https?://(?:dx\.)?doi\.org/(10\.\d{4,9}/[^?#]+)", re.IGNORECASE
)


class PaperUnfurl(Protocol):
    """Port for fetching paper metadata; the adapter enforces its own hard
    host allowlist per hop (feed_unfurl.AllowlistedPaperUnfurl)."""

    def allowed(self, url: str) -> bool: ...

    def unfurl(self, url: str) -> dict[str, Any]: ...


def normalize_paper_identity(
    *, url: str = "", doi: str = "", arxiv_id: str = ""
) -> tuple[str, str, str]:
    """Resolve (norm_key, source_kind, canonical_url) for a citation input."""
    doi = doi.strip()
    arxiv_id = arxiv_id.strip()
    url = url.strip()
    if arxiv_id:
        m = _ARXIV_ID_RE.match(arxiv_id)
        if m is None:
            raise ValidationError(f"not a recognizable arXiv id: {arxiv_id}")
        bare = m.group(1) or m.group(2)
        return f"arxiv:{bare}", "arxiv", f"https://arxiv.org/abs/{bare}"
    if doi:
        if _DOI_RE.match(doi) is None:
            raise ValidationError(f"not a recognizable DOI: {doi}")
        return f"doi:{doi.casefold()}", "doi", f"https://doi.org/{doi}"
    if not url:
        raise ValidationError("provide url, doi, or arxiv_id")
    m = _ARXIV_URL_RE.match(url)
    if m:
        ident = _ARXIV_ID_RE.match(urllib.parse.unquote(m.group(1)))
        if ident is not None:
            bare = ident.group(1) or ident.group(2)
            return f"arxiv:{bare}", "arxiv", f"https://arxiv.org/abs/{bare}"
    m = _DOI_URL_RE.match(url)
    if m:
        doi_part = urllib.parse.unquote(m.group(1)).rstrip("/")
        return f"doi:{doi_part.casefold()}", "doi", f"https://doi.org/{doi_part}"
    return _normalize_url_key(url), "url", url


def _normalize_url_key(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValidationError(f"not a fetchable paper URL: {url}")
    host = parsed.hostname.casefold()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError(f"invalid port in URL: {url}") from exc
    # Only the scheme's own default port is dropped: https://host:80 is a
    # different endpoint from https://host and must not dedupe into it.
    default_port = 443 if parsed.scheme == "https" else 80
    if port is not None and port != default_port:
        host = f"{host}:{port}"
    path = parsed.path or "/"
    if path == "/":
        path = ""
    key = f"{host}{path}"
    if parsed.query:
        key += f"?{parsed.query}"
    return f"url:{key}"


class LiteratureService:
    def __init__(self, *, store: BaseStateStore, unfurl: PaperUnfurl) -> None:
        self.store = store
        self.unfurl = unfurl

    # ------------------------------------------------------------------ reads

    def view(
        self,
        *,
        project_id: str | None = None,
        section: str = "",
        papers: bool = False,
        cursor: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 50))
        with closing(self.store.connect()) as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            if section:
                row = self._resolve_section(conn=conn, project_id=project_id, address=section)
                return {
                    "section": self._present_section(
                        conn=conn, project_id=project_id, row=row, full=True
                    )
                }
            if papers:
                return self._paper_page(
                    conn=conn, project_id=project_id, cursor=int(cursor), limit=limit
                )
            return self._overview(conn=conn, project_id=project_id)

    def ui_snapshot(self, *, project_id: str | None = None) -> dict[str, Any]:
        """The whole review in one read — UI endpoint only, no agent tool."""
        with closing(self.store.connect()) as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            overview = self._overview(conn=conn, project_id=project_id)
            sections = conn.execute(
                """
                SELECT * FROM litreview_sections
                WHERE project_id = ? AND kind = 'section'
                ORDER BY position, created_seq
                """,
                (project_id,),
            ).fetchall()
            papers = conn.execute(
                "SELECT * FROM papers WHERE project_id = ? ORDER BY created_seq",
                (project_id,),
            ).fetchall()
            links = conn.execute(
                "SELECT paper_id, target_type, target_id, note FROM paper_links "
                "WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            by_paper: dict[str, list[dict[str, Any]]] = {}
            for link in links:
                by_paper.setdefault(str(link["paper_id"]), []).append(
                    {k: link[k] for k in ("target_type", "target_id", "note")}
                )
            ledger = []
            for row in papers:
                item = dict(row)
                item["authors"] = json.loads(item.pop("authors_json") or "[]")
                item.pop("created_seq", None)
                item["links"] = by_paper.get(str(row["id"]), [])
                ledger.append(item)
            return {
                "summary": overview["summary"],
                "sections": [
                    self._present_section(
                        conn=conn, project_id=project_id, row=row, full=True
                    )
                    for row in sections
                ],
                "papers": ledger,
            }

    def _overview(self, *, conn: Any, project_id: str) -> dict[str, Any]:
        summary = conn.execute(
            "SELECT * FROM litreview_sections WHERE project_id = ? AND kind = 'summary'",
            (project_id,),
        ).fetchone()
        sections = conn.execute(
            """
            SELECT id, title, tldr, position, revision, updated_at
            FROM litreview_sections
            WHERE project_id = ? AND kind = 'section'
            ORDER BY position, created_seq
            """,
            (project_id,),
        ).fetchall()
        paper_count = conn.execute(
            "SELECT COUNT(*) AS n FROM papers WHERE project_id = ?", (project_id,)
        ).fetchone()
        return {
            "summary": (
                {
                    **self._present_section(
                        conn=conn, project_id=project_id, row=summary, full=True
                    ),
                    "exists": True,
                }
                if summary is not None
                # Synthesized when absent — reads never create it; the first
                # write (edit op=edit, section='summary', expected_revision=0)
                # does.
                else {
                    "id": "",
                    "title": SUMMARY_TITLE,
                    "tldr": "",
                    "body": "",
                    "revision": 0,
                    "exists": False,
                }
            ),
            "sections": rows_to_dicts(rows=sections),
            "paper_count": int(paper_count["n"]),
        }

    def _paper_page(
        self, *, conn: Any, project_id: str, cursor: int, limit: int
    ) -> dict[str, Any]:
        rows = conn.execute(
            """
            SELECT id, norm_key, url, title, authors_json, year, source_kind,
                   fetch_status, created_seq
            FROM papers
            WHERE project_id = ? AND created_seq > ?
            ORDER BY created_seq
            LIMIT ?
            """,
            (project_id, cursor, limit + 1),
        ).fetchall()
        page, more = rows[:limit], len(rows) > limit
        papers = []
        for row in page:
            links = conn.execute(
                "SELECT target_type, target_id FROM paper_links "
                "WHERE paper_id = ? AND project_id = ?",
                (row["id"], project_id),
            ).fetchall()
            item = dict(row)
            item["authors"] = json.loads(item.pop("authors_json") or "[]")
            item["links"] = rows_to_dicts(rows=links)
            papers.append(item)
        return {
            "papers": papers,
            "next_cursor": int(page[-1]["created_seq"]) if more and page else None,
        }

    def _present_section(
        self, *, conn: Any, project_id: str, row: Any, full: bool
    ) -> dict[str, Any]:
        data = dict(row)
        data.pop("created_seq", None)
        if full:
            links = conn.execute(
                """
                SELECT p.id, p.title, p.url
                FROM paper_links l JOIN papers p ON p.id = l.paper_id
                WHERE l.target_type = 'litreview_section' AND l.target_id = ?
                  AND l.project_id = ? AND p.project_id = ?
                ORDER BY p.created_seq
                """,
                (data["id"], project_id, project_id),
            ).fetchall()
            data["cited_papers"] = rows_to_dicts(rows=links)
        return data

    # ----------------------------------------------------------------- writes

    def edit(
        self,
        *,
        project_id: str | None = None,
        op: str,
        section: str = "",
        title: str = "",
        tldr: str = "",
        body: str = "",
        expected_revision: int | None = None,
        order: list[dict[str, Any]] | None = None,
        created_by: str = "",
    ) -> dict[str, Any]:
        if op not in ("add", "edit", "delete", "reorder"):
            raise ValidationError(f"unknown op: {op}")
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            if op == "add":
                return self._add(
                    conn=conn, project_id=project_id, title=title, tldr=tldr,
                    body=body, created_by=created_by,
                )
            if op == "reorder":
                return self._reorder(
                    conn=conn, project_id=project_id, order=order or [],
                )
            if expected_revision is None:
                raise ValidationError(f"op={op} requires expected_revision")
            if op == "edit":
                return self._edit(
                    conn=conn, project_id=project_id, address=section, title=title,
                    tldr=tldr, body=body, expected_revision=int(expected_revision),
                    created_by=created_by,
                )
            return self._delete(
                conn=conn, project_id=project_id, address=section,
                expected_revision=int(expected_revision),
            )

    def _add(
        self, *, conn: Any, project_id: str, title: str, tldr: str, body: str,
        created_by: str,
    ) -> dict[str, Any]:
        title, tldr = self._check_text(title=title, tldr=tldr, body=body)
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM litreview_sections WHERE project_id = ? AND kind = 'section'",
            (project_id,),
        ).fetchone()
        if int(count["n"]) >= MAX_SECTIONS:
            raise ValidationError(f"section limit reached ({MAX_SECTIONS})")
        self._check_title_free(conn=conn, project_id=project_id, title=title)
        position = conn.execute(
            "SELECT COALESCE(MAX(position), 0) AS p FROM litreview_sections "
            "WHERE project_id = ? AND kind = 'section'",
            (project_id,),
        ).fetchone()
        now = now_iso()
        section_id = new_id(prefix="lit")
        conn.execute(
            """
            INSERT INTO litreview_sections
              (id, project_id, kind, title, tldr, body, position, revision,
               created_by, created_seq, created_at, updated_at)
            VALUES (?, ?, 'section', ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                section_id, project_id, title, tldr, body,
                int(position["p"]) + 1, created_by,
                next_created_seq(conn=conn, table="litreview_sections"), now, now,
            ),
        )
        self._section_event(
            conn=conn, project_id=project_id, event="litreview.section_added",
            section_id=section_id,
        )
        return {
            "section": self._read_section(
                conn=conn, project_id=project_id, section_id=section_id
            )
        }

    def _edit(
        self, *, conn: Any, project_id: str, address: str, title: str, tldr: str,
        body: str, expected_revision: int, created_by: str,
    ) -> dict[str, Any]:
        if self._is_summary_address(address):
            existing = conn.execute(
                "SELECT * FROM litreview_sections WHERE project_id = ? AND kind = 'summary'",
                (project_id,),
            ).fetchone()
            if existing is None:
                # First write creates the summary; expected_revision=0 is the
                # caller asserting "it does not exist yet".
                if expected_revision != 0:
                    raise ValidationError(
                        "summary does not exist yet; pass expected_revision=0 to create it"
                    )
                _, tldr = self._check_text(title=SUMMARY_TITLE, tldr=tldr, body=body)
                now = now_iso()
                section_id = new_id(prefix="lit")
                conn.execute(
                    """
                    INSERT INTO litreview_sections
                      (id, project_id, kind, title, tldr, body, position, revision,
                       created_by, created_seq, created_at, updated_at)
                    VALUES (?, ?, 'summary', ?, ?, ?, 0, 1, ?, ?, ?, ?)
                    """,
                    (
                        section_id, project_id, SUMMARY_TITLE, tldr, body, created_by,
                        next_created_seq(conn=conn, table="litreview_sections"),
                        now, now,
                    ),
                )
                self._section_event(
                    conn=conn, project_id=project_id,
                    event="litreview.section_edited", section_id=section_id,
                )
                return {
                    "section": self._read_section(
                        conn=conn, project_id=project_id, section_id=section_id
                    )
                }
            row = existing
        else:
            row = self._resolve_section(conn=conn, project_id=project_id, address=address)
        if int(row["revision"]) != expected_revision:
            raise ValidationError(
                f"stale revision for {row['id']}: expected {expected_revision}, "
                f"current {row['revision']} (tldr: {row['tldr']})"
            )
        next_title = title or str(row["title"])
        if row["kind"] == "summary":
            next_title = SUMMARY_TITLE  # the summary's title is fixed
        next_tldr = tldr or str(row["tldr"])
        next_body = body if body else str(row["body"])
        next_title, next_tldr = self._check_text(
            title=next_title, tldr=next_tldr, body=next_body
        )
        if next_title.casefold() != str(row["title"]).casefold():
            self._check_title_free(conn=conn, project_id=project_id, title=next_title)
        # revision in the WHERE clause makes this a database-level CAS, not
        # just the check-then-act read above.
        cursor = conn.execute(
            """
            UPDATE litreview_sections
            SET title = ?, tldr = ?, body = ?, revision = revision + 1, updated_at = ?
            WHERE id = ? AND project_id = ? AND revision = ?
            """,
            (
                next_title, next_tldr, next_body, now_iso(),
                row["id"], project_id, expected_revision,
            ),
        )
        if cursor.rowcount != 1:
            raise ValidationError(
                f"stale revision for {row['id']}: it changed mid-write — re-read and retry"
            )
        self._section_event(
            conn=conn, project_id=project_id, event="litreview.section_edited",
            section_id=str(row["id"]),
        )
        return {
            "section": self._read_section(
                conn=conn, project_id=project_id, section_id=str(row["id"])
            )
        }

    def _delete(
        self, *, conn: Any, project_id: str, address: str, expected_revision: int
    ) -> dict[str, Any]:
        row = self._resolve_section(conn=conn, project_id=project_id, address=address)
        if row["kind"] == "summary":
            raise ValidationError("the General Summary cannot be deleted")
        if int(row["revision"]) != expected_revision:
            raise ValidationError(
                f"stale revision for {row['id']}: expected {expected_revision}, "
                f"current {row['revision']} (tldr: {row['tldr']})"
            )
        snapshot = self._read_section(
            conn=conn, project_id=project_id, section_id=str(row["id"])
        )
        conn.execute(
            "DELETE FROM paper_links WHERE project_id = ? "
            "AND target_type = 'litreview_section' AND target_id = ?",
            (project_id, row["id"]),
        )
        cursor = conn.execute(
            "DELETE FROM litreview_sections "
            "WHERE id = ? AND project_id = ? AND revision = ?",
            (row["id"], project_id, expected_revision),
        )
        if cursor.rowcount != 1:
            raise ValidationError(
                f"stale revision for {row['id']}: it changed mid-write — re-read and retry"
            )
        self.store.record_event(
            conn=conn, project_id=project_id, event_type="litreview.section_deleted",
            target_type="litreview_section", target_id=str(row["id"]), payload=snapshot,
        )
        return {"deleted": str(row["id"])}

    def _reorder(
        self, *, conn: Any, project_id: str, order: list[dict[str, Any]]
    ) -> dict[str, Any]:
        current = conn.execute(
            "SELECT id, revision FROM litreview_sections "
            "WHERE project_id = ? AND kind = 'section'",
            (project_id,),
        ).fetchall()
        want = {str(item.get("id", "")): int(item.get("revision", -1)) for item in order}
        have = {str(r["id"]): int(r["revision"]) for r in current}
        if want != have or len(order) != len(current):
            raise ValidationError(
                "reorder requires the complete current outline as {id, revision} "
                "pairs; it is stale or incomplete — re-read litreview.view"
            )
        now = now_iso()
        for position, item in enumerate(order, start=1):
            cursor = conn.execute(
                "UPDATE litreview_sections "
                "SET position = ?, revision = revision + 1, updated_at = ? "
                "WHERE id = ? AND project_id = ? AND revision = ?",
                (position, now, str(item["id"]), project_id, have[str(item["id"])]),
            )
            if cursor.rowcount != 1:
                raise ValidationError(
                    f"stale revision for {item['id']}: the outline changed "
                    "mid-write — re-read litreview.view and retry"
                )
        # Post-state per section (bodies are unchanged by reorder, so the
        # order envelope is the full history this event needs).
        rows = conn.execute(
            "SELECT id, title, position, revision FROM litreview_sections "
            "WHERE project_id = ? AND kind = 'section' ORDER BY position",
            (project_id,),
        ).fetchall()
        self.store.record_event(
            conn=conn, project_id=project_id, event_type="litreview.sections_reordered",
            target_type="litreview_section", target_id="",
            payload={"order": rows_to_dicts(rows=rows)},
        )
        return {"order": [str(item["id"]) for item in order]}

    def cite(
        self,
        *,
        project_id: str | None = None,
        url: str = "",
        doi: str = "",
        arxiv_id: str = "",
        targets: list[dict[str, str]] | None = None,
        note: str = "",
        title: str = "",
        created_by: str = "",
    ) -> dict[str, Any]:
        note = note.strip()
        if len(note) > MAX_NOTE_CHARS:
            raise ValidationError(f"note exceeds {MAX_NOTE_CHARS} characters")
        norm_key, source_kind, canonical_url = normalize_paper_identity(
            url=url, doi=doi, arxiv_id=arxiv_id
        )
        # Scope is validated before any network I/O: a bad project must never
        # trigger an outbound fetch.
        with closing(self.store.connect()) as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
        # Network work happens before the write transaction opens.
        card: dict[str, Any] | None = None
        fetch_status = "manual"
        if self.unfurl.allowed(canonical_url):
            try:
                card = self.unfurl.unfurl(canonical_url)
                fetch_status = "fetched"
            except Exception:  # noqa: BLE001 - fetch failure must not block the cite
                fetch_status = "failed"
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            paper_id, dedupe = self._upsert_paper(
                conn=conn, project_id=project_id, norm_key=norm_key,
                source_kind=source_kind, canonical_url=canonical_url,
                fetch_status=fetch_status, card=card, fallback_title=title.strip(),
                created_by=created_by,
            )
            linked = self._link_targets(
                conn=conn, project_id=project_id, paper_id=paper_id,
                targets=targets or [], note=note, created_by=created_by,
            )
            paper = dict(
                conn.execute(
                    "SELECT * FROM papers WHERE id = ? AND project_id = ?",
                    (paper_id, project_id),
                ).fetchone()
            )
            paper["authors"] = json.loads(paper.pop("authors_json") or "[]")
            paper.pop("created_seq", None)
            self.store.record_event(
                conn=conn, project_id=project_id, event_type="litreview.paper_cited",
                target_type="paper", target_id=paper_id,
                payload={**paper, "new_links": linked, "deduplicated": dedupe},
            )
            return {"paper": paper, "deduplicated": dedupe, "new_links": linked}

    # ---------------------------------------------------------------- helpers

    def _upsert_paper(
        self, *, conn: Any, project_id: str, norm_key: str, source_kind: str,
        canonical_url: str, fetch_status: str, card: dict[str, Any] | None,
        fallback_title: str, created_by: str,
    ) -> tuple[str, bool]:
        existing = conn.execute(
            "SELECT * FROM papers WHERE project_id = ? AND norm_key = ?",
            (project_id, norm_key),
        ).fetchone()
        meta = {
            "title": (card or {}).get("title") or fallback_title,
            "authors_json": json.dumps((card or {}).get("authors") or []),
            "year": str((card or {}).get("year") or ""),
            "description": (card or {}).get("description") or "",
        }
        if existing is not None:
            # Fetched metadata is never downgraded; a successful fetch upgrades
            # a manual/failed row.
            if fetch_status == "fetched" and existing["fetch_status"] != "fetched":
                conn.execute(
                    """
                    UPDATE papers
                    SET title = ?, authors_json = ?, year = ?, description = ?,
                        fetch_status = 'fetched', updated_at = ?
                    WHERE id = ? AND project_id = ?
                    """,
                    (
                        meta["title"], meta["authors_json"], meta["year"],
                        meta["description"], now_iso(), existing["id"], project_id,
                    ),
                )
            return str(existing["id"]), True
        now = now_iso()
        paper_id = new_id(prefix="paper")
        conn.execute(
            """
            INSERT INTO papers
              (id, project_id, norm_key, url, title, authors_json, year,
               description, source_kind, fetch_status, created_by, created_seq,
               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper_id, project_id, norm_key, canonical_url, meta["title"],
                meta["authors_json"], meta["year"], meta["description"],
                source_kind, fetch_status, created_by,
                next_created_seq(conn=conn, table="papers"), now, now,
            ),
        )
        return paper_id, False

    def _link_targets(
        self, *, conn: Any, project_id: str, paper_id: str,
        targets: list[dict[str, str]], note: str, created_by: str,
    ) -> list[dict[str, str]]:
        linked: list[dict[str, str]] = []
        for target in targets:
            target_type = str(target.get("type", ""))
            target_id = str(target.get("id", ""))
            if target_type not in PAPER_LINK_TARGET_TYPES:
                raise ValidationError(
                    f"unknown link target type: {target_type}. "
                    f"Allowed: {', '.join(PAPER_LINK_TARGET_TYPES)}"
                )
            if target_type == "litreview_section":
                row = self._resolve_section(
                    conn=conn, project_id=project_id, address=target_id
                )
                target_id = str(row["id"])
            else:
                row = conn.execute(
                    f"SELECT id FROM {_TARGET_TABLES[target_type]} "
                    "WHERE id = ? AND project_id = ?",
                    (target_id, project_id),
                ).fetchone()
                if row is None:
                    raise NotFoundError(
                        f"{target_type} not found in project {project_id}: {target_id}"
                    )
            duplicate = conn.execute(
                "SELECT id FROM paper_links WHERE project_id = ? AND paper_id = ? "
                "AND target_type = ? AND target_id = ?",
                (project_id, paper_id, target_type, target_id),
            ).fetchone()
            if duplicate is not None:
                continue
            conn.execute(
                """
                INSERT INTO paper_links
                  (id, project_id, paper_id, target_type, target_id, note,
                   created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id(prefix="plink"), project_id, paper_id, target_type,
                    target_id, note, created_by, now_iso(),
                ),
            )
            linked.append({"type": target_type, "id": target_id})
        return linked

    def _resolve_section(self, *, conn: Any, project_id: str, address: str) -> Any:
        address = address.strip()
        if not address:
            raise ValidationError("section is required (id or exact title)")
        if self._is_summary_address(address):
            row = conn.execute(
                "SELECT * FROM litreview_sections WHERE project_id = ? AND kind = 'summary'",
                (project_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    "the General Summary has not been written yet "
                    "(litreview.edit op=edit section=summary expected_revision=0 creates it)"
                )
            return row
        if address.startswith("lit_"):
            row = conn.execute(
                "SELECT * FROM litreview_sections WHERE id = ? AND project_id = ?",
                (address, project_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(self._miss_message(conn=conn, project_id=project_id, address=address))
            return row
        wanted = address.casefold()
        rows = conn.execute(
            "SELECT * FROM litreview_sections WHERE project_id = ?", (project_id,)
        ).fetchall()
        matches = [r for r in rows if str(r["title"]).casefold() == wanted]
        if len(matches) == 1:
            return matches[0]
        raise NotFoundError(self._miss_message(conn=conn, project_id=project_id, address=address))

    def _miss_message(self, *, conn: Any, project_id: str, address: str) -> str:
        # Return the TLDR outline so the agent can self-correct in one round trip.
        rows = conn.execute(
            "SELECT id, title FROM litreview_sections WHERE project_id = ? "
            "ORDER BY kind DESC, position",
            (project_id,),
        ).fetchall()
        outline = "; ".join(f"{r['id']} ({r['title']})" for r in rows) or "no sections yet"
        return f"section not found: {address}. Existing: {outline}"

    def _check_title_free(self, *, conn: Any, project_id: str, title: str) -> None:
        # Reserved: a kind='section' row with the summary's name could never
        # be addressed by title again (_is_summary_address intercepts it).
        if self._is_summary_address(title):
            raise ValidationError(
                f"'{title}' is reserved for the General Summary — "
                "address it as section='summary'"
            )
        wanted = title.casefold()
        rows = conn.execute(
            "SELECT title FROM litreview_sections WHERE project_id = ?", (project_id,)
        ).fetchall()
        if any(str(r["title"]).casefold() == wanted for r in rows):
            raise ValidationError(f"a section titled '{title}' already exists")

    @staticmethod
    def _is_summary_address(address: str) -> bool:
        return address.strip().casefold() in ("summary", SUMMARY_TITLE.casefold())

    @staticmethod
    def _check_text(*, title: str, tldr: str, body: str) -> tuple[str, str]:
        title = title.strip()
        tldr = tldr.strip()
        if not title:
            raise ValidationError("title is required")
        if len(title) > MAX_TITLE_CHARS:
            raise ValidationError(f"title exceeds {MAX_TITLE_CHARS} characters")
        if not tldr:
            raise ValidationError("tldr is required on every section write")
        if len(tldr) > MAX_TLDR_CHARS:
            raise ValidationError(f"tldr exceeds {MAX_TLDR_CHARS} characters")
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValidationError(f"body exceeds {MAX_BODY_BYTES} bytes")
        return title, tldr

    def _read_section(
        self, *, conn: Any, project_id: str, section_id: str
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM litreview_sections WHERE id = ? AND project_id = ?",
            (section_id, project_id),
        ).fetchone()
        data = dict(row)
        data.pop("created_seq", None)
        return data

    def _section_event(
        self, *, conn: Any, project_id: str, event: str, section_id: str
    ) -> None:
        # Full post-state: litreview events double as the document's history.
        self.store.record_event(
            conn=conn, project_id=project_id, event_type=event,
            target_type="litreview_section", target_id=section_id,
            payload=self._read_section(
                conn=conn, project_id=project_id, section_id=section_id
            ),
        )
