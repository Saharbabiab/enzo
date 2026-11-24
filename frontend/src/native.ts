/**
 * native.ts — what the Android app (Capacitor) adds on top of the website.
 *
 * - NativeSpeechRecognition: Android's built-in WebView has no Web Speech API, so this class
 *   looks like the browser's SpeechRecognition (lang / onresult / onend / start / stop) but talks to the
 *   phone's own recognizer. useEnzoAssistant.ts uses it when the browser has none.
 * - runDeviceAction: things Enzo asks the phone to do (open the dialer for a contact or number).
 *
 * In a normal browser none of this is used (isNative is false).
 */

import { Capacitor, registerPlugin } from '@capacitor/core';
import { SpeechRecognition as NativeSR } from '@capacitor-community/speech-recognition';

export const isNative: boolean = Capacitor.isNativePlatform();

function makeResult(text: string, isFinal: boolean) {
  const alt = { transcript: text, confidence: 1 };
  return Object.assign([alt], { isFinal, length: 1 });
}

export class NativeSpeechRecognition {
  lang = 'en-US';
  continuous = true;
  interimResults = true;
  maxAlternatives = 1;
  onresult: ((e: any) => void) | null = null;
  onend: (() => void) | null = null;
  onerror: ((e: any) => void) | null = null;

  private active = false;
  private last = '';
  private handles: { remove: () => Promise<void> }[] = [];

  async start(): Promise<void> {
    if (this.active) throw new Error('InvalidStateError: already started');
    this.active = true;
    this.last = '';
    try {
      let perm = await NativeSR.checkPermissions();
      if (perm.speechRecognition !== 'granted') perm = await NativeSR.requestPermissions();
      if (perm.speechRecognition !== 'granted') return this.fail('not-allowed');
      if (!(await NativeSR.available()).available) return this.fail('service-not-allowed');
      if (!this.active) return;   // stopped while we were checking

      this.handles.push(
        await NativeSR.addListener('partialResults', (d: { matches?: string[] }) => {
          const text = (d.matches && d.matches[0]) || '';
          if (!text) return;
          this.last = text;
          this.onresult?.({ resultIndex: 0, results: [makeResult(text, false)] });
        }),
        await NativeSR.addListener('listeningState', (d: { status: string }) => {
          if (d.status === 'stopped') void this.finish();
        }),
      );
      await NativeSR.start({ language: this.lang, maxResults: 1, partialResults: true, popup: false });
    } catch (e) {
      console.warn('Native speech start failed:', e);
      void this.fail('network');
    }
  }

  stop(): void { void this.halt(false); }
  abort(): void { void this.halt(true); }

  private async clearListeners() {
    const hs = this.handles; this.handles = [];
    await Promise.all(hs.map((h) => h.remove().catch(() => {})));
  }

  /** Android's recognizer ended an utterance: hand the last text over as the final result. */
  private async finish() {
    if (!this.active) return;
    this.active = false;
    const text = this.last; this.last = '';
    await this.clearListeners();
    if (text) this.onresult?.({ resultIndex: 0, results: [makeResult(text, true)] });
    this.onend?.();
  }

  private async halt(silent: boolean) {
    const was = this.active;
    this.active = false;
    this.last = '';
    await this.clearListeners();
    try { await NativeSR.stop(); } catch { /* already stopped */ }
    if (was && !silent) this.onend?.();
  }

  private async fail(error: string) {
    this.active = false;
    await this.clearListeners();
    this.onerror?.({ error });
  }
}

// ── Actions Enzo asks the phone to do ───────────────────────

export interface DeviceAction {
  action: string;
  who?: string;
  number?: string;
  text?: string;
  level?: number;
  direction?: string;
  stream?: string;
  on?: boolean;
  mode?: string;
  send?: boolean;
  query?: string;
  key?: string;
  ui?: boolean;          // spotify_open: show Spotify's screen (last resort)
  url?: string;
  moovit_start?: boolean;   // open_url of a moovit:// link: tap the trip and Start
  moovit_line?: string;
  items?: unknown[];
  id?: string;
  alarm?: boolean;
  stale_after?: number;   // notify: drop it if it arrives after this time (ms)
  role?: string;
  title?: string;
  seconds?: number;
  hour?: number;
  minute?: number;
  label?: string;
  days?: number[];
  do?: string;           // screen: read | tap | type | scroll | back | home
  target?: number;       // screen: item number from the last read
}

