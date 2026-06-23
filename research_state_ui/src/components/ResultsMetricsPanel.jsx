import { useEffect, useState } from 'react';
import { api } from '../api';

// Format a metric value to a sane precision: integers stay integers, other
// numbers render with up to 4 significant digits. Non-numbers pass through.
function fmtNum(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return String(v ?? '');
  if (Number.isInteger(v)) return String(v);
  return Number(v.toPrecision(4)).toString();
}

// The durable series for a metric is an ascending list of points; the final
// value is the last point's `value`.
function finalValue(series) {
  if (!Array.isArray(series) || series.length === 0) return null;
  const last = series[series.length - 1];
  return last ? last.value : null;
}

/**
 * ResultsMetricsPanel — DURABLE archived metrics that outlive the sandbox VM.
 *
 * Fetches GET /experiments/{id}/results/metrics, the control-plane copy
 * recorded on sync and at release. Distinct from the live /sandbox/metrics
 * (which vanishes with the VM), so this never polls — the data is durable.
 * Renders the final value of every recorded metric per run.
 */
export default function ResultsMetricsPanel({ projectId, experimentId, refreshKey }) {
  const [data, setData] = useState(null);

  useEffect(() => {
    let cancelled = false;
    api.getResultsMetrics(projectId, experimentId)
      .then(d => { if (!cancelled) setData(d); })
      .catch(() => { /* non-fatal: leave last good (or null) — durable, no spinner */ });
    return () => { cancelled = true; };
  }, [projectId, experimentId, refreshKey]);

  if (!data) return null;

  if (data.available === false) {
    // Nothing archived yet. Stay unobtrusive: render a tiny muted hint only
    // when a sandbox has actually run, otherwise render nothing at all.
    if (data.hint && data.sandbox_status && data.sandbox_status !== 'none') {
      return <div className="results-metrics-sub">{data.hint}</div>;
    }
    return null;
  }

  const experiments = Array.isArray(data.experiments) ? data.experiments : [];

  return (
    <section className="results-metrics">
      <div className="results-metrics-head">
        <span className="results-metrics-title">Recorded results</span>
        <span className="results-metrics-sub">durable</span>
      </div>
      {experiments.map((exp, ei) => (
        (Array.isArray(exp.runs) ? exp.runs : []).map((run, ri) => {
          const metrics = run.metrics && typeof run.metrics === 'object' ? run.metrics : {};
          const params = run.params && typeof run.params === 'object' ? run.params : {};
          const metricKeys = Object.keys(metrics);
          const paramKeys = Object.keys(params);
          return (
            <div className="results-metrics-run" key={`${exp.name || ei}:${run.run_id || ri}`}>
              <div className="results-metrics-run-head">
                {run.name || run.run_id}
                {run.status ? <span className="results-metrics-run-meta">{run.status}</span> : null}
              </div>
              <div className="results-metrics-grid">
                {metricKeys.map(key => (
                  <div className="results-metric" key={key}>
                    <div className="results-metric-key">{key}</div>
                    <div className="results-metric-val">{fmtNum(finalValue(metrics[key]))}</div>
                  </div>
                ))}
                {paramKeys.map(key => (
                  <div className="results-metric" key={`param:${key}`}>
                    <div className="results-metric-key">{key}</div>
                    <div className="results-metric-val">{fmtNum(params[key])}</div>
                  </div>
                ))}
              </div>
            </div>
          );
        })
      ))}
    </section>
  );
}
