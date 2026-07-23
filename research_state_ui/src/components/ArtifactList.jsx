import { useState } from 'react';
import ObjId from './ObjId';
import ArtifactContentView from './ArtifactContentView';
import { formatBytes } from '../utils/format';

const basename = (p) => (p || '').split('/').filter(Boolean).pop() || p || '';

/**
 * ArtifactList — flat per-target artifact rows with one-at-a-time
 * expand-to-view content. An artifact id pins exact bytes (resubmission mints
 * a new id), so there is no version drift to warn about.
 */
export default function ArtifactList({ projectId, artifacts, historical = false }) {
  const [openId, setOpenId] = useState(null);
  return (
    <div className="list card card--flush">
      {artifacts.map(a => {
        const open = openId === a.id;
        return (
          <div key={`${a.id}:${a.role || ''}:${a.attempt_index || 0}`} style={{ borderBottom: '1px solid var(--line-soft)' }}>
            <div className="list-row" onClick={() => setOpenId(open ? null : a.id)} style={{ cursor: 'pointer' }}>
              <div className="list-row-main">
                <div className="res-path">{a.title || basename(a.path)}</div>
                <div className="list-row-sub">
                  <ObjId id={a.id} />
                  {a.role && <> · role: <span className="mono">{a.role}</span></>}
                  {a.attempt_index != null && historical && <> · attempt {a.attempt_index}</>}
                  {a.size_bytes != null && <> · {formatBytes(a.size_bytes)}</>}
                </div>
              </div>
              <div className="list-row-aside">
                <span className={`twist${open ? ' open' : ''}`} aria-hidden="true">▸</span>
              </div>
            </div>
            {open && (
              <div style={{ padding: '0 14px 14px' }}>
                <ArtifactContentView
                  projectId={projectId}
                  artifactId={a.id}
                  size={a.size_bytes}
                  path={a.path}
                />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
