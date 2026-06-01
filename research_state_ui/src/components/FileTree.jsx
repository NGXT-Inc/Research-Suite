import { useEffect, useMemo, useRef, useState } from 'react';
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
 * page can keep the preview pane wired to a single resource.
 *
 * Search auto-expands every folder that contains a matching descendant so
 * matches are visible without manual fiddling.
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
        node.children.set(part, {
          name: part,
          path: r.path,
          kind: 'file',
          resource: r,
        });
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

function collectFolderPaths(node, out) {
  if (node.kind !== 'dir') return;
  if (node.path) out.add(node.path);
  for (const c of node.sortedChildren || []) collectFolderPaths(c, out);
}

function topLevelFolderPaths(root) {
  const out = new Set();
  for (const c of root.sortedChildren || []) {
    if (c.kind === 'dir') out.add(c.path);
  }
  return out;
}

function matchesQuery(node, q) {
  if (!q) return true;
  if (node.kind === 'file') return node.path.toLowerCase().includes(q);
  for (const c of node.sortedChildren || []) {
    if (matchesQuery(c, q)) return true;
  }
  return false;
}

function TreeNode({ node, selectedId, onSelect, expanded, toggle, query }) {
  // Indent + vertical-line guides come from `.ft-subtree` wrappers around each
  // expanded folder (see CSS), so each row itself only carries a small base
  // padding. No depth-based padding here — that lets us keep nesting visually
  // tight regardless of how deep the folder goes.
  if (node.kind === 'file') {
    if (query && !node.path.toLowerCase().includes(query)) return null;
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

  if (!matchesQuery(node, query)) return null;
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
              query={query}
            />
          ))}
        </div>
      )}
    </>
  );
}

export default function FileTree({ resources, selectedId, onSelect, query = '' }) {
  const tree = useMemo(() => buildTree(resources || []), [resources]);
  // Initialize expansion lazily: the user's clicks must survive the 3s
  // polling refresh, so we only seed from `tree` once. Newly-appearing
  // top-level folders auto-expand on first sight; existing user state stays.
  const [expanded, setExpanded] = useState(() => topLevelFolderPaths(tree));
  const seenTopRef = useRef(new Set(topLevelFolderPaths(tree)));
  const normalized = query.trim().toLowerCase();

  useEffect(() => {
    const tops = topLevelFolderPaths(tree);
    setExpanded(prev => {
      const next = new Set(prev);
      let changed = false;
      for (const p of tops) {
        if (!seenTopRef.current.has(p)) {
          next.add(p);
          seenTopRef.current.add(p);
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [tree]);

  // When a file is selected via URL deep-link, expand its ancestor folders
  // so the highlight is visible without the user manually drilling in.
  useEffect(() => {
    if (!selectedId) return;
    const r = (resources || []).find(x => x.id === selectedId);
    if (!r?.path) return;
    const parts = String(r.path).split('/').filter(Boolean);
    if (parts.length <= 1) return;
    setExpanded(prev => {
      const next = new Set(prev);
      let changed = false;
      for (let i = 0; i < parts.length - 1; i++) {
        const p = parts.slice(0, i + 1).join('/');
        if (!next.has(p)) { next.add(p); changed = true; }
      }
      return changed ? next : prev;
    });
  }, [selectedId, resources]);

  // While searching, force-expand every folder so matches are visible.
  const effectiveExpanded = useMemo(() => {
    if (!normalized) return expanded;
    const all = new Set();
    collectFolderPaths(tree, all);
    return all;
  }, [normalized, expanded, tree]);

  const toggle = (p) => {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(p)) next.delete(p);
      else next.add(p);
      return next;
    });
  };

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
          expanded={effectiveExpanded}
          toggle={toggle}
          query={normalized}
        />
      ))}
    </div>
  );
}
