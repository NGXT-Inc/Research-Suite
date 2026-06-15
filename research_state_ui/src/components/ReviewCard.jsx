import StatusPill from './StatusPill';
import ObjId from './ObjId';

function shortDateTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  } catch { return iso; }
}

export default function ReviewCard({ review, bare = false }) {
  if (!review) return null;
  const verdict = (review.verdict || 'pending').toLowerCase();
  // `bare` drops the card chrome (border, fill, padding) so the review reads as
  // plain content inside a disclosure — the standalone Reviews pages keep the
  // boxed card.
  const cls = ['review-card', `review-card--${verdict}`];
  if (bare) cls.push('review-card--bare');
  const findings = Array.isArray(review.findings) ? review.findings : [];
  return (
    <div className={cls.join(' ')}>
      <div className="review-card-head">
        <div className="cluster">
          {/* In bare (disclosure) mode the verdict already lives in the
              artifact's status badge — show only a quiet who·when provenance. */}
          {!bare && <StatusPill value={verdict} />}
          <span className="muted" style={{ fontSize: 'var(--text-xs)' }}>
            {review.role}
          </span>
          {!bare && review.attempt_index != null && (
            <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>
              · attempt {review.attempt_index}
            </span>
          )}
        </div>
        <div className="review-card-meta">
          {shortDateTime(review.created_at)}
          {!bare && review.id && <> · <ObjId id={review.id} /></>}
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
