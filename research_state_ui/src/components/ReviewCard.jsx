import StatusPill from './StatusPill';
import ObjId from './ObjId';

function shortDateTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  } catch { return iso; }
}

export default function ReviewCard({ review }) {
  if (!review) return null;
  const verdict = (review.verdict || 'pending').toLowerCase();
  const cls = ['review-card', `review-card--${verdict}`];
  const findings = Array.isArray(review.findings) ? review.findings : [];
  return (
    <div className={cls.join(' ')}>
      <div className="review-card-head">
        <div className="cluster">
          <StatusPill value={verdict} />
          <span className="muted" style={{ fontSize: 'var(--text-xs)' }}>
            {review.role}
          </span>
          {review.attempt_index != null && (
            <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>
              · attempt {review.attempt_index}
            </span>
          )}
        </div>
        <div className="review-card-meta">
          {shortDateTime(review.created_at)}
          {review.id && <> · <ObjId id={review.id} /></>}
        </div>
      </div>
      {review.notes && <div className="review-card-notes">{review.notes}</div>}
      {findings.length > 0 && (
        <div className="review-findings">
          {findings.map((f, i) => {
            const sev = (f.severity || 'low').toLowerCase();
            return (
              <div key={i} className="review-finding">
                <span className={`review-finding-sev review-finding-sev--${sev}`}>{sev}</span>
                <span style={{ marginLeft: 8 }}>
                  <strong style={{ color: 'var(--text)', fontWeight: 540 }}>{f.issue}</strong>
                  {f.evidence && <span className="faint"> — {f.evidence}</span>}
                  {f.recommended_change && <div className="muted" style={{ marginLeft: 58, marginTop: 2 }}>↳ {f.recommended_change}</div>}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
