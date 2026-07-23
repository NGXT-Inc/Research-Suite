import { useCallback, useEffect, useState } from 'react';
import { useProjectStore } from '../store/useProjectStore';
import { api } from '../api';
import { isAuthEnabled } from '../auth';

// Compact absolute stamp for the ledger ("Jul 23 · 6:03 PM"). Null-safe.
function fmtWhen(iso) {
  if (!iso) return null;
  try {
    const d = new Date(iso);
    return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} · ${d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;
  } catch { return iso; }
}

// One key's lifecycle state, derived fresh from its stamps (the same order the
// backend enforces: revoked wins, then expiry).
function keyState(k) {
  if (k.revoked_at) return 'revoked';
  if (k.expires_at && Date.parse(k.expires_at) <= Date.now()) return 'expired';
  return 'active';
}

/**
 * Project settings — MCP keys.
 *
 * Mint, list, and revoke the project-scoped `mk_` keys that connect an MCP
 * client (Claude, Codex, …) to this one project. Every route is owner-only and
 * needs a hosted browser session, so in local/API-key mode the list can't load
 * — we say so plainly rather than surfacing a raw 401.
 */
export default function Settings() {
  const projectId = useProjectStore((s) => s.projectId);
  const hosted = isAuthEnabled();

  const [keys, setKeys] = useState([]);
  const [loading, setLoading] = useState(hosted);
  const [error, setError] = useState('');
  const [minting, setMinting] = useState(false);
  // The mk_ secret is held here for exactly one view. It lives only in this
  // component's state — navigating away (or dismissing) drops it for good.
  const [minted, setMinted] = useState(null);
  const [copied, setCopied] = useState(false);
  const [confirming, setConfirming] = useState('');
  const [revoking, setRevoking] = useState('');

  const load = useCallback(async () => {
    if (!hosted || !projectId) return;
    setLoading(true);
    setError('');
    try {
      const res = await api.listProjectKeys(projectId);
      setKeys(res?.keys || []);
    } catch (err) {
      setError(err?.message || 'Could not load this project’s keys.');
    } finally {
      setLoading(false);
    }
  }, [hosted, projectId]);

  useEffect(() => { load(); }, [load]);

  async function mint() {
    if (minting) return;
    setMinting(true);
    setError('');
    try {
      const res = await api.createProjectKey(projectId);
      setMinted(res); // { key, secret }
      setCopied(false);
      setKeys((prev) => [res.key, ...prev]);
    } catch (err) {
      setError(err?.message || 'Could not mint a key.');
    } finally {
      setMinting(false);
    }
  }

  async function copySecret() {
    if (!minted?.secret) return;
    try {
      await navigator.clipboard.writeText(minted.secret);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopied(false);
    }
  }

  async function revoke(keyId) {
    setRevoking(keyId);
    setError('');
    try {
      const res = await api.revokeProjectKey(projectId, keyId);
      const updated = res?.key;
      setKeys((prev) => prev.map((k) => (k.id === keyId ? updated || { ...k, revoked_at: new Date().toISOString() } : k)));
    } catch (err) {
      setError(err?.message || 'Could not revoke the key.');
    } finally {
      setRevoking('');
      setConfirming('');
    }
  }

  return (
    <div className="page-stage">
      <div className="page-header">
        <div className="page-head-row">
          <div>
            <h1 className="page-title">MCP keys</h1>
            <p className="page-summary page-summary--lead">
              A key connects an MCP client to this project. It is bearer-equivalent
              to full project access — anyone holding it can act as this project.
            </p>
          </div>
          {hosted && (
            <div className="page-actions">
              <button type="button" className="btn btn--primary" onClick={mint} disabled={minting}>
                {minting ? 'Minting…' : 'Mint MCP key'}
              </button>
            </div>
          )}
        </div>
      </div>

      {!hosted && (
        <div className="empty-state empty-state--compact">
          <p>Project keys are managed from your hosted RapidReview account. Sign in on the hosted app to mint or revoke keys.</p>
        </div>
      )}

      {minted && (
        <div className="mcpk-reveal" role="region" aria-label="New MCP key">
          <div className="mcpk-reveal-head">
            <span className="mcpk-reveal-title">Copy your new key now</span>
            <button type="button" className="btn btn--ghost btn--sm" onClick={() => setMinted(null)}>
              Done
            </button>
          </div>
          <div className="mcpk-secret">
            <code className="mcpk-secret-value mono">{minted.secret}</code>
            <button type="button" className="btn btn--sm" onClick={copySecret}>
              {copied ? 'Copied' : 'Copy'}
            </button>
          </div>
          <p className="mcpk-reveal-warn">
            This is the only time the secret is shown. It grants full access to this
            project — store it like a password. Once you leave this page it cannot be
            recovered; mint a new key if you lose it.
          </p>
        </div>
      )}

      {error && <div className="error-message" style={{ marginBottom: 14 }}>{error}</div>}

      {hosted && (
        <div className="mcpk">
          {loading ? (
            <div className="empty-state empty-state--compact"><p>Loading keys…</p></div>
          ) : keys.length === 0 ? (
            <div className="empty-state empty-state--compact"><p>No keys yet. Mint one to connect an MCP client.</p></div>
          ) : (
            <div className="mcpk-table" role="table" aria-label="Project MCP keys">
              <div className="mcpk-row mcpk-row--head" role="row">
                <span className="th" role="columnheader">Key</span>
                <span className="th" role="columnheader">Created</span>
                <span className="th" role="columnheader">Expires</span>
                <span className="th" role="columnheader">State</span>
                <span className="th th--r" role="columnheader" aria-label="Actions" />
              </div>
              {keys.map((k) => {
                const state = keyState(k);
                return (
                  <div className="mcpk-row" role="row" key={k.id}>
                    <span className="mcpk-id mono" role="cell" title={k.id}>{k.id}</span>
                    <span className="mcpk-when" role="cell">{fmtWhen(k.created_at) || '—'}</span>
                    <span className="mcpk-when" role="cell">{fmtWhen(k.expires_at) || 'Never'}</span>
                    <span className="mcpk-cell" role="cell">
                      <span className={`mcpk-state mcpk-state--${state}`}>{state}</span>
                    </span>
                    <span className="mcpk-cell mcpk-cell--action" role="cell">
                      {state === 'active' && (
                        confirming === k.id ? (
                          <span className="mcpk-confirm">
                            <button type="button" className="btn btn--danger btn--sm" onClick={() => revoke(k.id)} disabled={revoking === k.id}>
                              {revoking === k.id ? 'Revoking…' : 'Revoke'}
                            </button>
                            <button type="button" className="btn btn--ghost btn--sm" onClick={() => setConfirming('')} disabled={revoking === k.id}>
                              Cancel
                            </button>
                          </span>
                        ) : (
                          <button type="button" className="btn btn--ghost btn--sm" onClick={() => setConfirming(k.id)}>
                            Revoke
                          </button>
                        )
                      )}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
