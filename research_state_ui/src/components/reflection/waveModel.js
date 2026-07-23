/**
 * waveModel — shared reflection-wave role-resolution policy + belief-state
 * logic, consumed by both the desktop ProjectReflectionPanel and the mobile
 * MobileReflectionScreen so the two surfaces never drift. Pure helpers, no JSX.
 */

export const TERMINAL_WAVE = new Set(['published', 'abandoned']);

// Roles with their own dedicated section above; everything else a wave
// submits falls through to the quiet "change_spec / other docs" disclosures.
const PRIMARY_ROLES = new Set(['graph', 'project_graph', 'reflection_lens_doc', 'reflection_doc']);

// Nice labels for known secondary doc roles; anything else is humanized so a
// new backend role never goes unrendered as the reflection model evolves.
const DOC_ROLE_META = {
  change_spec: { label: 'Change spec — belief-state update', order: 0 },
  proposals: { label: "What's next — proposals", order: 1 },
};

function humanizeRole(role) {
  return role.replace(/[_-]+/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

// Resolve each roster lens to the reflection artifact submitted for the wave's
// current attempt. Artifacts carry an explicit `lens_id` (submission requires
// it for role reflection_lens_doc), so the match is direct — no filename
// heuristics. An artifact id pins exact bytes, so no version pinning either.
export function reflectionsByLens(wave) {
  const byLens = {};
  for (const r of wave?.current_attempt_artifacts || []) {
    if (r.role === 'reflection_lens_doc' && r.lens_id) byLens[r.lens_id] = r;
  }
  const map = {};
  for (const lens of wave?.reflection_coverage?.lenses || []) {
    const res = byLens[lens.lens_id] || null;
    map[lens.lens_id] = {
      covered: Boolean(lens.covered),
      artifactId: res?.id || null,
      path: res?.path || lens.path || null,
    };
  }
  return map;
}

// The secondary docs (everything that isn't graph / lens doc / reflection_doc):
// today just the change_spec, but derived from the artifacts so new roles render
// automatically. First artifact per role wins.
export function secondaryDocs(artifacts) {
  const seen = new Set();
  const docs = [];
  for (const r of artifacts) {
    const role = r.role;
    if (!role || PRIMARY_ROLES.has(role) || seen.has(role)) continue;
    seen.add(role);
    const meta = DOC_ROLE_META[role] || {};
    docs.push({ role, res: r, label: meta.label || humanizeRole(role), order: meta.order ?? 100 });
  }
  return docs.sort((a, b) => a.order - b.order || a.role.localeCompare(b.role));
}

export function resolveReflectionDoc(artifacts) {
  return artifacts.find(r => r.role === 'reflection_doc') || null;
}
