import { useState, useRef, useEffect } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useProjectStore, projectPath } from '../store/useProjectStore';
import LogicGraphHero from '../components/LogicGraphHero';

// Grow a textarea to fit its content so the summary can run to a few lines
// without an inner scrollbar breaking the minimal underline look.
function autoGrow(el) {
  if (!el) return;
  el.style.height = 'auto';
  el.style.height = `${el.scrollHeight}px`;
}

/**
 * CreateProject — the single new-project flow, used both as bootstrap (no
 * projects yet, rendered bare) and as the in-shell route at /projects/new.
 * A two-step logic-graph hero either way.
 *
 * Props:
 *   bootstrap: bool. When true, drops the "← Projects" cancel link (there's
 *              nowhere to go back to) and tweaks the name placeholder.
 */
export default function CreateProject({ bootstrap = false }) {
  const navigate = useNavigate();
  const createProject = useProjectStore(s => s.createProject);
  const [name, setName] = useState('');
  const [summary, setSummary] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  // Bootstrap is a two-step flow: 0 = name the project, 1 = describe it.
  const [step, setStep] = useState(0);
  const summaryRef = useRef(null);

  // Re-fit the summary box when entering step 1 (its value may be preserved
  // from an earlier visit via the back-to-rename control). Defer one frame so
  // the measurement runs after layout settles, not during the entry transition.
  useEffect(() => {
    if (step !== 1) return;
    const id = requestAnimationFrame(() => autoGrow(summaryRef.current));
    return () => cancelAnimationFrame(id);
  }, [step]);

  function advance(e) {
    e.preventDefault();
    if (!name.trim()) return;
    setError(null);
    setStep(1);
  }

  async function submit(e) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const row = await createProject({
        name: name.trim(),
        summary: summary.trim(),
      });
      navigate(projectPath(row.id));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  // One create flow for every project — first-run or from inside the app.
  // Step 0: name it (the hook). Step 1: describe it with a brief summary.
  // Both steps speak the same minimal language: a forming logic graph behind a
  // single underline field and one "→" to proceed.
  return (
    <div className="boot-hero">
      <div className="boot-hero__field">
        <LogicGraphHero />
      </div>

      {!bootstrap && (
        <Link to="/projects" className="boot-exit">
          <span aria-hidden="true">←</span> Projects
        </Link>
      )}

      {step === 0 ? (
        <form className="boot-create" onSubmit={advance} key="name">
          <div className="boot-create__field">
            <input
              className="boot-create__input"
              value={name}
              onChange={e => setName(e.target.value)}
              placeholder={bootstrap ? 'Name your first project' : 'Name your project'}
              autoFocus
              required
            />
            <button
              type="submit"
              className="boot-create__go"
              aria-label="Continue"
              disabled={!name.trim()}
            >
              →
            </button>
          </div>
        </form>
      ) : (
        <form className="boot-create" onSubmit={submit} key="details">
          <button type="button" className="boot-back" onClick={() => setStep(0)} title="Rename">
            <span aria-hidden="true">←</span> {name}
          </button>
          <div className="boot-create__field boot-create__field--grow">
            <textarea
              ref={summaryRef}
              rows={1}
              className="boot-create__input boot-create__input--summary"
              value={summary}
              onChange={e => { setSummary(e.target.value); autoGrow(e.target); }}
              onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submit(e); }}
              placeholder="Brief summary to set the context."
              autoFocus
            />
            <button
              type="submit"
              className="boot-create__go"
              aria-label="Create project"
              disabled={busy || !name.trim()}
            >
              →
            </button>
          </div>
          {error && <div className="boot-create__error">{error}</div>}
        </form>
      )}
    </div>
  );
}
