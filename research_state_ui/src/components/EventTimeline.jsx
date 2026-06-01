import { Link } from 'react-router-dom';
import ObjId from './ObjId';

function shortTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
      + ' · ' + d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  } catch { return iso; }
}

/**
 * Routes an event's target_id to the right detail page. Returns null when
 * we don't have a navigable destination (renders as plain text instead).
 */
function targetHref(targetType, targetId) {
  if (!targetId) return null;
  switch (targetType) {
    case 'experiment': return `/experiments/${targetId}`;
    case 'claim':      return `/claims/${targetId}`;
    case 'project':    return `/projects`;
    case 'resource':   return `/resources`;
    case 'review':     return `/reviews`;
    case 'job':        return `/jobs`;
    default:           return null;
  }
}

export default function EventTimeline({ events, limit = 20 }) {
  const rows = (events || []).slice(0, limit);
  if (rows.length === 0) {
    return <div className="empty">No events yet.</div>;
  }
  return (
    <div className="timeline">
      {rows.map((e, i) => {
        const type = e.event_type || e.type;
        const href = targetHref(e.target_type, e.target_id);
        return (
          <div key={e.id || i} className="timeline-row">
            <div className="timeline-time">{shortTime(e.created_at)}</div>
            <div className="timeline-event">
              <span className="timeline-event-type">{type}</span>
              {e.target_id && (
                href
                  ? <Link to={href}><ObjId id={e.target_id} className="timeline-event-target timeline-event-target--link" /></Link>
                  : <ObjId id={e.target_id} className="timeline-event-target" />
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
