import { useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api } from '../api';
import { useProjectStore, useProjectHref } from '../store/useProjectStore';
import { FrontierChart } from '../components/LedgerCharts';
import { planLedger, anchorValueOf } from '../utils/metricProfile';
import { statusColor } from '../utils/experiment';
import { Skeleton } from './Skeleton';
import { fmtNum } from '../utils/format';

const REFRESH_MS = 60000;

/**
 * MLflow (mobile) — the project ledger as a split instrument. The top block
 * (pulse verdict → frontier → metric chips → column headers) sticks under
 * the app bar; the run table scrolls beneath it with native physics — the
 * split screen without scroll hijack. One metric is in focus at a time
 * (chips pivot the chart, the value column, and the sort), tapping a row or
 * a dot pins its experiment (?focus=, same contract as desktop).
 */
export default function MobileMlflow() {
  const px = useProjectHref();
  const projectId = useProjectStore(s => s.projectId);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const focusExpId = searchParams.get('focus');
  const [metricKey, setMetricKey] = useState(null); // null → the plan's focus metric
  const [sort, setSort] = useState({ col: 'value', asc: null }); // asc null → column default

  useEffect(() => {
    if (!projectId) return undefined;
    let cancelled = false;
    const load = () => api.getMlflowOverview(projectId)
      .then(d => { if (!cancelled) { setData(d); setError(null); } })
      .catch(e => { if (!cancelled) setError(e.message); });
    load();
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') load();
    }, REFRESH_MS);
    return () => { cancelled = true; clearInterval(t); };
  }, [projectId]);

  const plan = useMemo(() => (data && data.mlflow?.configured ? planLedger(data) : null), [data]);
  const hasLedger = !!(plan && plan.runs.length > 0);

  // The pivoted metric: chips switch it; everything below follows.
  const fp = useMemo(() => {
    if (!hasLedger) return null;
    return plan.strips.find(s => s.key === metricKey)
      || plan.strips.find(s => s.key === plan.focus?.key)
      || plan.strips[0] || null;
  }, [plan, hasLedger, metricKey]);
  // Direction: the declared/heuristic focus direction for the primary key,
  // the name heuristic otherwise; an anchored metric defaults to lower-good.
  const dir = fp ? (fp.key === plan.focus?.key ? plan.focus.direction : (fp.direction || (fp.hasAnchor ? -1 : 0))) : 0;

  const rows = useMemo(() => {
    if (!fp) return [];
    const firstSeen = new Map();
    plan.runs.forEach(r => { if (!firstSeen.has(r.expId)) firstSeen.set(r.expId, firstSeen.size); });
    return fp.values.map(p => {
      const run = plan.runs[p.i];
      const anchor = anchorValueOf(run, fp.key);
      return {
        i: p.i, run, value: p.v,
        ord: firstSeen.get(run.expId),
        name: run.expName,
        delta: anchor != null ? p.v - anchor : null,
      };
    });
  }, [plan, fp]);

  const goodFirst = (dir || -1) < 0;
  const defaultAsc = { ord: true, name: true, value: goodFirst, delta: goodFirst };
  const sortAsc = sort.asc ?? defaultAsc[sort.col];
  const board = useMemo(() => {
    const cmp = {
      ord: (a, b) => a.ord - b.ord,
      name: (a, b) => a.name.localeCompare(b.name),
      value: (a, b) => a.value - b.value,
      delta: (a, b) => (a.delta ?? Infinity) - (b.delta ?? Infinity),
    }[sort.col];
    const s = rows.slice().sort(cmp);
    if (!sortAsc) s.reverse();
    return sort.col === 'delta' ? [...s.filter(r => r.delta != null), ...s.filter(r => r.delta == null)] : s;
  }, [rows, sort, sortAsc]);
  const onSort = (col) =>
    setSort(prev => (prev.col === col ? { col, asc: !(prev.asc ?? defaultAsc[col]) } : { col, asc: null }));

  // Champion of the pivoted metric — only where direction makes "best" honest.
  const bestI = useMemo(() => {
    if (!fp || !dir) return null;
    let b = null;
    for (const { i, v } of fp.values) if (!b || (dir < 0 ? v < b.v : v > b.v)) b = { i, v };
    return b ? b.i : null;
  }, [fp, dir]);

  const emphasized = (i) => (focusExpId ? plan.runs[i].expId === focusExpId : i === bestI);
  const colorOf = (i) => (emphasized(i)
    ? 'var(--active)'
    : focusExpId ? 'color-mix(in srgb, var(--faint) 45%, transparent)' : 'var(--faint)');
  const sizeOf = (i) => (emphasized(i) ? 13 : 10);
  const toggleFocus = (expId) =>
    setSearchParams(expId && expId !== focusExpId ? { focus: expId } : {});
  const pick = (i) => toggleFocus(plan.runs[i].expId);

  const failures = hasLedger
    ? plan.diagnostics.flatMap(d => d.values.filter(p => p.v !== 0).map(p => ({ key: d.key, run: plan.runs[p.i], v: p.v })))
    : [];
  const norun = hasLedger && Array.isArray(data.experiments)
    ? data.experiments.filter(e => !plan.runs.some(r => r.expId === e.experiment_id)).length
    : 0;

  return (
    <div className="mlg">
      <div className="mlg-top">
        <h1 className="mtitle-lg">MLflow</h1>

        {error && <div className="mbanner">{error}</div>}
        {!data ? (
          <Skeleton lines={5} />
        ) : !data.mlflow?.configured ? (
          <div className="mquiet">MLflow isn't configured{data.mlflow?.note ? ` — ${data.mlflow.note}` : ''}</div>
        ) : !hasLedger ? (
          <div className="mquiet">no runs recorded yet</div>
        ) : (
          <>
            {plan.summary && <PulseLine plan={plan} />}
            {failures.length > 0 && (
              <div className="mlg-fail">
                {failures.map((f, j) => (
                  <span key={`${f.key}:${f.run.runId}`}>{j > 0 && ' · '}{f.run.runName} {f.key} = {fmtNum(f.v)}</span>
                ))}
              </div>
            )}

            {plan.strips.length > 1 && (
              <div className="mlg-chips" role="tablist" aria-label="Pivot on one metric">
                {plan.strips.map(s => (
                  <button
                    key={s.key}
                    type="button"
                    role="tab"
                    aria-selected={fp?.key === s.key}
                    className={fp?.key === s.key ? 'on' : ''}
                    onClick={() => setMetricKey(s.key)}
                  >{s.key}</button>
                ))}
              </div>
            )}

            {fp && (
              <FrontierChart
                runs={plan.runs}
                values={fp.values}
                direction={dir}
                focusKey={fp.key}
                colorOf={colorOf}
                sizeOf={sizeOf}
                onPick={pick}
              />
            )}

            {focusExpId && fp && (
              <FocusStrip
                plan={plan}
                fp={fp}
                dir={dir}
                expId={focusExpId}
                openHref={px(`/experiments/${focusExpId}`)}
                onClear={() => toggleFocus(null)}
              />
            )}

            {fp && (
              <div className="mlg-thead">
                {[
                  ['ord', '#', 'mlg-c-ord'],
                  ['name', 'experiment', 'mlg-c-name'],
                  ['value', fp.key, 'mlg-c-val'],
                  ['delta', 'Δ', 'mlg-c-delta'],
                ].map(([col, label, cls]) => (
                  <button key={col} type="button" className={`${cls}${sort.col === col ? ' on' : ''}`} onClick={() => onSort(col)}>
                    {label}{sort.col === col && <span className="arr"> {sortAsc ? '▲' : '▼'}</span>}
                  </button>
                ))}
              </div>
            )}
          </>
        )}
      </div>

      {hasLedger && board.map(row => {
        const improved = row.delta != null && dir !== 0 && (dir < 0 ? row.delta < 0 : row.delta > 0);
        const champ = row.i === bestI;
        const focused = focusExpId === row.run.expId;
        return (
          <div
            key={row.run.runId || row.i}
            className={`mlg-row${champ ? ' champ' : ''}${focused ? ' focused' : ''}`}
            role="button"
            tabIndex={0}
            onClick={() => pick(row.i)}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(row.i); } }}
          >
            <span className="mlg-c-ord">{row.ord}</span>
            <span className="mlg-c-name">{row.name}</span>
            <span className="mlg-c-val">{fmtNum(row.value)}</span>
            <span className={`mlg-c-delta${row.delta == null ? ' na' : dir === 0 ? '' : improved ? ' good' : ' bad'}`}>
              {row.delta == null ? '—' : `${row.delta >= 0 ? '+' : '−'}${fmtNum(Math.abs(row.delta))}`}
            </span>
          </div>
        );
      })}

      {hasLedger && norun > 0 && (
        <p className="mlg-note">+ {norun} experiment{norun === 1 ? '' : 's'} without recorded runs</p>
      )}
    </div>
  );
}

