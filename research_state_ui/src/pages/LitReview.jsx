import { useCallback, useMemo, useRef, useState } from 'react';
import { useProjectStore } from '../store/useProjectStore';
import { useStreamAwarePoll } from '../store/useEventStream';
import { api } from '../api';
import MarkdownView from '../components/MarkdownView';
import EntityChip from '../components/EntityChip';

/**
 * The living literature review: one sectioned document (General Summary +
 * dynamic sections, TLDRs always visible) and the papers ledger with two-way
 * links to sections, experiments, and claims. Agents write it through
 * litreview.* tools; this screen is the human read.
 */
export default function LitReview() {
  const projectId = useProjectStore(s => s.projectId);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState('document');
  const [open, setOpen] = useState(() => new Set());
  const etagRef = useRef(null);

  const fetchReview = useCallback(async () => {
    if (!projectId) return;
    try {
      const res = await api.getLitReviewIfChanged(projectId, etagRef.current);
      if (res?.notModified) { setError(null); return; }
      etagRef.current = res?.etag || null;
      setData(res?.data ?? res);
      setError(null);
    } catch (e) {
      setError(e?.message || 'Failed to load the literature review');
    }
  }, [projectId]);

  useStreamAwarePoll(fetchReview, {
    matches: (row) => String(row?.type || '').startsWith('litreview.'),
  });

  const sections = data?.sections || [];
  const papers = data?.papers || [];
  const papersById = useMemo(
    () => new Map(papers.map((p) => [p.id, p])),
    [papers],
  );

  const toggle = (id) => {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // A transient poll failure never blanks last-good data — the error screen
  // only shows when there is nothing to render at all.
  if (!data) {
    return <div className="page-stage"><p className="muted">{error || 'Loading…'}</p></div>;
  }

  const empty = !data.summary?.exists && sections.length === 0 && papers.length === 0;

  return (
    <div className="page-stage litreview">
      <div className="litreview-tabs" role="tablist" aria-label="Literature review views">
        <button
          type="button" role="tab" aria-selected={tab === 'document'}
          className={'litreview-tab' + (tab === 'document' ? ' active' : '')}
          onClick={() => setTab('document')}
        >
          Document
        </button>
        <button
          type="button" role="tab" aria-selected={tab === 'papers'}
          className={'litreview-tab' + (tab === 'papers' ? ' active' : '')}
          onClick={() => setTab('papers')}
        >
          Papers <span className="litreview-count">{papers.length}</span>
        </button>
      </div>

      {empty && (
        <p className="muted">
          No literature review yet. Agents build it as papers enter the
          project — citing a paper (litreview.cite) and making targeted
          section edits (litreview.edit).
        </p>
      )}

      {tab === 'document' && !empty && (
        <div className="litreview-doc">
          <section className="litreview-summary">
            <h2>{data.summary?.title || 'General Summary'}</h2>
            {data.summary?.exists === false ? (
              <p className="muted">Not written yet.</p>
            ) : (
              <>
                {data.summary?.tldr ? <p className="litreview-tldr">{data.summary.tldr}</p> : null}
                {data.summary?.body ? <MarkdownView text={data.summary.body} /> : null}
              </>
            )}
          </section>

          {sections.map((s) => (
            <section key={s.id} className="litreview-section">
              <button
                type="button"
                className="litreview-section-head"
                aria-expanded={open.has(s.id)}
                onClick={() => toggle(s.id)}
              >
                <span className={'litreview-chevron' + (open.has(s.id) ? ' open' : '')}>›</span>
                <span className="litreview-section-title">{s.title}</span>
              </button>
              <p className="litreview-tldr">{s.tldr}</p>
              {open.has(s.id) && (
                <div className="litreview-body">
                  {s.body ? <MarkdownView text={s.body} /> : <p className="muted">No body yet.</p>}
                  {(s.cited_papers || []).length > 0 && (
                    <p className="litreview-cited">
                      Cites: {(s.cited_papers || []).map((p) => (
                        <span key={p.id} className="litreview-cite-item">{paperLabel(p, papersById)}</span>
                      ))}
                    </p>
                  )}
                </div>
              )}
            </section>
          ))}

          {papers.length > 0 && (
            <section className="litreview-references">
              <h2>References</h2>
              {/* Derived from the papers ledger — never hand-edited. */}
              <ol>
                {papers.map((p) => (
                  <li key={p.id}>
                    <PaperLine paper={p} />
                  </li>
                ))}
              </ol>
            </section>
          )}
        </div>
      )}

      {tab === 'papers' && !empty && (
        <div className="litreview-papers">
          {papers.length === 0 && <p className="muted">No papers cited yet.</p>}
          {papers.map((p) => (
            <div key={p.id} className="litreview-paper-card">
              <PaperLine paper={p} />
              {(p.links || []).length > 0 && (
                <div className="litreview-paper-links">
                  {(p.links || []).map((l, i) => (
                    <EntityChip key={`${l.target_id}-${i}`} id={l.target_id} compact />
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function paperLabel(p, papersById) {
  const full = papersById.get(p.id);
  return (full?.title || p.title || p.url || p.id) + ' ';
}

function PaperLine({ paper }) {
  const authors = (paper.authors || []).join(', ');
  const meta = [authors, paper.year].filter(Boolean).join(' · ');
  return (
    <span className="litreview-paper">
      <a href={paper.url} target="_blank" rel="noreferrer">
        {paper.title || paper.url}
      </a>
      {meta ? <span className="litreview-paper-meta"> — {meta}</span> : null}
      {paper.fetch_status !== 'fetched' && (
        <span className="litreview-paper-flag"> ({paper.fetch_status})</span>
      )}
    </span>
  );
}
