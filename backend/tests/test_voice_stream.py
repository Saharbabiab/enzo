"""Streaming speech: sentences are synthesized ahead (no gap after a short first one) and sent in order."""

import asyncio

from agent import voice


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, obj):
        self.sent.append(obj)

    async def send_bytes(self, data):
        self.sent.append(data)


def test_chunks_synthesized_ahead_and_sent_in_order(monkeypatch):
    running, peak = 0, 0

    async def fake_synth(text, mixed=None):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05 if text.startswith("Long") else 0.01)
        running -= 1
        return text.encode()

    monkeypatch.setattr(voice, "synthesize", fake_synth)
    ws = FakeWS()
    text = "Hi. " + " ".join(f"Long sentence number {i} with quite a few words in it to fill the chunk up nicely." for i in range(12))
    asyncio.run(voice.speak_streaming(ws, text))
    chunks = [m["text"] for m in ws.sent if isinstance(m, dict) and m.get("type") == "speech_chunk"]
    audio = [m for m in ws.sent if isinstance(m, bytes)]
    assert chunks == voice.split_for_speech(text) and audio == [c.encode() for c in chunks]
    assert ws.sent[0] == {"type": "speech_start"} and ws.sent[-1] == {"type": "speech_end"}
    assert 1 < peak <= 3


def test_elevenlabs_skipped_after_failure(monkeypatch):
    monkeypatch.setattr(voice, "_USE_ELEVENLABS", True)
    monkeypatch.setattr(voice, "_elevenlabs_off_until", 0.0)
    calls = []

    class Boom:
        def __init__(self, **kw):
            calls.append(1)
            raise RuntimeError("quota_exceeded")

    import sys, types
    mod = types.SimpleNamespace(ElevenLabs=Boom)
    monkeypatch.setitem(sys.modules, "elevenlabs.client", mod)
    assert asyncio.run(voice._tts_elevenlabs("hello")) is None
    assert asyncio.run(voice._tts_elevenlabs("hello")) is None
    assert calls == [1]


def test_edge_retried_when_no_audio(monkeypatch):
    monkeypatch.setattr(voice, "_USE_ELEVENLABS", False)
    results = iter([None, b"mp3"])

    async def flaky(text):
        return next(results)

    monkeypatch.setattr(voice, "_tts_edge", flaky)
    assert asyncio.run(voice.synthesize("Here's your morning briefing, sir.")) == b"mp3"


def test_speech_end_asks_app_to_resume_music(monkeypatch):
    async def fake_synth(text, mixed=None):
        await asyncio.sleep(0.01)
        return b"x"

    async def playing():
        return True

    monkeypatch.setattr(voice, "synthesize", fake_synth)
    monkeypatch.setattr(voice, "_music_playing", playing)
    ws = FakeWS()
    asyncio.run(voice.speak_streaming(ws, "Hello there. Music is back."))
    assert ws.sent[-1] == {"type": "speech_end", "resume_music": True}


def test_kokoro_first_then_falls_back_and_rests_after_a_failure(monkeypatch):
    import httpx
    calls, down = [], [False]
    real = httpx.AsyncClient

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(500) if down[0] else httpx.Response(200, content=b"kokoro-mp3")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(voice, "_KOKORO_URL", "http://kokoro:8880")
    monkeypatch.setattr(voice, "_kokoro_off_until", 0.0)
    monkeypatch.setattr(voice, "_USE_ELEVENLABS", False)
    async def edge(text): return b"edge-mp3"
    monkeypatch.setattr(voice, "_tts_edge", edge)
    assert asyncio.run(voice.synthesize("Good morning, sir.")) == b"kokoro-mp3"
    down[0] = True
    assert asyncio.run(voice.synthesize("Good morning, sir.")) == b"edge-mp3"       # Kokoro down: the Microsoft voice
    assert asyncio.run(voice.synthesize("Good morning, sir.")) == b"edge-mp3"
    assert calls == ["/v1/audio/speech"] * 2                                        # not asked again for a minute