// The project verdict in one standing line — always the primary metric,
// whatever the chips pivot below. Staleness lives in the chart, not here.
function PulseLine({ plan }) {
  const { best, projectBaseline, liveCount } = plan.summary;
  const deltaPct = projectBaseline != null
    ? ((best.value - projectBaseline) / Math.abs(projectBaseline)) * 100
    : null;
  return (
    <p className="mlg-pulse">
      best {plan.focus.key} <b>{fmtNum(best.value)}</b>
      {deltaPct != null && <> · <b>{deltaPct >= 0 ? '+' : '−'}{Math.abs(deltaPct).toFixed(2)}%</b> vs baseline</>}
      {liveCount > 0 && <> · {liveCount} live</>}
    </p>
  );
}

// The pinned experiment vs the field on the pivoted metric, in one line.
function FocusStrip({ plan, fp, dir, expId, openHref, onClear }) {
  const run = plan.runs.find(r => r.expId === expId);
  const mine = fp.values.filter(p => plan.runs[p.i].expId === expId);
  const d = dir || -1;
  const best = mine.length
    ? mine.reduce((a, b) => (d < 0 ? (b.v < a.v ? b : a) : (b.v > a.v ? b : a)))
    : null;
  const sorted = fp.values.slice().sort((a, b) => (d < 0 ? a.v - b.v : b.v - a.v));
  const rank = best ? sorted.findIndex(p => p.i === best.i) + 1 : null;
  const status = run?.expStatus;
  return (
    <div className="mlg-fstrip">
      <span className="mlg-fstrip-name">{run?.expName || expId}</span>
      {status && <span className="mlg-fstrip-status" style={{ color: statusColor(status) }}>{String(status).replace(/_/g, ' ')}</span>}
      {best && <span className="mlg-fstrip-val">{fmtNum(best.v)}<span className="r"> #{rank}/{fp.values.length}</span></span>}
      <Link className="mlg-fstrip-open" to={openHref}>open →</Link>
      <button type="button" className="mlg-fstrip-x" onClick={onClear} aria-label="Clear focus">×</button>
    </div>
  );
}
