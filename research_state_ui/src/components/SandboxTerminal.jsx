import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import StatusPill from './StatusPill';
import TerminalLog from './TerminalLog';

/**
 * SandboxTerminal — a window into one experiment's cloud sandbox.
 *
 * Replaces the old job dashboard. The agent procures the sandbox (sandbox.request
 * over MCP) and runs commands over SSH itself; this panel only *observes*:
 *   - sandbox status + SSH connection details (read-only, copyable);
 *   - a live transcript of every command + output recorded in the sandbox;
 *   - **MLflow** (port 5000) and **TensorBoard** (port 6006) dashboards
 *     surfaced as provider URLs or daemon-owned local SSH forwards — rendered as
 *     `<iframe>` tabs sitting next to "Terminal" so the user can watch
 *     loss curves, gradients, and TB scalars live without ever opening a
 *     separate tab. Tabs only appear when the row exposes a URL for them.
 *
 * Polls GET /sandbox + /metrics every 3s. The terminal polls separately at
 * 1.5s while live, using the `since` cursor so each poll transfers only new
 * bytes (accumulated client-side) instead of re-pulling the whole tail.
 */
const PANEL_TABS = [
  { key: 'terminal', label: 'Terminal' },
  { key: 'mlflow', label: 'MLflow' },
  { key: 'tensorboard', label: 'TensorBoard' },
];
const RUNNING = 'running';
const PROVISIONING = 'provisioning';
const FAILED = 'failed';
const TERMINAL_POLL_MS = 1500;
// Client-side scrollback cap: keep memory bounded on day-long sandboxes.
const MAX_ACCUMULATED_CHARS = 2_000_000;

