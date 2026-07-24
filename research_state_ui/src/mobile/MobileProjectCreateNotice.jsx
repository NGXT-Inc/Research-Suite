import CreateProject from '../pages/CreateProject';

/**
 * MobileProjectCreateNotice — the phone create-project screen. Projects are
 * created against hosted control with no local path field, so this is a thin
 * pass-through to the shared CreateProject form. (Kept as a distinct component
 * so mobile call sites need not change.)
 */
export default function MobileProjectCreateNotice({ bootstrap = false }) {
  return <CreateProject bootstrap={bootstrap} />;
}