/** Our own Android plugin (EnzoDevicePlugin.java). */
interface EnzoDevicePlugin {
  contacts(): Promise<{ contacts: { name: string; number: string }[] }>;
  call(o: { number: string }): Promise<{ direct: boolean }>;
  whatsapp(o: { number: string; text: string; send?: boolean }): Promise<{ autoSend: boolean; needsAccessibility: boolean }>;
  accessibilityStatus(): Promise<{ enabled: boolean }>;
  openAccessibilitySettings(): Promise<void>;
  openAppSettings(): Promise<void>;
  playMusic(o: { query: string }): Promise<{ auto: boolean }>;
  spotifyReport(): Promise<{ report: string }>;
  armMoovit(o: { line: string }): Promise<{ auto: boolean }>;
  moovitReport(): Promise<{ report: string }>;
  openUrl(o: { url: string; fresh?: boolean }): Promise<void>;
  openSpotify(o?: { ui?: boolean }): Promise<{ woke?: string }>;
  appVersion(): Promise<{ code: number; canInstall: boolean }>;
  openInstallPermission(): Promise<void>;
  requestNotifications(): Promise<{ granted: boolean }>;
  testNotification(o: { text?: string }): Promise<{ posted: boolean; waiting: number; next: number }>;
  notifyNow(o: { id: string; text: string; alarm?: boolean }): Promise<{ posted: boolean }>;
  pushToken(): Promise<{ token: string }>;
  reminderStatus(): Promise<{ waiting: number; next: number }>;
  pickContact(o?: { title?: string }): Promise<{ cancelled: boolean; name?: string; number?: string }>;
  setTimer(o: { seconds: number; label?: string }): Promise<void>;
  setAlarm(o: { hour: number; minute: number; label?: string; days?: number[] }): Promise<void>;
  syncReminders(o: { items: unknown[] }): Promise<void>;
  notifications(): Promise<{ enabled: boolean; items: { app: string; pkg: string; title: string; text: string; time: number }[] }>;
  clearNotifications(): Promise<void>;
  openNotificationAccessSettings(): Promise<void>;
  installApk(o: { url: string }): Promise<void>;
  openNotificationAccessSettings(): Promise<void>;
  mediaKey(o: { key: string }): Promise<void>;
  normalAudio(): Promise<{ mode: number }>;
  sms(o: { number: string; text: string }): Promise<void>;
  setVolume(o: { level?: number; direction?: string; stream?: string }): Promise<{ percent: number }>;
  setRinger(o: { mode: string }): Promise<void>;
  setBrightness(o: { level?: number; direction?: string }): Promise<{ percent?: number; needsPermission?: boolean }>;
  setFlashlight(o: { on: boolean }): Promise<void>;
  isPopup(): Promise<{ popup: boolean }>;
  closePopup(): Promise<void>;
  popupSize(o: { expanded: boolean }): Promise<void>;
  screen(o: { do: string; target: number; text: string }): Promise<{ error?: string; result?: string; app?: string; items?: unknown[]; image?: string }>;
  addListener(event: 'assist', cb: () => void): Promise<{ remove: () => Promise<void> }>;
}
const Device = registerPlugin<EnzoDevicePlugin>('EnzoDevice');

/** True when this page runs in the small popup over another app (the phone's assistant shortcut opened it). */
export let inPopup = false;
export async function detectPopup(): Promise<boolean> {
  if (!isNative) return false;
  try { inPopup = (await Device.isPopup()).popup; } catch { inPopup = false; }   // older APK: no popup
  return inPopup;
}

export function closePopup(): void {
  if (inPopup) Device.closePopup().catch(() => {});
}

/** Popup: grow the pill at the camera into the answer panel (true) or shrink it back (false). */
export function setPopupExpanded(expanded: boolean): void {
  if (inPopup) Device.popupSize({ expanded }).catch(() => {});   // older APK: stays one size
}

/** The phone's assistant shortcut was pressed (Enzo is the default digital assistant). Returns an unsubscribe. */
export function onAssist(cb: () => void): () => void {
  if (!isNative) return () => {};
  const h = Device.addListener('assist', cb);
  return () => { h.then((x) => x.remove()).catch(() => {}); };
}

/** Take the phone out of call mode once the microphone is closed (older APKs lack this): Android's audio mode after, or -1. */
export async function normalAudio(): Promise<number> {
  if (!isNative) return -1;
  try { return (await Device.normalAudio()).mode; } catch { return -1; }
}