export default function SandboxTerminal({ projectId, experimentId, readOnly = false, collapsible = false }) {
  const [sandbox, setSandbox] = useState(null);
  // When `collapsible`, the panel defaults to its liveness: expanded while a
  // sandbox is live/provisioning, collapsed to just the header once the run has
  // ended (archived noise shouldn't sit open). `userCollapsed` stays null until
  // the user clicks, after which their choice wins over the liveness default —
  // this is an artifact-local rule, not page-level stage adaptation.
  const [userCollapsed, setUserCollapsed] = useState(null);
  const [transcript, setTranscript] = useState(null);
  const [termMeta, setTermMeta] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [error, setError] = useState(null);
  const [releasing, setReleasing] = useState(false);
  const [showRaw, setShowRaw] = useState(false);
  // Active observability tab. Terminal is always available; mlflow/tensorboard
  // only render when their URL is non-empty on the row. If the active tab loses
  // its URL (e.g. the sandbox was released), fall back to terminal.
  const [activeTab, setActiveTab] = useState('terminal');
  // Cursor-accumulated transcript: { sandboxId, cursor, text }. Reset whenever
  // the sandbox id changes or the server cursor regresses (new transcript).
  const accRef = useRef({ sandboxId: null, cursor: null, text: '' });
  const termBusyRef = useRef(false);

  const fetchOnce = useCallback(async () => {
    try {
      const sb = await api.getSandbox(projectId, experimentId);
      setSandbox(sb);
      setError(null);
      if (sb && sb.status === RUNNING) {
        try {
          setMetrics(await api.getSandboxMetrics(projectId, experimentId));
        } catch {
          /* live usage is best-effort */
        }
      } else {
        setMetrics(null);
      }
    } catch (err) {
      setError(err.message);
    }
  }, [projectId, experimentId]);

  const sandboxId = sandbox?.sandbox_id || null;
  const isLiveSandbox = sandbox?.status === RUNNING;

  const pollTerminal = useCallback(async () => {
    if (!sandboxId || termBusyRef.current) return;
    termBusyRef.current = true;
    try {
      const acc = accRef.current;
      const fresh = acc.sandboxId !== sandboxId || acc.cursor == null;
      const term = await api.getSandboxTerminal(
        projectId,
        experimentId,
        fresh ? {} : { since: acc.cursor },
      );
      setTermMeta({
        running: term.running,
        status: term.status,
        command_running: term.command_running,
        last_exit_code: term.last_exit_code,
        last_command_finished_at: term.last_command_finished_at,
      });
      const chunk = term.transcript || '';
      // A transient read failure returns an "(terminal unavailable: …)" body
      // with a meaningless cursor — keep the scrollback we already have.
      if (!fresh && chunk.startsWith('(terminal unavailable')) return;
      if (!fresh && term.cursor != null && term.cursor < acc.cursor) {
        // Cursor regressed (transcript replaced): refetch from scratch.
        accRef.current = { sandboxId, cursor: null, text: '' };
        return;
      }
      let text = fresh ? chunk : acc.text + chunk;
      if (text.length > MAX_ACCUMULATED_CHARS) {
        const cut = text.length - MAX_ACCUMULATED_CHARS;
        const nl = text.indexOf('\n', cut);
        text = text.slice(nl >= 0 ? nl + 1 : cut);
      }
      accRef.current = { sandboxId, cursor: term.cursor ?? null, text };
      setTranscript(text);
    } catch {
      /* terminal is best-effort */
    } finally {
      termBusyRef.current = false;
    }
  }, [projectId, experimentId, sandboxId]);

  // Pause polling while the tab/app is backgrounded and refresh on return.
  // Without this the 3s sandbox poll keeps the radio awake on a locked phone
  // (and the 1.5s terminal poll below is worse) — a live battery bug on every
  // surface, called out in docs/MOBILE_UX_REVIEW.md §1.4.
  useEffect(() => {
    let cancelled = false;
    fetchOnce();
    const tick = () => { if (!cancelled && document.visibilityState === 'visible') fetchOnce(); };
    const t = setInterval(tick, 3000);
    const onVis = () => { if (document.visibilityState === 'visible') fetchOnce(); };
    document.addEventListener('visibilitychange', onVis);
    return () => { cancelled = true; clearInterval(t); document.removeEventListener('visibilitychange', onVis); };
  }, [fetchOnce]);

  // Terminal poll: fast incremental while the sandbox is live, a single fetch
  // otherwise (a dead sandbox's transcript no longer changes). Also paused on
  // hide, with an immediate catch-up on return.
  useEffect(() => {
    if (!sandboxId) return undefined;
    let cancelled = false;
    const tick = () => { if (!cancelled && document.visibilityState === 'visible') pollTerminal(); };
    tick();
    const onVis = () => { if (document.visibilityState === 'visible') pollTerminal(); };
    document.addEventListener('visibilitychange', onVis);
    if (!isLiveSandbox) {
      return () => { cancelled = true; document.removeEventListener('visibilitychange', onVis); };
    }
    const t = setInterval(tick, TERMINAL_POLL_MS);
    return () => { cancelled = true; clearInterval(t); document.removeEventListener('visibilitychange', onVis); };
  }, [pollTerminal, sandboxId, isLiveSandbox]);

  const onRelease = useCallback(async () => {
    setReleasing(true);
    try {
      await api.releaseSandbox(projectId, experimentId);
      await fetchOnce();
    } catch (err) {
      setError(err.message);
    } finally {
      setReleasing(false);
    }
  }, [projectId, experimentId, fetchOnce]);

  const status = sandbox?.status || 'none';
  const isLive = status === RUNNING;
  const isProvisioning = status === PROVISIONING;
  const isFailed = status === FAILED;
  const hasPanel = status !== 'none';
  const collapsed = collapsible && (userCollapsed == null ? !(isLive || isProvisioning) : userCollapsed);

  return (
    <section className={`sbx${collapsed ? ' sbx--collapsed' : ''}`} id="execution">
      <header className="sbx-head">
        <div className="cluster" style={{ gap: 8 }}>
          <span className="sbx-title">Sandbox terminal</span>
          {hasPanel && <StatusPill value={status} />}
          {isLive && <span className="log-tail-live-dot" title="live" />}
        </div>
        <div className="cluster" style={{ gap: 8 }}>
          {/* readOnly hides the release path here so it flows only through the
              guarded slide-to-confirm on the Sandboxes screen (mobile) — see
              docs/MOBILE_UX_REVIEW.md §1.2. */}
          {!readOnly && (isLive || isProvisioning) && (
            <button className="btn btn--sm btn--ghost" onClick={onRelease} disabled={releasing}>
              {releasing ? 'Releasing…' : isProvisioning ? 'Cancel' : 'Release sandbox'}
            </button>
          )}
          {collapsible && (
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              aria-expanded={!collapsed}
              onClick={() => setUserCollapsed(!collapsed)}
            >
              <span className="toggle-verb">{collapsed ? 'Show' : 'Hide'}</span>
            </button>
          )}
        </div>
      </header>

      {!collapsed && (
        <>
          {error && <div className="error-message">{error}</div>}

          {!hasPanel ? (
            <div className="sbx-empty">
              No sandbox for this experiment yet. The agent provisions one with{' '}
              <span className="mono">sandbox.request</span> and then runs commands over SSH.
            </div>
          ) : isProvisioning ? (
            <div className="sbx-provisioning">
              <span className="log-tail-live-dot" title="provisioning" />
              <div>
                <div className="sbx-provisioning-title">
                  Provisioning{sandbox.phase ? ` · ${sandbox.phase}` : ''}
                </div>
                <div className="sbx-provisioning-detail">
                  {sandbox.detail || 'Setting up the sandbox (sync → create → SSH)…'}
                </div>
              </div>
            </div>
          ) : isFailed ? (
            <div className="sbx-failed">
              <div className="sbx-failed-title">Provisioning failed</div>
              <div className="sbx-failed-detail mono">{sandbox.error || 'unknown error'}</div>
              <div className="sbx-failed-hint">
                The agent can call <span className="mono">sandbox.request</span> to retry.
              </div>
            </div>
          ) : (
            <>
              <SandboxMeta sandbox={sandbox} />
              <SandboxUsage metrics={metrics} sandbox={sandbox} />
              <SandboxPanelTabs
                sandbox={sandbox}
                transcript={transcript}
                termMeta={termMeta}
                isLive={isLive}
                showRaw={showRaw}
                setShowRaw={setShowRaw}
                activeTab={activeTab}
                setActiveTab={setActiveTab}
                readOnly={readOnly}
              />
            </>
          )}
        </>
      )}
    </section>
  );
}

