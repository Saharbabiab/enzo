/**
 * serverSpeech.ts - a better speech recognizer for the Android app.
 *
 * The phone's built-in recognizer mishears (Enzo -> "shop", "Charlotte"). This one listens with the microphone,
 * notices by itself when you start and stop talking, and sends each sentence (as a small WAV) to Enzo's server, where
 * Whisper transcribes it with a vocabulary prompt (Enzo, the user, Spotify, ...). It has the same shape as the browser's
 * SpeechRecognition (lang / onresult / onend / onerror / start / stop / abort), so the assistant uses it unchanged.
 *
 * If the server cannot be reached twice in a row, `serverSttEnabled()` turns false for 5 minutes and the app goes back to
 * the phone's own recognizer. Force it either way with localStorage 'enzo.stt' = 'phone' | 'server'.
 */

import { SpeechRecognition as NativeSR } from '@capacitor-community/speech-recognition';
import { getToken } from './auth';

const FAIL_KEY = 'enzo.stt.brokenUntil';

export function serverSttEnabled(): boolean {
  try {
    const forced = localStorage.getItem('enzo.stt');
    if (forced === 'phone') return false;
    if (forced === 'server') return true;
    return Date.now() > Number(localStorage.getItem(FAIL_KEY) || 0);
  } catch {
    return true;
  }
}

function markBroken() {
  try { localStorage.setItem(FAIL_KEY, String(Date.now() + 5 * 60_000)); } catch { /* ignore */ }
}

function makeResult(text: string, isFinal: boolean) {
  const alt = { transcript: text, confidence: 1 };
  return Object.assign([alt], { isFinal, length: 1 });
}

/** Mono Float32 blocks at `rate` Hz -> 16 kHz 16-bit WAV. */
export function toWav16k(blocks: Float32Array[], rate: number): Blob {
  let n = 0;
  for (const b of blocks) n += b.length;
  const all = new Float32Array(n);
  let o = 0;
  for (const b of blocks) { all.set(b, o); o += b.length; }
  const outLen = Math.floor(n * 16000 / rate);
  const pcm = new Int16Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const pos = i * rate / 16000;
    const i0 = Math.floor(pos);
    const f = pos - i0;
    const s = (all[i0] || 0) * (1 - f) + (all[i0 + 1] || 0) * f;
    pcm[i] = Math.max(-1, Math.min(1, s)) * 0x7fff;
  }
  const buf = new ArrayBuffer(44 + pcm.length * 2);
  const v = new DataView(buf);
  const w = (off: number, s: string) => { for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); };
  w(0, 'RIFF'); v.setUint32(4, 36 + pcm.length * 2, true); w(8, 'WAVE'); w(12, 'fmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, 16000, true); v.setUint32(28, 32000, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  w(36, 'data'); v.setUint32(40, pcm.length * 2, true);
  new Int16Array(buf, 44).set(pcm);
  return new Blob([buf], { type: 'audio/wav' });
}

export class ServerSpeechRecognition {
  lang = 'en-US';
  continuous = true;
  interimResults = true;
  maxAlternatives = 1;
  onresult: ((e: any) => void) | null = null;
  onend: (() => void) | null = null;
  onerror: ((e: any) => void) | null = null;
  /** Extra signal for the UI: 'hearing' (you are talking) and 'transcribing' (sent, waiting for the text). */
  onactivity: ((s: 'hearing' | 'transcribing') => void) | null = null;

  private active = false;
  private stream: MediaStream | null = null;
  private ctx: AudioContext | null = null;
  private node: ScriptProcessorNode | null = null;
  private src: MediaStreamAudioSourceNode | null = null;