// Every microphone stream the page opens, so mute can close all of them (a leaked one keeps the mic and call mode on).
const micStreams = new Set<MediaStream>();
const live = (s: MediaStream) => s.getTracks().some((t) => t.readyState === 'live');
if (navigator.mediaDevices?.getUserMedia) {
  const open = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  navigator.mediaDevices.getUserMedia = async (c?: MediaStreamConstraints) => {
    const s = await open(c);
    for (const old of micStreams) if (!live(old)) micStreams.delete(old);
    micStreams.add(s);
    return s;
  };
}

/** Close every open microphone: the page's streams and Android's own recognizer. Returns how many tracks were still live. */
export function closeAllMics(): number {
  let n = 0;
  for (const s of micStreams) for (const t of s.getTracks()) if (t.readyState === 'live') { t.stop(); n++; }
  micStreams.clear();
  if (isNative) NativeSR.stop().catch(() => {});
  return n;
}

/** Lower-case, no punctuation / Hebrew vowel marks, Hebrew final letters folded (ך->כ ...). */
function norm(s: string): string {
  return s
    .toLowerCase()
    .replace(/[\u0591-\u05C7\u200e\u200f]/g, '')
    .replace(/[ךםןףץ]/g, (c) => ({ 'ך': 'כ', 'ם': 'מ', 'ן': 'נ', 'ף': 'פ', 'ץ': 'צ' } as Record<string, string>)[c])
    .replace(/[^\p{L}\p{N} ]/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/** Edit distance, to forgive a letter the speech recognizer got wrong. */
function distance(a: string, b: string): number {
  const d = Array.from({ length: a.length + 1 }, (_, i) => [i, ...Array(b.length).fill(0)]);
  for (let j = 1; j <= b.length; j++) d[0][j] = j;
  for (let i = 1; i <= a.length; i++)
    for (let j = 1; j <= b.length; j++)
      d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
  return d[a.length][b.length];
}

function matchScore(contact: string, query: string): number {
  if (!contact || !query) return 0;
  if (contact === query) return 5;
  if (contact.startsWith(query)) return 4;
  if (contact.includes(query) || query.includes(contact)) return 3;
  const cw = contact.split(' '), qw = query.split(' ');
  if (qw.every((w) => cw.some((c) => c === w || (w.length >= 4 && distance(c, w) <= 1)))) return 2;
  if (query.length >= 4 && distance(contact, query) <= 1) return 1;
  return 0;
}

export class ContactsPermissionError extends Error {}

/** Find a phone number for a spoken name in the contacts (best match first). null = no match. */
export async function findContactNumber(who: string): Promise<{ name: string; number: string; searched: number } | null> {
  let contacts: { name: string; number: string }[];
  try {
    contacts = (await Device.contacts()).contacts;
  } catch (e: any) {
    if (String(e?.message || e).includes('permission')) throw new ContactsPermissionError('contacts permission off');
    throw e;
  }
  const q = norm(who);
  let best: { score: number; name: string; number: string } | null = null;
  for (const c of contacts) {
    if (!c.number || !c.name) continue;
    const score = matchScore(norm(c.name), q);
    if (score && (!best || score > best.score)) best = { score, name: c.name, number: c.number };
  }
  console.info(`Contacts: searched ${contacts.length} for "${who}" ->`, best ? best.name : 'no match');
  if (best) return { name: best.name, number: best.number, searched: contacts.length };
  searchedCount = contacts.length;
  return null;
}
let searchedCount = 0;

/** International format for WhatsApp: 050-123 4567 -> +972501234567 (Israeli numbers by default). */
function international(raw: string): string {
  const n = raw.replace(/[^\d+]/g, '');
  if (n.startsWith('+')) return n;
  if (n.startsWith('00')) return '+' + n.slice(2);
  if (n.startsWith('0')) return '+972' + n.slice(1);
  return '+' + n;
}

/** A number from the action itself, or looked up by name in the contacts. */
async function resolveNumber(a: DeviceAction, say: (t: string) => void, reply?: (o: object) => void): Promise<{ number: string; label: string } | null> {
  const direct = (a.number || '').replace(/[^\d+*#]/g, '');
  if (direct) return { number: direct, label: a.who || direct };
  if (!a.who) { say('Who should I contact?'); return null; }
  try {
    const hit = await findContactNumber(a.who);
    if (!hit && searchedCount > 0 && reply) {
      // Nobody with that name: Enzo (the server) lets the user pick the person and remembers it.
      reply({ type: 'device_result', action: 'contact_not_found', who: a.who, pending: { action: a.action, text: a.text, send: a.send } });
      return null;
    }
    if (!hit) {
      say(searchedCount === 0
        ? 'Android gave Enzo no contacts. In Android settings > Apps > Enzo > Permissions, allow Contacts.'
        : `I looked through ${searchedCount} contacts and found none like "${a.who}". Tell me the number once and I'll remember it.`);
      return null;
    }
    return { number: hit.number.replace(/[^\d+*#]/g, ''), label: hit.name };
  } catch (e) {
    say(e instanceof ContactsPermissionError
      ? 'Contacts permission is off. In Android settings > Apps > Enzo > Permissions, allow Contacts.'
      : `I could not read your contacts: ${(e as any)?.message || e}`);
    return null;
  }
}

let blockedSettingsOpened = false;

/** Run a device action; `say` shows a short message in the chat. Messages are only drafted: you press send. */
export async function runDeviceAction(a: DeviceAction, say: (text: string) => void, reply?: (o: object) => void): Promise<void> {
  try {
    switch (a.action) {
      case 'timer': {
        await Device.setTimer({ seconds: a.seconds || 0, label: a.label || '' });
        const m = Math.floor((a.seconds || 0) / 60), sec = (a.seconds || 0) % 60;
        say(`Timer started in your Clock app: ${m ? m + ' min ' : ''}${sec ? sec + ' s' : ''}`.trim());
        return;
      }
      case 'alarm': {
        await Device.setAlarm({ hour: a.hour ?? 7, minute: a.minute ?? 0, label: a.label || '', days: a.days || [] });
        say(`Alarm set in your Clock app for ${String(a.hour ?? 7).padStart(2, '0')}:${String(a.minute ?? 0).padStart(2, '0')}.`);
        return;
      }
      case 'pick_contact': {
        say(a.title || 'Choose the person from your contacts.');
        const r = await Device.pickContact({ title: a.title || '' });
        reply?.({ type: 'device_result', action: 'contact_picked', role: a.role || '', name: r.name || '', number: r.number || '', cancelled: !!r.cancelled || !r.number });
        return;
      }
      case 'call': {
        const t = await resolveNumber(a, say, reply);
        if (!t) return;
        say(`Calling ${t.label}…`);
        await Device.call({ number: t.number });
        return;
      }
      case 'whatsapp': {
        const t = await resolveNumber(a, say, reply);
        if (!t) return;
        const r = await Device.whatsapp({ number: international(t.number), text: a.text || '', send: a.send === true });
        if (r.needsAccessibility) {
          say(`WhatsApp is open for ${t.label} - press send. To let Enzo press send himself, switch on "Enzo" (the top switch, not the shortcut) in the Accessibility screen that is opening (Installed apps). If it is greyed out: Android settings > Apps > Enzo > top-right menu > Allow restricted settings.`);
          await Device.openAccessibilitySettings();
        } else {
          say(r.autoSend ? `Sending your WhatsApp to ${t.label}…` : `Opening WhatsApp for ${t.label} - press send.`);
        }
        return;
      }
      case 'sms': {
        const t = await resolveNumber(a, say, reply);
        if (!t) return;
        say(`Opening a text to ${t.label} - press send.`);
        await Device.sms({ number: t.number, text: a.text || '' });
        return;
      }
      case 'volume': {
        const r = await Device.setVolume({ level: a.level, direction: a.direction, stream: a.stream });
        say(`Volume ${r.percent}%`);
        return;
      }
      case 'brightness': {
        const r = await Device.setBrightness({ level: a.level, direction: a.direction });
        if (r.needsPermission) say('Allow "Modify system settings" for Enzo in the screen that just opened, then ask again.');
        else say(`Brightness ${r.percent}%`);
        return;
      }
      case 'flashlight':
        await Device.setFlashlight({ on: a.on !== false });
        say(a.on === false ? 'Flashlight off' : 'Flashlight on');
        return;
      case 'play':
        say(`Playing "${a.query}" on Spotify…`);
        const r = await Device.playMusic({ query: a.query || '' });
        if (!r.auto) {
          say('Spotify is searching. To make it start the song by itself, switch on "Enzo" in Accessibility (the top switch, not the shortcut).');
          return;
        }
        // Tell the user what the automation did, and what it saw if it got stuck.
        setTimeout(async () => {
          try {
            const rep = (await Device.spotifyReport()).report;
            if (rep) say(rep);
          } catch { /* ignore */ }
        }, 14000);
        return;
      case 'sync_reminders': {
        await Device.syncReminders({ items: a.items || [] });
        const st = await Device.reminderStatus();
        window.dispatchEvent(new CustomEvent('enzo-reminders', { detail: st }));  // App shows "N waiting" in the header
        if ((a.items || []).length) {
          if (st.next) {
            const t = new Date(st.next).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
            say(`Phone alarm set: ${st.waiting} waiting, next at ${t}.`);
          }
          const p = await Device.requestNotifications();
          if (!p.granted) {
            say('Reminders can only ring if notifications are allowed for Enzo. Switch on "Notifications" in the screen that is opening.');
            await Device.openAppSettings();
          }
        }
        return;
      }
      case 'notify': {
        // Android paused this page in the background, so it arrives late: a heads-up past its event is wrong, drop it
        // (the phone's alarm clock already showed it on time).
        if (a.stale_after && Date.now() > Number(a.stale_after)) return;
        const r = await Device.notifyNow({ id: String(a.id ?? a.text ?? 'sharp'), text: String(a.text ?? ''), alarm: !!a.alarm });
        // Enzo confirms in the chat; if Android blocked it, Enzo says so once and we open the settings once.
        reply?.({ type: 'device_result', action: 'reminder_delivered', id: a.id ?? '', posted: !!r.posted });
        if (!r.posted && !blockedSettingsOpened) {
          blockedSettingsOpened = true;
          await Device.openAppSettings();
        }
        return;
      }
      case 'test_notification': {
        const p = await Device.requestNotifications();
        const r = await Device.testNotification({ text: 'This is a test from Enzo, sir.' });
        const next = r.next ? new Date(r.next).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
        if (!p.granted || !r.posted) {
          say('Android is blocking Enzo notifications. Switch on "Notifications" for Enzo in the screen that is opening.');
          await Device.openAppSettings();
        } else {
          say(`Notification sent. Reminders waiting on the phone: ${r.waiting}${next ? `, next at ${next}` : ''}.`);
        }
        return;
      }
      case 'read_notifications': {
        const n = await Device.notifications();
        if (!n.enabled) {
          say('To read your notifications, switch on "Enzo (notifications)" in the screen that is opening. If it is greyed out: Android settings > Apps > Enzo > top-right menu > Allow restricted settings.');
          await Device.openNotificationAccessSettings();
          return;
        }
        reply?.({ type: 'device_result', action: 'notifications', items: n.items });
        return;
      }
      case 'open_url':
        if (a.moovit_start) {
          // The accessibility service taps the trip and Start (APK 48+); an older APK just opens the list.
          const armed = await Device.armMoovit({ line: a.moovit_line || '' }).catch(() => ({ auto: false }));
          if (armed.auto) setTimeout(async () => {
            try {
              const rep = (await Device.moovitReport()).report;
              if (rep) say(rep);
            } catch { /* ignore */ }
          }, 20000);
        }
        await Device.openUrl({ url: a.url || '', fresh: (a.url || '').startsWith('moovit:') });
        return;
      case 'spotify_open':
        await Device.openSpotify({ ui: !!a.ui });
        return;
      case 'media':
        await Device.mediaKey({ key: a.key || 'playpause' });
        say(`Music: ${a.key || 'play/pause'}`);
        return;
      case 'screen': {
        // Enzo looks at / uses the phone screen; the backend waits for this answer (same id).
        const r = await Device.screen({ do: a.do || 'read', target: a.target || 0, text: a.text || '' });
        reply?.({ type: 'device_result', action: 'screen', id: a.id, ...r });
        if (r.error?.startsWith('Screen access is off')) {
          say('To let Enzo see your screen, switch on "Enzo" in the Accessibility screen that is opening. If it is greyed out: Android settings > Apps > Enzo > top-right menu > Allow restricted settings.');
          await Device.openAccessibilitySettings();
        }
        return;
      }
      case 'close_popup':
        window.dispatchEvent(new Event('enzo-close-popup'));   // the popup closes once the goodbye is spoken
        return;
      case 'ringer':
        await Device.setRinger({ mode: a.mode || 'normal' });
        say(`Ringer: ${a.mode || 'normal'}`);
        return;
      default:
        say(`The app can't do "${a.action}" yet.`);
    }
  } catch (e: any) {
    say(typeof e?.message === 'string' ? e.message : 'That did not work on the phone.');
  }
}


const UPDATE_LATER_KEY = 'enzo-update-later';

/** First lines of what changed in the new build (written by the APK workflow), or '' if the server has none. */
async function whatsNew(): Promise<string> {
  try {
    const r = await fetch('/sharp-whatsnew.txt', { cache: 'no-store' });
    const text = r.ok ? (await r.text()).trim() : '';
    if (!text || text.startsWith('<')) return ''; // the site's index page, not the file
    return text.split('\n').map((l) => l.trim()).filter(Boolean).slice(0, 5)
      .map((l) => '- ' + (l.length > 90 ? l.slice(0, 87) + '...' : l)).join('\n');
  } catch {
    return '';
  }
}

/** A small bar at the top while the APK downloads (the plugin resolves once the installer opens). */
function showDownloading(): () => void {
  const bar = document.createElement('div');
  bar.setAttribute('role', 'status');
  bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:9999;padding:10px 16px;text-align:center;'
    + 'font:14px system-ui,sans-serif;background:#1d4ed8;color:#fff';
  const start = Date.now();
  const tick = () => { bar.textContent = `Downloading the Enzo update... ${Math.round((Date.now() - start) / 1000)} s`; };
  tick();
  document.body.appendChild(bar);
  const timer = window.setInterval(tick, 1000);
  return () => { window.clearInterval(timer); bar.remove(); };
}

/** On start-up: if the server has a newer app build, offer a one-tap update (no manual download/reinstall).
 *  manual (the Update button in Settings): ask even if he said "later", and say what happened. */
export async function checkForAppUpdate(manual = false): Promise<string> {
  if (!isNative) return 'Updates are for the Enzo app on the phone';
  try {
    const mine = (await Device.appVersion()).code;
    const r = await fetch('/sharp-version.txt', { cache: 'no-store' });
    if (!r.ok) return 'The server did not answer';
    const latest = parseInt((await r.text()).trim(), 10);
    if (!latest || latest <= mine) return `You have the newest version (${mine})`;
    try {
      if (!manual && Number(localStorage.getItem(UPDATE_LATER_KEY) || 0) > Date.now()) return ''; // he said "later" in the last day
    } catch { /* storage blocked: just ask */ }
    const news = await whatsNew();
    const ask = 'A new version of the Enzo app is ready.' + (news ? '\n\nWhat is new:\n' + news : '')
      + '\n\nUpdate now? (Android will ask you to press Install.)\nCancel = ask again tomorrow.';
    if (!window.confirm(ask)) {
      try { localStorage.setItem(UPDATE_LATER_KEY, String(Date.now() + 24 * 3600 * 1000)); } catch { /* ignore */ }
      return `Version ${latest} is ready: tap to update`;
    }
    const info = await Device.appVersion();
    if (!info.canInstall) {
      window.alert('Allow Enzo to install updates on the next screen (one time), then press back and ' + (manual ? 'tap Update again.' : 'open Enzo again.'));
      await Device.openInstallPermission();
      return 'Allow installs, then tap again';
    }
    const done = showDownloading();
    try {
      await Device.installApk({ url: new URL('/sharp.apk', window.location.origin).toString() });
      return 'Press Install on the Android screen';
    } catch (e: any) {
      window.alert(`The update did not download: ${e?.message || e}. ` + (manual ? 'Tap Update to try again.' : 'It will be offered again next time you open Enzo.'));
      return 'The download failed: tap to try again';
    } finally {
      done();
    }
  } catch (e) {
    console.warn('Update check failed', e);
    return 'Could not check for an update';
  }
}


/** Ask for the notification permission (reminders). Separate from the update check: an older app build
 *  may not have this method, and that must never stop the update from being offered. */
/** This phone's push address for the server ('' in a browser, an older app or without Firebase). */
export async function pushToken(): Promise<string> {
  if (!isNative) return '';
  try { return (await Device.pushToken()).token || ''; } catch { return ''; }
}

/** On start-up: give the server this phone's push address, so notifications arrive while the app sleeps. */
export async function registerPush(authToken: string): Promise<void> {
  if (!isNative || !authToken) return;
  let token = '', error = '';
  try { token = (await Device.pushToken()).token || ''; if (!token) error = 'Firebase gave no token'; }
  catch (e: any) { error = `pushToken failed: ${e?.message || e}`.slice(0, 200); }
  try {
    await fetch('/api/panel/push', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${authToken}` },
      body: JSON.stringify({ token, error }) });
  } catch { /* offline: the live connection sends it later */ }
}

export async function askNotificationPermission(): Promise<void> {
  if (!isNative) return;
  try {
    await Device.requestNotifications();
  } catch {
    /* older app build */
  }
}
