/**
 * FSMStrip — experiment lifecycle pill row for the lean v0.0001 backend.
 *
 *   planned → design_review → ready_to_run → running → experiment_review → complete
 *
 * `failed` and `abandoned` are terminal exits and rendered on the last cell.
 *
 * The strip is the page's single source of stage truth. On the experiment
 * detail page the current step doubles as the gate disclosure: pass
 * `onToggle` to make it a button (with a chevron, `expanded` state, and an
 * optional `badge` like "action" when a manual transition is waiting), and
 * render the gate panel as `children` — it appears attached under the strip.
 */

const STAGES = [
  { id: 'planned',           label: 'Planned' },
  { id: 'design_review',     label: 'Design review' },
  { id: 'ready_to_run',      label: 'Ready' },
  { id: 'running',           label: 'Running' },
  { id: 'experiment_review', label: 'Exp. review' },
  { id: 'complete',          label: 'Complete' },
];

const GATE_STATES = new Set(['design_review', 'experiment_review']);
const TERMINAL = new Set(['complete', 'failed', 'abandoned']);

export default function FSMStrip({ status, badge = null, expanded = false, onToggle = null, children = null }) {
  const s = String(status || '').toLowerCase();
  const isFailed = s === 'failed' || s === 'abandoned';
  const currentIdx = STAGES.findIndex(x => x.id === s);
  const idx = currentIdx >= 0 ? currentIdx : 0;

  return (
    <div className="fsm-strip-wrap">
      <ol className="fsm-strip" aria-label="Experiment lifecycle">
        {STAGES.map((stage, i) => {
          let state;
          if (isFailed) {
            state = i < STAGES.length - 1 ? 'past' : 'failed';
          } else if (i < idx) {
            state = 'past';
          } else if (i === idx) {
            state = GATE_STATES.has(stage.id) ? 'gate' : 'current';
          } else {
            state = 'future';
          }
          const isCurrent = i === idx && !isFailed;
          const sub =
            i === idx && !TERMINAL.has(s)
              ? state === 'gate' ? 'awaiting review' : 'in progress'
              : null;
          const label = state === 'failed' ? (isFailed ? s : 'Failed') : stage.label;
          const head = (
            <span className="fsm-step-head">
              <span className="fsm-step-dot" />
              <span className="fsm-step-label">{label}</span>
              {isCurrent && badge && <span className="fsm-step-badge">{badge}</span>}
              {isCurrent && onToggle && (
                <span className="fsm-step-twist" aria-hidden="true">{expanded ? '▾' : '▸'}</span>
              )}
            </span>
          );
          return (
            <li key={stage.id} className={`fsm-step fsm-step--${state}`}>
              {isCurrent && onToggle ? (
                <button type="button" className="fsm-step-toggle" onClick={onToggle} aria-expanded={expanded}>
                  {head}
                  {sub && <span className="fsm-step-sub">{sub}</span>}
                </button>
              ) : (
                <>
                  {head}
                  {sub && <span className="fsm-step-sub">{sub}</span>}
                </>
              )}
            </li>
          );
        })}
      </ol>
      {expanded && children}
    </div>
  );
}
