"""
agent/voice.py — S.H.A.R.P. TTS pipeline.

Priority: Kokoro (free, on the NAS) → ElevenLabs → edge-tts fallback
"""

import os
import re
import time
import uuid
import asyncio
from loguru import logger

_SCRIPT_DIR          = os.path.dirname(os.path.abspath(__file__))
_ELEVENLABS_KEY      = os.getenv("ELEVENLABS_API_KEY", "")
_ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
_USE_ELEVENLABS      = bool(_ELEVENLABS_KEY and _ELEVENLABS_VOICE_ID)
_elevenlabs_off_until = 0.0   # after a failure (e.g. no credits) skip ElevenLabs for a while instead of waiting on it every sentence

_KOKORO_URL          = os.getenv("KOKORO_URL", "").rstrip("/")   # Kokoro-FastAPI container (docker-compose.yml), e.g. http://kokoro:8880
_KOKORO_VOICE        = os.getenv("KOKORO_VOICE", "bm_george")
_KOKORO_SPEED        = float(os.getenv("KOKORO_SPEED", "1.0"))   # 1.0 = Kokoro's normal pace; the user found it slow
_kokoro_off_until    = 0.0

logger.info(f"🎙️ TTS: {'Kokoro' if _KOKORO_URL else 'ElevenLabs' if _USE_ELEVENLABS else 'edge-tts'}")


async def _tts_kokoro(text: str) -> bytes | None:
    global _kokoro_off_until
    if not _KOKORO_URL or time.monotonic() < _kokoro_off_until:
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{_KOKORO_URL}/v1/audio/speech", json={
                "model": "kokoro", "input": text, "voice": _KOKORO_VOICE, "response_format": "mp3",
                "speed": _KOKORO_SPEED})
            r.raise_for_status()
            return r.content or None
    except Exception as e:
        logger.warning(f"Kokoro TTS (skipped for 1 min): {e}")
        _kokoro_off_until = time.monotonic() + 60
        return None


async def _tts_elevenlabs(text: str) -> bytes | None:
    global _elevenlabs_off_until
    if not _USE_ELEVENLABS or time.monotonic() < _elevenlabs_off_until:
        return None
    try:
        from elevenlabs.client import ElevenLabs
        client = ElevenLabs(api_key=_ELEVENLABS_KEY)

        def _gen():
            audio = client.text_to_speech.convert(
                text=text,
                voice_id=_ELEVENLABS_VOICE_ID,
                model_id="eleven_multilingual_v2",
            )
            return b"".join(audio)

        return await asyncio.to_thread(_gen)
    except Exception as e:
        logger.warning(f"ElevenLabs TTS (skipped for 10 min): {e}")
        _elevenlabs_off_until = time.monotonic() + 600
        return None


async def _tts_edge(text: str) -> bytes | None:
    import edge_tts
    voice    = os.getenv("TTS_VOICE", "en-GB-RyanNeural")
    filename = os.path.join(_SCRIPT_DIR, f"reply_{uuid.uuid4().hex}.mp3")
    try:
        await edge_tts.Communicate(text, voice).save(filename)
        with open(filename, "rb") as f:
            return f.read()
    except Exception as e:
        logger.warning(f"edge-tts: {e}")
        return None
    finally:
        if os.path.exists(filename):
            os.remove(filename)


_VOICE_EN = os.getenv("TTS_VOICE", "en-GB-RyanNeural")
_VOICE_HE = os.getenv("TTS_VOICE_HE", "he-IL-AvriNeural")


async def _tts_edge_voice(text: str, voice: str) -> bytes | None:
    import edge_tts
    try:
        out = b""
        async for c in edge_tts.Communicate(text, voice).stream():
            if c["type"] == "audio":
                out += c["data"]
        return out or None
    except Exception as e:
        logger.warning(f"edge-tts ({voice}): {e}")
        return None


async def _tts_mixed(text: str) -> bytes | None:
    """Hebrew parts → Hebrew voice, English parts → English voice, joined into one MP3
    (same encoder/format for every part, so the pieces play back-to-back)."""
    from agent.lang import split_lang_segments
    parts = []
    for lang, seg in split_lang_segments(text):
        voice = _VOICE_HE if lang == "he" else _VOICE_EN
        audio = await _tts_edge_voice(seg, voice) or await _tts_edge_voice(seg, voice)   # one retry, see synthesize
        if audio:
            parts.append(audio)
    return b"".join(parts) or None


