import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useProjectStore, selectExperiments } from '../store/useProjectStore';
import { api } from '../api';
import ObjId from '../components/ObjId';
import StatusPill from '../components/StatusPill';
import ReviewCard from '../components/ReviewCard';

/**
 * Reviews page. Shows:
 *   - the open review_requests queue (no submitted review yet)
 *   - submitted reviews history with verdict + findings
 *
 * Reviewer-agent composition is handled outside this UI (Codex spawns the
 * reviewer with a capability obtained from MCP). We display only.
 */
export default function Reviews() {
  const projectId = useProjectStore(s => s.projectId);
  const experiments = useProjectStore(selectExperiments);
  const [queue, setQueue] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setQueue(null);
    setError(null);
    api.listReviews(projectId)
      .then(data => !cancelled && setQueue(data))
      .catch(err => !cancelled && setError(err.message));
    return () => { cancelled = true; };
  }, [projectId]);

  const expById = Object.fromEntries(experiments.map(e => [e.id, e]));

  if (error) return <div className="page-stage"><div className="error-message">{error}</div></div>;
  if (!queue) return <div className="page-stage"><div className="empty">Loading…</div></div>;

  // Server returns { requests: [...], reviews: [...] } at /reviews
  const openRequests = queue.requests || queue.open_requests || queue.openRequests || [];
  const submitted = queue.reviews || queue.submitted || [];

  // Group submitted reviews by target experiment
  const byExp = new Map();
  for (const r of submitted) {
    const eid = r.target_id || r.experiment_id;
    if (!byExp.has(eid)) byExp.set(eid, []);
    byExp.get(eid).push(r);
  }

  return (
    <div className="page-stage">
      <header className="page-header page-header--lg">
        <div className="page-eyebrow">Reviews</div>
        <h1 className="page-title">Design & experiment review history</h1>
        <p className="page-summary">
          Reviewers (separate Codex sub-agents) submit verdicts through MCP. This page
          displays both open review requests and the durable history. Failed and
          needs-changes reviews stay visible — they are the load-bearing context for
          the next attempt.
        </p>
      </header>

      <section className="section">
        <div className="section-title">Open requests</div>
        {openRequests.length === 0 ? (
          <div className="empty">No open review requests.</div>
        ) : (
          <div className="list card card--flush">
            {openRequests.map(req => {
              const exp = expById[req.target_id];
              return (
                <div key={req.id} className="list-row">
                  <div className="list-row-main">
                    <div className="list-row-title">
                      {req.role.replace(/_/g, ' ')} · {exp?.intent || req.target_id}
                    </div>
                    <div className="list-row-sub">
                      <ObjId id={req.id} /> · target: <ObjId id={req.target_id} />
                      {req.reason && <> · {req.reason}</>}
                    </div>
                  </div>
                  <div className="list-row-aside">
                    <StatusPill value={req.status || 'requested'} />
                    {exp && <Link to={`/experiments/${exp.id}`} className="btn btn--sm btn--ghost">Open →</Link>}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      <section className="section">
        <div className="section-title">Submitted</div>
        {byExp.size === 0 ? (
          <div className="empty">No reviews submitted yet.</div>
        ) : (
          <div className="stack stack--lg">
            {Array.from(byExp.entries()).map(([eid, reviews]) => {
              const exp = expById[eid];
              return (
                <div key={eid}>
                  <div className="cluster--between" style={{ marginBottom: 10 }}>
                    <div className="cluster">
                      <ObjId id={eid} accent />
                      {exp && <span style={{ fontSize: 'var(--text-base)' }}>{exp.intent}</span>}
                    </div>
                    {exp && <Link to={`/experiments/${eid}`} className="btn btn--sm btn--ghost">Open experiment →</Link>}
                  </div>
                  <div className="stack stack--sm">
                    {reviews.map(r => <ReviewCard key={r.id || r.created_at} review={r} />)}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
