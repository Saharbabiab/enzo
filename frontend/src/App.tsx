import { useCallback, useEffect, useRef, useState } from 'react';
import './App.css';
import { useEnzoAssistant, AssistantState, ChatMessage } from './useEnzoAssistant';
import { getToken, login, logout, passwordRequired } from './auth';
import { isNative, onAssist, detectPopup, closePopup, setPopupExpanded, runDeviceAction, checkForAppUpdate } from './native';
import Panel from './Panel';
import Card from './Cards';

// ── Small pieces ───────────────────────────────────────────

const STATE_LABEL: Record<AssistantState, string> = {
  idle: 'Standby', connecting: 'Connecting', connected: 'Online', listening: 'Listening', processing: 'Thinking', speaking: 'Speaking',
};

function hint(state: AssistantState, native: boolean): string {
  switch (state) {
    case 'idle':       return native ? 'Tap the ring to talk' : 'Say "Wake up Enzo" or tap the ring';
    case 'connecting': return 'Connecting';
    case 'connected':  return 'Ready';
    case 'listening':  return "I'm listening";
    case 'processing': return 'Working on it';
    case 'speaking':   return native ? 'Talk over me or tap to interrupt' : 'Tap the ring to interrupt';
  }
}

const using = (tool: string) => `Using ${tool.replace(/_/g, ' ')}`;

const svg = (d: string, w = 1.8) => (
  <svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={w} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d={d} />
  </svg>
);
const Icon = {
  back: svg('m15 6-6 6 6 6', 2),
  memory: svg('M12 3a6 6 0 0 0-6 6c0 2.2 1.2 3.6 2 4.5V17h8v-3.5c.8-.9 2-2.3 2-4.5a6 6 0 0 0-6-6ZM9 21h6'),
  settings: svg('M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6ZM12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9 7 7M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1'),
  chat: svg('M4 5h16v11H9l-5 4z'),
  mic: svg('M12 3a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3ZM5 11a7 7 0 0 0 14 0M12 18v3'),
  micOff: svg('M9 9v2a3 3 0 0 0 5 2.2M15 10V6a3 3 0 0 0-5.7-1.3M5 11a7 7 0 0 0 11.5 5.4M19 11a7 7 0 0 1-.6 2.8M12 18v3M3 3l18 18'),
  speaker: svg('M4 9.5v5h3.5L12 18.5v-13L7.5 9.5H4zM15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11'),
  speakerOff: svg('M4 9.5v5h3.5L12 18.5v-13L7.5 9.5H4zM16 9.5l5 5M21 9.5l-5 5'),
  send: svg('M5 12h14M13 6l6 6-6 6', 2),
  close: svg('M6 6l12 12M18 6 6 18', 2.4),
  play: <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z" /></svg>,
};

/** Enzo's "Nearly zero" ring: an open blue circle with a brown oopsie dot in the gap. It turns clockwise while the user
 *  talks (faster with his mic level), anticlockwise and slower while Enzo speaks, breathes while thinking and is still
 *  when idle. The speed eases through 0 when the turn changes, so it never snaps (motion spec, board 13). */
const LISTEN_MS = 1600, SPEAK_RATE = -1600 / 2400;
const reduceMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;

