/**
 * FSMStrip — experiment lifecycle pill row for the lean v0.0001 backend.
 *
 *   planned → design_review → ready_to_run → running → experiment_review → complete
 *
 * `failed` and `abandoned` are terminal exits and rendered on the last cell.
 * Adapts the mockup's FSMStrip to the lean status enum.
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

export default function FSMStrip({ status }) {
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
          const sub =
            i === idx && !TERMINAL.has(s)
              ? state === 'gate' ? 'awaiting review' : 'in progress'
              : null;
          return (
            <li key={stage.id} className={`fsm-step fsm-step--${state}`}>
              <span className="fsm-step-head">
                <span className="fsm-step-dot" />
                <span className="fsm-step-label">
                  {state === 'failed' ? (isFailed ? s : 'Failed') : stage.label}
                </span>
              </span>
              {sub && <span className="fsm-step-sub">{sub}</span>}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
