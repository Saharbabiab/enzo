/**
 * useEnzoAssistant.ts
 *
 * Custom React hook that manages the entire S.H.A.R.P. voice assistant lifecycle:
 *
 * 1. IDLE: Continuously listens for the wake word "wake up enzo" using
 * the Web Speech API (SpeechRecognition). No WebSocket connection.
 *
 * 2. CONNECTING: Wake word detected → opens a WebSocket to the backend.
 *
 * 3. CONNECTED / LISTENING: WebSocket is open. The Speech Recognition API
 * continuously transcribes speech. Each final transcript is sent as
 * JSON `{ "text": "..." }` to the backend.
 *
 * 4. SPEAKING: Backend sends back MP3 audio bytes. We play them. During
 * playback we pause recognition so S.H.A.R.P.'s own voice isn't fed back.
 *
 * 5. SLEEP: User says "sleep enzo" → backend processes it, sends its
 * farewell audio + a `{"type":"command","action":"sleep"}` JSON message.
 * After we finish playing the farewell audio, we close the WebSocket
 * and return to IDLE (listening for wake word again).
 *
 * Backend protocol (WebSocket on /ws):
 * Client → Server:
 * - JSON text:  { "text": "user said this" }
 * Server → Client:
 * - JSON text:  { "type": "transcript",  "role": "user"|"sharp", "text": "..." }
 * - JSON text:  { "type": "command",     "action": "sleep" }
 * - Binary:     MP3 audio bytes (TTS response)
 */

import { useState, useRef, useCallback, useEffect } from 'react';
import { getToken, clearToken, wsUrl } from './auth';
import { isNative, inPopup, NativeSpeechRecognition, runDeviceAction, normalAudio, closeAllMics, pushToken } from './native';
import { ServerSpeechRecognition, serverSttEnabled } from './serverSpeech';

// ── Types ──────────────────────────────────────────────────

export type AssistantState =
  | 'idle'         // Waiting for wake word
  | 'connecting'   // WebSocket opening
  | 'connected'    // WS open, ready
  | 'listening'    // Actively hearing user speech
  | 'processing'   // Sent text, waiting for response
  | 'speaking';    // Playing backend's TTS audio

/** A tool's real result drawn as a card under Enzo's reply (server: {"type": "card", "kind", "data"}). */
export interface AnswerCard {
  kind: string;
  data: any;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'sharp' | 'system';
  text: string;
  timestamp: Date;
  cards?: AnswerCard[];
}

interface UseEnzoReturn {
  state: AssistantState;
  messages: ChatMessage[];
  interimText: string;
  isMuted: boolean;
  isEnzoMuted: boolean;
  toggleMute: () => void;
  toggleEnzoMute: () => void;
  /** Language the microphone listens in (Chrome hears one language at a time). */
  speechLang: SpeechLang;
  toggleSpeechLang: () => void;
  startSetup: () => void;
  sendFeedback: (rating: 'good' | 'bad') => void;
  activity: string;   // the tool Enzo is running now, while it works ('' otherwise)
  connect: () => void;
  disconnect: () => void;
  sendTextMsg: (text: string) => void;
  /** A risky action Enzo wants approval for (null when nothing is pending). */
  pendingConfirm: { id: string; text: string } | null;
  answerConfirm: (answer: 'yes' | 'always' | 'no') => void;
  /** Stop Enzo talking right now (barge-in / tap the orb). */
  interrupt: (reason?: string) => void;
}

// ── Configuration ──────────────────────────────────────────


/**
 * Tell the backend what device we're on, our timezone and — if the user allows
 * it — our location, so Enzo can answer "what's the weather?" without asking.
 * Sent once right away (no location) and again when the GPS fix arrives.
 * Note: browsers only allow geolocation on https:// or localhost.
 */
function sendSessionContext(ws: WebSocket) {
  const base = {
    type: 'context',
    device: /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent) ? 'mobile' : 'desktop',
    app: isNative ? 'android' : 'web',
    popup: inPopup,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  };
  const send = (extra: object = {}) => {
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ ...base, ...extra }));
  };
  send();
  void pushToken().then((t) => { if (t) send({ push_token: t }); });   // the server pushes notifications to it
  if (!('geolocation' in navigator)) return;
  navigator.geolocation.getCurrentPosition(
    (pos) =>
      send({
        location: {
          latitude: pos.coords.latitude,
          longitude: pos.coords.longitude,
          accuracy: pos.coords.accuracy,
        },
      }),
    (err) => {   // 1 = permission denied, 2 = no fix, 3 = timeout; goes to the server log so a phone problem is visible
      console.info('Location not shared:', err.message);
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'client_log', text: `location not shared: code ${err.code} ${err.message}` }));
    },
    { enableHighAccuracy: false, timeout: 10000, maximumAge: 10 * 60 * 1000 },
  );
}

