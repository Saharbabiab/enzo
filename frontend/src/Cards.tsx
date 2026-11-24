// Answer cards: a tool's real result drawn under Enzo's reply. The data comes from the server
// ({"type": "card", "kind", "data"}); a field it did not send is left out, never made up.
import { useState } from 'react';
import type { AnswerCard } from './useEnzoAssistant';

const num = (v: unknown): v is number => typeof v === 'number' && isFinite(v);
const deg = (v: number) => `${Math.round(v)}°`;
const mmss = (ms: number) => `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}`;
const AVATAR = ['var(--brown)', 'var(--accent)', '#4a4c55'];

function Weather({ d }: { d: any }) {
  const hours: { time: string; temp: number }[] = Array.isArray(d.hours) ? d.hours.filter((h: any) => num(h?.temp)) : [];
  const all = [...hours.map((h) => h.temp), ...(num(d.temp) ? [d.temp] : [])];
  const lo = Math.min(...all), hi = Math.max(...all);
  const bar = (t: number) => `${12 + (hi > lo ? (t - lo) / (hi - lo) : 0.5) * 28}px`;
  const parts = [d.condition, num(d.low) && num(d.high) ? `${deg(d.low)} to ${deg(d.high)}` : '', num(d.wind) ? `wind ${Math.round(d.wind)} km/h` : '']
    .filter(Boolean).join(' · ');
  return (
    <section className="acard acard--weather" aria-label="Weather">
      <div className="acard__place">{String(d.place || '').split(',')[0]}</div>
      {num(d.temp) && <div className="acard__big">{deg(d.temp)}</div>}
      {parts && <div className="acard__sub">{parts}</div>}
      {hours.length > 0 && (
        <div className="hours">
          {num(d.temp) && <Hour label="Now" t={d.temp} h={bar(d.temp)} />}
          {hours.map((h) => <Hour key={h.time} label={h.time} t={h.temp} h={bar(h.temp)} />)}
        </div>
      )}
    </section>
  );
}

function Hour({ label, t, h }: { label: string; t: number; h: string }) {
  return (
    <div className="hour">
      <span className="hour__time">{label}</span>
      <span className="hour__bar" style={{ height: h }} />
      <span className="hour__temp">{deg(t)}</span>
    </div>
  );
}

function Calendar({ d }: { d: any }) {
  const events: { start: string; end: string; title: string; location: string }[] = Array.isArray(d.events) ? d.events : [];
  return (
    <section className="acard" aria-label={`Calendar, ${d.day || 'today'}`}>
      <div className="acard__label">{d.day}</div>
      {events.length === 0 && <div className="acard__sub">Nothing on the calendar.</div>}
      {events.map((e, i) => (
        <div key={i} className={`slot${e.start ? '' : ' slot--allday'}`}>
          <span className="slot__time">{e.start ? e.start : 'All day'}</span>
          <span className="slot__title" dir="auto">{e.title}{e.end && e.start ? <span className="slot__end"> until {e.end}</span> : null}</span>
        </div>
      ))}
    </section>
  );
}

function Mail({ d }: { d: any }) {
  const mails: { from: string; subject: string }[] = Array.isArray(d.emails) ? d.emails : [];
  if (!mails.length) return null;
  return (
    <section className="acard acard--list" aria-label="Mail">
      {mails.map((m, i) => (
        <div key={i} className="mrow">
          <span className="mrow__avatar" style={{ background: AVATAR[i % AVATAR.length] }} aria-hidden="true">
            {(m.from || '?').trim().charAt(0).toUpperCase()}
          </span>
          <span className="mrow__text">
            <span className="mrow__who" dir="auto">{m.from}</span>
            <span className="mrow__what" dir="auto">{m.subject}</span>
          </span>
        </div>
      ))}
    </section>
  );
}

