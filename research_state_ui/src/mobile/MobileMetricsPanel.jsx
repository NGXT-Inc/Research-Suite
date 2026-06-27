import { useEffect, useState } from 'react';
import { api } from '../api';
import Sparkline from './Sparkline';

// Same precision rule as the desktop ResultsMetricsPanel.
function fmtNum(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return String(v ?? '');
  if (Number.isInteger(v)) return String(v);
  return Number(v.toPrecision(4)).toString();
}

// A metric's durable series is an ascending list of points ({value} objects,
// or bare numbers). Pull the numeric values for charting + final readout.
function seriesValues(series) {
  if (!Array.isArray(series)) return [];
  return series.map(p => (typeof p === 'number' ? p : (p && typeof p.value === 'number' ? p.value : NaN)));
}
function finalOf(vals) {
  for (let i = vals.length - 1; i >= 0; i--) if (Number.isFinite(vals[i])) return vals[i];
  return null;
}

/**
 * MobileMetricsPanel — centralized MLflow metrics for the experiment, over
 * GET …/results/metrics. Adds sparkline curves on top of the desktop panel's
 * final-value grid. Never polls.
 */
export default function MobileMetricsPanel({ projectId, experimentId, refreshKey }) {
  const [data, setData] = useState(null);

  useEffect(() => {
    let cancelled = false;
    api.getResultsMetrics(projectId, experimentId)
      .then(d => { if (!cancelled) setData(d); })
      .catch(() => { /* durable; keep last good (or null) */ });
    return () => { cancelled = true; };
  }, [projectId, experimentId, refreshKey]);

  if (!data) return null;
  if (data.available === false) {
    if (data.hint) {
      return <div className="results-metrics-sub">{data.hint}</div>;
    }
    return null;
  }

  const experiments = Array.isArray(data.experiments) ? data.experiments : [];

  return (
    <section className="results-metrics">
      <div className="results-metrics-head">
        <span className="results-metrics-title">Recorded results</span>
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
              <div className="mmetric-list">
                {metricKeys.map(key => {
                  const vals = seriesValues(metrics[key]);
                  const hasCurve = vals.filter(Number.isFinite).length > 1;
                  return (
                    <div className="mmetric" key={key}>
                      <div className="mmetric-head">
                        <span className="mmetric-key">{key}</span>
                        <span className="mmetric-val">{fmtNum(finalOf(vals))}</span>
                      </div>
                      {hasCurve && <Sparkline points={vals} />}
                    </div>
                  );
                })}
              </div>
              {paramKeys.length > 0 && (
                <div className="mmetric-params">
                  {paramKeys.map(key => (
                    <span className="mmetric-param" key={key}>
                      <span className="mmetric-param-key">{key}</span> {fmtNum(params[key])}
                    </span>
                  ))}
                </div>
              )}
            </div>
          );
        })
      ))}
    </section>
  );
}