export function Ring({ state, size, onClick, label, muted }: { state: AssistantState; size: number; onClick?: () => void; label?: string; muted?: boolean }) {
  const mode = state === 'listening' ? 'listen' : state === 'speaking' ? 'speak' : state === 'processing' || state === 'connecting' ? 'busy' : 'calm';
  const g = useRef<SVGGElement>(null);
  const spin = useRef<Animation | null>(null);
  useEffect(() => {
    if (!g.current?.animate) return;
    const anim = g.current.animate([{ transform: 'rotate(0deg)' }, { transform: 'rotate(360deg)' }], { duration: LISTEN_MS, iterations: Infinity });
    anim.currentTime = LISTEN_MS * 1e5;   // room to run backwards: below 0 an infinite animation would finish instead of looping
    anim.playbackRate = 0;
    spin.current = anim;
    return () => anim.cancel();
  }, []);
  useEffect(() => {
    const anim = spin.current;
    if (!anim) return;
    const still = reduceMotion();
    let raf = 0, last = performance.now();
    const step = (now: number) => {
      const vol = mode === 'listen' ? Number(document.documentElement.style.getPropertyValue('--vol')) || 0 : 0;
      const target = still ? 0 : mode === 'listen' ? 1 + 0.5 * vol : mode === 'speak' ? SPEAK_RATE : 0;
      const k = Math.min(1, (now - last) / 100);   // ~300 ms to settle, passing smoothly through 0 on a direction change
      last = now;
      const rate = anim.playbackRate + (target - anim.playbackRate) * k;
      anim.playbackRate = target === 0 && Math.abs(rate) < 0.002 ? 0 : rate;
      if (target !== 0 || anim.playbackRate !== 0) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [mode]);
  const cls = `ring ring--${mode}${muted ? ' ring--muted' : ''}`;
  const inner = (
    <svg viewBox="0 0 100 100" aria-hidden="true"><g className="ring__g" ref={g}>
      <circle cx="50" cy="50" r="30" fill="none" stroke="#5f8ce0" strokeWidth="9" strokeLinecap="round" pathLength="100" strokeDasharray="84 16" transform="rotate(-40 50 50)" />
      <circle cx="62.6" cy="22.8" r="5.5" fill="#b08a68" />
    </g></svg>
  );
  const style = { width: size, height: size };
  if (!onClick) return <span className={cls} style={style} aria-hidden="true">{inner}</span>;
  return <button className={cls} style={style} onClick={onClick} aria-label={label}>{inner}</button>;
}

/** Keeps something on screen for `ms` after `open` turns false, so it can animate out. [show it, it is closing]. */
function useExit(open: boolean, ms: number): [boolean, boolean] {
  const [kept, setKept] = useState(open);
  useEffect(() => {
    if (open) { setKept(true); return; }
    const t = setTimeout(() => setKept(false), ms);
    return () => clearTimeout(t);
  }, [open, ms]);
  return [open || kept, !open && kept];
}

function Header({ title, onBack, children }: { title: string; onBack: () => void; children?: React.ReactNode }) {
  return (
    <header className="topbar">
      <button className="icon44" onClick={onBack} aria-label="Back to home">{Icon.back}</button>
      <span className="topbar__title">{title}</span>
      {children}
    </header>
  );
}

// ── Login ──────────────────────────────────────────────────

function LoginScreen({ onDone }: { onDone: () => void }) {
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!password || busy) return;
    setBusy(true);
    setError('');
    try {
      await login(password);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed.');
      setPassword('');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="screen login">
      <header className="login__brand"><Ring state="idle" size={32} />Enzo</header>
      <main className="login__main enter">
        <h1 className="login__title">Hey, it's Enzo.<br />Let's make sure it's you.</h1>
        <form className="login__form" onSubmit={submit}>
          <label className="field">
            <span className="field__label">Password</span>
            <input className="field__input" type="password" autoComplete="current-password" value={password}
              onChange={(e) => setPassword(e.target.value)} autoFocus />
          </label>
          <button className="btn btn--primary" type="submit" disabled={!password || busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
          {error && <p className="login__error" role="alert">{error}</p>}
        </form>
      </main>
      <p className="login__foot">Only for the user. Each device gets its own key, and you can sign it out from Settings.</p>
    </div>
  );
}

type Gate = 'checking' | 'login' | 'in' | 'popup';

export default function App() {
  const [gate, setGate] = useState<Gate>('checking');

  useEffect(() => {
    detectPopup().then((native) => {
      const popup = native || (import.meta.env.DEV && location.search.includes('popup'));   // dev: ?popup previews the popup layout
      if (getToken()) { setGate(popup ? 'popup' : 'in'); return; }
      if (popup) { setGate('login'); return; }   // sign in once in the full app; the popup shares it
      // No password on the backend yet → this PC may connect without logging in.
      passwordRequired().then((required) => setGate(required ? 'login' : 'in'));
    });
  }, []);

  if (gate === 'checking') return <div className="screen" />;
  if (gate === 'popup') return <PopupAssistant onAuthFailed={() => setGate('login')} />;
  if (gate === 'login') return <LoginScreen onDone={() => setGate('in')} />;
  return (
    <Assistant
      onAuthFailed={() => setGate('login')}
      onSignOut={async () => { await logout(); setGate('login'); }}
    />
  );
}

// ── Popup: Enzo in a small window over another app (the phone's assistant shortcut) ──

const POPUP_SILENCE_MS = 9000;   // closes by itself after this long with nothing said

function PopupAssistant({ onAuthFailed }: { onAuthFailed: () => void }) {
  const { state, messages, interimText, connect, pendingConfirm, answerConfirm, interrupt, activity } = useEnzoAssistant(onAuthFailed);
  const started = useRef(false);
  const goodbye = useRef(false);
  const [chat, setChat] = useState(false);   // a tap on the island (not the X) shows the conversation
  const chatEnd = useRef<HTMLDivElement>(null);

  const tap = () => {
    if (state === 'idle') connect();
    else if (state === 'speaking') interrupt('tap');
  };
  const tapRef = useRef(tap);
  tapRef.current = tap;

  useEffect(() => {
    const press = () => {   // the shortcut again while open acts like a tap on the pill
      if (!started.current) { started.current = true; connect(); } else tapRef.current();
    };
    const off = onAssist(press);
    if (!started.current) { started.current = true; connect(); }
    const bye = () => { goodbye.current = true; };   // end_conversation: close once the goodbye is spoken
    window.addEventListener('enzo-close-popup', bye);
    return () => { off(); window.removeEventListener('enzo-close-popup', bye); };
  }, []);

  // Close after the goodbye, or after a quiet stretch with nothing said and nothing waiting.
  useEffect(() => {
    if (chat || pendingConfirm || interimText || state === 'processing' || state === 'speaking' || state === 'connecting') return;
    const t = setTimeout(closePopup, goodbye.current ? 400 : POPUP_SILENCE_MS);
    return () => clearTimeout(t);
  }, [chat, state, interimText, pendingConfirm, messages.length]);

  // A pill around the camera; answers are only spoken (The user, 2026-11-23). It opens only for an approval, so he sees what he says yes to.
  const askedAt = messages.map((m) => m.role).lastIndexOf('user');
  const asked = askedAt >= 0 ? messages[askedAt].text : '';
  const reply = askedAt >= 0 ? messages.slice(askedAt + 1).reverse().find((m) => m.role === 'sharp')?.text : '';
  const line = interimText || reply || '';
  const open = !!pendingConfirm || chat;
  const recent = messages.filter((m) => m.role !== 'system').slice(-12);
  useEffect(() => { chatEnd.current?.scrollIntoView({ block: 'end' }); }, [chat, recent.length, interimText]);
  const [shown, closing] = useExit(open, 140);   // the text fades out first, then the island shrinks back into the hole
  const confirmRef = useRef(pendingConfirm);
  if (pendingConfirm) confirmRef.current = pendingConfirm;
  const mode = state === 'speaking' ? 'speak' : state === 'processing' || state === 'connecting' ? 'think' : 'listen';
  useEffect(() => { setPopupExpanded(shown); }, [shown]);

  return (
    <div className={`popup${shown ? ` popup--open popup--${mode}` : ''}`}>
      <div className="popup__pill" onClick={() => setChat((c) => !c)}>
        <button className="popup__state" onClick={tap} aria-label={state === 'speaking' ? 'Interrupt Enzo' : `Enzo is ${STATE_LABEL[state].toLowerCase()}`}>
          <Ring state={state} size={26} />
        </button>
        <span className="popup__hole" aria-hidden="true" />
        <button className="popup__close" onClick={(e) => { e.stopPropagation(); closePopup(); }} aria-label="Close Enzo"><span>{Icon.close}</span></button>
      </div>
      {shown && (<div className={`popup__panel${closing ? ' popup__panel--closing' : ''}`}>
        <div className="popup__text">
          {chat ? recent.map((m, i) => (
            <div key={i} className={`popup__msg popup__msg--${m.role}`} dir="auto">{m.text}</div>
          )) : asked && !interimText && <div className="popup__asked" dir="auto">{asked}</div>}
          {chat ? interimText && <div className="popup__msg popup__msg--user popup__text--live" dir="auto">{interimText}</div>
            : <div className={interimText ? 'popup__text--live' : ''} dir="auto">{line}</div>}
          {state === 'processing' && activity && <div className="popup__asked">{using(activity)}</div>}
          <div ref={chatEnd} />
        </div>
        {(pendingConfirm || closing) && confirmRef.current && (
          <div className="popup__confirm" role="alertdialog" aria-label="Enzo needs your approval">
            <button className="btn btn--soft" onClick={() => answerConfirm('no')} disabled={closing}>No</button>
            <button className="btn btn--primary" onClick={() => answerConfirm('yes')} disabled={closing}>Yes</button>
          </div>
        )}
      </div>)}
    </div>
  );
}

// ── Main app ───────────────────────────────────────────────

type Screen = 'home' | 'chat' | 'memory' | 'settings';
type HomeData = { weather: { place: string; temp: number; condition: string } | null; events: { start: string; title: string }[] | null;
  unread: number | null; briefing_ready: boolean };
type Status = { brain: string; claude_spent: number; claude_budget: number;
  google?: { connected: boolean; problem: string; days_left: number | null; can_renew: boolean } };

/** GET /api/panel/<path> for the signed-in device; null when signed out or the server can't say. */
function usePanel<T>(path: string, when: unknown): T | null {
  const [data, setData] = useState<T | null>(null);
  useEffect(() => {
    if (!getToken()) return;
    let live = true;
    fetch(`/api/panel/${path}`, { headers: { Authorization: `Bearer ${getToken()}` } })
      .then((r) => (r.ok ? r.json() : null)).then((d) => { if (live) setData(d); }).catch(() => {});
    return () => { live = false; };
  }, [path, when]);
  return data;
}

const SILENT_MP3 = 'data:audio/mp3;base64,SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjU5LjE2LjEwMAAAAAAAAAAAAAAA//OEAAAAAAAAAAAAAAAAAAAAAAAASW5mbwAAAA8AAAAEAAABIwDY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY';

function Assistant({ onAuthFailed, onSignOut }: { onAuthFailed: () => void; onSignOut: () => void }) {
  const a = useEnzoAssistant(onAuthFailed);
  const { state, interimText, isMuted, pendingConfirm, activity } = a;
  const [screen, setScreen] = useState<Screen>('home');
  const [dir, setDir] = useState<'none' | 'fwd' | 'back'>('none');   // which way the next screen slides in
  const [sheet, setSheet] = useState(false);
  const [hasInteracted, setHasInteracted] = useState(false);
  const [phoneReminders, setPhoneReminders] = useState<{ waiting: number; next: number } | null>(null);

  useEffect(() => {
    const on = (e: Event) => setPhoneReminders((e as CustomEvent).detail);
    window.addEventListener('enzo-reminders', on);
    return () => window.removeEventListener('enzo-reminders', on);
  }, []);

  // A new approval request opens the sheet; closing it keeps a "Review" row until it is answered.
  useEffect(() => { setSheet(!!pendingConfirm); }, [pendingConfirm?.id, pendingConfirm?.text]);

  const unlockAudio = () => {
    if (hasInteracted) return;
    setHasInteracted(true);
    new Audio(SILENT_MP3).play().catch(() => {});   // unlocks audio output for this page
  };

  const tapOrb = () => {
    unlockAudio();
    if (state === 'idle') a.connect();
    else if (state === 'speaking') a.interrupt('tap');   // tap the ring to stop Enzo talking
  };
  // The phone's assistant shortcut acts like a tap on the ring: wake Enzo, or stop him talking so you can speak.
  const orbRef = useRef(tapOrb);
  orbRef.current = tapOrb;
  useEffect(() => onAssist(() => orbRef.current()), []);

  // Each screen is a history step, so the phone's Back key (MainActivity) and the in-app back arrow return Home.
  const go = (s: Screen) => { if (s !== 'home') history.pushState({ screen: s }, ''); setDir(s === 'home' ? 'back' : 'fwd'); setScreen(s); };
  const home = () => { setDir('back'); if (history.state?.screen) history.back(); else setScreen('home'); };
  useEffect(() => {
    const on = () => { setDir('back'); setScreen('home'); };
    window.addEventListener('popstate', on);
    return () => window.removeEventListener('popstate', on);
  }, []);

  const say = useCallback((text: string) => { unlockAudio(); a.sendTextMsg(text); if (screen !== 'chat') go('chat'); }, [a.sendTextMsg, hasInteracted, screen]);

  const status = isMuted ? 'Microphone muted' : interimText ? interimText : activity ? using(activity) : hint(state, isNative);
  const [sheetShown, sheetClosing] = useExit(sheet && !!pendingConfirm, 260);
  const [splashShown, splashLeaving] = useExit(!hasInteracted, 320);
  const confirmRef = useRef(pendingConfirm);
  if (pendingConfirm) confirmRef.current = pendingConfirm;
  const orbLabel = state === 'idle' ? 'Talk to Enzo' : state === 'speaking' ? 'Interrupt Enzo' : `Enzo is ${STATE_LABEL[state].toLowerCase()}`;

  return (
    <div className="screen">
      {splashShown && (
        <button className={`splash${splashLeaving ? ' splash--out' : ''}`} onClick={unlockAudio} aria-label="Tap to begin">
          <Ring state="idle" size={96} />
          <span className="splash__title">Enzo</span>
          <span className="splash__hint">Tap to begin</span>
        </button>
      )}

      <div className={`view view--${dir}`} key={screen}>
      {screen === 'home' && (
        <Home a={a} status={status} orbLabel={orbLabel} tapOrb={tapOrb} go={go} say={say}
          review={() => setSheet(true)} reminders={phoneReminders} />
      )}
      {screen === 'chat' && <Chat a={a} status={status} orbLabel={orbLabel} tapOrb={tapOrb} back={home} say={say} review={() => setSheet(true)} />}
      {screen === 'memory' && <Panel onClose={home} onAuthFailed={onAuthFailed} />}
      {screen === 'settings' && <Settings a={a} back={home} onSignOut={onSignOut} />}
      </div>

      {sheetShown && confirmRef.current && (
        <div className={`sheet-wrap${sheetClosing ? ' sheet-wrap--closing' : ''}`} onClick={() => setSheet(false)}>
          <section className="sheet" role="dialog" aria-modal="true" aria-label="Enzo needs your approval" onClick={(e) => e.stopPropagation()}>
            <div className="sheet__head">
              <h1 className="sheet__title" dir="auto">{confirmRef.current.text}</h1>
              <p className="sheet__detail">Enzo waits for your answer before doing it.</p>
            </div>
            <div className="sheet__buttons">
              <button className="btn btn--soft" onClick={() => a.answerConfirm('no')} disabled={sheetClosing}>Not now</button>
              <button className="btn btn--primary" onClick={() => a.answerConfirm('yes')} disabled={sheetClosing}>Yes, do it</button>
            </div>
            <button className="sheet__always" onClick={() => a.answerConfirm('always')} disabled={sheetClosing}>Always allow this</button>
          </section>
        </div>
      )}
    </div>
  );
}

type A = ReturnType<typeof useEnzoAssistant>;

// ── Home ───────────────────────────────────────────────────

function greeting(now: Date): string {
  const h = now.getHours();
  return h < 5 ? 'Good evening, sir.' : h < 12 ? 'Morning, sir.' : h < 18 ? 'Good afternoon, sir.' : 'Good evening, sir.';
}

function daySentence(events: { start: string; title: string }[], now: Date): string {
  if (events.length === 0) return 'Nothing on the calendar today.';
  const hm = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
  const next = events.filter((e) => e.start && e.start > hm);
  if (next.length === 0) return events.length === 1 ? 'One thing on the calendar today, nothing left.' : `${events.length} things on the calendar today, nothing left.`;
  return `Next up: ${next[0].title} at ${next[0].start}.` + (next.length > 1 ? ` Then ${next.length - 1} more.` : '');
}

function Home({ a, status, orbLabel, tapOrb, go, say, review, reminders }: {
  a: A; status: string; orbLabel: string; tapOrb: () => void; go: (s: Screen) => void; say: (t: string) => void;
  review: () => void; reminders: { waiting: number; next: number } | null;
}) {
  const home = usePanel<HomeData>('home', 0);   // fetched each time Home opens
  const now = new Date();
  const w = home?.weather;
  return (
    <div className="home">
      <div className="home__glow" aria-hidden="true" />
      <header className="home__top">
        <span className="home__date">
          {now.toLocaleDateString('en-GB', { weekday: 'long' })}{w ? ` · ${Math.round(w.temp)}° ${w.place}` : ''}
        </span>
        <span className="home__icons">
          {getToken() && <button className="icon44" onClick={() => go('memory')} aria-label="Memory">{Icon.memory}</button>}
          <button className="icon44" onClick={() => go('settings')} aria-label="Settings">{Icon.settings}</button>
        </span>
      </header>

      <main className="home__main">
        <div className="enter home__hello">
          <h1 className="home__title" dir="auto">{greeting(now)}{home?.events ? ` ${daySentence(home.events, now)}` : ''}</h1>
          {home?.briefing_ready && (
            <button className="pill" onClick={() => say('Give me my morning briefing')}>{Icon.play}Play my briefing</button>
          )}
        </div>

        <div className="home__tiles">
          {(w || home?.unread != null) && (
            <div className="tiles">
              {w && (
                <button className="tile tile--weather" onClick={() => say("What's the weather?")} aria-label={`Weather in ${w.place}: ${Math.round(w.temp)} degrees${w.condition ? `, ${w.condition}` : ''}`}>
                  <span className="tile__label">{w.condition || w.place}</span>
                  <span className="tile__big">{Math.round(w.temp)}°</span>
                  <span className="tile__sub">{w.place}</span>
                </button>
              )}
              {home?.unread != null && (
                <button className="tile" onClick={() => say('Any important mail?')} aria-label={`Mail: ${home.unread} unread`}>
                  <span className="tile__label">Mail</span>
                  <span className="tile__big">{home.unread}</span>
                  <span className="tile__sub">unread in Primary</span>
                </button>
              )}
            </div>
          )}

          {a.pendingConfirm && (
            <button className="row" onClick={review}>
              <span className="row__text" dir="auto">{a.pendingConfirm.text}</span>
              <span className="row__action">Review</span>
            </button>
          )}

          {reminders && reminders.waiting > 0 && (
            <span className="home__note">
              {reminders.waiting} reminder{reminders.waiting === 1 ? '' : 's'} waiting on the phone, next at{' '}
              {new Date(reminders.next).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}.
            </span>
          )}
        </div>
      </main>

      <footer className="home__foot">
        <div className="status" key={a.interimText ? 'live' : status} dir="auto" aria-live="polite">{status}</div>
        <div className="dock">
          <button className="round" onClick={() => go('chat')} aria-label="Chat">{Icon.chat}</button>
          <Ring state={a.state} size={80} onClick={tapOrb} label={orbLabel} muted={a.isMuted} />
          <button className={`round${a.isMuted ? ' round--off' : ''}`} onClick={a.toggleMute}
            aria-label={a.isMuted ? 'Unmute microphone' : 'Mute microphone'} aria-pressed={a.isMuted}>
            {a.isMuted ? Icon.micOff : Icon.mic}
          </button>
        </div>
      </footer>
    </div>
  );
}

// ── Chat ───────────────────────────────────────────────────

const CHIPS = ["What's next today?", 'Any important mail?', "What's the weather?"];

function Message({ m, say, onRate, still }: { m: ChatMessage; say: (t: string) => void; onRate?: (r: 'good' | 'bad') => void; still?: boolean }) {
  const [rated, setRated] = useState<'good' | 'bad' | null>(null);
  const rate = (r: 'good' | 'bad') => { if (!rated && onRate) { setRated(r); onRate(r); } };
  const st = still ? ' msg--still' : '';   // messages already there when the chat opens don't animate in again
  if (m.role === 'user') return <div className={`msg msg--user${st}`} dir="auto">{m.text}</div>;
  if (m.role === 'system') return <div className={`msg msg--system${st}`} dir="auto">{m.text}</div>;
  // A new reply fades in word by word (under ~1 s however long it is); its cards and the rating follow it.
  const parts = still ? [] : m.text.split(/(\s+)/);
  const step = Math.min(30, 900 / Math.max(1, parts.length / 2));
  return (
    <div className={`msg msg--sharp${st}`}>
      <Ring state="idle" size={26} />
      <div className="msg__body" style={{ '--reveal': `${Math.round(step * parts.length / 2)}ms` } as React.CSSProperties}>
        <p className="msg__text" dir="auto">{still ? m.text : parts.map((w, i) =>
          i % 2 ? w : <span key={i} className="w" style={{ animationDelay: `${Math.round(step * i / 2)}ms` }}>{w}</span>)}</p>
        {m.cards?.map((c, i) => <Card key={`${c.kind}-${i}`} card={c} say={say} />)}
        {onRate && (
          <div className="msg__rate" aria-label="Rate this answer">
            <button className={`rate${rated === 'good' ? ' rate--on' : ''}`} onClick={() => rate('good')} disabled={!!rated} aria-label="Good answer">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M7 11v9H4v-9h3zm0 0 4-7a2 2 0 0 1 2 2v4h5a2 2 0 0 1 2 2.3l-1 5.7a2 2 0 0 1-2 1.7H7" /></svg>
            </button>
            <button className={`rate${rated === 'bad' ? ' rate--on' : ''}`} onClick={() => rate('bad')} disabled={!!rated} aria-label="Wrong answer">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M17 13V4h3v9h-3zm0 0-4 7a2 2 0 0 1-2-2v-4H6a2 2 0 0 1-2-2.3l1-5.7A2 2 0 0 1 7 4h10" /></svg>
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function Chat({ a, status, orbLabel, tapOrb, back, say, review }: {
  a: A; status: string; orbLabel: string; tapOrb: () => void; back: () => void; say: (t: string) => void; review: () => void;
}) {
  const [text, setText] = useState('');
  const endRef = useRef<HTMLDivElement>(null);
  const before = useRef(a.messages.length);
  const scrolled = useRef(false);
  useEffect(() => {   // jump to the end when the chat opens, then follow new messages smoothly
    const behavior = scrolled.current && !reduceMotion() ? 'smooth' : 'auto';
    const list = endRef.current?.parentElement;
    const last = [...(list?.querySelectorAll<HTMLElement>('.msg') ?? [])].pop();
    // a reply with many cards is taller than the screen: show where it starts, not its last card
    if (scrolled.current && list && last?.classList.contains('msg--sharp') && last.offsetHeight > list.clientHeight - 40)
      list.scrollTo({ top: last.offsetTop - list.offsetTop - 8, behavior });   // offsetTop: its resting place, not mid slide-in
    else endRef.current?.scrollIntoView({ behavior });
    scrolled.current = true;
  }, [a.messages, a.pendingConfirm]);
  useEffect(() => {   // the keyboard shrinks the screen: keep the newest message in view above it
    const keep = () => endRef.current?.scrollIntoView({ behavior: 'auto' });
    const vv = window.visualViewport;
    vv?.addEventListener('resize', keep);
    return () => vv?.removeEventListener('resize', keep);
  }, []);
  const send = (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!text.trim()) return;
    say(text.trim());
    setText('');
  };
  const lastEnzo = a.messages.length - 1;
  return (
    <div className="chat">
      <Header title="Enzo" onBack={back}>
        <span className="topbar__state">{STATE_LABEL[a.state]}</span>
        <button className="lang" onClick={a.toggleSpeechLang} aria-label={`Speech language: ${a.speechLang === 'he-IL' ? 'Hebrew' : 'English'}. Tap to switch.`}>
          {a.speechLang === 'he-IL' ? 'עברית' : 'EN'}
        </button>
        <button className={`icon44${a.isMuted ? ' icon44--off' : ''}`} onClick={a.toggleMute}
          aria-label={a.isMuted ? 'Unmute microphone' : 'Mute microphone'} aria-pressed={a.isMuted}>
          {a.isMuted ? Icon.micOff : Icon.mic}
        </button>
        <button className={`icon44${a.isEnzoMuted ? ' icon44--off' : ''}`} onClick={a.toggleEnzoMute}
          aria-label={a.isEnzoMuted ? "Turn Enzo's voice on" : "Mute Enzo's voice"} aria-pressed={a.isEnzoMuted}>
          {a.isEnzoMuted ? Icon.speakerOff : Icon.speaker}
        </button>
      </Header>

      <main className="chat__list" aria-live="polite">
        {a.messages.length === 0 && <div className="chat__empty">At your service, sir.<span>Speak, or type below.</span></div>}
        {a.messages.map((m, i) => (
          <Message key={m.id} m={m} say={say} still={i < before.current}
            onRate={m.role === 'sharp' && i === lastEnzo && a.state !== 'processing' ? a.sendFeedback : undefined} />
        ))}
        {a.pendingConfirm && (
          <button className="row row--inline" onClick={review}>
            <span className="row__text" dir="auto">{a.pendingConfirm.text}</span>
            <span className="row__action">Review</span>
          </button>
        )}
        <div ref={endRef} />
      </main>

      <footer className="chat__foot">
        <div className="status status--left" key={a.interimText ? 'live' : status} dir="auto" aria-live="polite">{status}</div>
        {a.state !== 'processing' && !text && (
          <div className="chips">{CHIPS.map((c) => <button key={c} className="chip" onClick={() => say(c)}>{c}</button>)}</div>
        )}
        <form className="composer" onSubmit={send}>
          <label className="composer__field">
            <span className="sr">Message Enzo</span>
            <input className="composer__input" placeholder="Ask anything" dir="auto" value={text} onChange={(e) => setText(e.target.value)} />
          </label>
          {text.trim()
            ? <button type="submit" className="composer__send" aria-label="Send">{Icon.send}</button>
            : <Ring state={a.state} size={44} onClick={tapOrb} label={orbLabel} muted={a.isMuted} />}
        </form>
      </footer>
    </div>
  );
}

// ── Settings: only what really works ───────────────────────

function Switch({ on, label, sub, onToggle }: { on: boolean; label: string; sub: string; onToggle: () => void }) {
  return (
    <button className="setting" role="switch" aria-checked={on} onClick={onToggle}>
      <span className="setting__text"><span>{label}</span><span className="setting__sub">{sub}</span></span>
      <span className={`switch${on ? ' switch--on' : ''}`} aria-hidden="true"><span /></span>
    </button>
  );
}

/** Google (calendar + mail): its state, and Reconnect, which opens Google's login in the browser (Google refuses
 *  logins inside an app's web view); Google then sends the browser back to the server, which saves the login. */
function GoogleLogin({ g }: { g: NonNullable<Status['google']> }) {
  const [msg, setMsg] = useState('');
  const soon = g.days_left != null && g.days_left <= 2;
  const sub = msg || (!g.connected ? 'Not connected: calendar and mail are off'
    : soon ? `Login ends in ${g.days_left} days` : 'Calendar and mail are connected');
  const reconnect = async () => {
    setMsg('Opening Google…');
    try {
      const r = await fetch('/api/panel/google/connect', { method: 'POST', headers: { Authorization: `Bearer ${getToken()}` } });
      const d = await r.json();
      if (!r.ok) { setMsg(d.detail || 'The server could not start the login.'); return; }
      if (isNative) await runDeviceAction({ action: 'open_url', url: d.url }, () => {});
      else if (!window.open(d.url, '_blank', 'noopener')) window.location.assign(d.url);
      setMsg('Finish the login in the browser, then come back.');
    } catch {
      setMsg('The server did not answer.');
    }
  };
  return (
    <section className="group" aria-label="Google">
      <button className="setting" onClick={g.can_renew ? reconnect : undefined} disabled={!g.can_renew}>
        <span className="setting__text"><span>Google</span><span className="setting__sub">{sub}</span></span>
        {g.can_renew && <span className="setting__value">{g.connected && !soon ? 'Reconnect' : 'Sign in'}</span>}
      </button>
    </section>
  );
}

/** Update the Enzo app from the server (the start-up offer stays quiet for a day after "Cancel"). Phone app only. */
function AppUpdate() {
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);
  if (!isNative) return null;
  const run = async () => {
    setBusy(true);
    setMsg('Checking…');
    setMsg(await checkForAppUpdate(true));
    setBusy(false);
  };
  return (
    <section className="group" aria-label="App">
      <button className="setting" onClick={run} disabled={busy}>
        <span className="setting__text"><span>Enzo app</span><span className="setting__sub">{msg || 'Check for a new version and install it'}</span></span>
        <span className="setting__value">Update</span>
      </button>
    </section>
  );
}

function Settings({ a, back, onSignOut }: { a: A; back: () => void; onSignOut: () => void }) {
  const st = usePanel<Status>('status', 0);
  const active = a.state !== 'idle';
  const pct = st && st.claude_budget > 0 ? Math.min(100, (st.claude_spent / st.claude_budget) * 100) : 0;
  return (
    <div className="page">
      <Header title="Settings" onBack={back} />
      <main className="page__body">
        {st && (
          <section className="group group--pad" aria-label="Brain">
            <h2 className="group__title">Who answers first</h2>
            <p className="group__line">{st.brain === 'claude' ? 'Claude, with the local model as backup' : 'The local model, Claude when it needs help'}</p>
            {st.claude_budget > 0 && (
              <div className="budget">
                <div className="budget__row"><span>Claude today</span><span className="muted">${st.claude_spent.toFixed(2)} of ${st.claude_budget.toFixed(2)}</span></div>
                <div className="budget__bar" role="progressbar" aria-label="Claude budget used" aria-valuemin={0}
                  aria-valuemax={st.claude_budget} aria-valuenow={st.claude_spent}><span style={{ width: `${pct}%` }} /></div>
              </div>
            )}
          </section>
        )}

        {st?.google && <GoogleLogin g={st.google} />}

        <section className="group" aria-label="Voice">
          <Switch on={!a.isMuted} label="Microphone" sub={a.isMuted ? 'Closed: Enzo does not hear you' : 'Enzo hears you while connected'} onToggle={a.toggleMute} />
          <Switch on={!a.isEnzoMuted} label="Enzo's voice" sub={a.isEnzoMuted ? 'Replies are text only' : 'Replies are spoken'} onToggle={a.toggleEnzoMute} />
          <button className="setting" onClick={a.toggleSpeechLang}>
            <span className="setting__text"><span>Speech language</span><span className="setting__sub">The microphone hears one language at a time</span></span>
            <span className="setting__value">{a.speechLang === 'he-IL' ? 'Hebrew' : 'English'}</span>
          </button>
        </section>

        {active && (
          <section className="group" aria-label="Connection">
            <button className="setting" onClick={() => { a.startSetup(); back(); }}>
              <span className="setting__text"><span>Set up</span><span className="setting__sub">Tell Enzo about you and your people</span></span>
            </button>
            <button className="setting" onClick={a.disconnect}>
              <span className="setting__text"><span>Disconnect</span><span className="setting__sub">Enzo stops listening until you wake him</span></span>
            </button>
          </section>
        )}

        <AppUpdate />

        <div className="page__grow" />
        {getToken() && <button className="btn btn--soft" onClick={onSignOut}>Sign out of this device</button>}
      </main>
    </div>
  );
}
