import { useState } from 'react';
import ObjId from './ObjId';
import ResourceContentView from './ResourceContentView';
import { formatBytes } from '../utils/format';

/**
 * Generic resource list with one-at-a-time expand-to-view content.
 *
 * Version-aware: if a row has an `association_version_id` that differs from
 * the resource's `current_version_id`, render a small "live file has advanced"
 * pill so the user knows the file changed since this association. Expanding a
 * row always shows the live file — the backend stores version metadata only,
 * not historical content.
 */
export default function ResourceList({ projectId, resources, historical = false }) {
  const [openId, setOpenId] = useState(null);
  return (
    <div className="list card card--flush">
      {resources.map(r => {
        const open = openId === r.id;
        const liveAdvanced = r.association_version_id && r.current_version_id
          && r.association_version_id !== r.current_version_id;
        return (
          <div key={`${r.id}:${r.association_role || ''}:${r.association_attempt_index || 0}`} style={{ borderBottom: '1px solid var(--line-soft)' }}>
            <div className="list-row" onClick={() => setOpenId(open ? null : r.id)} style={{ cursor: 'pointer' }}>
              <div className="list-row-main">
                <div className="res-path">
                  {r.path}
                  {liveAdvanced && (
                    <span className="plan-version-tag" style={{ marginLeft: 8 }} title="The live file has advanced past this version">
                      live file has advanced
                    </span>
                  )}
                </div>
                <div className="list-row-sub">
                  <ObjId id={r.id} />
                  {r.association_role && <> · role: <span className="mono">{r.association_role}</span></>}
                  {r.association_attempt_index != null && historical && <> · attempt {r.association_attempt_index}</>}
                  {r.kind && <> · kind: {r.kind}</>}
                  {r.size_bytes != null && <> · {formatBytes(r.size_bytes)}</>}
                </div>
              </div>
              <div className="list-row-aside">
                {r.missing ? <span className="res-missing">missing</span> : null}
                <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>{open ? '−' : '+'}</span>
              </div>
            </div>
            {open && (
              <div style={{ padding: '0 14px 14px' }}>
                <ResourceContentView
                  projectId={projectId}
                  resourceId={r.id}
                  size={r.size_bytes}
                  path={r.path}
                />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
