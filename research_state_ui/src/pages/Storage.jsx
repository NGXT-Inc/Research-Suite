import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useProjectStore, useProjectHref } from '../store/useProjectStore';
import { useStorageLedger } from '../store/useStorageLedger';
import { api } from '../api';
import ObjId from '../components/ObjId';
import { formatBytes, fmtDuration } from '../utils/format';
import './storage.css';

const KINDS = ['all', 'dataset', 'model', 'other'];

// One small badge describing where the object stands against its 60-day shelf.
function ttlBadge(o) {
  if (o.status === 'expired') return { text: 'expired', tone: 'danger' };
  if (!o.expires_at) return { text: 'pinned', tone: 'pin' };
  const ms = new Date(o.expires_at).getTime() - Date.now();
  if (ms <= 0) return { text: 'expiring', tone: 'danger' };
  return { text: `expires in ${fmtDuration(ms)}`, tone: ms < 7 * 86400000 ? 'soon' : 'ok' };
}

function fmtDate(iso) {
  if (!iso) return '—';
  try { return new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }); }
  catch { return iso; }
}

export default function Storage() {
  const { objectId } = useParams();
  const navigate = useNavigate();
  const px = useProjectHref();
  const projectId = useProjectStore(s => s.projectId);
  const [kind, setKind] = useState('all');
  const [includeExpired, setIncludeExpired] = useState(false);
  const { objects, loading, error, unsupported, reload } = useStorageLedger(projectId, { kind, includeExpired });

  return (
    <div className="page-stage">
      <header className="page-header page-header--lg">
        <div className="page-head-row">
          <div>
            <h1 className="page-title">Long-term storage</h1>
            <p className="page-summary">
              Datasets and trained models preserved off-repo in S3-compatible storage.
              Objects keep a 60-day shelf life that renews on access — pin the ones worth keeping forever.
            </p>
          </div>
          <div className="page-actions">
            <button className="btn btn--ghost" onClick={reload} disabled={loading}>
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
          </div>
        </div>
      </header>

      <div className="cluster" style={{ gap: 8, marginBottom: 16, flexWrap: 'wrap' }}>
        {KINDS.map(k => (
          <button
            key={k}
            type="button"
            className={`btn btn--sm ${kind === k ? 'btn--primary' : 'btn--ghost'}`}
            onClick={() => setKind(k)}
          >
            {k}
          </button>
        ))}
        <label className="cluster" style={{ gap: 6, marginLeft: 'auto', fontSize: 'var(--text-sm)' }}>
          <input type="checkbox" checked={includeExpired} onChange={e => setIncludeExpired(e.target.checked)} />
          Show expired
        </label>
      </div>

      {error && <div className="error-message">{error}</div>}

      {unsupported ? (
        <div className="empty-state">
          <h2>Storage isn’t enabled on this backend yet</h2>
          <p>The long-term storage service is part of an in-progress rollout. Once the backend exposes it, datasets and models saved by the agent will appear here.</p>
        </div>
      ) : loading && objects.length === 0 ? (
        <div className="empty">Loading…</div>
      ) : objects.length === 0 ? (
        <div className="empty-state">
          <h2>Nothing in long-term storage yet</h2>
          <p>Agents save precious datasets and trained models here with the <span className="mono">storage.*</span> tools. Preserved objects show up in this ledger.</p>
        </div>
      ) : (
        <div className="storage-list">
          {objects.map(o => (
            <StorageRow
              key={o.id}
              o={o}
              projectId={projectId}
              expanded={o.id === objectId}
              onToggle={() => navigate(px(o.id === objectId ? '/storage' : `/storage/${o.id}`))}
              onChanged={reload}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function StorageRow({ o, projectId, expanded, onToggle, onChanged }) {
  const ttl = ttlBadge(o);
  return (
    <div className={`storage-row${expanded ? ' is-open' : ''}`}>
      <button type="button" className="storage-row-head" onClick={onToggle} aria-expanded={expanded}>
        <span className="storage-row-twist" aria-hidden="true">{expanded ? '▾' : '▸'}</span>
        <span className="storage-row-name">
          {o.name}{o.version != null && <span className="storage-row-ver">v{o.version}</span>}
        </span>
        <span className={`ft-tag storage-kind storage-kind--${o.kind}`}>{o.kind}</span>
        <span className="storage-row-size">{formatBytes(o.size_bytes)}</span>
        <span className={`storage-ttl storage-ttl--${ttl.tone}`}>{ttl.text}</span>
        {o.status && o.status !== 'available' && <span className="ft-tag">{o.status}</span>}
      </button>
      {expanded && (
        <StorageDetail o={o} projectId={projectId} onChanged={onChanged} />
      )}
    </div>
  );
}

function StorageDetail({ o, projectId, onChanged }) {
  const [busy, setBusy] = useState('');
  const [err, setErr] = useState(null);
  const [link, setLink] = useState(null);

  async function run(action, fn) {
    setBusy(action);
    setErr(null);
    try { return await fn(); }
    catch (e) { setErr(e.message); }
    finally { setBusy(''); }
  }

  const pinned = !o.expires_at && o.status !== 'expired';

  return (
    <div className="storage-detail">
      <div className="storage-detail-grid">
        <Meta label="id"><ObjId id={o.id} /></Meta>
        <Meta label="sha256"><span className="mono">{(o.content_sha256 || '').slice(0, 16)}…</span></Meta>
        <Meta label="type">{o.content_type || '—'}</Meta>
        <Meta label="created">{fmtDate(o.created_at)}</Meta>
        <Meta label="last access">{fmtDate(o.last_accessed_at)}</Meta>
        {o.producing_experiment_id && <Meta label="from experiment"><span className="mono">{o.producing_experiment_id}</span></Meta>}
        {o.producing_run && <Meta label="run"><span className="mono">{o.producing_run}</span></Meta>}
        {o.source_uri && <Meta label="source">{o.source_uri}</Meta>}
      </div>
      {o.notes && <div className="storage-detail-notes">{o.notes}</div>}

      <div className="storage-detail-actions">
        <button
          className="btn btn--sm btn--primary"
          disabled={!!busy || o.status === 'expired'}
          onClick={() => run('download', async () => {
            const r = await api.storageDownloadLink(projectId, o.id);
            setLink(r?.download?.url || null);
            onChanged();
          })}
        >
          {busy === 'download' ? '…' : 'Get download link'}
        </button>
        {pinned ? (
          <button className="btn btn--sm btn--ghost" disabled={!!busy}
            onClick={() => run('unpin', async () => { await api.unpinStorage(projectId, o.id); onChanged(); })}>
            {busy === 'unpin' ? '…' : 'Unpin'}
          </button>
        ) : (
          <button className="btn btn--sm btn--ghost" disabled={!!busy}
            onClick={() => run('pin', async () => { await api.pinStorage(projectId, o.id); onChanged(); })}>
            {busy === 'pin' ? '…' : 'Pin (keep forever)'}
          </button>
        )}
        <button className="btn btn--sm btn--ghost" disabled={!!busy || pinned}
          title={pinned ? 'Pinned objects have no expiry to renew' : 'Reset the 60-day shelf life'}
          onClick={() => run('renew', async () => { await api.renewStorage(projectId, o.id); onChanged(); })}>
          {busy === 'renew' ? '…' : 'Renew 60d'}
        </button>
        <button className="btn btn--sm btn--danger" disabled={!!busy}
          onClick={() => run('delete', async () => {
            if (!window.confirm(`Delete "${o.name}" from storage? The bytes are removed if no other version references them.`)) return;
            await api.deleteStorage(projectId, o.id);
            onChanged();
          })}>
          {busy === 'delete' ? 'Deleting…' : 'Delete'}
        </button>
      </div>

      {link && (
        <div className="storage-detail-link">
          <a className="mono" href={link} target="_blank" rel="noreferrer">{link.slice(0, 80)}…</a>
          <span className="storage-detail-link-note">short-lived presigned URL</span>
        </div>
      )}
      {err && <div className="error-message">{err}</div>}
    </div>
  );
}

function Meta({ label, children }) {
  return (
    <div className="storage-meta">
      <span className="storage-meta-label">{label}</span>
      <span className="storage-meta-value">{children}</span>
    </div>
  );
}
