import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useProjectStore, selectExperiments, useProjectHref } from '../store/useProjectStore';
import { api } from '../api';
import ObjId from '../components/ObjId';
import StatusPill from '../components/StatusPill';
import ReviewCard from '../components/ReviewCard';
import { expName } from '../utils/experiment';
import { SkeletonCards } from './Skeleton';

/**
 * MobileReviews — read-only review queue + history as stacked cards (the
 * desktop page packs everything into nowrap .list-rows that crush on a phone).
 * docs/MOBILE_UX_REVIEW.md §2.8.
 */
export default function MobileReviews() {
  const projectId = useProjectStore(s => s.projectId);
  const experiments = useProjectStore(selectExperiments);
  const px = useProjectHref();
  const [queue, setQueue] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setQueue(null);
    setError(null);
    api.listReviews(projectId)
      .then(d => { if (!cancelled) setQueue(d); })
      .catch(e => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [projectId]);

  const expById = Object.fromEntries(experiments.map(e => [e.id, e]));

  if (error) return <div className="page-stage"><div className="error-message">{error}</div></div>;
  if (!queue) {
    return (
      <div className="page-stage">
        <header className="page-header"><h1 className="page-title">Reviews</h1></header>
        <SkeletonCards />
      </div>
    );
  }

  const openRequests = queue.requests || queue.open_requests || queue.openRequests || [];
  const submitted = queue.reviews || queue.submitted || [];
  const byExp = new Map();
  for (const r of submitted) {
    const eid = r.target_id || r.experiment_id;
    if (!byExp.has(eid)) byExp.set(eid, []);
    byExp.get(eid).push(r);
  }

  return (
    <div className="page-stage">
      <header className="page-header"><h1 className="page-title">Reviews</h1></header>

      <section className="section">
        <div className="section-title">Open requests</div>
        {openRequests.length === 0 ? (
          <div className="empty-state empty-state--compact"><p>No open review requests.</p></div>
        ) : (
          <div className="mcard-list">
            {openRequests.map(req => {
              const exp = expById[req.target_id];
              return (
                <Link key={req.id} to={exp ? px(`/experiments/${exp.id}`) : px('/reviews')} className="mcard mcard--attn">
                  <div className="mcard-head">
                    <div className="mcard-title">{(req.role || 'review').replace(/_/g, ' ')}</div>
                    <StatusPill value={req.status || 'requested'} />
                  </div>
                  <div className="mcard-sub">
                    {exp ? expName(exp) : <ObjId id={req.target_id} />}
                    {req.reason ? ` — ${req.reason}` : ''}
                  </div>
                </Link>
              );
            })}
          </div>
        )}
      </section>

      <section className="section">
        <div className="section-title">Submitted</div>
        {byExp.size === 0 ? (
          <div className="empty-state empty-state--compact"><p>No reviews submitted yet.</p></div>
        ) : (
          <div className="stack stack--lg">
            {Array.from(byExp.entries()).map(([eid, reviews]) => {
              const exp = expById[eid];
              return (
                <div key={eid}>
                  <div className="cluster--between" style={{ marginBottom: 8 }}>
                    {exp
                      ? <Link to={px(`/experiments/${eid}`)} style={{ fontWeight: 600 }}>{expName(exp)}</Link>
                      : <ObjId id={eid} accent />}
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
