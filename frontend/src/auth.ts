/**
 * auth.ts — login for the web app.
 *
 * The password goes to the backend once (POST /api/login) and comes back as a
 * random session token for this device, kept in localStorage. No secret is
 * baked into the JS bundle any more. Each device has its own token, so a lost
 * phone can be signed out alone (backend: python sessions.py revoke <id>).
 */

const KEY = 'enzo.session';

export function getToken(): string {
  try { return localStorage.getItem(KEY) || ''; } catch { return ''; }
}

function setToken(token: string) {
  try { localStorage.setItem(KEY, token); } catch { /* private mode: lasts this tab only */ }
}

export function clearToken() {
  try { localStorage.removeItem(KEY); } catch { /* ignore */ }
}

/** A readable name for this device, shown in the backend's session list. */
export function deviceName(): string {
  const ua = navigator.userAgent;
  const os = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android'
    : /Windows/.test(ua) ? 'Windows PC' : /Mac/.test(ua) ? 'Mac' : 'Device';
  const browser = /Edg\//.test(ua) ? 'Edge' : /CriOS|Chrome\//.test(ua) ? 'Chrome'
    : /FxiOS|Firefox\//.test(ua) ? 'Firefox' : /Safari\//.test(ua) ? 'Safari' : 'browser';
  const standalone = window.matchMedia?.('(display-mode: standalone)').matches ? ' app' : '';
  return `${os} ${browser}${standalone}`;
}

/** Is a password set on the backend? (If not, only this PC can connect, without login.) */
export async function passwordRequired(): Promise<boolean> {
  try {
    const r = await fetch('/api/auth/status');
    if (!r.ok) return true;
    return Boolean((await r.json()).password_set);
  } catch {
    return true;
  }
}

export async function login(password: string): Promise<void> {
  let r: Response;
  try {
    r = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password, device: deviceName() }),
    });
  } catch {
    throw new Error('Cannot reach Enzo — is the backend running?');
  }
  if (!r.ok) {
    let detail = r.status === 429 ? 'Too many wrong tries — wait 15 minutes.' : 'Login failed.';
    try { detail = (await r.json()).detail || detail; } catch { /* keep default */ }
    throw new Error(detail);
  }
  setToken((await r.json()).token);
}

export async function logout(): Promise<void> {
  const token = getToken();
  clearToken();
  if (!token) return;
  try {
    await fetch('/api/logout', { method: 'POST', headers: { Authorization: `Bearer ${token}` } });
  } catch { /* offline: the token is gone locally anyway */ }
}

/** Same address the page came from: wss:// behind HTTPS (phone), ws:// on the PC. */
export function wsUrl(): string {
  const override = import.meta.env.VITE_WS_URL;
  if (override) return override;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}/ws`;
}