const WAKE_PHRASES = ['wake up enzo', 'wakeup enzo', 'hey enzo', 'wake up enzo', 'wakeup enzo', 'wake up shark', 'wake up shop',
  'שארפ תתעורר', 'תתעורר שארפ', 'היי שארפ', 'שארפ קום'];

export type SpeechLang = 'en-US' | 'he-IL';
const LANG_KEY = 'enzo.speechLang';
function loadSpeechLang(): SpeechLang {
  try { return localStorage.getItem(LANG_KEY) === 'he-IL' ? 'he-IL' : 'en-US'; } catch { return 'en-US'; }
}
// Sleep detection is handled server-side — the backend sends
// { "type": "command", "action": "sleep" } when it hears "sleep enzo".

// ── SpeechRecognition polyfill ─────────────────────────────

// In the Android app (WebView) the browser has no speech API: use the phone's own recognizer instead.
const SpeechRecognition: any =
  (window as any).SpeechRecognition ||
  (window as any).webkitSpeechRecognition ||
  (isNative ? NativeSpeechRecognition : undefined);

// ── Hook ───────────────────────────────────────────────────

export function useEnzoAssistant(onAuthFailed?: () => void): UseEnzoReturn {
  const onAuthFailedRef = useRef(onAuthFailed);
  onAuthFailedRef.current = onAuthFailed;
  const [state, setState] = useState<AssistantState>('idle');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [interimText, setInterimText] = useState('');
  const [pendingConfirm, setPendingConfirm] = useState<{ id: string; text: string } | null>(null);
  const [activity, setActivity] = useState('');   // the tool Enzo is running right now (Phase 7)
  const [isMuted, setIsMuted] = useState(false);
  const [isEnzoMuted, setIsEnzoMuted] = useState(false);
  const [speechLang, setSpeechLang] = useState<SpeechLang>(loadSpeechLang);
  const speechLangRef = useRef<SpeechLang>(speechLang);

  const wsRef = useRef<WebSocket | null>(null);
  const micDeniedRef = useRef(false);  // set once the browser blocks the mic; stops restart loops
  const manualCloseRef = useRef(false);        // user pressed Disconnect → no auto-reconnect
  const reconnectAttemptsRef = useRef(0);
  const connectRef = useRef<(() => void) | null>(null);  // latest connectToBackend, for reconnect timers
  const recognitionRef = useRef<any>(null);
  const audioQueueRef = useRef<Blob[]>([]);
  const isPlayingRef = useRef(false);
  const sleepPendingRef = useRef(false);
  const stateRef = useRef<AssistantState>('idle');
  const isMutedRef = useRef(false);
  const isEnzoMutedRef = useRef(false);
  const currentAudioRef = useRef<HTMLAudioElement | null>(null);
  // Streaming speech / barge-in
  const speechActiveRef = useRef(false);        // between speech_start and speech_end
  const resumeMusicRef = useRef(false);         // Android paused Spotify while Enzo talked: press play when he is done
  const resumeMusic = () => {
    if (!resumeMusicRef.current) return;
    resumeMusicRef.current = false;
    if (isNative) void runDeviceAction({ action: 'media', key: 'play' }, () => {}, () => {});
  };
  const spokenTextRef = useRef('');             // what Enzo is saying (to ignore our own echo)
  const bargeInStartRef = useRef<(() => void | Promise<void>) | null>(null);
  const lastEnzoTextRef = useRef('');          // Enzo's latest reply (the phone mic must not transcribe it)
  const audioEndedAtRef = useRef(0);            // when Enzo's voice last stopped
  const listenTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const voiceBargeStopRef = useRef<(() => void) | null>(null);  // stops the app's "user talks over Enzo" detector

  // Keep refs in sync with state for event listeners
  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    isMutedRef.current = isMuted;
  }, [isMuted]);

  useEffect(() => {
    isEnzoMutedRef.current = isEnzoMuted;
    if (currentAudioRef.current) {
      currentAudioRef.current.muted = isEnzoMuted;
    }
  }, [isEnzoMuted]);

  // ── Helpers ──────────────────────────────────────────────

  const pendingCardsRef = useRef<AnswerCard[]>([]);   // cards of this turn, shown with Enzo's next reply
  const pendingTextRef = useRef('');                    // typed before the connection was open: sent once it is

  const pushMessage = useCallback((role: ChatMessage['role'], text: string) => {
    const cards = role === 'sharp' && pendingCardsRef.current.length ? pendingCardsRef.current : undefined;
    if (cards) pendingCardsRef.current = [];
    setMessages(prev => [
      ...prev,
      { id: crypto.randomUUID(), role, text, timestamp: new Date(), cards },
    ]);
  }, []);

  const toggleMute = useCallback(() => {
    setIsMuted(prev => !prev);
  }, []);

  const toggleEnzoMute = useCallback(() => {
    setIsEnzoMuted(prev => !prev);
  }, []);

  // ── Audio Playback Queue ─────────────────────────────────

  const playNextRef = useRef<() => void>(() => {});

  const playNextAudio = useCallback(() => {
    if (audioQueueRef.current.length === 0) {
      isPlayingRef.current = false;

      // More sentences are still coming from the backend → stay in "speaking"
      // (and keep the barge-in listener running) instead of flipping to listening.
      if (speechActiveRef.current) return;
      resumeMusic();

      // If sleep was pending and we finished playing farewell, close everything
      if (sleepPendingRef.current) {
        sleepPendingRef.current = false;
        // Close WebSocket
        if (wsRef.current) {
          wsRef.current.close();
          wsRef.current = null;
        }
        setState('idle');
        pushMessage('system', 'Enzo is in standby. Say "Wake up Enzo" to reconnect.');
        // Restart wake-word listening
        startWakeWordListening();
        return;
      }

      // Resume listening after playback (in the app wait a moment so the mic doesn't catch the tail of Enzo's voice)
      voiceBargeStopRef.current?.();
      audioEndedAtRef.current = Date.now();
      if (stateRef.current !== 'idle') {
        setState('listening');
        stateRef.current = 'listening';
        if (isNative) {
          if (listenTimerRef.current) clearTimeout(listenTimerRef.current);
          listenTimerRef.current = setTimeout(() => {
            if (stateRef.current === 'listening' && !isPlayingRef.current && !speechActiveRef.current) startActiveListening();
          }, 450);
        } else {
          startActiveListening();
        }
      }
      return;
    }

    isPlayingRef.current = true;
    const blob = audioQueueRef.current.shift()!;
    if (stateRef.current !== 'speaking') {
      setState('speaking');
      stateRef.current = 'speaking';
      // Listen for the user talking over Enzo (barge-in) instead of going deaf. On Android, opening the
      // microphone WHILE audio plays mutes the start of it, so wait (at most 1.2 s) until the mic is open.
      const started = bargeInStartRef.current?.();
      if (started && typeof (started as Promise<void>).then === 'function') {
        let go = false;
        const play = () => { if (!go) { go = true; if (isPlayingRef.current) playBlob(blob); } };   // not if interrupted meanwhile
        (started as Promise<void>).then(play, play);
        setTimeout(play, 1200);
        return;
      }
    }
    playBlob(blob);
  }, [pushMessage]);

  const playBlob = useCallback((blob: Blob) => {
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    currentAudioRef.current = audio;
    audio.muted = isEnzoMutedRef.current;

    audio.onended = () => {
      URL.revokeObjectURL(url);
      currentAudioRef.current = null;
      playNextRef.current();
    };

    audio.onerror = () => {
      console.error('Audio object error:', audio.error);
      pushMessage('system', 'Audio decoding error. Could not play the response.');
      URL.revokeObjectURL(url);
      playNextRef.current();
    };

    audio.play().catch((err) => {
      console.error('Audio playback failed:', err);
      if (err.name === 'AbortError') {
        // play() interrupted by the browser (e.g. Enzo muted / app in background) - harmless
      } else if (err.name === 'NotAllowedError') {
        pushMessage('system', 'Audio playback blocked by your browser. Please click anywhere on the page or tap the ring to enable audio.');
      } else {
        pushMessage('system', 'Audio playback error: ' + err.message);
      }
      URL.revokeObjectURL(url);
      currentAudioRef.current = null;
      playNextRef.current();
    });
  }, [pushMessage]);

  playNextRef.current = playNextAudio;

  const enqueueAudio = useCallback((blob: Blob) => {
    audioQueueRef.current.push(blob);
    if (!isPlayingRef.current) {
      playNextAudio();
    }
  }, [playNextAudio]);

  // ── Speech Recognition ───────────────────────────────────

  const stopRecognition = useCallback(() => {
    if (recognitionRef.current) {
      try {
        recognitionRef.current.onresult = null;
        recognitionRef.current.onend = null;
        recognitionRef.current.onerror = null;
        recognitionRef.current.abort();
      } catch (_) {}
      recognitionRef.current = null;
    }
    setInterimText('');
  }, []);

  const startActiveListening = useCallback(() => {
    if (isMutedRef.current) { stopRecognition(); return; }   // muted = the microphone is closed, not just ignored
    if (!SpeechRecognition) {
      pushMessage('system', 'Speech recognition not supported in this browser. Use Chrome.');
      return;
    }

    stopRecognition();

    // Android app: Enzo's own recognizer (the server's Whisper, much better than the phone's); the phone's as backup.
    const Rec: any = isNative && serverSttEnabled() ? ServerSpeechRecognition : SpeechRecognition;
    const recognition = new Rec();
    recognition.lang = speechLangRef.current;
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;
    if (Rec === ServerSpeechRecognition) {
      recognition.onactivity = (s: 'hearing' | 'transcribing') => setInterimText(s === 'hearing' ? '...' : 'transcribing...');
    }

    // Chrome (especially in Hebrew) can take several seconds to "finalize" a
    // sentence after you stop talking. If the words stop changing for
    // SILENCE_MS, send what we heard right away instead of waiting.
    const SILENCE_MS = 1200;
    let silenceTimer: ReturnType<typeof setTimeout> | null = null;
    let sentEarly = '';   // text already sent by the timer (ignore Chrome's late final for it)

    const send = (text: string) => {
      if (text && wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ text }));
        setState('processing');
        stateRef.current = 'processing';
        setInterimText('');
        if (isNative) setTimeout(stopRecognition, 0);  // don't listen while Enzo thinks and answers
      }
    };

    // The phone may still catch Enzo's own words: drop results that mostly repeat his last reply.
    const wordsOf = (t: string) => t.toLowerCase().replace(/[^a-z0-9'֐-׿ ]/g, ' ').split(/\s+/).filter(Boolean);
    const looksLikeEcho = (heard: string) => {
      if (Date.now() - audioEndedAtRef.current > 6000) return false;
      const spoken = new Set(wordsOf(lastEnzoTextRef.current));
      const h = wordsOf(heard);
      return h.length > 0 && spoken.size > 0 && h.filter(w => spoken.has(w)).length / h.length >= 0.7;
    };

    recognition.onresult = (event: any) => {
      if (isMutedRef.current) return; // Ignore audio processing if muted
      if (isNative && (stateRef.current === 'speaking' || stateRef.current === 'processing' || isPlayingRef.current)) return;
      if (isNative && event.results[event.results.length - 1]?.[0] && looksLikeEcho(event.results[event.results.length - 1][0].transcript)) { setInterimText(''); return; }

      let interim = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const transcript = event.results[i][0].transcript;
        if (event.results[i].isFinal) {
          if (silenceTimer) { clearTimeout(silenceTimer); silenceTimer = null; }
          const text = transcript.trim();
          if (sentEarly) {           // already sent this utterance on silence
            sentEarly = '';
            setInterimText('');
            continue;
          }
          send(text);
        } else {
          interim += transcript;
        }
      }
      if (interim) {
        setInterimText(interim);
        if (silenceTimer) clearTimeout(silenceTimer);
        const pending = interim.trim();
        silenceTimer = setTimeout(() => {
          silenceTimer = null;
          if (pending && stateRef.current !== 'processing') {
            sentEarly = pending;
            send(pending);
          }
        }, SILENCE_MS);
      }
    };

    recognition.onend = () => {
      // Auto-restart if we should still be listening
      if (
        !micDeniedRef.current && (
        stateRef.current === 'listening' ||
        stateRef.current === 'connected' ||
        (stateRef.current === 'processing' && !isNative))
      ) {
        try {
          recognition.start();
        } catch (_) {}
      }
    };

    recognition.onerror = (e: any) => {
      if (e.error === 'aborted' || e.error === 'no-speech') return;
      if (Rec === ServerSpeechRecognition && e.error === 'network') {
        // The server's recognizer is unreachable: use the phone's own for a while and tell the user once.
        pushMessage('system', "Enzo's voice recognizer is not answering, using the phone's recognizer for now.");
        setTimeout(() => { if (stateRef.current === 'listening') startActiveListening(); }, 300);
        return;
      }
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        micDeniedRef.current = true;  // stop the restart loop; text input still works
      }
      console.warn('Recognition error:', e.error);
    };

    try {
      recognition.start();
      recognitionRef.current = recognition;
    } catch (_) {}
  }, [stopRecognition, pushMessage]);

  // ── Barge-in: stop Enzo when the user talks over him ────

  /** Stop talking right now: halt audio, drop queued sentences, tell the backend. */
  const interrupt = useCallback((reason = 'tap') => {
    voiceBargeStopRef.current?.();
    if (currentAudioRef.current) {
      try { currentAudioRef.current.pause(); } catch (_) {}
      currentAudioRef.current = null;
    }
    audioQueueRef.current = [];
    isPlayingRef.current = false;
    speechActiveRef.current = false;
    spokenTextRef.current = '';
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'interrupt', reason }));
    }
    if (stateRef.current === 'speaking') {
      setState('listening');
      stateRef.current = 'listening';
    }
  }, []);

  const startVoiceBargeIn = useCallback(async () => {
    voiceBargeStopRef.current?.();
    if (isMutedRef.current || !navigator.mediaDevices?.getUserMedia) return;
    let stream: MediaStream | null = null;
    let ctx: AudioContext | null = null;
    let timer: ReturnType<typeof setInterval> | null = null;
    const stop = () => {
      if (timer) { clearInterval(timer); timer = null; }
      try { stream?.getTracks().forEach(t => t.stop()); } catch (_) {}
      try { void ctx?.close(); } catch (_) {}
      stream = null; ctx = null;
      if (voiceBargeStopRef.current === stop) voiceBargeStopRef.current = null;
    };
    voiceBargeStopRef.current = stop;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: false },
      });
      if (voiceBargeStopRef.current !== stop) { stop(); return; }   // playback ended while we were starting
      ctx = new AudioContext();
      const src = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 1024;
      src.connect(analyser);
      const buf = new Float32Array(analyser.fftSize);
      const startedAt = Date.now();
      let baseline = 0.01;   // how loud Enzo's own voice still is after echo cancellation
      let loud = 0;          // ms of sustained "someone is talking"
      timer = setInterval(() => {
        analyser.getFloatTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
        const rms = Math.sqrt(sum / buf.length);
        if (Date.now() - startedAt < 700) { baseline = Math.max(baseline, rms); return; }  // learn the echo level first
        const threshold = Math.max(0.045, baseline * 2.6);
        if (rms > threshold) loud += 50;
        else {
          loud = Math.max(0, loud - 30);
          baseline = baseline * 0.97 + rms * 0.03;     // follow slow changes (Enzo's volume, room)
        }
        if (loud >= 300) {                              // ~0.3 s of real speech: he stops, you talk
          console.log('✋ Voice barge-in', { rms, threshold });
          stop();
          interrupt(`voice loudness rms=${rms.toFixed(3)} threshold=${threshold.toFixed(3)}`);
          startActiveListening();
        }
      }, 50);
    } catch (e) {
      console.warn('Voice barge-in unavailable:', e);
      stop();
    }
  }, [interrupt, startActiveListening]);

  const startBargeInListening = useCallback(() => {
    if (isNative) {
      // The phone's speech recognizer would transcribe Enzo's own voice, so it stays off while he talks.
      // Instead an echo-cancelled microphone only measures LOUDNESS: if you talk over him he stops and listens.
      if (listenTimerRef.current) { clearTimeout(listenTimerRef.current); listenTimerRef.current = null; }
      stopRecognition();
      return startVoiceBargeIn();      // the caller waits for it, so the mic is open BEFORE the voice starts
    }
    if (!SpeechRecognition || micDeniedRef.current || isMutedRef.current) return;
    stopRecognition();

    const STOP_WORDS = /\b(stop|wait|enough|quiet|hold on|shut up|cancel|never ?mind)\b/i;
    const words = (s: string) => s.toLowerCase().replace(/[^a-z0-9'֐-׿ ]/g, ' ').split(/\s+/).filter(Boolean);
    // Our own voice coming back through the mic: most heard words are in what Enzo is saying.
    const isEcho = (heard: string) => {
      const spoken = new Set(words(spokenTextRef.current));
      const h = words(heard);
      if (!h.length || !spoken.size) return false;
      return h.filter(w => spoken.has(w)).length / h.length >= 0.6;
    };

    let barged = false;
    const recognition = new SpeechRecognition();
    recognition.lang = speechLangRef.current;
    recognition.continuous = true;
    recognition.interimResults = true;

    recognition.onresult = (event: any) => {
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const text: string = event.results[i][0].transcript.trim();
        const isFinal = event.results[i].isFinal;
        if (!text) continue;
        if (!barged) {
          const wantsToTalk = STOP_WORDS.test(text) || words(text).length >= 3;
          if (!wantsToTalk || isEcho(text)) continue;
          barged = true;
          console.log('✋ Barge-in:', text);
          interrupt(`heard: ${text}`);
        }
        if (isFinal) {
          // Pure "stop"/"wait" just silences Enzo; anything else is a new request.
          const isOnlyStop = STOP_WORDS.test(text) && words(text).length <= 3;
          if (!isOnlyStop && wsRef.current?.readyState === WebSocket.OPEN) {
            wsRef.current.send(JSON.stringify({ text }));
            setState('processing');
          }
          setInterimText('');
          try { recognition.stop(); } catch (_) {}
          return;
        } else {
          setInterimText(text);
        }
      }
    };

    recognition.onend = () => {
      if (recognitionRef.current !== recognition) return;
      recognitionRef.current = null;
      // Still talking → keep listening for barge-in; otherwise back to normal listening.
      if (!barged && (speechActiveRef.current || isPlayingRef.current)) {
        try { recognition.start(); recognitionRef.current = recognition; } catch (_) {}
      } else if (stateRef.current !== 'idle' && stateRef.current !== 'processing') {
        startActiveListening();
      }
    };
    recognition.onerror = (e: any) => {
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') micDeniedRef.current = true;
    };

    try {
      recognition.start();
      recognitionRef.current = recognition;
    } catch (_) {}
  }, [stopRecognition, startActiveListening, interrupt, startVoiceBargeIn]);

  bargeInStartRef.current = startBargeInListening;

  const startWakeWordListening = useCallback(() => {
    if (!SpeechRecognition || isNative || isMutedRef.current) return;  // app: tap the orb (Android's recognizer beeps on every restart)

    stopRecognition();

    const recognition = new SpeechRecognition();
    recognition.lang = speechLangRef.current;
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.maxAlternatives = 4;

    recognition.onresult = (event: any) => {
      for (let i = event.resultIndex; i < event.results.length; i++) {
        if (event.results[i].isFinal) {
          // Check all alternatives for the wake phrase
          for (let j = 0; j < event.results[i].length; j++) {
            const transcript = event.results[i][j].transcript.toLowerCase().trim();
            if (WAKE_PHRASES.some(wake => transcript.includes(wake))) {
              console.log('🎤 Wake word detected:', transcript);
              stopRecognition();
              connectToBackend();
              return;
            } 
          }
        }
      }
    };

    recognition.onend = () => {
      // Auto-restart wake word listening (but not if the mic is blocked — that loops forever)
      if (stateRef.current === 'idle' && !micDeniedRef.current) {
        try {
          recognition.start();
        } catch (_) {}
      }
    };

    recognition.onerror = (e: any) => {
      if (e.error === 'aborted' || e.error === 'no-speech') return;
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        if (!micDeniedRef.current) {
          micDeniedRef.current = true;
          pushMessage('system', 'Microphone is blocked — tap the ring to connect and type instead, or allow microphone access and reload.');
        }
        return;
      }
      // Retry on transient errors
      if (stateRef.current === 'idle') {
        setTimeout(() => {
          if (stateRef.current === 'idle') startWakeWordListening();
        }, 1000);
      }
    };

    try {
      recognition.start();
      recognitionRef.current = recognition;
    } catch (_) {}
  }, [stopRecognition, pushMessage]);

  // ── WebSocket Connection ─────────────────────────────────

  const connectToBackend = useCallback(() => {
    if (wsRef.current) return;

    setState('connecting');
    if (reconnectAttemptsRef.current === 0) pushMessage('system', 'Connecting to Enzo...');

    const ws = new WebSocket(wsUrl());
    wsRef.current = ws;

    ws.onopen = () => {
      // First message must authenticate, or the backend closes the socket.
      ws.send(JSON.stringify({ type: 'auth', token: getToken() }));
      sendSessionContext(ws);
      if (reconnectAttemptsRef.current > 0) pushMessage('system', 'Reconnected.');
      reconnectAttemptsRef.current = 0;
      console.log('✅ WebSocket connected');
      setState('connected');
      pushMessage('system', 'Connected. Listening...');
      if (pendingTextRef.current) {
        ws.send(JSON.stringify({ text: pendingTextRef.current }));
        pendingTextRef.current = '';
        setState('processing');
        return;
      }
      // Give a small delay for the greeting audio to arrive before starting recognition
      setTimeout(() => {
        if (stateRef.current === 'connected') {
          setState('listening');
          startActiveListening();
        }
      }, 500);
    };

    ws.onmessage = (event: MessageEvent) => {
      if (typeof event.data === 'string') {
        // JSON message from backend
        try {
          const msg = JSON.parse(event.data);

          if (msg.type === 'activity' && typeof msg.tool === 'string') {
            setActivity(msg.tool);
          } else if (msg.type === 'card' && typeof msg.kind === 'string' && msg.data && typeof msg.data === 'object') {
            pendingCardsRef.current = [...pendingCardsRef.current.filter((c) => c.kind !== msg.kind), { kind: msg.kind, data: msg.data }];
          } else if (msg.type === 'transcript') {
            setActivity('');
            if (msg.role === 'user') {
              // Defensive parsing to fix the stringified JSON display bug
              let displayText = msg.text;
              try {
                const parsedText = JSON.parse(msg.text);
                if (parsedText.text) displayText = parsedText.text;
              } catch (e) {
                // If it fails to parse, it means it's normal text, so we leave it as is
              }
              pendingCardsRef.current = [];   // a new turn: cards from an unanswered one are stale
              pushMessage('user', displayText);
            } else if (msg.role === 'sharp') {
              pushMessage('sharp', msg.text);
              lastEnzoTextRef.current = msg.text;
              // App: if no voice reply comes (Enzo muted / text-only), go back to listening by ourselves.
              if (isNative) {
                if (listenTimerRef.current) clearTimeout(listenTimerRef.current);
                listenTimerRef.current = setTimeout(() => {
                  if (stateRef.current === 'processing' && !isPlayingRef.current && !speechActiveRef.current) {
                    setState('listening'); stateRef.current = 'listening'; startActiveListening();
                  }
                }, 6000);
              }
            }
          } else if (msg.type === 'speech_start') {
            speechActiveRef.current = true;
            spokenTextRef.current = '';
          } else if (msg.type === 'speech_chunk' && typeof msg.text === 'string') {
            spokenTextRef.current += ' ' + msg.text;
          } else if (msg.type === 'speech_end') {
            speechActiveRef.current = false;
            resumeMusicRef.current = !!msg.resume_music;
            if (!isPlayingRef.current && audioQueueRef.current.length === 0) resumeMusic();
            // Last sentence already finished playing → go back to listening now.
            if (!isPlayingRef.current && audioQueueRef.current.length === 0 && stateRef.current === 'speaking') {
              setState('listening');
              stateRef.current = 'listening';
              startActiveListening();
            }
          } else if (msg.type === 'device_action') {
            // Enzo asks the phone to do something (e.g. open the dialer for a contact)
            if (isNative) void runDeviceAction(msg, (t) => pushMessage('system', t), (o) => { if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(JSON.stringify(o)); });
            else if (msg.action !== 'notify') pushMessage('system', 'That action needs the Enzo Android app.');
          } else if (msg.type === 'confirm' && typeof msg.text === 'string') {
            // Enzo asks before a risky action — show Yes / Always / No
            setPendingConfirm({ id: String(msg.id ?? ''), text: msg.text });
            pushMessage('sharp', msg.text);
          } else if (msg.type === 'status' && typeof msg.text === 'string') {
            // e.g. "Thinking harder..." when the Claude big brain takes over
            pushMessage('system', msg.text);
          } else if (msg.type === 'command' && msg.action === 'sleep') {
            // Mark sleep pending — we'll close after playing farewell audio
            sleepPendingRef.current = true;
          }
        } catch (e) {
          console.warn('Failed to parse message:', e);
        }
      } else if (event.data instanceof Blob) {
        // Binary MP3 audio from backend
        // We recreate the Blob with an explicit MIME type in case the browser needs it
        const audioBlob = new Blob([event.data], { type: 'audio/mpeg' });
        enqueueAudio(audioBlob);
      }
    };

    ws.onclose = (event: CloseEvent) => {
      console.log('🔌 WebSocket closed', event.code);
      wsRef.current = null;
      if (manualCloseRef.current || sleepPendingRef.current) {
        manualCloseRef.current = false;
        return;
      }
      if (event.code === 4401) {
        setState('idle');
        clearToken();
        reconnectAttemptsRef.current = 0;
        pushMessage('system', 'Please log in again (signed out, expired, or too many tries).');
        onAuthFailedRef.current?.();
        return;
      }
      // Unexpected drop (backend restarted, Wi-Fi blip) → reconnect by itself.
      if (reconnectAttemptsRef.current < 6) {
        const attempt = ++reconnectAttemptsRef.current;
        const delay = Math.min(1000 * 2 ** (attempt - 1), 15000);
        if (attempt === 1) pushMessage('system', 'Connection lost — reconnecting...');
        setState('connecting');
        setTimeout(() => connectRef.current?.(), delay);
        return;
      }
      reconnectAttemptsRef.current = 0;
      setState('idle');
      pushMessage('system', 'Could not reconnect. Tap the ring or say "Wake up Enzo" to try again.');
      startWakeWordListening();
    };

    ws.onerror = (err) => {
      console.error('WebSocket error:', err);
      // While auto-reconnecting, onclose handles messaging — don't spam the chat.
      if (reconnectAttemptsRef.current === 0) pushMessage('system', 'Connection error. Please ensure the backend is running.');
    };
  }, [pushMessage, startActiveListening, enqueueAudio, startWakeWordListening]);

  connectRef.current = connectToBackend;

  // ── Public API ───────────────────────────────────────────

  const connect = useCallback(() => {
    connectToBackend();
  }, [connectToBackend]);

  const sendTextMsg = useCallback((text: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      if (stateRef.current === 'speaking' || isPlayingRef.current) interrupt('answered');   // his answer cuts Enzo off
      wsRef.current.send(JSON.stringify({ text }));
      setState('processing');
      setPendingConfirm(null);  // a typed/spoken "yes"/"no" also answers a pending question
    } else if (!wsRef.current || stateRef.current === 'idle') {
      pendingTextRef.current = text;   // not connected yet: connect, then send it
      connectRef.current?.();
    }
  }, [interrupt]);

  /** The "Set up" button: Enzo starts getting to know you (people are chosen from the phone's contacts). */
  const startSetup = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'command', action: 'setup' }));
    }
  }, []);

  /** Thumbs under Enzo's latest reply: the server records it on that turn, and Enzo learns from it. */
  const sendFeedback = useCallback((rating: 'good' | 'bad') => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'feedback', rating }));
    }
  }, []);

  const answerConfirm = useCallback((answer: 'yes' | 'always' | 'no') => {
    const pending = pendingConfirm;
    if (!pending || wsRef.current?.readyState !== WebSocket.OPEN) return;
    if (stateRef.current === 'speaking' || isPlayingRef.current) interrupt('answered');
    wsRef.current.send(JSON.stringify({ type: 'confirm_reply', id: pending.id, answer }));
    pushMessage('user', answer === 'always' ? 'Yes, always' : answer === 'yes' ? 'Yes' : 'No');
    setPendingConfirm(null);
    setState('processing');
  }, [pendingConfirm, pushMessage, interrupt]);

  const disconnect = useCallback(() => {
    voiceBargeStopRef.current?.();
    sleepPendingRef.current = false;
    manualCloseRef.current = true;     // don't auto-reconnect after a user disconnect
    reconnectAttemptsRef.current = 0;
    stopRecognition();
    audioQueueRef.current = [];
    isPlayingRef.current = false;
    setIsMuted(false); // Reset mute state on disconnect
    isMutedRef.current = false;
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setState('idle');
    pushMessage('system', 'Disconnected. Say "Wake up Enzo" to reconnect.');
    startWakeWordListening();
  }, [stopRecognition, pushMessage, startWakeWordListening]);

  // ── Start wake word listening on mount ───────────────────

  useEffect(() => {
    startWakeWordListening();
    return () => {
      stopRecognition();
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, []);

  const toggleSpeechLang = useCallback(() => {
    const next: SpeechLang = speechLangRef.current === 'he-IL' ? 'en-US' : 'he-IL';
    speechLangRef.current = next;
    setSpeechLang(next);
    try { localStorage.setItem(LANG_KEY, next); } catch { /* private mode */ }
    // Restart whichever recognizer is running so the new language applies now.
    if (stateRef.current === 'idle') startWakeWordListening();
    else if (stateRef.current === 'listening' || stateRef.current === 'connected') startActiveListening();
  }, [startWakeWordListening, startActiveListening]);

  // Mute releases the microphone (an open mic keeps Android in call mode: music sounds wrong); unmute opens it again.
  useEffect(() => {
    if (isMuted) {
      voiceBargeStopRef.current?.();
      stopRecognition();
      setTimeout(async () => {   // after the WebView has let go of the mic
        if (!isMutedRef.current) return;
        const leaked = closeAllMics();
        const mode = await normalAudio();
        if (wsRef.current?.readyState === WebSocket.OPEN) {
          wsRef.current.send(JSON.stringify({ type: 'client_log', text: `mute: ${leaked} mic track(s) were still open, audio mode now ${mode}` }));
        }
      }, 600);
    } else if (!recognitionRef.current) {
      if (stateRef.current === 'idle') startWakeWordListening();
      else if (stateRef.current === 'listening' || stateRef.current === 'connected') startActiveListening();
    }
  }, [isMuted, stopRecognition, startWakeWordListening, startActiveListening]);

  return { state, messages, interimText, isMuted, isEnzoMuted, toggleMute, toggleEnzoMute, connect, disconnect, sendTextMsg, pendingConfirm, answerConfirm, interrupt, speechLang, toggleSpeechLang, startSetup, sendFeedback, activity: state === 'processing' ? activity : '' };
}