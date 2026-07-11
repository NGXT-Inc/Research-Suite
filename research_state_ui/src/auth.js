/**
 * Supabase session plumbing for the hosted UI.
 *
 * Dormant on localhost: initAuth() constructs a supabase-js client only when
 * /api/meta advertises auth.required (hosted control plane), so local dev
 * never loads the library, shows a login, or attaches Authorization headers.
 * The client persists + refreshes the session itself; this module just mirrors
 * the current access token into a synchronous read for api.js.
 */

let client = null;
let token = '';
let email = '';
const listeners = new Set();

function notify() {
  listeners.forEach((fn) => fn());
}

function applySession(session) {
  token = session?.access_token || '';
  email = session?.user?.email || '';
  notify();
}

// Synchronous reads for the fetch wrapper and UI chrome.
export function getAuthToken() {
  return token;
}

export function getAuthEmail() {
  return email;
}

export function onAuthChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// Returns true when hosted auth is active (a client exists after this call).
export async function initAuth(authMeta) {
  if (!authMeta?.required || !authMeta.supabase_url || !authMeta.supabase_anon_key) {
    return false;
  }
  if (client) return true;
  // Dynamic import keeps supabase-js out of the boot path entirely on local.
  const { createClient } = await import('@supabase/supabase-js');
  client = createClient(authMeta.supabase_url, authMeta.supabase_anon_key);
  const { data } = await client.auth.getSession();
  applySession(data?.session || null);
  client.auth.onAuthStateChange((_event, session) => applySession(session));
  return true;
}

export async function signInWithPassword(emailInput, password) {
  const { error } = await client.auth.signInWithPassword({ email: emailInput, password });
  if (error) throw new Error(error.message);
}

export async function signInWithGoogle() {
  const { error } = await client.auth.signInWithOAuth({
    provider: 'google',
    options: { redirectTo: window.location.href },
  });
  if (error) throw new Error(error.message);
}

export async function signUp(emailInput, password) {
  const { error } = await client.auth.signUp({ email: emailInput, password });
  if (error) throw new Error(error.message);
}

// Sends a Supabase recovery email. redirectTo is the origin; if it isn't in the
// project's allow-list Supabase falls back to the Site URL (the shared
// RapidReview reset page), so the same account is reset either way.
export async function resetPassword(emailInput) {
  const { error } = await client.auth.resetPasswordForEmail(emailInput, {
    redirectTo: window.location.origin,
  });
  if (error) throw new Error(error.message);
}

export async function signOut() {
  if (client) await client.auth.signOut();
  applySession(null);
}

// Full session tokens for the CLI device-flow handoff (/auth/sdk page): the
// signed-in browser posts these to the brain, which holds them for the
// polling terminal.
export async function getSessionTokens() {
  if (!client) return null;
  const { data } = await client.auth.getSession();
  const session = data?.session;
  if (!session) return null;
  return {
    access_token: session.access_token,
    refresh_token: session.refresh_token || '',
    expires_in: session.expires_in || 3600,
    email: session.user?.email || '',
  };
}
