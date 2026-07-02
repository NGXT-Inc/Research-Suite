import { useEffect, useLayoutEffect, useMemo } from 'react';
import { useProjectStore } from '../store/useProjectStore';
import {
  useExpandedFolders, togglePath, expandPaths, autoExpandNewTops,
  selectionHandled, markSelectionHandled,
} from '../store/useTreeExpansion';
import FileIcon from './FileIcon';

/**
 * FileTree — VSCode-style nested folder/file explorer for resources.
 *
 * Builds a tree from each resource's `path`. Folders come first, then files,
 * alphabetically within each level. The tree is purely structural — we don't
 * read the filesystem; we render exactly the paths that exist in the
 * registered resource set.
 *
 * Selection is controlled by the parent (`selectedId` + `onSelect`) so the
 * page can keep the preview pane wired to a single resource. Expansion lives
 * in the useTreeExpansion module store, keyed by project, so it survives
 * remounts (drawer toggles, transient empty payloads) and the 3s poll.
 */

function buildTree(resources) {
  const root = { name: '', path: '', kind: 'dir', children: new Map() };
  for (const r of resources) {
    if (!r?.path) continue;
    const parts = String(r.path).split('/').filter(Boolean);
    if (parts.length === 0) continue;
    let node = root;
    for (let i = 0; i < parts.length; i++) {
      const part = parts[i];
      const isLast = i === parts.length - 1;
      if (isLast) {
        // On a file/dir name collision the dir wins: another resource already
        // nests beneath this path, and dropping its subtree would be worse.
        if (node.children.get(part)?.kind !== 'dir') {
          node.children.set(part, {
            name: part,
            path: r.path,
            kind: 'file',
            resource: r,
          });
        }
      } else {
        const segPath = parts.slice(0, i + 1).join('/');
        if (!node.children.has(part) || node.children.get(part).kind !== 'dir') {
          node.children.set(part, {
            name: part,
            path: segPath,
            kind: 'dir',
            children: new Map(),
          });
        }
        node = node.children.get(part);
      }
    }
  }
  return sortTree(root);
}

function sortTree(node) {
  if (node.kind !== 'dir') return node;
  const sorted = Array.from(node.children.values())
    .map(sortTree)
    .sort((a, b) => {
      if (a.kind !== b.kind) return a.kind === 'dir' ? -1 : 1;
      return a.name.localeCompare(b.name, undefined, { numeric: true });
    });
  return { ...node, sortedChildren: sorted };
}

function topLevelFolderPaths(root) {
  const out = new Set();
  for (const c of root.sortedChildren || []) {
    if (c.kind === 'dir') out.add(c.path);
  }
  return out;
}

function TreeNode({ node, selectedId, onSelect, expanded, toggle }) {
  // Indent + vertical-line guides come from `.ft-subtree` wrappers around each
  // expanded folder (see CSS), so each row itself only carries a small base
  // padding. No depth-based padding here — that lets us keep nesting visually
  // tight regardless of how deep the folder goes.
  if (node.kind === 'file') {
    const selected = selectedId && node.resource?.id === selectedId;
    return (
      <button
        type="button"
        className={`ft-row ft-row--file${selected ? ' is-selected' : ''}`}
        onClick={() => onSelect(node.resource)}
        title={node.path}
      >
        <span className="ft-twist" aria-hidden="true" />
        <FileIcon name={node.name} />
        <span className="ft-name">{node.name}</span>
        {node.resource?.missing ? <span className="ft-tag ft-tag--missing">missing</span> : null}
      </button>
    );
  }

  const isOpen = expanded.has(node.path);

  return (
    <>
      <button
        type="button"
        className="ft-row ft-row--folder"
        onClick={() => toggle(node.path)}
        aria-expanded={isOpen}
      >
        <span className="ft-twist" aria-hidden="true">{isOpen ? '▾' : '▸'}</span>
        <span className="ft-name ft-name--folder">{node.name}</span>
      </button>
      {isOpen && (
        <div className="ft-subtree">
          {node.sortedChildren.map(child => (
            <TreeNode
              key={child.path || child.name}
              node={child}
              selectedId={selectedId}
              onSelect={onSelect}
              expanded={expanded}
              toggle={toggle}
            />
          ))}
        </div>
      )}
    </>
  );
}

export default function FileTree({ resources, selectedId, onSelect }) {
  const projectId = useProjectStore(s => s.projectId);
  const tree = useMemo(() => buildTree(resources || []), [resources]);
  const expanded = useExpandedFolders(projectId);

  // Newly appearing top-level folders auto-expand on first sight; layout
  // effect so the very first mount paints them open (no collapsed flash).
  useLayoutEffect(() => {
    autoExpandNewTops(projectId, topLevelFolderPaths(tree));
  }, [projectId, tree]);

  // When a file is selected, expand its ancestors so the highlight is
  // visible — once per selection, tracked in the store (not a ref, which
  // would replay on remount). `resources` stays a dep only so a deep link
  // can resolve once the payload lands; the handled mark stops the re-runs
  // the 3s poll triggers (each poll is a new `resources` identity) from
  // reopening folders the user has since collapsed.
  useEffect(() => {
    if (!selectedId || selectionHandled(projectId, selectedId)) return;
    const r = (resources || []).find(x => x.id === selectedId);
    if (!r?.path) return; // selection precedes payload — retry when resources land
    markSelectionHandled(projectId, selectedId);
    const parts = String(r.path).split('/').filter(Boolean);
    if (parts.length <= 1) return;
    expandPaths(projectId, parts.slice(0, -1).map((_, i) => parts.slice(0, i + 1).join('/')));
  }, [selectedId, resources, projectId]);

  const toggle = (p) => togglePath(projectId, p);

  const children = tree.sortedChildren || [];
  if (children.length === 0) {
    return <div className="empty ft-empty">No files registered.</div>;
  }

  return (
    <div className="ft" role="tree">
      {children.map(child => (
        <TreeNode
          key={child.path || child.name}
          node={child}
          selectedId={selectedId}
          onSelect={onSelect}
          expanded={expanded}
          toggle={toggle}
        />
      ))}
    </div>
  );
}
