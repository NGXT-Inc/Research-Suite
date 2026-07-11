/**
 * MetricAdvisories — the system's "something looks off" observations over a
 * results_metrics payload (payload.advisories). Each row states what was seen
 * and why that pattern is usually worth a look; it never prescribes an
 * action — deciding whether anything is actually wrong belongs to the agent
 * and the researcher. Warnings (non-finite values, divergence) lead;
 * notices (plateaus) follow.
 */
export default function MetricAdvisories({ advisories, dense = false }) {
  const items = Array.isArray(advisories) ? advisories : [];
  if (items.length === 0) return null;
  return (
    <div className="madv" role="status">
      {items.map(a => (
        <div
          className={`madv-row madv-row--${a.severity === 'warning' ? 'warning' : 'notice'}`}
          key={`${a.run_id}:${a.metric}:${a.code}`}
        >
          <span className="madv-dot" aria-hidden="true" />
          <div className="madv-body">
            <span className="madv-summary">
              {a.summary}
              {a.run_name && <span className="madv-run"> · {a.run_name}</span>}
            </span>
            {!dense && a.reasoning && <span className="madv-why">{a.reasoning}</span>}
          </div>
        </div>
      ))}
    </div>
  );
}
