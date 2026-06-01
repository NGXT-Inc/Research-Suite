import { useState } from 'react';
import ObjId from './ObjId';
import ResourceContentView from './ResourceContentView';

function bytes(n) {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

/**
 * Generic resource list with one-at-a-time expand-to-view content.
 *
 * Version-aware: if a row has an `association_version_id` that differs from
 * the resource's `current_version_id`, render a small "live file has advanced"
 * pill so the user knows this row points at a past snapshot. When the row is
 * expanded, content is loaded from the pinned version (so an attempt-3 row
 * shows attempt-3's content, not the live file).
 */
export default function ResourceList({ projectId, resources, historical = false }) {
  const [openId, setOpenId] = useState(null);
  return (
    <div className="list card card--flush">
      {resources.map(r => {
        const open = openId === r.id;
        const pinnedVersionId = r.association_version_id || null;
        // Two cases for a version-drift pill:
        //   (1) historical rows that pin a specific past version
        //   (2) any row whose pinned version != current version (live file advanced)
        const liveAdvanced = pinnedVersionId && r.current_version_id && pinnedVersionId !== r.current_version_id;
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
                  {r.size_bytes != null && <> · {bytes(r.size_bytes)}</>}
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
                  versionId={pinnedVersionId}
                />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
