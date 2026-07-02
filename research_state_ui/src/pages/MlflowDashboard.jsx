import { useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useProjectStore, useProjectHref } from '../store/useProjectStore';
import { api } from '../api';
import { FrontierChart, DotStrip, KnobScatter } from '../components/LedgerCharts';
import { planLedger, rankRuns, anchorValueOf } from '../utils/metricProfile';
import { statusColor } from '../utils/experiment';
import { fmtNum, fmtStamp } from '../utils/format';

/**
 * MlflowDashboard — the project ledger as an instrument, not a lookup table.
 *
 * The page renders whatever `planLedger` decided the data supports: pulse →
 * frontier → leaderboard → per-metric strips → knob scatters → diagnostics →
 * invariants → sparse footnote, degrading panel-by-panel for thin projects.
 * `?focus=<experiment_id>` pins one experiment: its runs light up in every
 * panel and a readout compares it to the field on each metric.
 */
export default function MlflowDashboard() {
  const projectId = useProjectStore(s => s.projectId);
  const px = useProjectHref();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();
  const focusExpId = searchParams.get('focus');

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

  const plan = useMemo(() => (data && mlflow?.configured ? planLedger(data) : null), [data, mlflow]);
  const hasLedger = !!(plan && plan.runs.length > 0);
  const board = useMemo(() => (hasLedger ? rankRuns(plan) : []), [plan, hasLedger]);
  const bestI = plan?.summary?.best.i;

  // Focus contract shared by every panel: the pinned experiment's runs carry
  // the accent; without a pin, the accent marks the champion run.
  const emphasized = (i) => (focusExpId ? plan.runs[i].expId === focusExpId : i === bestI);
  const colorOf = (i) => (emphasized(i)
    ? 'var(--active)'
    : focusExpId ? 'color-mix(in srgb, var(--faint) 45%, transparent)' : 'var(--faint)');
  const sizeOf = (i) => (emphasized(i) ? 11 : 8);
  const toggleFocus = (expId) =>
    setSearchParams(expId && expId !== focusExpId ? { focus: expId } : {});
  const pick = (i) => toggleFocus(plan.runs[i].expId);

  const focusedExp = focusExpId ? experiments.find(e => e.experiment_id === focusExpId) : null;

  return (
    <div className="page-stage">
      <header className="page-header page-header--lg">
        <div className="page-head-row">
          <div>
            <h1 className="page-title">MLflow</h1>
            <p className="page-summary">The whole project&rsquo;s quantitative story — click any mark to focus its experiment.</p>
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
        <>
          {hasLedger && plan.summary && <Pulse plan={plan} />}

          {focusedExp && (
            <FocusBanner
              exp={focusedExp}
              plan={plan}
              onClear={() => toggleFocus(null)}
              openHref={px(`/experiments/${focusedExp.experiment_id}`)}
            />
          )}

          {hasLedger && plan.focus && (
            <section className="section">
              <h2 className="section-title">Frontier — {plan.focus.key}</h2>
              {plan.focus.directionAssumed && (
                <p className="lgd-note">No direction convention matched — assuming lower is better.</p>
              )}
              <FrontierChart
                runs={plan.runs}
                values={plan.strips.find(s => s.key === plan.focus.key)?.values || []}
                direction={plan.focus.direction}
                focusKey={plan.focus.key}
                colorOf={colorOf}
                sizeOf={sizeOf}
                onPick={pick}
              />
            </section>
          )}

          {board.length > 1 && (
            <section className="section">
              <h2 className="section-title">Leaderboard — {plan.focus.key}, delta vs each run&rsquo;s baseline</h2>
              <div className="lgd-board">
                {board.map((p, rank) => (
                  <BoardRow
                    key={plan.runs[p.i].runId || p.i}
                    rank={rank + 1}
                    run={plan.runs[p.i]}
                    value={p.v}
                    plan={plan}
                    champ={p.i === bestI}
                    focused={focusExpId === plan.runs[p.i].expId}
                    onFocus={() => pick(p.i)}
                    openHref={px(`/experiments/${plan.runs[p.i].expId}`)}
                  />
                ))}
              </div>
            </section>
          )}

          {hasLedger && plan.strips.length > 0 && (
            <section className="section">
              <h2 className="section-title">Metrics across runs</h2>
              <div className="lgd-strips">
                {plan.strips.map(fp => (
                  <div className="lgd-striprow" key={fp.key}>
                    <span className="lgd-strip-key" title={fp.key}>
                      {fp.key}{fp.direction !== 0 && <span className="dir"> {fp.direction < 0 ? '↓ good' : '↑ good'}</span>}
                    </span>
                    <DotStrip runs={plan.runs} fp={fp} colorOf={colorOf} sizeOf={sizeOf} onPick={pick} />
                    <span className="lgd-strip-range">{fmtNum(fp.min)} – {fmtNum(fp.max)}</span>
                  </div>
                ))}
              </div>
            </section>
          )}

          {hasLedger && plan.knobs.length > 0 && plan.focus && (
            <section className="section">
              <h2 className="section-title">Which knob moves {plan.focus.key}</h2>
              <div className="lgd-knobs">
                {/* A scatter needs ≥3 points to say anything; thinner knobs
                    get a mention, not a panel. */}
                {plan.knobs.filter(k => k.points.length >= 3).map(knob => (
                  <KnobScatter
                    key={knob.key}
                    runs={plan.runs}
                    knob={knob}
                    focusKey={plan.focus.key}
                    colorOf={colorOf}
                    sizeOf={sizeOf}
                    onPick={pick}
                  />
                ))}
              </div>
              {plan.knobs.some(k => k.points.length < 3) && (
                <p className="lgd-line">
                  also varied:&nbsp;
                  {plan.knobs.filter(k => k.points.length < 3).map((k, j) => (
                    <span key={k.key}>{j > 0 && ' · '}<span className="lgd-line-k">{k.key}</span> ({k.points.length} run{k.points.length === 1 ? '' : 's'})</span>
                  ))}
                </p>
              )}
              <p className="lgd-note">Spearman ρ over recorded runs — association, not causation.</p>
            </section>
          )}

          {hasLedger && (plan.diagnostics.length > 0 || plan.invariants.length > 0 || plan.sparse.length > 0) && (
            <LedgerFootnotes plan={plan} />
          )}

          <section className="section">
            <h2 className="section-title">Timeline</h2>
            <Timeline
              experiments={experiments}
              plan={plan}
              focusExpId={focusExpId}
              onFocus={toggleFocus}
              hrefOf={(id) => px(`/experiments/${id}`)}
            />
          </section>
        </>
      )}
    </div>
  );
}