/**
 * SandboxPanelTabs — switches between the terminal transcript and the in-sandbox
 * observability dashboards (MLflow + TensorBoard). The dashboard URLs come from
 * the row's `dashboards` map (provider URLs or daemon-owned local SSH forwards).
 * A tab whose URL is empty is hidden; if the currently-active tab disappears
 * (sandbox released, tunnel relocation lost the URL temporarily), we fall back
 * to Terminal so the panel never goes blank.
 */
function SandboxPanelTabs({
  sandbox,
  transcript,
  termMeta,
  isLive,
  showRaw,
  setShowRaw,
  activeTab,
  setActiveTab,
  readOnly = false,
}) {
  const dashboards = sandbox?.dashboards || {};
  // readOnly (mobile) drops the MLflow/TensorBoard iframe tabs: those URLs are
  // usually 127.0.0.1 SSH forwards owned by the desktop and never load on the
  // phone — see docs/MOBILE_UX_REVIEW.md §1.5.
  const availableTabs = readOnly
    ? PANEL_TABS.filter((t) => t.key === 'terminal')
    : PANEL_TABS.filter((t) => t.key === 'terminal' || dashboards[t.key]);
  const effectiveTab = availableTabs.some((t) => t.key === activeTab)
    ? activeTab
    : 'terminal';
  // Full-screen dashboard overlay. Reset when leaving dashboard tabs so a
  // stale overlay can't swallow the page after the tab disappears.
  const [dashFullscreen, setDashFullscreen] = useState(false);
  useEffect(() => {
    if (effectiveTab === 'terminal') setDashFullscreen(false);
  }, [effectiveTab]);

  return (
    <div className="sbx-tabs">
      <div className="sbx-tabs-strip" role="tablist">
        {availableTabs.map((tab) => (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={effectiveTab === tab.key}
            className={`sbx-tab${effectiveTab === tab.key ? ' is-active' : ''}`}
            onClick={() => setActiveTab(tab.key)}
            title={
              tab.key === 'mlflow'
                ? 'MLflow tracking server (port 5000) — runs, metrics, params, artifacts'
                : tab.key === 'tensorboard'
                ? 'TensorBoard (port 6006) — scalars and event-file visualizations'
                : 'Recorded SSH command transcript'
            }
          >
            {tab.label}
            {tab.key !== 'terminal' && isLive && (
              <span className="log-tail-live-dot" title="live" />
            )}
          </button>
        ))}
        <div className="sbx-tabs-spacer" />
        {effectiveTab === 'terminal' && transcript && transcript.trim() !== '' && (
          <button
            type="button"
            className="sbx-term-toggle"
            onClick={() => setShowRaw((v) => !v)}
            title={showRaw ? 'Show formatted view' : 'Show raw transcript'}
          >
            {showRaw ? 'formatted' : 'raw'}
          </button>
        )}
        {effectiveTab !== 'terminal' && dashboards[effectiveTab] && (
          <>
            <button
              type="button"
              className="sbx-term-toggle"
              onClick={() => setDashFullscreen(true)}
              title="Expand this dashboard to full screen (Esc or the exit button to leave)"
            >
              full screen
            </button>
            <a
              href={dashboards[effectiveTab]}
              target="_blank"
              rel="noreferrer noopener"
              className="sbx-term-toggle"
              title="Open this dashboard in a new tab"
            >
              open ↗
            </a>
          </>
        )}
      </div>

      {effectiveTab === 'terminal' ? (
        <SandboxTerminalPane
          transcript={transcript}
          termMeta={termMeta}
          isLive={isLive}
          showRaw={showRaw}
        />
      ) : (
        <SandboxDashboardFrame
          name={effectiveTab}
          url={dashboards[effectiveTab]}
          fullscreen={dashFullscreen}
          onExitFullscreen={() => setDashFullscreen(false)}
        />
      )}
    </div>
  );
}