function Music({ d, say }: { d: any; say: (text: string) => void }) {
  const pct = num(d.duration_ms) && d.duration_ms > 0 && num(d.progress_ms) ? Math.min(100, (d.progress_ms / d.duration_ms) * 100) : null;
  return (
    <section className="acard acard--music" aria-label="Now playing">
      <div className="music__top">
        {d.image ? <img className="music__art" src={d.image} alt="" /> : <span className="music__art" aria-hidden="true" />}
        <div className="music__meta">
          <span className="music__title" dir="auto">{d.title}</span>
          <span className="music__artist" dir="auto">{d.artist ? `${d.artist} · ` : ''}Spotify{d.playing ? '' : ' (paused)'}</span>
        </div>
      </div>
      {pct !== null && (
        <div className="music__progress">
          <span className="music__track"><span style={{ width: `${pct}%` }} /></span>
          <span className="music__times"><span>{mmss(d.progress_ms)}</span><span>{mmss(d.duration_ms)}</span></span>
        </div>
      )}
      {/* The buttons ask Enzo in words, like saying it: the model picks the tool. */}
      <div className="music__controls">
        <button className="icon44" aria-label="Previous song" onClick={() => say('Previous song')}>
          <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7 6h2v12H7zM20 6v12l-9-6z" /></svg>
        </button>
        <button className="music__play" aria-label={d.playing ? 'Pause' : 'Resume'} onClick={() => say(d.playing ? 'Pause the music' : 'Resume the music')}>
          {d.playing
            ? <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="5" width="4" height="14" rx="1" /><rect x="14" y="5" width="4" height="14" rx="1" /></svg>
            : <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z" /></svg>}
        </button>
        <button className="icon44" aria-label="Next song" onClick={() => say('Next song')}>
          <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M15 6h2v12h-2zM4 6v12l9-6z" /></svg>
        </button>
      </div>
    </section>
  );
}

// Enzo's own screen (show_ui / ask_user): options, drafts, lists, steps, links. A tap answers him in words, like
// saying it, and the card then keeps the choice so it can't be sent twice.
type Opt = { label: string; say: string };
const host = (u: string) => { try { return new URL(u).hostname; } catch { return ''; } };
const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x) => typeof x === 'string' && x.trim()) : []);

function Ui({ kind, d, say }: { kind: string; d: any; say: (text: string) => void }) {
  const [picked, setPicked] = useState<string | null>(null);
  const items = strs(d.items);
  const opts: Opt[] = Array.isArray(d.options) ? d.options.filter((o: any) => o && typeof o.label === 'string' && o.label.trim()) : [];
  const pick = (label: string, text: string) => { if (!picked) { setPicked(label); say(text); } };
  const links = items.map((l) => l.split('|')).filter((p) => p.length > 1 && /^https?:\/\//.test(p.slice(1).join('|').trim()));
  const long = opts.some((o) => o.label.length > 28);
  return (
    <section className={`acard acard--ui acard--${kind}`} aria-label={d.title || kind}>
      {d.title && <div className="acard__label" dir="auto">{d.title}</div>}
      {kind === 'draft' && items.map((t, i) =>
        items.length > 1
          ? <button key={i} className={`draft draft--pick${picked === t ? ' draft--on' : ''}`} disabled={!!picked} dir="auto"
              onClick={() => pick(t, `Use this one: "${t}"`)}>{t}</button>
          : <p key={i} className="draft" dir="auto">{t}</p>)}
      {kind === 'list' && items.map((t, i) => {
        const [main, ...rest] = t.split(/\s[-–—]\s/);
        return <div key={i} className="urow" dir="auto"><span>{main}</span>{rest.length > 0 && <span className="urow__sub">{rest.join(' - ')}</span>}</div>;
      })}
      {kind === 'steps' && <ol className="steps">{items.map((t, i) => <li key={i} dir="auto">{t}</li>)}</ol>}
      {kind === 'links' && links.map(([label, ...url], i) => (
        <a key={i} className="urow urow--link" href={url.join('|').trim()} target="_blank" rel="noopener noreferrer" dir="auto">
          <span>{label.trim()}</span><span className="urow__sub">{host(url.join('|').trim())}</span>
        </a>
      ))}
      {opts.length > 0 && (
        <div className={`chips chips--wrap${long ? ' chips--stack' : ''}`}>
          {opts.map((o) => (
            <button key={o.label} className={`chip${picked === o.label ? ' chip--on' : ''}`} disabled={!!picked && picked !== o.label}
              aria-pressed={picked === o.label} dir="auto" onClick={() => pick(o.label, o.say || o.label)}>{o.label}</button>
          ))}
        </div>
      )}
    </section>
  );
}

export default function Card({ card, say }: { card: AnswerCard; say: (text: string) => void }) {
  switch (card.kind) {
    case 'options': case 'draft': case 'list': case 'steps': case 'links': return <Ui kind={card.kind} d={card.data || {}} say={say} />;
    case 'weather': return <Weather d={card.data} />;
    case 'calendar': return <Calendar d={card.data} />;
    case 'mail': return <Mail d={card.data} />;
    case 'music': return card.data?.title ? <Music d={card.data} say={say} /> : null;
    default: return null;   // a kind this app version does not know yet
  }
}