// ── planned panels ─────────────────────────────────────────────────────

function Pulse({ plan }) {
  const { summary, focus } = plan;
  const { best, projectBaseline, sinceBest, runCount, expCount, liveCount } = summary;
  const dir = focus.direction;
  const deltaPct = projectBaseline != null ? ((best.value - projectBaseline) / Math.abs(projectBaseline)) * 100 : null;
  const improved = deltaPct != null && (dir < 0 ? deltaPct < 0 : deltaPct > 0);
  return (
    <div className="lgd-pulse">
      <div className="lgd-tile">
        <div className="lgd-tile-k">best {focus.key}</div>
        <div className="lgd-tile-v">{fmtNum(best.value)}</div>
        <div className="lgd-tile-s">{best.run.expName}</div>
      </div>
      {deltaPct != null && (
        <div className="lgd-tile">
          <div className="lgd-tile-k">vs first baseline</div>
          <div className={`lgd-tile-v ${improved ? 'good' : 'bad'}`}>{deltaPct >= 0 ? '+' : '−'}{Math.abs(deltaPct).toFixed(2)}%</div>
          <div className="lgd-tile-s">from {fmtNum(projectBaseline)}</div>
        </div>
      )}
      <div className="lgd-tile">
        <div className="lgd-tile-k">runs</div>
        <div className="lgd-tile-v">{runCount}</div>
        <div className="lgd-tile-s">{expCount} experiment{expCount === 1 ? '' : 's'}{liveCount > 0 ? ` · ${liveCount} live` : ''}</div>
      </div>
      <div className="lgd-tile">
        <div className="lgd-tile-k">since best</div>
        <div className="lgd-tile-v">{sinceBest === 0 ? 'current' : sinceBest}</div>
        <div className="lgd-tile-s">{sinceBest === 0 ? 'latest run holds it' : `run${sinceBest === 1 ? '' : 's'} without improvement`}</div>
      </div>
    </div>
  );
}

