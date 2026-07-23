import { useEffect, useMemo, useState } from 'react';
import { useProjectStore, selectExperiments } from '../store/useProjectStore';
import { api } from '../api';
import EventTimeline from '../components/EventTimeline';

const CATEGORIES = [
  { id: 'all',        label: 'all',        prefixes: null },
  { id: 'lifecycle',  label: 'lifecycle',  prefixes: ['experiment.', 'project.', 'claim.'] },
  { id: 'artifacts',  label: 'artifacts',  prefixes: ['artifact.'] },
  { id: 'reviews',    label: 'reviews',    prefixes: ['review.'] },
  { id: 'sandboxes',  label: 'sandboxes',  prefixes: ['sandbox.'] },
];

function inCategory(category, type) {
  if (category.id === 'all') return true;
  if (!type) return false;
  return (category.prefixes || []).some(p => type.startsWith(p));
}

/**
 * Events page — append-only project log.
 *
 * Provides:
 *   - category filter pills (lifecycle / artifacts / reviews / sandboxes / all)
 *   - per-event-type pills (data-driven from the current set of events)
 *   - clickable target_id chip on each row that routes to the right detail page
 */
export default function Events() {
  const projectId = useProjectStore(s => s.projectId);
  const experiments = useProjectStore(selectExperiments);
  const [events, setEvents] = useState(null);
  const [error, setError] = useState(null);
  const [category, setCategory] = useState('all');
  const [typeFilter, setTypeFilter] = useState('all');

  useEffect(() => {
    let cancelled = false;
    setEvents(null);
    setError(null);
    api.listEvents(projectId, 500)
      .then(data => !cancelled && setEvents(data.events || data || []))
      .catch(err => !cancelled && setError(err.message));
    return () => { cancelled = true; };
  }, [projectId]);

  const allTypes = useMemo(() => {
    const set = new Set();
    for (const e of (events || [])) set.add(e.event_type || e.type);
    return Array.from(set).sort();
  }, [events]);

  const visibleTypes = useMemo(() => {
    const cat = CATEGORIES.find(c => c.id === category);
    if (!cat || cat.id === 'all') return allTypes;
    return allTypes.filter(t => (cat.prefixes || []).some(p => (t || '').startsWith(p)));
  }, [allTypes, category]);

  const filtered = useMemo(() => {
    const cat = CATEGORIES.find(c => c.id === category);
    return (events || []).filter(e => {
      const t = e.event_type || e.type;
      if (!inCategory(cat, t)) return false;
      if (typeFilter !== 'all' && t !== typeFilter) return false;
      return true;
    });
  }, [events, category, typeFilter]);

  // Reset typeFilter if the category change makes the current pick invisible.
  useEffect(() => {
    if (typeFilter !== 'all' && !visibleTypes.includes(typeFilter)) {
      setTypeFilter('all');
    }
  }, [typeFilter, visibleTypes]);

  const countByType = useMemo(() => {
    const map = {};
    for (const e of (events || [])) {
      const t = e.event_type || e.type;
      map[t] = (map[t] || 0) + 1;
    }
    return map;
  }, [events]);

  const countByCategory = useMemo(() => {
    const map = { all: (events || []).length };
    for (const c of CATEGORIES) {
      if (c.id === 'all') continue;
      map[c.id] = (events || []).filter(e => inCategory(c, e.event_type || e.type)).length;
    }
    return map;
  }, [events]);

  return (
    <div className="page-stage">
      <header className="page-header page-header--lg">
        <h1 className="page-title">Append-only event log</h1>
        <p className="page-summary">Every accepted mutation, newest first.</p>
      </header>

      <div className="events-filter-bar">
        <div className="tab-row">
          {CATEGORIES.map(c => (
            <button key={c.id} className={`tab${category === c.id ? ' active' : ''}`} onClick={() => setCategory(c.id)}>
              {c.label}
              <span className="tab-count">{countByCategory[c.id] || 0}</span>
            </button>
          ))}
        </div>
        {visibleTypes.length > 1 && (
          <select className="select" value={typeFilter} onChange={e => setTypeFilter(e.target.value)} style={{ maxWidth: 280 }}>
            <option value="all">all types ({(events || []).length})</option>
            {visibleTypes.map(t => (
              <option key={t} value={t}>{t} ({countByType[t] || 0})</option>
            ))}
          </select>
        )}
        <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>
          showing {filtered.length}
        </span>
      </div>

      {error && <div className="error-message">{error}</div>}
      {events == null
        ? <div className="empty">Loading…</div>
        : filtered.length === 0
          ? <div className="empty">No events match these filters.</div>
          : <EventTimeline events={filtered} limit={500} experiments={experiments} />
      }
    </div>
  );
}