  async start(): Promise<void> {
    if (this.active) throw new Error('InvalidStateError: already started');
    this.active = true;
    try {
      let perm = await NativeSR.checkPermissions();
      if (perm.speechRecognition !== 'granted') perm = await NativeSR.requestPermissions();
      if (perm.speechRecognition !== 'granted') return this.fail('not-allowed');
      if (!this.active) return;   // stopped while we were checking
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      if (!this.active) { stream.getTracks().forEach((t) => t.stop()); return; }   // stopped while the mic was opening
      this.stream = stream;
      const ctx = new AudioContext();
      this.ctx = ctx;
      const src = ctx.createMediaStreamSource(stream);
      const node = ctx.createScriptProcessor(4096, 1, 1);
      this.src = src; this.node = node;
      const blockMs = (4096 / ctx.sampleRate) * 1000;

      let baseline = 0.01;                 // quiet-room loudness, follows slow changes
      let voiced = 0;                      // recent blocks that were louder than the room
      let silentMs = 0;
      let speakingMs = 0;
      let speaking = false;
      let busy = false;                    // a sentence is being transcribed
      let pre: Float32Array[] = [];        // the last ~0.4 s, so the first syllable is not lost
      let rec: Float32Array[] = [];

      node.onaudioprocess = (e) => {
        if (!this.active || busy) return;
        const x = new Float32Array(e.inputBuffer.getChannelData(0));
        let sum = 0;
        for (let i = 0; i < x.length; i++) sum += x[i] * x[i];
        const rms = Math.sqrt(sum / x.length);
        document.documentElement.style.setProperty('--vol', Math.min(1, rms * 8).toFixed(2));   // the ring swells with his voice
        const thr = Math.max(0.02, baseline * 3);
        const loud = rms > thr;
        if (!speaking) {
          baseline = baseline * 0.95 + rms * 0.05;
          pre.push(x);
          if (pre.length > 5) pre.shift();
          voiced = loud ? voiced + 1 : Math.max(0, voiced - 1);
          if (voiced >= 2) {
            speaking = true; silentMs = 0; speakingMs = 0;
            rec = [...pre]; pre = [];
            this.onactivity?.('hearing');
          }
          return;
        }
        rec.push(x);
        speakingMs += blockMs;
        silentMs = loud ? 0 : silentMs + blockMs;
        if (silentMs >= 900 || speakingMs >= 25000) {
          const blocks = rec; rec = []; speaking = false; voiced = 0; silentMs = 0;
          if (speakingMs - silentMs < 300) return;           // a click or cough, not a sentence
          busy = true;
          this.onactivity?.('transcribing');
          void this.transcribe(blocks, ctx.sampleRate).finally(() => { busy = false; });
        }
      };
      src.connect(node);
      node.connect(ctx.destination);       // required for the processor to run (it outputs silence)
    } catch (e) {
      console.warn('Server speech start failed:', e);
      void this.fail('not-allowed');
    }
  }

  private async transcribe(blocks: Float32Array[], rate: number) {
    let text = '';
    try {
      const wav = toWav16k(blocks, rate);
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), 20000);
      const r = await fetch(`/api/stt?lang=${this.lang.startsWith('he') ? 'he' : 'en'}`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${getToken()}`, 'Content-Type': 'audio/wav' },
        body: wav,
        signal: ctl.signal,
      });
      clearTimeout(timer);
      if (!r.ok) throw new Error(`stt ${r.status}`);
      text = String((await r.json()).text || '').trim();
      try { localStorage.removeItem('enzo.stt.fails'); } catch { /* ignore */ }
    } catch (e) {
      console.warn('Server speech failed:', e);
      let fails = 1;
      try { fails = Number(localStorage.getItem('enzo.stt.fails') || 0) + 1; localStorage.setItem('enzo.stt.fails', String(fails)); } catch { /* ignore */ }
      if (fails >= 2) { markBroken(); void this.fail('network'); return; }
    }
    if (!this.active) return;
    if (text) {
      await this.finish(text);
    } else {
      this.onactivity?.('hearing');        // nothing was said (noise): carry on listening
    }
  }

  private release() {
    try { this.node?.disconnect(); } catch { /* ignore */ }
    try { this.src?.disconnect(); } catch { /* ignore */ }
    try { this.stream?.getTracks().forEach((t) => t.stop()); } catch { /* ignore */ }
    document.documentElement.style.setProperty('--vol', '0');
    try { void this.ctx?.close(); } catch { /* ignore */ }
    this.node = null; this.src = null; this.stream = null; this.ctx = null;
  }

  private async finish(text: string) {
    if (!this.active) return;
    this.active = false;
    this.release();
    this.onresult?.({ resultIndex: 0, results: [makeResult(text, true)] });
    this.onend?.();
  }

  stop(): void { const was = this.active; this.active = false; this.release(); if (was) this.onend?.(); }
  abort(): void { this.active = false; this.release(); }

  private fail(error: string) {
    this.active = false;
    this.release();
    this.onerror?.({ error });
  }
}
