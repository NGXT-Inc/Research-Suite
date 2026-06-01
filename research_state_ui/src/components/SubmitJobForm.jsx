import { useState } from 'react';
import { api } from '../api';

/**
 * Minimal job-submission form: command + cwd + expected_outputs.
 *
 * The backend enforces:
 *   - command executable ∈ {python, python3, pytest, uv}
 *   - no shell control syntax (; && || | ` $( > <)
 *   - all paths must be repo-relative, no '..'
 *   - experiment must be in status 'ready_to_run' or 'running'
 */
export default function SubmitJobForm({ projectId, experimentId, onCancel, onSubmitted }) {
  const [command, setCommand] = useState('python3 scripts/run.py');
  const [cwd, setCwd] = useState('.');
  const [outputs, setOutputs] = useState(''); // newline-separated
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function submit(e) {
    e.preventDefault();
    if (!command.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const expected_outputs = outputs.split('\n').map(s => s.trim()).filter(Boolean);
      await api.submitJob(projectId, {
        experiment_id: experimentId,
        command: command.trim(),
        cwd: cwd.trim() || '.',
        expected_outputs,
      });
      if (onSubmitted) await onSubmitted();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form-card" onSubmit={submit} style={{ marginBottom: 12 }}>
      <div className="form-row">
        <label className="label">Command</label>
        <input
          className="input mono"
          value={command}
          onChange={e => setCommand(e.target.value)}
          placeholder="python3 train.py --epochs 10"
          required
        />
      </div>
      <div className="form-row">
        <label className="label">Working dir (repo-relative)</label>
        <input
          className="input mono"
          value={cwd}
          onChange={e => setCwd(e.target.value)}
          placeholder="."
        />
      </div>
      <div className="form-row">
        <label className="label">Expected outputs (one repo-relative path per line)</label>
        <textarea
          className="textarea mono"
          value={outputs}
          onChange={e => setOutputs(e.target.value)}
          placeholder={'experiments/e001/results.json\nexperiments/e001/metrics.csv'}
          rows={3}
        />
      </div>
      {error && <div className="error-message">{error}</div>}
      <div className="form-actions">
        <button type="button" className="btn btn--ghost btn--sm" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn--primary btn--sm" disabled={busy || !command.trim()}>
          {busy ? 'Submitting…' : 'Submit job'}
        </button>
      </div>
      <p className="faint" style={{ fontSize: 10.5, marginTop: 8 }}>
        Allowed executables: python · python3 · pytest · uv. No shell operators.
      </p>
    </form>
  );
}
