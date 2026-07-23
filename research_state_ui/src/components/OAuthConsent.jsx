import { useEffect, useState } from 'react';
import { api, request } from '../api';

export default function OAuthConsent() {
  const [state, setState] = useState({ loading: true, client: null, projects: [], error: '' });
  const [projectId, setProjectId] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let disposed = false;
    const query = window.location.search;
    (async () => {
      try {
        const [client, projectResult] = await Promise.all([
          request(`/oauth/authorize/details${query}`),
          api.listProjects(),
        ]);
        if (!disposed) {
          setState({
            loading: false,
            client,
            projects: projectResult?.projects || [],
            error: '',
          });
        }
      } catch (error) {
        if (!disposed) {
          setState({
            loading: false,
            client: null,
            projects: [],
            error: error.message || 'Could not load this authorization request.',
          });
        }
      }
    })();
    return () => { disposed = true; };
  }, []);

  const decide = async (decision) => {
    if (decision === 'approve' && !projectId) return;
    setBusy(true);
    setState(current => ({ ...current, error: '' }));
    try {
      const params = Object.fromEntries(new URLSearchParams(window.location.search));
      const result = await request('/oauth/authorize', {
        method: 'POST',
        body: { ...params, decision, project_id: decision === 'approve' ? projectId : '' },
      });
      window.location.assign(result.redirect_to);
    } catch (error) {
      setState(current => ({
        ...current,
        error: error.message || 'Could not complete authorization.',
      }));
      setBusy(false);
    }
  };

  if (state.loading) {
    return <ConsentFrame><p className="auth-modal-sub">Loading authorization request…</p></ConsentFrame>;
  }
  if (!state.client) {
    return <ConsentFrame><p className="oauth-consent-error">{state.error}</p></ConsentFrame>;
  }

  return (
    <ConsentFrame>
      <h2 className="auth-modal-title">Connect {state.client.client_name}</h2>
      <p className="auth-modal-sub">
        Choose the one Merv project this client may access. You can revoke the
        resulting project key from Merv at any time.
      </p>
      <label className="auth-field">
        <span>Project</span>
        <select
          className="auth-input oauth-project-select"
          value={projectId}
          onChange={event => setProjectId(event.target.value)}
          disabled={busy}
        >
          <option value="">Select one project…</option>
          {state.projects.map(project => (
            <option key={project.id} value={project.id}>{project.name}</option>
          ))}
        </select>
      </label>
      <p className="oauth-consent-resource">Resource: {state.client.resource}</p>
      {state.error && <p className="oauth-consent-error">{state.error}</p>}
      <div className="oauth-consent-actions">
        <button type="button" className="btn btn--ghost" disabled={busy} onClick={() => decide('deny')}>
          Cancel
        </button>
        <button
          type="button"
          className="btn btn--primary"
          disabled={busy || !projectId}
          onClick={() => decide('approve')}
        >
          {busy ? 'Connecting…' : 'Approve'}
        </button>
      </div>
    </ConsentFrame>
  );
}

function ConsentFrame({ children }) {
  return (
    <div className="auth-gate">
      <div className="auth-modal oauth-consent">{children}</div>
    </div>
  );
}
