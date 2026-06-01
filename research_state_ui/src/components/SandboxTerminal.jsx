import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import StatusPill from './StatusPill';
import TerminalLog from './TerminalLog';

/**
 * SandboxTerminal — a window into one experiment's Modal sandbox.
 *
 * Replaces the old job dashboard. The agent procures the sandbox (sandbox.request
 * over MCP) and runs commands over SSH itself; this panel only *observes*:
 *   - sandbox status + SSH connection details (read-only, copyable);
 *   - a live transcript of every command + output recorded in the sandbox.
 *
 * Polls GET /sandbox + /sandbox/terminal every 3s while the sandbox is running.
 */
const RUNNING = 'running';
const PROVISIONING = 'provisioning';
const FAILED = 'failed';

export default function SandboxTerminal({ projectId, experimentId }) {
  const [sandbox, setSandbox] = useState(null);
  const [transcript, setTranscript] = useState(null);
  const [error, setError] = useState(null);
  const [releasing, setReleasing] = useState(false);
  const [showRaw, setShowRaw] = useState(false);

  const fetchOnce = useCallback(async () => {
    try {
      const sb = await api.getSandbox(projectId, experimentId);
      setSandbox(sb);
      setError(null);
      if (sb && sb.sandbox_id) {
        try {
          const term = await api.getSandboxTerminal(projectId, experimentId);
          setTranscript(term.transcript || '');
        } catch {
          /* terminal is best-effort */
        }
      }
    } catch (err) {
      setError(err.message);
    }
  }, [projectId, experimentId]);

  useEffect(() => {
    let cancelled = false;
    fetchOnce();
    const tick = () => { if (!cancelled) fetchOnce(); };
    const t = setInterval(tick, 3000);
    return () => { cancelled = true; clearInterval(t); };
  }, [fetchOnce]);

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

  return (
    <section className="sbx" id="execution">
      <header className="sbx-head">
        <div className="cluster" style={{ gap: 8 }}>
          <span className="sbx-title">Sandbox terminal</span>
          {hasPanel && <StatusPill value={status} />}
          {isLive && <span className="log-tail-live-dot" title="live" />}
        </div>
        {(isLive || isProvisioning) && (
          <button className="btn btn--sm btn--ghost" onClick={onRelease} disabled={releasing}>
            {releasing ? 'Releasing…' : isProvisioning ? 'Cancel' : 'Release sandbox'}
          </button>
        )}
      </header>

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
          <div className="sbx-term-head">
            <span>
              terminal transcript
              {transcript != null && ` · ${transcript.split('\n').length} lines`}
            </span>
            {transcript && transcript.trim() !== '' && (
              <button
                type="button"
                className="sbx-term-toggle"
                onClick={() => setShowRaw((v) => !v)}
                title={showRaw ? 'Show formatted view' : 'Show raw transcript'}
              >
                {showRaw ? 'formatted' : 'raw'}
              </button>
            )}
          </div>
          {transcript == null ? (
            <div className="log-tail-empty">Loading transcript…</div>
          ) : transcript.trim() === '' ? (
            <div className="log-tail-empty">
              No commands recorded yet. Output appears here as the agent runs commands over SSH.
            </div>
          ) : (
            <TerminalLog text={transcript} live={isLive} raw={showRaw} />
          )}
        </>
      )}
    </section>
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