function SandboxTerminalPane({ transcript, termMeta, isLive, showRaw }) {
  return (
    <>
      <div className="sbx-term-head">
        <span>
          terminal transcript
          {transcript != null && ` · ${transcript.split('\n').length} lines`}
        </span>
      </div>
      {transcript == null ? (
        <div className="log-tail-empty">Loading transcript…</div>
      ) : transcript.trim() === '' ? (
        <div className="log-tail-empty">
          No commands recorded yet. Output appears here as the agent runs commands over SSH.
        </div>
      ) : (
        <TerminalLog text={transcript} live={isLive} raw={showRaw} meta={termMeta} />
      )}
    </>
  );
}

/**
 * SandboxDashboardFrame — embeds an MLflow or TensorBoard dashboard served from
 * inside the sandbox. The iframe is sandboxed (allow-scripts + same-origin) but
 * not allowed top-navigation or forms, so a malicious page inside the iframe
 * can't redirect the user away. The wrapper key includes the URL so a tunnel
 * relocation forces a clean reload rather than reusing a now-404 src.
 */
function SandboxDashboardFrame({ name, url, fullscreen = false, onExitFullscreen }) {
  const wrapRef = useRef(null);

  // Prefer the native Fullscreen API: Esc then exits *this element's*
  // fullscreen via the browser's own reserved-key handling — properly layered,
  // so a fullscreen browser window stays fullscreen. The CSS overlay below is
  // the fallback when requestFullscreen is unavailable or rejected.
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    if (fullscreen) {
      if (document.fullscreenElement !== el && el.requestFullscreen) {
        el.requestFullscreen().catch(() => {
          /* CSS overlay fallback keeps working */
        });
      }
    } else if (document.fullscreenElement === el) {
      document.exitFullscreen().catch(() => {});
    }
  }, [fullscreen]);

  // When the browser exits our element's fullscreen natively (Esc), sync
  // React state so the overlay class comes off too.
  useEffect(() => {
    if (!fullscreen) return undefined;
    const onChange = () => {
      if (!document.fullscreenElement) onExitFullscreen?.();
    };
    document.addEventListener('fullscreenchange', onChange);
    return () => document.removeEventListener('fullscreenchange', onChange);
  }, [fullscreen, onExitFullscreen]);

  // CSS-fallback mode only: handle Esc ourselves (capture + preventDefault so
  // nothing else — including a browser that would honor it — also reacts),
  // and lock the page scroll behind the overlay. In native fullscreen the
  // browser owns Esc and there is nothing to scroll.
  useEffect(() => {
    if (!fullscreen) return undefined;
    const onKey = (e) => {
      if (e.key !== 'Escape' || document.fullscreenElement) return;
      e.preventDefault();
      e.stopPropagation();
      onExitFullscreen?.();
    };
    window.addEventListener('keydown', onKey, true);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      window.removeEventListener('keydown', onKey, true);
      document.body.style.overflow = prevOverflow;
    };
  }, [fullscreen, onExitFullscreen]);

  if (!url) {
    return (
      <div className="log-tail-empty">
        No {name} dashboard URL on this sandbox yet. The dashboard server
        comes up shortly after SSH; refreshes on the next poll.
      </div>
    );
  }
  const title = name === 'mlflow' ? 'MLflow tracking UI' : 'TensorBoard';
  // Full screen toggles a class on the SAME node: the iframe is never
  // remounted, so the dashboard keeps its state instead of reloading.
  return (
    <div ref={wrapRef} className={`sbx-dashboard${fullscreen ? ' is-fullscreen' : ''}`} key={url}>
      {fullscreen && (
        <div className="sbx-dash-exitbar">
          <span className="sbx-dash-exitbar-title">{title}</span>
          <button type="button" className="btn btn--sm btn--ghost" onClick={onExitFullscreen}>
            exit full screen · Esc
          </button>
        </div>
      )}
      <iframe
        src={url}
        title={title}
        className="sbx-dashboard-frame"
        sandbox="allow-scripts allow-same-origin allow-popups allow-downloads"
        loading="lazy"
        referrerPolicy="no-referrer"
      />
    </div>
  );
}

