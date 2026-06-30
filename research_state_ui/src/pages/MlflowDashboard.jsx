import { useEffect, useState } from 'react';
import { useProjectStore } from '../store/useProjectStore';
import { api } from '../api';
import StatusPill from '../components/StatusPill';
import Sparkline from '../mobile/Sparkline';

// Up to 4 significant digits; integers stay integers; non-numbers pass through.
function fmtNum(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return String(v ?? '');
  if (Number.isInteger(v)) return String(v);
  return Number(v.toPrecision(4)).toString();
}

// A metric history entry is [[step, value], …]; extract the finite y-values.
function seriesValues(points) {
  return (Array.isArray(points) ? points : [])
    .map(p => (Array.isArray(p) ? p[1] : null))
    .filter(v => Number.isFinite(v));
}

/**
 * MlflowDashboard — the project-scoped MLflow page. The central tracking server
 * spans every project and can't be URL-filtered, so we render a compact
 * project view from the backend's MLflow UI compatibility endpoint and offer a
 * per-experiment drill-in that embeds the real MLflow UI for full detail.
 */
export default function MlflowDashboard() {
  const projectId = useProjectStore(s => s.projectId);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [embedded, setEmbedded] = useState(null); // experiment_id with the iframe open

  useEffect(() => {
    if (!projectId) return undefined;
    let cancelled = false;
    setBusy(true);
    api.getMlflowOverview(projectId)
      .then(d => { if (!cancelled) { setData(d); setError(null); } })
      .catch(e => { if (!cancelled) setError(e.message); })
      .finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
  }, [projectId]);

  function refresh() {
    if (!projectId) return;
    setBusy(true);
    api.getMlflowOverview(projectId)
      .then(d => { setData(d); setError(null); })
      .catch(e => setError(e.message))
      .finally(() => setBusy(false));
  }

  const mlflow = data?.mlflow;
  const experiments = Array.isArray(data?.experiments) ? data.experiments : [];
  const dashboardUrl = mlflow?.configured ? (mlflow.dashboard_url || mlflow.tracking_uri) : null;

  return (
    <div className="page-stage">
      <header className="page-header page-header--lg">
        <div className="page-head-row">
          <div>
            <h1 className="page-title">MLflow</h1>
            <p className="page-summary">Every experiment&rsquo;s runs and metric curves, in one place.</p>
          </div>
          <div className="page-actions">
            <button className="btn btn--ghost" onClick={refresh} disabled={busy}>
              {busy ? 'Refreshing…' : 'Refresh'}
            </button>
            {dashboardUrl && (
              <a className="btn" href={dashboardUrl} target="_blank" rel="noreferrer">Open full MLflow ↗</a>
            )}
          </div>
        </div>
      </header>

      {error && <div className="error-message">{error}</div>}

      {!data ? null : !mlflow?.configured ? (
        <div className="empty-state">
          <h2>MLflow isn&rsquo;t configured</h2>
          {mlflow?.note && <p>{mlflow.note}</p>}
        </div>
      ) : experiments.length === 0 ? (
        <div className="empty-state"><h2>No experiments yet</h2></div>
      ) : (
        <div className="stack stack--lg">
          {experiments.map(exp => (
            <ExperimentMlflowCard
              key={exp.experiment_id}
              exp={exp}
              embedded={embedded === exp.experiment_id}
              onToggleEmbed={() => setEmbedded(prev => (prev === exp.experiment_id ? null : exp.experiment_id))}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ExperimentMlflowCard({ exp, embedded, onToggleEmbed }) {
  const metrics = exp.metrics && typeof exp.metrics === 'object' ? exp.metrics : {};
  const runs = (Array.isArray(metrics.experiments) ? metrics.experiments : [])
    .flatMap(me => (Array.isArray(me.runs) ? me.runs : []));
  const hasRuns = metrics.available !== false && runs.length > 0;
  const drillUrl = exp.dashboard_experiment_url || null;

  return (
    <section className="mlf-card">
      <div className="mlf-card-head">
        <div className="mlf-card-titles">
          <h2 className="mlf-card-name">{exp.name}</h2>
          {exp.intent && <p className="mlf-card-intent">{exp.intent}</p>}
        </div>
        <div className="cluster">
          {exp.status && <StatusPill value={exp.status} />}
          {drillUrl && (
            <button className="btn btn--sm btn--ghost" onClick={onToggleEmbed}>
              {embedded ? 'Hide MLflow' : 'View runs'}
            </button>
          )}
          {drillUrl && (
            <a className="btn btn--sm btn--ghost" href={drillUrl} target="_blank" rel="noreferrer">Open ↗</a>
          )}
        </div>
      </div>

      {hasRuns ? (
        <div className="mlf-runs">
          {runs.map((run, ri) => <RunCurves key={run.run_id || ri} run={run} />)}
        </div>
      ) : (
        <p className="mlf-empty">No runs recorded yet.</p>
      )}

      {embedded && drillUrl && (
        <div className="mlf-embed">
          {/* Sandboxed like the per-sandbox MLflow tab: scripts + same-origin so
              the app runs, but no top-navigation or forms can hijack the page. */}
          <iframe
            title={`MLflow — ${exp.name}`}
            src={drillUrl}
            className="mlf-embed-frame"
            sandbox="allow-scripts allow-same-origin"
          />
        </div>
      )}
    </section>
  );
}

function RunCurves({ run }) {
  const history = run.history && typeof run.history === 'object' ? run.history : {};
  const params = run.params && typeof run.params === 'object' ? run.params : {};
  const metricKeys = Object.keys(history);
  const paramEntries = Object.entries(params);

  return (
    <div className="mlf-run">
      <div className="mlf-run-head">
        <span className="mlf-run-name">{run.run_name || run.run_id}</span>
        {run.status && <span className="mlf-run-status">{run.status}</span>}
      </div>
      {metricKeys.length === 0 ? (
        <p className="mlf-empty">No metric history.</p>
      ) : (
        <div className="mlf-curve-grid">
          {metricKeys.map(key => {
            const values = seriesValues(history[key]);
            const final = values.length ? values[values.length - 1] : null;
            return (
              <div className="mlf-curve" key={key}>
                <div className="mlf-curve-head">
                  <span className="mlf-curve-key" title={key}>{key}</span>
                  <span className="mlf-curve-val">{fmtNum(final)}</span>
                </div>
                <Sparkline points={values} height={48} />
              </div>
            );
          })}
        </div>
      )}
      {paramEntries.length > 0 && (
        <div className="mlf-params">
          {paramEntries.map(([k, v]) => (
            <span className="mlf-param" key={k}><span className="mlf-param-k">{k}</span> {String(v)}</span>
          ))}
        </div>
      )}
    </div>
  );
}
