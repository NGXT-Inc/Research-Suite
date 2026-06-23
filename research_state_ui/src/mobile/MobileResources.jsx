import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useProjectStore, selectResources, useProjectHref } from '../store/useProjectStore';
import FileTree from '../components/FileTree';
import ResourceContentView from '../components/ResourceContentView';
import ObjId from '../components/ObjId';
import { formatBytes } from '../utils/format';

/**
 * Mobile Resources: the desktop page delegates file selection entirely to
 * the sidebar file tree, which does not exist on this surface — so the tree
 * mounts in-page as a collapsible panel above the content view.
 */
export default function MobileResources() {
  const { resourceId } = useParams();
  const navigate = useNavigate();
  const px = useProjectHref();
  const projectId = useProjectStore(s => s.projectId);
  const resources = useProjectStore(selectResources);
  const [treeOpen, setTreeOpen] = useState(!resourceId);

  // Opening a file collapses the tree; clearing the selection reopens it.
  useEffect(() => { setTreeOpen(!resourceId); }, [resourceId]);

  const selected = resourceId ? resources.find(r => r.id === resourceId) : null;

  return (
    <div className="page-stage">
      <header className="page-header">
        <h1 className="page-title">Repo files</h1>
      </header>

      <div className="mfiles">
        <button
          type="button"
          className="mfiles-head"
          onClick={() => setTreeOpen(v => !v)}
          aria-expanded={treeOpen}
        >
          <span>{selected ? selected.path : `${resources.length} file${resources.length === 1 ? '' : 's'}`}</span>
          <span aria-hidden="true">{treeOpen ? '▾' : '▸'}</span>
        </button>
        {treeOpen && (
          <div className="mfiles-body">
            {resources.length === 0 ? (
              <div className="empty-state empty-state--compact">
                <p>No files registered yet.</p>
              </div>
            ) : (
              <FileTree
                resources={resources}
                selectedId={resourceId || null}
                onSelect={(r) => navigate(px(`/resources/${r.id}`))}
              />
            )}
          </div>
        )}
      </div>

      {selected ? (
        <>
          <div className="mcard" style={{ marginBottom: 14 }}>
            <div className="mcard-meta">
              <span><ObjId id={selected.id} /></span>
              {selected.kind && <span>{selected.kind}</span>}
              {selected.size_bytes != null && <span>{formatBytes(selected.size_bytes)}</span>}
            </div>
          </div>
          <ResourceContentView
            projectId={projectId}
            resourceId={selected.id}
            size={selected.size_bytes}
            path={selected.path}
          />
        </>
      ) : (
        !treeOpen && (
          <div className="empty-state empty-state--compact">
            <p>Pick a file above to preview it.</p>
          </div>
        )
      )}
    </div>
  );
}