function SandboxMeta({ sandbox }) {
  const [copied, setCopied] = useState(false);
  const host = sandbox.ssh_host;
  const port = sandbox.ssh_port;
  const user = sandbox.ssh_user || 'root';
  const command =
    host && port
      ? `ssh -i <key> -p ${port} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ${user}@${host}`
      : null;

  function copy() {
    if (!command) return;
    navigator.clipboard?.writeText(command);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div className="sbx-meta">
      <div className="sbx-meta-row">
        <span className="sbx-meta-key">id</span>
        <span className="mono">{sandbox.sandbox_id}</span>
      </div>
      {(sandbox.gpu || sandbox.cpu || sandbox.memory) && (
        <div className="sbx-meta-row">
          <span className="sbx-meta-key">resources</span>
          <span className="mono">
            {[sandbox.gpu && `gpu ${sandbox.gpu}`, sandbox.cpu && `${sandbox.cpu} cpu`, sandbox.memory && `${sandbox.memory} MiB`]
              .filter(Boolean)
              .join(' · ')}
          </span>
        </div>
      )}
      {host && port && (
        <div className="sbx-meta-row">
          <span className="sbx-meta-key">ssh</span>
          <span className="mono sbx-ssh">{user}@{host}:{port}</span>
          <button className="btn btn--xs btn--ghost" onClick={copy}>{copied ? 'copied' : 'copy cmd'}</button>
        </div>
      )}
      {sandbox.workdir && (
        <div className="sbx-meta-row">
          <span className="sbx-meta-key">workdir</span>
          <span className="mono">{sandbox.workdir}</span>
        </div>
      )}
      {sandbox.expires_at && (
        <div className="sbx-meta-row">
          <span className="sbx-meta-key">expires</span>
          <span className="mono">{sandbox.expires_at}</span>
        </div>
      )}
    </div>
  );
}

/**
 * SandboxUsage — live in-container resource gauges (CPU / RAM / GPU), sampled
 * inside the sandbox every poll. Best-effort: renders nothing until the first
 * successful sample, and a quiet note when the sampler is unavailable (e.g. a
 * CPU-only image without nvidia-smi). Reserved gpu/cpu/memory from the sandbox
 * row frame the bars when the cgroup limit isn't readable.
 */
