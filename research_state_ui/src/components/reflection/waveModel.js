/**
 * waveModel — shared reflection-wave role-resolution policy + belief-state
 * logic, consumed by both the desktop ProjectReflectionPanel and the mobile
 * MobileReflectionScreen so the two surfaces never drift. Pure helpers, no JSX.
 */

export const TERMINAL_WAVE = new Set(['published', 'abandoned']);

// Roles with their own dedicated section above; everything else a wave
// associates falls through to the quiet "change_spec / other docs" disclosures.
// Two renames happened: the prose doc synthesis_doc -> reflection_doc, and the
// per-lens doc reflection -> reflection_lens_doc. Both are excluded here (and
// resolved with a fallback below) so old and new waves render either way.
export const REFLECTION_DOC_ROLES = ['reflection_doc', 'synthesis_doc'];
export const LENS_DOC_ROLES = ['reflection_lens_doc', 'reflection'];
const PRIMARY_ROLES = new Set(['graph', ...LENS_DOC_ROLES, ...REFLECTION_DOC_ROLES]);

// Nice labels for known secondary doc roles; anything else is humanized so a
// new backend role never goes unrendered as the reflection model evolves.
const DOC_ROLE_META = {
  change_spec: { label: 'Change spec — belief-state update', order: 0 },
  proposals: { label: "What's next — proposals", order: 1 },
};

function humanizeRole(role) {
  return role.replace(/[_-]+/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

// Resolve each roster lens to its reflection resource for the wave's current
// attempt. reflection_coverage already matched <lens_id>.md → path + pinned
// version server-side; here we just look up the resource id by that path.
export function reflectionsByLens(wave) {
  const byPath = {};
  for (const r of wave?.current_attempt_resources || []) {
    if (LENS_DOC_ROLES.includes(r.association_role) && r.path) byPath[r.path] = r;
  }
  const map = {};
  for (const lens of wave?.reflection_coverage?.lenses || []) {
    const res = lens.path ? byPath[lens.path] : null;
    map[lens.lens_id] = {
      covered: Boolean(lens.covered),
      resourceId: res?.id || null,
      versionId: lens.version_id || res?.association_version_id || null,
      path: lens.path || res?.path || null,
    };
  }
  return map;
}

// The secondary docs (everything that isn't graph / reflection / reflection_doc):
// today just the change_spec, but derived from the resources so new roles render
// automatically. First association per role wins.
export function secondaryDocs(resources) {
  const seen = new Set();
  const docs = [];
  for (const r of resources) {
    const role = r.association_role;
    if (!role || PRIMARY_ROLES.has(role) || seen.has(role)) continue;
    seen.add(role);
    const meta = DOC_ROLE_META[role] || {};
    docs.push({ role, res: r, label: meta.label || humanizeRole(role), order: meta.order ?? 100 });
  }
  return docs.sort((a, b) => a.order - b.order || a.role.localeCompare(b.role));
}

// Prefer the new reflection_doc role, fall back to legacy synthesis_doc so a
// wave published before the rename still renders. Mirrors app.py's resolution.
export function resolveReflectionDoc(resources) {
  return REFLECTION_DOC_ROLES
    .map(role => resources.find(r => r.association_role === role))
    .find(Boolean) || null;
}

// Pin every rendered doc to the exact version THIS wave associated. The living
// files (reflection_doc, change_spec, proposals) are one resource shared across
// waves, so the server's default "latest" can resolve to another wave's bytes —
// pinning keeps each wave faithful, the current one included.
export const docVersion = res => res.association_version_id || null;
