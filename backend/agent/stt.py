"""
agent/stt.py - speech to text for the Enzo app (a much better recognizer than the phone's built-in one).

The phone sends one utterance (webm/opus or similar). Engines:
  whisper (default): faster-whisper "small" on the NAS CPU (STT_WHISPER_MODEL=base|small|medium), with a vocabulary
     prompt so it writes Enzo, the user, Spotify... correctly. Steady, private, about 1-2 s per sentence.
  gemini: Google's audio understanding. Very accurate (knows the assistant is called Enzo, mixes Hebrew and English)
     but its free tier gets overloaded / out of quota, so it is not the default. STT_ENGINE=gemini-first tries it
     first and falls back to Whisper; STT_ENGINE=gemini uses only it.
STT_VOCAB="extra,words,like,names" adds to the vocabulary.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile

from loguru import logger

BASE_VOCAB = ["Enzo", "Spotify", "WhatsApp", "Telegram", "Gmail"]

_PROMPT = """Transcribe this audio exactly as spoken. Output ONLY the transcript: no quotes, no labels, no comments.
The speaker is the user talking to his personal assistant, which is called Enzo (never write
Enso, Renzo or Endzo for it). He speaks English, Hebrew, or both in one sentence. Write English in Latin
letters and Hebrew in Hebrew letters. Words that may come up: {vocab}.
If there is no clear speech, output nothing."""


def vocabulary() -> list[str]:
    extra = [w.strip() for w in os.getenv("STT_VOCAB", "").split(",") if w.strip()]
    seen, out = set(), []
    try:    # the people he has told Enzo about: Hebrew names are what the recognizer mishears ("Dana" -> "Mstit")
        from shared import memory_store
        extra += [n.split("/")[0].strip() for n in (memory_store.read() or {}).get("people", {})][:30]
    except Exception:
        pass
    for w in BASE_VOCAB + extra:
        if w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out


def clean(text: str) -> str:
    """Strip quotes/labels a model may add, and the 'no speech' placeholders."""
    t = (text or "").strip()
    t = re.sub(r"^(?:transcript|transcription)\s*[:\-]\s*", "", t, flags=re.IGNORECASE)
    t = t.strip(" \"'`")
    if re.fullmatch(r"[\[(<*_ ]*(?:no (?:clear )?speech|silence|inaudible|nothing)[\])>*_. ]*", t, flags=re.IGNORECASE):
        return ""
    return t


def _suffix(mime: str) -> str:
    m = (mime or "").lower()
    return ".mp4" if "mp4" in m or "aac" in m else ".ogg" if "ogg" in m else ".wav" if "wav" in m else ".webm"


async def _gemini(audio: bytes, mime: str) -> str:
    from agent import gemini_brain
    if not gemini_brain.available():
        raise RuntimeError("no Gemini key")
    from google.genai import types
    client = gemini_brain._get_client()
    parts = [types.Part.from_bytes(data=audio, mime_type=(mime or "audio/webm").split(";")[0]),
             _PROMPT.format(vocab=", ".join(vocabulary()))]
    last = None
    for model in [gemini_brain.GEMINI_MODEL] + list(gemini_brain.GEMINI_FALLBACK_MODELS):
        try:
            r = await asyncio.to_thread(
                client.models.generate_content, model=model, contents=parts,
                config=types.GenerateContentConfig(temperature=0.0))
            return r.text or ""
        except Exception as e:
            last = e
            code = getattr(e, "code", None) or getattr(e, "status_code", None)
            if code not in (404, 429, 500, 503, 504):
                break
    raise last or RuntimeError("Gemini transcription failed")


_whisper = None


def whisper_model():
    """Loaded once; the model files are kept in data/models so a redeploy does not download them again."""
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "models")
        os.makedirs(root, exist_ok=True)
        name = os.getenv("STT_WHISPER_MODEL", "small")
        logger.info(f"Loading Whisper {name} for speech recognition...")
        _whisper = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=int(os.getenv("STT_THREADS", "4")),
                                download_root=root)
    return _whisper


def preload() -> None:
    """Load Whisper in the background at start-up so the first sentence is not slow."""
    if os.getenv("STT_ENGINE", "whisper").strip().lower() != "gemini":
        try:
            whisper_model()
        except Exception as e:
            logger.warning(f"Whisper preload failed: {e.__class__.__name__}: {e}")


def _whisper_blocking(audio: bytes, mime: str, lang: str) -> str:
    fd, path = tempfile.mkstemp(suffix=_suffix(mime))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
        segs, _ = whisper_model().transcribe(
            path, language=lang or None, beam_size=int(os.getenv("STT_BEAM", "1")), vad_filter=True,
            initial_prompt="The user talks to his assistant Enzo. " + ", ".join(vocabulary()) + ".")
        return "".join(s.text for s in segs)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def transcribe(audio: bytes, mime: str = "audio/webm", lang: str = "") -> tuple[str, str]:
    """(text, engine). Empty text = nothing was said (or both engines failed)."""
    if not audio or len(audio) < 800:
        return "", "none"
    engine = os.getenv("STT_ENGINE", "whisper").strip().lower()
    if engine in ("gemini-first", "gemini"):
        try:
            return clean(await _gemini(audio, mime)), "gemini"
        except Exception as e:
            logger.warning(f"STT Gemini failed ({e.__class__.__name__}: {str(e)[:120]}) - falling back to Whisper")
            if engine == "gemini":
                return "", "gemini"
    try:
        return clean(await asyncio.to_thread(_whisper_blocking, audio, mime, lang)), "whisper"
    except Exception as e:
        logger.error(f"STT Whisper failed: {e.__class__.__name__}: {e}")
        return "", "none"