// The pinned experiment vs the field, one readout per comparable metric.
function FocusBanner({ exp, plan, onClear, openHref }) {
  const cells = plan.strips.map(fp => {
    const mine = fp.values.filter(p => plan.runs[p.i].expId === exp.experiment_id);
    if (!mine.length) return null;
    const dir = fp.direction || -1;
    const best = mine.reduce((a, b) => (dir < 0 ? (b.v < a.v ? b : a) : (b.v > a.v ? b : a)));
    const sorted = fp.values.slice().sort((a, b) => (dir < 0 ? a.v - b.v : b.v - a.v));
    const rank = sorted.findIndex(p => p.i === best.i) + 1;
    return { key: fp.key, value: best.v, rank, n: fp.values.length, directional: fp.direction !== 0 };
  }).filter(Boolean);

  return (
    <div className="lgd-focus">
      <div className="lgd-focus-head">
        <span className="lgd-focus-name">{exp.name}</span>
        {exp.status && (
          <span className="lgd-focus-status" style={{ color: statusColor(exp.status) }}>
            {String(exp.status).replace(/_/g, ' ')}
          </span>
        )}
        <Link className="btn btn--sm btn--ghost" to={openHref}>Open experiment</Link>
        <button className="btn btn--sm btn--ghost" onClick={onClear}>Clear focus</button>
      </div>
      {exp.intent && <p className="lgd-focus-intent">{exp.intent}</p>}
      {cells.length > 0 ? (
        <div className="lgd-focus-grid">
          {cells.map(c => (
            <div className="lgd-focus-cell" key={c.key}>
              <span className="lgd-focus-key" title={c.key}>{c.key}</span>
              <span className="lgd-focus-val">{fmtNum(c.value)}</span>
              <span className={`lgd-focus-rank${c.directional && c.rank === 1 ? ' top' : ''}`}>#{c.rank}/{c.n}</span>
            </div>
          ))}
        </div>
      ) : (
        <p className="lgd-note">No recorded runs yet — nothing to compare.</p>
      )}
    </div>
  );
}

function BoardRow({ rank, run, value, plan, champ, focused, onFocus, openHref }) {
  const anchor = anchorValueOf(run, plan.focus.key);
  const delta = anchor != null ? value - anchor : null;
  const improved = delta != null && (plan.focus.direction < 0 ? delta < 0 : delta > 0);
  return (
    <div
      className={`lgd-row${champ ? ' champ' : ''}${focused ? ' focused' : ''}`}
      role="button"
      tabIndex={0}
      onClick={onFocus}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onFocus(); } }}
    >
      <span className="lgd-rank">{rank}</span>
      <span className="lgd-run-name">
        {run.expName}
        {run.runName !== run.expName && <span className="lgd-run-sub">{run.runName}</span>}
      </span>
      <span className="lgd-val">{fmtNum(value)}</span>
      <span className={`lgd-delta ${delta == null ? 'na' : improved ? 'good' : 'bad'}`}>
        {delta == null ? '—' : `${delta >= 0 ? '+' : '−'}${fmtNum(Math.abs(delta))}`}
      </span>
      <Link className="lgd-open" to={openHref} onClick={(e) => e.stopPropagation()}>open →</Link>
    </div>
  );
}

// Footnotes: failures are verdicts and stay loud; health, shared config, and
// sparse bookkeeping are reference material behind one quiet disclosure.
function LedgerFootnotes({ plan }) {
  const [open, setOpen] = useState(false);
  const failures = plan.diagnostics
    .flatMap(fp => fp.values.filter(p => p.v !== 0).map(p => ({ key: fp.key, run: plan.runs[p.i], v: p.v })));
  const cleanDiags = plan.diagnostics.filter(fp => fp.values.every(p => p.v === 0));

  const foldedLabel = [
    plan.invariants.length > 0 && `${plan.invariants.length} shared constant${plan.invariants.length === 1 ? '' : 's'}`,
    plan.sparse.length > 0 && `${plan.sparse.length} sparsely logged`,
    cleanDiags.length > 0 && 'exit codes clean',
  ].filter(Boolean).join(' · ');

  if (failures.length === 0 && !foldedLabel) return null;

  return (
    <section className="section">
      <h2 className="section-title">Diagnostics &amp; constants</h2>
      {failures.length > 0 && (
        <p className="lgd-line">
          {failures.map((f, j) => (
            <span key={`${f.key}:${f.run.runId}`}>
              {j > 0 && ' · '}
              <span className="lgd-chip bad">{f.run.runName} {f.key} = {fmtNum(f.v)}</span>
            </span>
          ))}
        </p>
      )}
      {foldedLabel && (
        <>
          <button type="button" className="rr-more" onClick={() => setOpen(v => !v)} aria-expanded={open}>
            {open ? '▾' : '▸'} {foldedLabel}
          </button>
          {open && (
            <>
              {plan.invariants.length > 0 && (
                <p className="lgd-line">
                  shared across all runs:&nbsp;
                  {plan.invariants.map((iv, j) => (
                    <span key={iv.key}>{j > 0 && ' · '}<span className="lgd-line-k">{iv.key}</span> {fmtNum(iv.value)}</span>
                  ))}
                </p>
              )}
              {plan.sparse.length > 0 && (
                <p className="lgd-line">
                  sparsely logged:&nbsp;
                  {plan.sparse.map((fp, j) => (
                    <span key={fp.key}>{j > 0 && ' · '}<span className="lgd-line-k">{fp.key}</span> ({fp.values.length} run{fp.values.length === 1 ? '' : 's'})</span>
                  ))}
                </p>
              )}
              {cleanDiags.length > 0 && (
                <p className="lgd-line">
                  {cleanDiags.map((fp, j) => (
                    <span key={fp.key}>
                      {j > 0 && ' · '}
                      <span className="lgd-line-k">{fp.key}</span> <span className="lgd-chip ok">0 in all {fp.values.length} runs</span>
                    </span>
                  ))}
                </p>
              )}
            </>
          )}
        </>
      )}
    </section>
  );
}

