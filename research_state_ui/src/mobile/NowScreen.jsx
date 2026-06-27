import { Link } from 'react-router-dom';
import {
  useProjectStore,
  useProjectHref,
  selectProject,
  selectStats,
  selectClaims,
  selectActiveExperiments,
  selectReviewRequests,
  selectSandboxes,
  selectExperiments,
  selectEvents,
} from '../store/useProjectStore';
import EventTimeline from '../components/EventTimeline';
import FSMStrip from '../components/FSMStrip';
import StatusPill from '../components/StatusPill';
import MobileSynthesisCard from './MobileSynthesisCard';
import { expName } from '../utils/experiment';
import { fmtDuration } from '../utils/format';

const REVIEW_STATES = new Set(['design_review', 'experiment_review']);
const SOON_MS = 30 * 60 * 1000;
const sandboxRowId = (s) => s.sandbox_uid || s.sandbox_id || s.experiment_id;
const primaryExperimentId = (s) => (
  s.experiment_id
  || (Array.isArray(s.active_experiment_ids) ? s.active_experiment_ids[0] : '')
  || ''
);

/**
 * The mobile landing: one needs-attention stack, then what's in flight.
 * Everything renders from the already-polled /home + /sandboxes payloads —
 * no new requests. The empty state IS the product: "nothing needs you" is
 * a successful glance.
 */
