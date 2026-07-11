import { useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { api, request } from '../api';
import {
  getAuthToken,
  getSessionTokens,
  initAuth,
  onAuthChange,
  resetPassword,
  signInWithGoogle,
  signInWithPassword,
  signUp,
  signOut,
} from '../auth';

/**
 * AuthGate — mounts above the app. Fetches /api/meta once; when the backend
 * advertises auth.required (hosted control plane) it initializes the Supabase
 * session and holds the app behind a sign-in screen until a session exists.
 * Local mode: meta says required:false, the gate renders children untouched
 * and supabase-js is never loaded.
 */
export default function AuthGate({ children }) {
  const [state, setState] = useState({ checked: false, required: false, authed: false });

  useEffect(() => {
    let disposed = false;
    let unsubscribe = null;
    (async () => {
      // Meta is auth-exempt; a dead backend falls through to the app's own
      // boot-error surface rather than a misleading login wall.
      const meta = await api.getMeta().catch(() => null);
      const active = await initAuth(meta?.auth).catch(() => false);
      if (disposed) return;
      if (!active) {
        setState({ checked: true, required: false, authed: false });
        return;
      }
      const sync = () =>
        setState({ checked: true, required: true, authed: Boolean(getAuthToken()) });
      unsubscribe = onAuthChange(sync);
      sync();
    })();
    // A mid-session 401 (revoked account, rotated secret) clears the stale
    // session so the login screen returns instead of a silent data freeze.
    const onUnauthorized = () => {
      if (getAuthToken()) signOut();
    };
    window.addEventListener('rp:unauthorized', onUnauthorized);
    return () => {
      disposed = true;
      if (unsubscribe) unsubscribe();
      window.removeEventListener('rp:unauthorized', onUnauthorized);
    };
  }, []);

  const location = useLocation();
  if (!state.checked) return null;
  if (state.required && !state.authed) return <SignIn />;
  // CLI device-flow handoff: once signed in, /auth/sdk posts this browser's
  // session to the brain for the polling terminal — instead of the app,
  // which may still be booting (this route must work with zero projects).
  if (state.required && location.pathname === '/auth/sdk') return <SdkHandoff />;
  return children;
}

function SdkHandoff() {
  const [status, setStatus] = useState('working');
  useEffect(() => {
    const sessionId = new URLSearchParams(window.location.search).get('session');
    if (!sessionId) {
      setStatus('missing');
      return;
    }
    (async () => {
      try {
        const tokens = await getSessionTokens();
        if (!tokens) throw new Error('no session');
        await request('/api/sdk/auth/session/complete', {
          method: 'POST',
          body: { session_id: sessionId, ...tokens },
        });
        setStatus('done');
      } catch {
        setStatus('failed');
      }
    })();
  }, []);
  const message = {
    working: 'Completing sign-in…',
    done: 'Signed in — return to your terminal. You can close this tab.',
    missing: 'Missing login session. Rerun merv-client login and use the fresh link.',
    failed: 'Could not complete the sign-in. Rerun merv-client login and try again.',
  }[status];
  return (
    <div className="auth-gate">
      <div className="auth-modal auth-modal--center">
        <div className="auth-brand">Merv</div>
        <p className="auth-modal-sub">{message}</p>
      </div>
    </div>
  );
}

// Sign-in gate — the RapidReview account modal shared with the maps product:
// Google OAuth, email/password with show-hide, plus sign-up, forgot-password,
// and check-email modes. Rendered in Merv's tokens so it tracks light/dark.
function SignIn() {
  const [mode, setMode] = useState('signin'); // signin | signup | forgot | check
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [checkReason, setCheckReason] = useState('signup'); // signup | forgot
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const go = (next) => {
    setMode(next);
    setError('');
  };

  const submit = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      if (mode === 'signup') {
        await signUp(email.trim(), password);
        setCheckReason('signup');
        go('check');
      } else {
        await signInWithPassword(email.trim(), password);
      }
    } catch (err) {
      setError(err.message || 'Sign-in failed');
    } finally {
      setBusy(false);
    }
  };

  const forgot = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      await resetPassword(email.trim());
      setCheckReason('forgot');
      go('check');
    } catch (err) {
      setError(err.message || 'Could not send the reset link');
    } finally {
      setBusy(false);
    }
  };

  const google = async () => {
    setError('');
    try {
      await signInWithGoogle();
    } catch (err) {
      setError(err.message || 'Sign-in failed');
    }
  };

  if (mode === 'check') {
    return (
      <div className="auth-gate">
        <div className="auth-modal auth-modal--center">
          <div className="auth-check-icon">
            <MailIcon />
          </div>
          <h2 className="auth-modal-title">Check your email</h2>
          <p className="auth-modal-sub">
            {checkReason === 'signup'
              ? `We sent a verification link to ${email}. Click it to activate your account.`
              : `We sent a password reset link to ${email}. Click it to set a new password.`}
          </p>
          <button type="button" className="auth-link" onClick={() => go('signin')}>
            Back to sign in
          </button>
        </div>
      </div>
    );
  }

  if (mode === 'forgot') {
    return (
      <div className="auth-gate">
        <form className="auth-modal" onSubmit={forgot}>
          <div className="auth-brand">Merv</div>
          <h2 className="auth-modal-title">Reset password</h2>
          <p className="auth-modal-sub">
            Enter your email and we&apos;ll send you a link to reset your password.
          </p>
          <label className="auth-field">
            <span>Email</span>
            <input
              className="auth-input"
              type="email"
              autoComplete="email"
              inputMode="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </label>
          {error && <p className="auth-error">{error}</p>}
          <button className="auth-submit" type="submit" disabled={busy}>
            {busy ? 'Please wait…' : 'Send reset link'}
          </button>
          <button type="button" className="auth-link auth-link--back" onClick={() => go('signin')}>
            <ArrowLeftIcon />
            Back to sign in
          </button>
        </form>
      </div>
    );
  }

  return (
    <div className="auth-gate">
      <form className="auth-modal" onSubmit={submit}>
        <div className="auth-brand">Merv</div>
        <h2 className="auth-modal-title">{mode === 'signup' ? 'Create account' : 'Sign in'}</h2>
        <p className="auth-modal-sub">Use your RapidReview account.</p>

        <button type="button" className="auth-google-btn" onClick={google}>
          <GoogleIcon />
          <span>Continue with Google</span>
        </button>

        <div className="auth-divider">
          <span>or</span>
        </div>

        <label className="auth-field">
          <span>Email</span>
          <input
            className="auth-input"
            type="email"
            autoComplete="email"
            inputMode="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </label>

        <label className="auth-field">
          <span>Password</span>
          <div className="auth-password-wrap">
            <input
              className="auth-input"
              type={showPassword ? 'text' : 'password'}
              autoComplete={mode === 'signup' ? 'new-password' : 'current-password'}
              minLength={6}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
            <button
              type="button"
              className="auth-password-toggle"
              aria-label={showPassword ? 'Hide password' : 'Show password'}
              onClick={() => setShowPassword((v) => !v)}
              tabIndex={-1}
            >
              {showPassword ? <EyeOffIcon /> : <EyeIcon />}
            </button>
          </div>
          {mode === 'signin' && (
            <button type="button" className="auth-forgot" onClick={() => go('forgot')}>
              Forgot password?
            </button>
          )}
        </label>

        {error && <p className="auth-error">{error}</p>}

        <button className="auth-submit" type="submit" disabled={busy}>
          {busy ? 'Please wait…' : mode === 'signup' ? 'Sign up' : 'Sign in'}
        </button>

        <p className="auth-switch">
          {mode === 'signup' ? 'Already have an account?' : "Don't have an account?"}{' '}
          <button
            type="button"
            className="auth-link"
            onClick={() => go(mode === 'signup' ? 'signin' : 'signup')}
          >
            {mode === 'signup' ? 'Sign in' : 'Sign up'}
          </button>
        </p>

        <p className="auth-terms">
          By continuing, you agree to our{' '}
          <a href="https://rapidreview.io/terms" target="_blank" rel="noopener noreferrer">
            Terms of Service
          </a>{' '}
          and{' '}
          <a href="https://rapidreview.io/policy" target="_blank" rel="noopener noreferrer">
            Privacy Policy
          </a>
          .
        </p>
      </form>
    </div>
  );
}

function GoogleIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" fill="#4285F4" />
      <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853" />
      <path d="M5.84 14.09a7.18 7.18 0 0 1 0-4.17V7.07H2.18A11.97 11.97 0 0 0 0 12c0 1.94.46 3.77 1.28 5.4l3.56-2.77.01-.54z" fill="#FBBC05" />
      <path d="M12 4.75c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 1.09 14.97 0 12 0 7.7 0 3.99 2.47 2.18 6.07l3.66 2.84c.87-2.6 3.3-4.16 6.16-4.16z" fill="#EA4335" />
    </svg>
  );
}

function EyeIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

function EyeOffIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c6.5 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68" />
      <path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3.5 7 10 7a9.74 9.74 0 0 0 5.39-1.61" />
      <path d="M2 2l20 20" />
      <path d="M9.88 9.88a3 3 0 1 0 4.24 4.24" />
    </svg>
  );
}

function MailIcon() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="2" y="4" width="20" height="16" rx="2" />
      <path d="m22 7-10 5L2 7" />
    </svg>
  );
}

function ArrowLeftIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M19 12H5" />
      <path d="m12 19-7-7 7-7" />
    </svg>
  );
}