// The record of what was tried, when: flush chronological rows — stamp,
// state index, intent, and how the attempt landed on the focus metric.
function Timeline({ experiments, plan, focusExpId, onFocus, hrefOf }) {
  const entries = experiments.map(exp => {
    const runsOf = plan ? plan.runs.filter(r => r.expId === exp.experiment_id) : [];
    const stamp = runsOf.length
      ? Math.min(...runsOf.map(r => r.start || Infinity))
      : (exp.metrics?.experiments?.[0]?.last_update_time ?? null);
    return { exp, runsOf, stamp: Number.isFinite(stamp) ? stamp : null };
  }).sort((a, b) => (a.stamp ?? Infinity) - (b.stamp ?? Infinity));

  return (
    <div className="lgd-tl">
      {entries.map(({ exp, runsOf, stamp }) => (
        <TimelineRow
          key={exp.experiment_id}
          exp={exp}
          runsOf={runsOf}
          stamp={stamp}
          plan={plan}
          focused={focusExpId === exp.experiment_id}
          onFocus={() => onFocus(exp.experiment_id)}
          openHref={hrefOf(exp.experiment_id)}
        />
      ))}
    </div>
  );
}

function TimelineRow({ exp, runsOf, stamp, plan, focused, onFocus, openHref }) {
  const color = statusColor(exp.status);
  const focusKey = plan?.focus?.key;

  // How the attempt landed: its best focus-metric value and the shift vs its
  // own recorded baseline.
  let read = null;
  if (focusKey && runsOf.length) {
    const dir = plan.focus.direction;
    const vals = runsOf
      .map(r => ({ r, v: r.metrics[focusKey]?.last }))
      .filter(x => Number.isFinite(x.v));
    if (vals.length) {
      const best = vals.reduce((a, b) => (dir < 0 ? (b.v < a.v ? b : a) : (b.v > a.v ? b : a)));
      const anchor = anchorValueOf(best.r, focusKey);
      const delta = anchor != null ? best.v - anchor : null;
      read = { v: best.v, delta, improved: delta != null && (dir < 0 ? delta < 0 : delta > 0) };
    }
  }

  return (
    <div
      className={`lgd-tlrow${focused ? ' focused' : ''}`}
      style={{ borderLeftColor: color }}
      role="button"
      tabIndex={0}
      onClick={onFocus}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onFocus(); } }}
    >
      <span className="lgd-tl-when">{stamp ? fmtStamp(stamp) : '—'}</span>
      <span className="lgd-tl-main">
        <span className="lgd-tl-name">{exp.name}</span>
        <span className="lgd-tl-status" style={{ color }}>{String(exp.status || '').replace(/_/g, ' ')}</span>
        {exp.intent && <span className="lgd-tl-intent">{exp.intent}</span>}
      </span>
      <span className={`lgd-tl-read${read ? '' : ' na'}`}>
        {read ? (
          <>
            {fmtNum(read.v)}
            {read.delta != null && (
              <span className={read.improved ? 'good' : 'bad'}>
                {' '}{read.delta >= 0 ? '+' : '−'}{fmtNum(Math.abs(read.delta))}
              </span>
            )}
            {runsOf.length > 1 && <span className="n"> · {runsOf.length} runs</span>}
          </>
        ) : 'no runs'}
      </span>
      <Link className="lgd-open" to={openHref} onClick={(e) => e.stopPropagation()}>open →</Link>
      {exp.dashboard_experiment_url && (
        <a
          className="lgd-open"
          href={exp.dashboard_experiment_url}
          target="_blank"
          rel="noreferrer"
          onClick={(e) => e.stopPropagation()}
        >mlflow ↗</a>
      )}
    </div>
  );
}