async def synthesize(text: str, mixed: bool | None = None) -> bytes | None:
    """
    Returns audio bytes or None.
    • Text with Hebrew (or mixed=True) → Microsoft voices: Hebrew in he-IL-AvriNeural,
      English in TTS_VOICE — so Hebrew event names inside English sentences are read correctly.
    • Plain English → Kokoro, then ElevenLabs, then edge-tts.
    """
    from agent.lang import has_hebrew, strip_emoji
    clean = strip_emoji(re.sub(r"[*_`#>~]", "", text)).strip()
    if not clean:
        return None
    if mixed or (mixed is None and has_hebrew(clean)):
        return await _tts_mixed(clean)
    audio = await _tts_kokoro(clean) or await _tts_elevenlabs(clean)
    for attempt in range(3):          # edge-tts now and then returns "No audio was received"; a retry gets it
        if audio is not None:
            break
        if attempt:
            await asyncio.sleep(0.3 * attempt)
        audio = await _tts_edge(clean)
    return audio


_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+|\n+")


def split_for_speech(text: str, max_chars: int = 240) -> list[str]:
    """
    Split a reply into speakable chunks. The FIRST chunk is a single sentence so
    audio starts as fast as possible; later sentences are grouped (fewer TTS calls).
    """
    parts = [p.strip() for p in _SENTENCE_RE.split(re.sub(r"[*_`#>~]", "", text or "")) if p.strip()]
    if not parts:
        return []
    chunks = [parts[0]]
    buf = ""
    for p in parts[1:]:
        if buf and len(buf) + 1 + len(p) > max_chars:
            chunks.append(buf)
            buf = p
        else:
            buf = f"{buf} {p}".strip()
    if buf:
        chunks.append(buf)
    return chunks


async def _music_playing() -> bool:
    """Is Spotify playing on his phone? Android pauses it while Enzo talks, so the app presses play again afterwards."""
    try:
        from agent import spotify
        from agent.context import session
        if session().app != "android" or not spotify.connected():
            return False
        return bool((await asyncio.wait_for(spotify._now_playing(), 3)).get("is_playing"))
    except Exception:
        return False


async def speak_streaming(websocket, text: str):
    """
    Speak a reply sentence by sentence: chunk N+1 is synthesized while chunk N is
    sent/played. Protocol to the client:
      {"type": "speech_start"} → ({"type": "speech_chunk", "text"} + MP3 bytes)* → {"type": "speech_end"}
    Cancel the task to stop talking (barge-in).
    """
    from agent.lang import has_hebrew
    chunks = split_for_speech(text)
    if not chunks:
        return
    # Any Hebrew in the reply → use the Microsoft voices for every sentence, so the
    # voice doesn't jump between Kokoro / ElevenLabs and Microsoft in the middle of an answer.
    mixed = has_hebrew(text)
    # Synthesize up to 3 sentences at once, so a long sentence after a short one is ready in time (no gap).
    gate = asyncio.Semaphore(3)

    async def _synth(chunk: str):
        async with gate:
            return await synthesize(chunk, mixed)

    tasks: list[asyncio.Task] = []
    music = asyncio.create_task(_music_playing())
    try:
        await websocket.send_json({"type": "speech_start"})
        tasks = [asyncio.create_task(_synth(c)) for c in chunks]
        for chunk, task in zip(chunks, tasks):
            audio = await task
            if audio:
                await websocket.send_json({"type": "speech_chunk", "text": chunk})
                await websocket.send_bytes(audio)
    except asyncio.CancelledError:
        logger.info("🔇 Speech interrupted")
        raise
    except Exception as e:
        logger.warning(f"Streaming speech: {e}")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        resume = music.done() and not music.cancelled() and music.exception() is None and music.result()
        music.cancel()
        try:
            await websocket.send_json({"type": "speech_end", **({"resume_music": True} if resume else {})})
        except Exception:
            pass


async def generate_and_send_voice(websocket, text: str):
    """Synthesizes text and sends the audio bytes to the WebSocket."""
    audio = await synthesize(text)
    if audio:
        try:
            await websocket.send_bytes(audio)
        except Exception as e:
            logger.warning(f"Send audio: {e}")
