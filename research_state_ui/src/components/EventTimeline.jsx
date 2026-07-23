import { Link } from 'react-router-dom';
import { useProjectStore, projectPath } from '../store/useProjectStore';
import ObjId from './ObjId';
import EntityChip from './EntityChip';
import { entityType } from '../utils/entityResolve';
import { PARACHUTE_CHIPS } from '../utils/parachute';

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
  const pid = useProjectStore.getState().projectId;
  switch (targetType) {
    case 'experiment': return projectPath(pid, `/experiments/${targetId}`);
    case 'claim':      return projectPath(pid, `/claims/${targetId}`);
    case 'project':    return `/projects`;
    case 'artifact':   return projectPath(pid, `/artifacts/${targetId}`);
    case 'review':     return projectPath(pid, `/reviews`);
    case 'sandbox':    return projectPath(pid, `/experiments/${targetId}#execution`);
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
        const chip = PARACHUTE_CHIPS[type];
        // Research-entity targets become chips (name + hover detail); non-entity
        // targets (project, sandbox) keep the plain id + route.
        const href = targetHref(e.target_type, e.target_id);
        return (
          <div key={e.id || i} className="timeline-row">
            <div className="timeline-time">{shortTime(e.created_at)}</div>
            <div className="timeline-event">
              <span className="timeline-event-type">{type}</span>
              {chip && <span className={`parachute-chip parachute-chip--${chip.variant}`}>{chip.label}</span>}
              {e.target_id && (
                entityType(e.target_id)
                  ? <EntityChip id={e.target_id} compact />
                  : (href
                      ? <Link to={href}><ObjId id={e.target_id} className="timeline-event-target timeline-event-target--link" /></Link>
                      : <ObjId id={e.target_id} className="timeline-event-target" />)
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
