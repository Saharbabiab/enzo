// Memory screen (Phase 7 panel): what Enzo remembers, the lessons it uses, playbook ideas waiting for his OK, and recent turns.
import { useCallback, useEffect, useState } from 'react';
import { getToken } from './auth';

type Tab = 'memory' | 'lessons' | 'ideas' | 'turns';
type Fact = { id: string; text: string; trust: string; source: string; core: boolean; updated: number; quarantined: string };
type Lesson = { id: string; text: string; src: string; helped: number; hurt: number; off: boolean };
type Idea = { name: string; description: string; steps: string; tools: string[] };
type Turn = { id: string; ts: number; text: string; reply: string; route: string; ms: number; cost: number; outcome: string;
  tools: { name: string; ok: boolean; ms: number }[]; signals: string[] };
type Filter = 'all' | 'told' | 'noticed';

const TABS: { id: Tab; label: string }[] = [
  { id: 'memory', label: 'Memory' }, { id: 'lessons', label: 'Lessons' }, { id: 'ideas', label: 'Ideas' }, { id: 'turns', label: 'Turns' },
];
const FILTERS: { id: Filter; label: string }[] = [{ id: 'all', label: 'All' }, { id: 'told', label: 'You told me' }, { id: 'noticed', label: 'I noticed' }];
const when = (s: number) => (s ? new Date(s * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '');
const heldBack = (f: Fact) => f.trust === 'untrusted' || !!f.quarantined;

const trash = (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" /></svg>
);
const pencil = (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4" /></svg>
);

export default function Panel({ onClose, onAuthFailed }: { onClose: () => void; onAuthFailed: () => void }) {
  const [tab, setTab] = useState<Tab>('memory');
  const [data, setData] = useState<{ facts?: Fact[]; lessons?: Lesson[]; proposals?: Idea[]; traces?: Turn[] }>({});
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<Filter>('all');
  const [showHeld, setShowHeld] = useState(false);

  const api = useCallback(async (path: string, method = 'GET', body?: unknown) => {
    const r = await fetch(`/api/panel/${path}`, {
      method,
      headers: { Authorization: `Bearer ${getToken()}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (r.status === 401) { onAuthFailed(); throw new Error('Signed out.'); }
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `Failed (${r.status}).`);
    return r.json();
  }, [onAuthFailed]);

  const load = useCallback(async () => {
    setError('');
    try {
      const path = { memory: 'memory', lessons: 'lessons', ideas: 'playbooks', turns: 'traces' }[tab];
      const r = await api(path);
      setData((d) => ({ ...d, ...r }));   // per tab: a late reply for another tab can't wipe this one
    } catch (e) { setError(e instanceof Error ? e.message : 'Failed.'); }
  }, [api, tab]);

  useEffect(() => { void load(); }, [load]);

  const act = async (path: string, method: string, body?: unknown) => {
    if (busy) return;
    setBusy(true);
    try { await api(path, method, body); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : 'Failed.'); }
    finally { setBusy(false); }
  };

  const editFact = (f: Fact) => {
    const text = window.prompt('Correct this fact', f.text);
    if (text && text.trim() && text.trim() !== f.text) void act(`memory/${f.id}`, 'PUT', { text: text.trim() });
  };
  const addLesson = () => {
    const text = window.prompt('A rule Enzo should always follow');
    if (text && text.trim()) void act('lessons', 'POST', { text: text.trim() });
  };

  const q = query.trim().toLowerCase();
  const facts = data.facts || [];
  const held = facts.filter(heldBack);
  const shown = facts.filter((f) => !heldBack(f)
    && (filter === 'all' || (filter === 'told' ? f.trust === 'trusted' : f.trust !== 'trusted'))
    && (!q || f.text.toLowerCase().includes(q)));

  const factRow = (f: Fact) => (
    <div key={f.id} className="frow">
      <span className="frow__text">
        <span dir="auto">{f.text}</span>
        <span className="frow__meta">
          {f.quarantined ? `Held back: ${f.quarantined}` : f.trust === 'trusted' ? 'You told me' : f.trust === 'untrusted' ? 'From an email or web page' : 'I noticed'}
          {f.core ? ' · core' : ''}{f.updated ? ` · ${when(f.updated)}` : ''}
        </span>
      </span>
      {f.trust !== 'trusted' && (
        <button className="frow__ok" disabled={busy} onClick={() => void act(`memory/${f.id}/trust`, 'POST')}>{heldBack(f) ? 'Allow' : 'Confirm'}</button>
      )}
      <button className="icon44" disabled={busy} onClick={() => editFact(f)} aria-label="Correct this">{pencil}</button>
      <button className="icon44" disabled={busy} onClick={() => void act(`memory/${f.id}`, 'DELETE')} aria-label="Forget this">{trash}</button>
    </div>
  );

  return (
    <div className="page" role="region" aria-label="Memory">
      <header className="topbar">
        <button className="icon44" onClick={onClose} aria-label="Back to home">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><path d="m15 6-6 6 6 6" /></svg>
        </button>
        <div className="tabs" role="tablist">
          {TABS.map((t) => (
            <button key={t.id} role="tab" aria-selected={tab === t.id} className={`tab${tab === t.id ? ' tab--on' : ''}`} onClick={() => setTab(t.id)}>{t.label}</button>
          ))}
        </div>
      </header>

      <main className="page__body page__body--scroll">
        {error && <p className="page__error" role="alert">{error}</p>}

        {tab === 'memory' && data.facts && (<>
          <h1 className="page__title">Here's what I remember about you.</h1>
          <label className="search">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
            <span className="sr">Search memory</span>
            <input placeholder="Search what I remember" value={query} onChange={(e) => setQuery(e.target.value)} dir="auto" />
          </label>
          <div className="chips chips--wrap">
            {FILTERS.map((f) => (
              <button key={f.id} className={`chip${filter === f.id ? ' chip--on' : ''}`} aria-pressed={filter === f.id} onClick={() => setFilter(f.id)}>{f.label}</button>
            ))}
          </div>
          {shown.length > 0
            ? <section className="group" aria-label="Facts">{shown.map(factRow)}</section>
            : <p className="page__empty">{facts.length ? 'Nothing matches.' : 'Nothing remembered yet.'}</p>}
          {held.length > 0 && (
            <section className="held" aria-label="Held back">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--warn)" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><path d="M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6z" /></svg>
              <span className="held__text">
                <span>{held.length} note{held.length === 1 ? '' : 's'} held back</span>
                <span className="frow__meta">They came from an email or a web page, so I don't act on them until you check.</span>
              </span>
              <button className="link" onClick={() => setShowHeld((v) => !v)} aria-expanded={showHeld}>{showHeld ? 'Hide' : 'Check'}</button>
            </section>
          )}
          {showHeld && held.length > 0 && <section className="group" aria-label="Held back notes">{held.map(factRow)}</section>}
        </>)}

        {tab === 'lessons' && data.lessons && (<>
          <button className="pill" disabled={busy} onClick={addLesson}>Add a rule</button>
          <section className="group">
            {data.lessons.filter((n) => !n.off).map((n) => (
              <div key={n.id} className="frow">
                <span className="frow__text">
                  <span>{n.text}</span>
                  <span className="frow__meta">
                    {{ owner: 'Your rule', seed: 'Built in', learned: 'Learned', nightly: 'Learned overnight' }[n.src] || n.src}
                    {n.helped || n.hurt ? ` · helped ${n.helped}, hurt ${n.hurt}` : ''}
                  </span>
                </span>
                {n.src !== 'seed' && <button className="icon44" disabled={busy} onClick={() => void act(`lessons/${n.id}`, 'DELETE')} aria-label="Remove this rule">{trash}</button>}
              </div>
            ))}
          </section>
        </>)}

        {tab === 'ideas' && data.proposals && (data.proposals.length === 0 ? <p className="page__empty">No playbook ideas waiting.</p> : data.proposals.map((i) => (
          <section key={i.name} className="group group--pad">
            <h2 className="idea__name">{i.name.replace(/_/g, ' ')}</h2>
            <p className="idea__text">{i.description}</p>
            <pre className="idea__steps">{i.steps}</pre>
            {i.tools.length > 0 && <span className="frow__meta">Tools: {i.tools.join(', ')}</span>}
            <div className="sheet__buttons">
              <button className="btn btn--soft" disabled={busy} onClick={() => void act(`playbooks/${i.name}/reject`, 'POST')}>Reject</button>
              <button className="btn btn--primary" disabled={busy} onClick={() => void act(`playbooks/${i.name}/approve`, 'POST')}>Approve</button>
            </div>
          </section>
        )))}

        {tab === 'turns' && data.traces && (data.traces.length === 0 ? <p className="page__empty">No turns in the last two days.</p> : (
          <section className="group">
            {data.traces.map((t) => (
              <details key={t.id} className={`turn turn--${t.outcome}`}>
                <summary>
                  <span dir="auto">{t.text || '(no text)'}</span>
                  <span className="frow__meta">{when(t.ts)} · {t.route || '?'} · {(t.ms / 1000).toFixed(1)} s{t.cost ? ` · $${t.cost}` : ''}</span>
                </summary>
                {t.tools.length > 0 && <span className="frow__meta">Tools: {t.tools.map((x) => `${x.name}${x.ok ? '' : ' (failed)'}`).join(', ')}</span>}
                {t.signals.length > 0 && <span className="frow__meta">Signals: {t.signals.join(', ')}</span>}
                <span className="turn__reply" dir="auto">{t.reply}</span>
              </details>
            ))}
          </section>
        ))}
      </main>
    </div>
  );
}