function SandboxUsage({ metrics, sandbox }) {
  if (!metrics) return null;
  if (metrics.available === false || !metrics.metrics) {
    return (
      <div className="sbx-usage sbx-usage--empty">
        <span className="sbx-usage-title">live usage</span>
        <span className="sbx-usage-note">sampling…</span>
      </div>
    );
  }
  const m = metrics.metrics;
  const reservedMemBytes = sandbox?.memory ? sandbox.memory * 1024 * 1024 : null;

  const cpuUsed = m.cpu?.used_cores;
  const cpuLimit = m.cpu?.limit_cores || sandbox?.cpu || null;
  const memUsed = m.memory?.used_bytes;
  const memLimit = m.memory?.limit_bytes || reservedMemBytes;
  const gpus = Array.isArray(m.gpus) ? m.gpus : [];

  return (
    <div className="sbx-usage">
      <div className="sbx-usage-head">
        <span className="sbx-usage-title">live usage</span>
        <span className="log-tail-live-dot" title="sampled live" />
      </div>
      <div className="sbx-usage-grid">
        {cpuUsed != null && (
          <UsageBar
            label="CPU"
            value={cpuUsed}
            max={cpuLimit}
            pct={cpuLimit ? (cpuUsed / cpuLimit) * 100 : null}
            text={`${cpuUsed.toFixed(2)}${cpuLimit ? ` / ${fmtCores(cpuLimit)}` : ''} cores`}
          />
        )}
        {memUsed != null && (
          <UsageBar
            label="RAM"
            value={memUsed}
            max={memLimit}
            pct={memLimit ? (memUsed / memLimit) * 100 : null}
            text={`${fmtBytes(memUsed)}${memLimit ? ` / ${fmtBytes(memLimit)}` : ''}`}
            title="Resident memory in use (anonymous + unreclaimable). Excludes reclaimable page cache / mmapped files, so it reflects real pressure toward the reserved limit, not what `free` reports."
          />
        )}
        {gpus.map((g) => (
          <UsageBar
            key={`gpu-util-${g.index}`}
            label={gpus.length > 1 ? `GPU${g.index} util` : 'GPU util'}
            pct={g.util_pct}
            text={g.util_pct != null ? `${g.util_pct}%` : '—'}
          />
        ))}
        {gpus.map((g) => (
          g.mem_total_mib ? (
            <UsageBar
              key={`gpu-vram-${g.index}`}
              label={gpus.length > 1 ? `GPU${g.index} VRAM` : 'VRAM'}
              pct={g.mem_used_mib != null ? (g.mem_used_mib / g.mem_total_mib) * 100 : null}
              text={`${fmtMib(g.mem_used_mib)} / ${fmtMib(g.mem_total_mib)}`}
            />
          ) : null
        ))}
      </div>
    </div>
  );
}

function UsageBar({ label, pct, text, title }) {
  const clamped = pct == null ? null : Math.max(0, Math.min(100, pct));
  const hot = clamped != null && clamped >= 90;
  return (
    <div className="sbx-usage-item" title={title || undefined}>
      <div className="sbx-usage-item-head">
        <span className="sbx-usage-label">{label}</span>
        <span className="sbx-usage-value mono">{text}</span>
      </div>
      <div className="sbx-usage-track">
        <div
          className={`sbx-usage-fill${hot ? ' hot' : ''}`}
          style={{ width: clamped == null ? '0%' : `${clamped}%` }}
        />
      </div>
    </div>
  );
}

function fmtCores(n) {
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

function fmtBytes(bytes) {
  if (bytes == null) return '—';
  const gib = bytes / (1024 ** 3);
  if (gib >= 1) return `${gib.toFixed(gib >= 10 ? 0 : 1)} GiB`;
  const mib = bytes / (1024 ** 2);
  return `${Math.round(mib)} MiB`;
}

function fmtMib(mib) {
  if (mib == null) return '—';
  return fmtBytes(mib * 1024 * 1024);
}
