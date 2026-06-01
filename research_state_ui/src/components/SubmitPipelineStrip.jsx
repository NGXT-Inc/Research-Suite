/**
 * Thin pipeline stepper. Renders only while a Modal submit is in flight and
 * we recognise the current stage. Otherwise: nothing — keeps the UI calm.
 *
 * Mirrors the stage list in
 * backend/execution/backends/modal/submit_pipeline.py::_build_stages.
 * Hardcoded here because the order changes rarely and the alternative is
 * a round-trip through the API just to draw 8 dots.
 *
 * Props:
 *   nested   nested_status from the API ("submitting.acquiring_sandbox"…)
 */
const STAGES = [
  { id: 'preparing',         label: 'Prepare' },
  { id: 'volume',            label: 'Volume' },
  { id: 'syncing',           label: 'Sync' },
  { id: 'conflict_gate',     label: 'Gate' },
  { id: 'acquiring_sandbox', label: 'Sandbox' },
  { id: 'encoding',          label: 'Encode' },
  { id: 'ssh_setup',         label: 'SSH' },
  { id: 'starting',          label: 'Start' },
];

const STAGE_IDS = STAGES.map((s) => s.id);

export default function SubmitPipelineStrip({ nested }) {
  const raw = String(nested || '');
  if (!raw.startsWith('submitting.')) return null;
  const phase = raw.slice('submitting.'.length);
  const currentIdx = STAGE_IDS.indexOf(phase);
  if (currentIdx === -1) return null;

  return (
    <ol className="submit-pipeline-strip" aria-label="Submission progress">
      {STAGES.map((stage, i) => {
        let state;
        if (i < currentIdx) state = 'past';
        else if (i === currentIdx) state = 'current';
        else state = 'future';
        return (
          <li
            key={stage.id}
            className={`submit-pipeline-step submit-pipeline-step--${state}`}
            title={stage.id}
          >
            <span className="submit-pipeline-step-dot" />
            {state === 'current' && (
              <span className="submit-pipeline-step-label">{stage.label}</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}