export default function NowScreen() {
  const px = useProjectHref();
  const project = useProjectStore(selectProject);
  const stats = useProjectStore(selectStats);
  const claims = useProjectStore(selectClaims);
  const lastSyncError = useProjectStore(s => s.lastSyncError);
  const activeExperiments = useProjectStore(selectActiveExperiments);
  const reviewRequests = useProjectStore(selectReviewRequests);
  const sandboxes = useProjectStore(selectSandboxes);
  const experiments = useProjectStore(selectExperiments);
  const events = useProjectStore(selectEvents);

  if (!project) {
    return <div className="page-stage"><div className="empty-state">Loading project…</div></div>;
  }

  const now = Date.now();
  const expById = Object.fromEntries(experiments.map(e => [e.id, e]));

  // ── Attention items, most urgent first ──────────────────────────────
  const items = [];

  const running = sandboxes.filter(s => s.status === 'running');
  for (const s of running) {
    if (!s.expires_at) continue;
    const left = Date.parse(s.expires_at) - now;
    if (Number.isFinite(left) && left <= SOON_MS) {
      const exp = expById[s.experiment_id];
      items.push({
        key: `sbx-${s.experiment_id}`,
        danger: left <= 5 * 60 * 1000,
        to: px(`/sandboxes`),
        title: `Sandbox expiring ${left <= 0 ? 'now' : `in ${fmtDuration(left)}`}`,
        sub: `${exp ? expName(exp) : s.experiment_id}${s.gpu ? ` · ${s.gpu}` : ''}`,
        pill: 'running',
      });
    }
  }

  for (const e of activeExperiments) {
    if (!REVIEW_STATES.has(e.status)) continue;
    const wf = e.workflow || {};
    items.push({
      key: `rev-${e.id}`,
      to: px(`/experiments/${e.id}`),
      title: expName(e),
      sub: wf.next_action && wf.next_action !== 'none'
        ? `next: ${wf.next_action}`
        : 'awaiting review',
      pill: e.status,
    });
  }

  // Only requests still waiting on a reviewer count as attention —
  // 'submitted' means the verdict is in (the gate shows it elsewhere).
  const openRequests = reviewRequests.filter(
    r => r.status === 'requested' || r.status === 'started',
  );
  for (const r of openRequests) {
    const exp = r.target_type === 'experiment' ? expById[r.target_id] : null;
    items.push({
      key: `req-${r.id}`,
      to: exp ? px(`/experiments/${exp.id}`) : px('/reviews'),
      title: `${(r.role || 'review').replace(/_/g, ' ')} ${r.status || 'requested'}`,
      sub: exp ? expName(exp) : r.target_id,
      pill: r.status || 'requested',
    });
  }

  const inFlight = activeExperiments.filter(e => !REVIEW_STATES.has(e.status));

  return (
    <div className="page-stage">
      {lastSyncError && (
        <div className="mbanner">
          Backend unreachable — showing last known state. {lastSyncError}
        </div>
      )}

      <div className="mcounts">
        <CountPill to={px('/claims')} label="Claims" value={stats.claims ?? claims.length} />
        <CountPill to={px('/experiments')} label="Experiments" value={stats.experiments ?? experiments.length} />
        <CountPill to={px('/reviews')} label="Reviews" value={stats.open_reviews ?? stats.reviews ?? 0} />
        <CountPill to={px('/sandboxes')} label="Running" value={running.length} accent={running.length > 0} />
      </div>

      <section className="section">
        <div className="section-title">Needs you</div>
        {items.length === 0 ? (
          <div className="mclear">
            <span className="mclear-glyph" aria-hidden="true">✓</span>
            Nothing needs you right now.
          </div>
        ) : (
          <div className="mcard-list">
            {items.map(it => (
              <Link key={it.key} to={it.to} className={`mcard ${it.danger ? 'mcard--danger' : 'mcard--attn'}`}>
                <div className="mcard-head">
                  <div className="mcard-title">{it.title}</div>
                  <StatusPill value={it.pill} />
                </div>
                {it.sub && <div className="mcard-sub">{it.sub}</div>}
              </Link>
            ))}
          </div>
        )}
      </section>

      {inFlight.length > 0 && (
        <section className="section">
          <div className="section-title">In flight</div>
          <div className="mcard-list">
            {inFlight.map(e => (
              <Link key={e.id} to={px(`/experiments/${e.id}`)} className="mcard">
                <div className="mcard-head">
                  <div className="mcard-title">{expName(e)}</div>
                  <StatusPill value={e.status} />
                </div>
                {e.intent && <div className="mcard-sub">{e.intent}</div>}
                <FSMStrip status={e.status} />
              </Link>
            ))}
          </div>
        </section>
      )}

      <MobileSynthesisCard projectId={project.id} />

      <section className="section">
        <div className="section-title">Sandboxes</div>
        {running.length === 0 ? (
          <div className="empty-state empty-state--compact"><p>No running sandboxes.</p></div>
        ) : (
          <div className="mcard-list">
            {running.map(s => {
              const experimentId = primaryExperimentId(s);
              const exp = expById[experimentId];
              const up = s.requested_at ? now - Date.parse(s.requested_at) : null;
              const left = s.expires_at ? Date.parse(s.expires_at) - now : null;
              const title = exp ? expName(exp) : experimentId || s.sandbox_uid || s.sandbox_id;
              const body = (
                <>
                  <div className="mcard-head">
                    <div className="mcard-title">{title}</div>
                    <StatusPill value={s.status} />
                  </div>
                  <div className="mcard-meta">
                    {s.gpu && <span className="mono">{s.gpu}</span>}
                    {up != null && <span>up {fmtDuration(up)}</span>}
                    {left != null && <span>expires in {left <= 0 ? 'soon' : fmtDuration(left)}</span>}
                  </div>
                </>
              );
              if (!experimentId) {
                return <div key={sandboxRowId(s)} className="mcard">{body}</div>;
              }
              return (
                <Link key={sandboxRowId(s)} to={px(`/experiments/${experimentId}`)} className="mcard">
                  {body}
                </Link>
              );
            })}
          </div>
        )}
      </section>

      <section className="section">
        <div className="cluster--between" style={{ marginBottom: 12 }}>
          <div className="section-title" style={{ marginBottom: 0 }}>Recent</div>
          <Link to={px('/events')} className="btn btn--sm btn--ghost">Timeline →</Link>
        </div>
        <EventTimeline events={events} limit={8} experiments={experiments} />
      </section>
    </div>
  );
}

function CountPill({ to, label, value, accent = false }) {
  return (
    <Link to={to} className={`mcount${accent ? ' mcount--accent' : ''}`}>
      <span className="mcount-value tabular">{value}</span>
      <span className="mcount-label">{label}</span>
    </Link>
  );
}
