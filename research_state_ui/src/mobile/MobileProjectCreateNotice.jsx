import { Link } from 'react-router-dom';
import CreateProject from '../pages/CreateProject';
import { useProjectStore, selectHasLocalDataPlaneHttp } from '../store/useProjectStore';

/**
 * MobileProjectCreateNotice — replaces the local-mode CreateProject form on
 * the phone. Local project creation needs a daemon-machine absolute path a
 * phone cannot pick; hosted-control project creation has no local path field,
 * so it can fall through to the regular form.
 */
export default function MobileProjectCreateNotice({ bootstrap = false }) {
  const hasLocalDataPlane = useProjectStore(selectHasLocalDataPlaneHttp);

  if (!hasLocalDataPlane) {
    return <CreateProject bootstrap={bootstrap} />;
  }

  return (
    <div className="page-stage">
      <header className="page-header">
        <h1 className="page-title">{bootstrap ? 'No project yet' : 'New project'}</h1>
      </header>
      <div className="empty-state">
        <p>Create a project from desktop or CLI.</p>
        {!bootstrap && (
          <Link to="/projects" className="btn" style={{ marginTop: 12 }}>← Projects</Link>
        )}
      </div>
    </div>
  );
}
