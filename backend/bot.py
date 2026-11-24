"""
bot.py — S.H.A.R.P. v10  (OpenClaw-style autonomous reasoning)

Architecture:
  - gemma4:e2b (via Ollama) — all reasoning, planning, conversation
  - qwen2.5-coder:7b        — code generation only

Think loop per message:
  1. Fast-intent (regex, zero LLM)
  2. think_and_plan() — task decomposition
  3. execute_steps()  — run each task, collect results
  4. Self-critique loop — judge quality, regenerate until score ≥ 4 (max 3 attempts)
  5. Background: extract_and_save(), reflect_and_learn(), reflect_and_improve()
"""

import os
import re
import json
import asyncio
from loguru import logger
from dotenv import load_dotenv
from faster_whisper import WhisperModel

from scheduler import register_websocket, unregister_websocket

from agent.responder import reflect_and_improve, reflect_and_learn
from agent.voice     import generate_and_send_voice, speak_streaming
from agent           import pipeline
from agent.context   import new_session

# "tools" = native tool-calling agent (default); "planner" = old JSON planner loop
AGENT_MODE = os.getenv("AGENT_MODE", "tools").lower()

load_dotenv()

_SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
whisper_model = WhisperModel("base", device="cpu", compute_type="int8")


def _transcribe(audio: bytes) -> str:
    """Blocking Whisper transcription. Uses a unique temp file per call so
    two clients talking at once don't overwrite each other's audio."""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".webm")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
        segs, _ = whisper_model.transcribe(path, language=None)  # auto: Hebrew or English
        return "".join(s.text for s in segs)  # segments are lazy; consume here
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

MAX_STEPS          = 6     # max tool steps per request
MAX_QUALITY_LOOPS  = 3     # max self-critique retries — only runs for tool results
MIN_QUALITY_SCORE  = 4     # threshold: 4+ is required to ensure specific formatting
MAX_HISTORY        = 16    # chat turns to keep

# Conversation history for the web app. Module-level so a page reload or a
# WebSocket reconnect continues the same conversation, and saved to a local
# file (gitignored) so it also survives a backend restart.
_HISTORY_FILE = os.path.join(_SCRIPT_DIR, "data", "web_history.json")
_HISTORY_MAX_AGE_H = float(os.getenv("WEB_HISTORY_MAX_AGE_HOURS", "12"))


def _load_web_history() -> list[dict]:
    try:
        if not os.path.exists(_HISTORY_FILE):
            return []
        import time
        if (time.time() - os.path.getmtime(_HISTORY_FILE)) / 3600 > _HISTORY_MAX_AGE_H:
            return []  # stale conversation — start fresh
        with open(_HISTORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return [m for m in data if isinstance(m, dict) and m.get("role") in ("user", "assistant")][-MAX_HISTORY:]
    except Exception as e:
        logger.warning(f"Could not load web history: {e}")
        return []


def _save_web_history(history: list[dict]) -> None:
    try:
        os.makedirs(os.path.dirname(_HISTORY_FILE), exist_ok=True)
        tmp = _HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(history[-MAX_HISTORY:], f, ensure_ascii=False)
        os.replace(tmp, _HISTORY_FILE)
    except Exception as e:
        logger.warning(f"Could not save web history: {e}")


_WEB_HISTORY: list[dict] = _load_web_history()


def _make_ws_confirm(websocket, sess):
    """
    Build the approval callback for a web client. While the agent waits for an
    answer, the main receive loop is suspended (it's awaiting the agent), so it's
    safe to read the socket here.
    Client → {"type": "confirm_reply", "id": ..., "answer": "yes"|"always"|"no"}
    or plain text/voice "yes" / "no".
    """
    import uuid
    from agent.approvals import parse_answer, APPROVAL_TIMEOUT

    async def confirm(question: str) -> str:
        cid = uuid.uuid4().hex[:8]
        await websocket.send_json({"type": "confirm", "id": cid, "text": question})
        try:
            await generate_and_send_voice(websocket, question)
        except Exception:
            pass
        loop = asyncio.get_running_loop()
        deadline = loop.time() + APPROVAL_TIMEOUT
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await websocket.send_json({"type": "status", "text": "No answer — I didn't do it."})
                return "no"
            try:
                msg = await asyncio.wait_for(websocket.receive(), timeout=remaining)
            except asyncio.TimeoutError:
                continue
            if msg.get("type") == "websocket.disconnect":
                raise ConnectionError("client disconnected during approval")
            if "text" not in msg:
                continue  # ignore audio while waiting
            try:
                data = json.loads(msg["text"])
            except (json.JSONDecodeError, TypeError):
                data = {"text": msg["text"]}
            if not isinstance(data, dict):
                continue
            if data.get("type") == "context":
                sess.update(data)
                continue
            if data.get("type") == "confirm_reply":
                if data.get("id") not in (None, cid):
                    continue
                ans = str(data.get("answer", "no")).lower()
                return ans if ans in ("yes", "always") else "no"
            if data.get("text"):
                await websocket.send_json({"type": "transcript", "role": "user", "text": data["text"]})
                return parse_answer(data["text"])

    return confirm


def _make_ws_ask_device(websocket, sess):
    """Ask the phone app for something and wait for its {"type": "device_result", "id": ...} (e.g. the screen).
    Like confirm: the receive loop is suspended while a tool runs, so reading the socket here is safe.
    Returns the result dict, or None if the app did not answer in time."""
    import uuid

    async def ask(payload: dict, timeout: float = 12.0) -> dict | None:
        rid = uuid.uuid4().hex[:8]
        await websocket.send_json({"type": "device_action", "id": rid, **payload})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (remaining := deadline - loop.time()) > 0:
            try:
                msg = await asyncio.wait_for(websocket.receive(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if msg.get("type") == "websocket.disconnect":
                raise ConnectionError("client disconnected while the phone was answering")
            try:
                data = json.loads(msg.get("text") or "")
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, dict) and data.get("type") == "context":
                sess.update(data)
            elif isinstance(data, dict) and data.get("type") == "device_result" and data.get("id") == rid:
                return data
        return None

    return ask


# "forget our conversation" / "clear the chat history" - wipes the saved web conversation (only when the user asks)
_FORGET_RE = re.compile(
    r"^\s*(?:please\s+)?(?:forget|clear|reset|wipe)\s+(?:(?:our|the|this|my|all)\s+)*"
    r"(?:conversation|chat|chat history|history|everything we (?:said|talked about))\b.*$",
    re.IGNORECASE,
)


async def profile_setup_send_later(sess, action, delay):
    from agent import profile_setup
    await profile_setup.send_later(sess, action, delay)


async def run_bot(websocket):
    # ── Startup ─────────────────────────────────────────────────
    register_websocket(websocket)

    # Memory migration (cleanup old format keys)
    from shared import memory_store
    _migrate_memory_if_needed(memory_store)

    await websocket.send_json({
        "type": "transcript",
        "role": "sharp",
        "text": "Systems online. Awaiting your command, sir.",
    })

    # Shared across reconnects/page reloads (single-user assistant) — see _WEB_HISTORY.
    chat_history:  list[dict] = _WEB_HISTORY
    message_count: int        = 0
    sess = new_session()  # per-connection device / location context
    sess.confirm = _make_ws_confirm(websocket, sess)  # lets risky tools ask "are you sure?"
    sess.send_device = lambda payload: websocket.send_json({"type": "device_action", **payload})  # e.g. open the phone dialer
    sess.ask_device = _make_ws_ask_device(websocket, sess)                                       # e.g. what is on the phone screen
    sess.activity = lambda tool: websocket.send_json({"type": "activity", "tool": tool})        # "Using weather..." in the app
    sess.card = lambda kind, data: websocket.send_json({"type": "card", "kind": kind, "data": data})  # weather / mail / ... card

    async def _ws_status(text: str):
        await websocket.send_json({"type": "status", "text": text})

    # Voice runs as a background task so the loop can still receive an
    # {"type": "interrupt"} (barge-in) or a new message while Enzo is talking.
    speech_task: asyncio.Task | None = None

    def _stop_speech() -> bool:
        nonlocal speech_task
        if speech_task is not None and not speech_task.done():
            speech_task.cancel()
            speech_task = None
            return True
        return False

    try:
        while True:
            # ── Receive message ──────────────────────────────────
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break

            user_text = ""

            if "text" in message:
                try:
                    data = json.loads(message["text"])
                    if isinstance(data, dict) and data.get("type") == "context":
                        if data.get("push_token"):
                            from agent import push
                            push.save_token(data["push_token"])          # notifications reach the phone as pushes
                        sess.update(data)
                        logger.info(f"📍 Session context: {sess.describe()}")
                        from agent import phone_reminders
                        asyncio.create_task(phone_reminders.sync(sess))   # phone app: put reminders on its alarm clock
                        continue
                    if isinstance(data, dict) and data.get("type") == "device_result":
                        if data.get("action") == "reminder_delivered":
                            from agent import phone_reminders
                            note = phone_reminders.delivery_note(data, sess)
                            if note:
                                await websocket.send_json({"type": "transcript", "role": "sharp", "text": note})
                            continue
                        if data.get("action") == "contact_not_found":
                            # The phone found nobody called that: let him pick the person; remember what he wanted to do.
                            who = str(data.get("who") or "").strip()
                            sess.__dict__["pending_pick"] = {"role": who, "pending": data.get("pending") or {}}
                            reply = f"I could not find \"{who}\" in your contacts, sir. Choose them from the list and I will remember."
                            await websocket.send_json({"type": "transcript", "role": "sharp", "text": reply})
                            asyncio.create_task(profile_setup_send_later(sess, {"action": "pick_contact", "role": who, "title": f"Choose {who}"}, 3.0))
                            _stop_speech()
                            speech_task = asyncio.create_task(speak_streaming(websocket, reply))
                            continue
                        if data.get("action") == "contact_picked":
                            from agent import profile_setup
                            reply = profile_setup.handle_pick(data)
                            pend = sess.__dict__.pop("pending_pick", None)
                            if pend and data.get("number") and not data.get("cancelled") and pend.get("pending", {}).get("action"):
                                p = pend["pending"]
                                await sess.send_device({"action": p["action"], "who": data.get("name") or pend.get("role", ""),
                                                        "number": data["number"], "text": p.get("text", ""), "send": p.get("send", False)})
                                reply = f"Got it, sir. {data.get('name') or 'Contact'} is saved as {data.get('role') or 'that person'}. Doing it now."
                            queue = sess.__dict__.get("pick_queue") or []
                            if queue and not (pend and pend.get("pending", {}).get("action")):
                                if data.get("cancelled") or not data.get("number"):
                                    sess.__dict__["pick_queue"] = []
                                    reply = "No problem, sir. Tell me whenever you want to add someone."
                                else:
                                    nxt_role = queue.pop(0)
                                    sess.__dict__["pick_queue"] = queue
                                    reply = f"{reply} Now please choose your {nxt_role} from your contacts."
                                    asyncio.create_task(profile_setup.send_later(
                                        sess, {"action": "pick_contact", "role": nxt_role, "title": f"Choose your {nxt_role}"}, 4.0))
                            elif sess.__dict__.get("setup_active") and not (pend and pend.get("pending", {}).get("action")):
                                if data.get("cancelled") or not data.get("number"):
                                    # He backed out: stop here (nothing is skipped for good); he continues with "let's do a setup".
                                    sess.__dict__["setup_active"] = False
                                    reply = "No problem, sir. Say \"let's do a setup\" whenever you want to carry on."
                                else:
                                    nxt_text, nxt_action = profile_setup.next_step()
                                    if nxt_text.startswith("That is everything"):
                                        sess.__dict__["setup_active"] = False
                                    reply = f"{reply} {nxt_text}"
                                    if nxt_action:
                                        asyncio.create_task(profile_setup.send_later(sess, nxt_action, 4.0))   # after the instruction is spoken
                            await websocket.send_json({"type": "transcript", "role": "sharp", "text": reply})
                            chat_history.append({"role": "assistant", "content": reply})
                            _stop_speech()
                            speech_task = asyncio.create_task(speak_streaming(websocket, reply))
                            continue
                        if data.get("action") == "notifications":
                            from agent import notifications
                            reply = notifications.summarize(data)
                            await websocket.send_json({"type": "transcript", "role": "sharp", "text": reply})
                            chat_history.append({"role": "assistant", "content": reply})
                            _stop_speech()
                            speech_task = asyncio.create_task(speak_streaming(websocket, reply))
                        continue
                    if isinstance(data, dict) and data.get("type") == "command" and data.get("action") == "setup":
                        # The "Set up" button in the app: no guessing, straight to the first step.
                        from agent import profile_setup
                        sess.__dict__["setup_active"] = True
                        step_text, step_action = profile_setup.next_step()
                        if sess.app != "android" and step_action:
                            reply = "The setup needs the Enzo Android app, sir, because you pick people from the phone's contacts."
                            step_action = None
                        else:
                            reply = ("Let's get to know each other, sir. " if step_action or not step_text.startswith("That is everything") else "") + step_text
                        await websocket.send_json({"type": "transcript", "role": "sharp", "text": reply})
                        chat_history.append({"role": "assistant", "content": reply})
                        _stop_speech()
                        speech_task = asyncio.create_task(speak_streaming(websocket, reply))
                        if step_action:
                            asyncio.create_task(profile_setup.send_later(sess, step_action, 5.0))
                        continue
                    if isinstance(data, dict) and data.get("type") == "interrupt":
                        # Recorded even when all the audio was already sent: the phone may still be playing it.
                        _stop_speech()
                        detail = str(data.get("reason") or "")[:120]
                        logger.info(f"✋ Barge-in: stopped talking {detail}")
                        from agent import traces
                        last = (sess.extra.get("last_trace") or {}).get("id")
                        if last and detail != "answered":     # he tapped Yes / an answer: not a complaint about the reply
                            traces.add_signal(last, "interrupted", detail)
                        continue
                    if isinstance(data, dict) and data.get("type") == "client_log":
                        # A short diagnostic line from the app (e.g. what mute closed), so phone problems show in the log.
                        logger.info(f"📱 App: {str(data.get('text', ''))[:300]}")
                        continue
                    if isinstance(data, dict) and data.get("type") == "feedback":
                        # Thumbs up / down under Enzo's latest reply in the app.
                        from agent import traces
                        last = (sess.extra.get("last_trace") or {}).get("id")
                        if last and data.get("rating") in ("good", "bad"):
                            traces.add_signal(last, "feedback_" + data["rating"], str(data.get("note", ""))[:200])
                            logger.info(f"👍👎 Feedback on the last answer: {data['rating']}")
                        continue
                    if isinstance(data, dict):
                        user_text = data.get("text", "")
                    else:
                        user_text = message["text"]
                except (json.JSONDecodeError, TypeError):
                    user_text = message["text"]

            elif "bytes" in message:
                # Run Whisper in a worker thread so the server (heartbeat,
                # other clients) keeps responding while it transcribes.
                user_text = await asyncio.to_thread(_transcribe, message["bytes"])

            user_text = user_text.strip()
            if not user_text:
                continue
            _stop_speech()  # a new request always cuts off the previous answer

            message_count += 1
            logger.info(f"👤 [{message_count}] User: {user_text}")
            await websocket.send_json({"type": "transcript", "role": "user", "text": user_text})

            if _FORGET_RE.match(user_text):
                # The user asked to wipe Enzo's saved conversation (it can get stuck repeating old errors).
                chat_history.clear()
                _save_web_history(chat_history)
                from agent import compaction
                compaction.clear()
                final = "Done, sir. I've forgotten our conversation so far - a clean slate."
                await websocket.send_json({"type": "transcript", "role": "sharp", "text": final})
                await generate_and_send_voice(websocket, final)
                continue
            chat_history.append({"role": "user", "content": user_text})

            # One shared pipeline for every channel (agent/pipeline.py): the model decides, tools act.
            final, context = await pipeline.answer(user_text, chat_history, status=_ws_status)

            # ── 5. Send response ──────────────────────────────────
            logger.info(f"🤖 Enzo: {final}")
            await websocket.send_json({"type": "transcript", "role": "sharp", "text": final})
            chat_history.append({"role": "assistant", "content": final})

            # Trim history
            if len(chat_history) > MAX_HISTORY:
                del chat_history[:-MAX_HISTORY]
            _save_web_history(chat_history)

            # ── 6. Background learning ────────────────────────────
            if "[SEARCH RESULTS" in context:
                asyncio.create_task(reflect_and_learn(user_text, context))

            if message_count % 8 == 0:
                asyncio.create_task(reflect_and_improve(chat_history))

            # ── 7. Voice — sentence by sentence, in the background ─
            speech_task = asyncio.create_task(speak_streaming(websocket, final))

    except Exception as e:
        logger.error(f"Bot error: {e}", exc_info=True)
    finally:
        _stop_speech()
        unregister_websocket(websocket)


# ════════════════════════════════════════════════════════════════
# MEMORY MIGRATION HELPER
# ════════════════════════════════════════════════════════════════
_OLD_KEYS = {
    "favorite_color","favorite_food","favorite_league_champion",
    "favorite_anime_character","hobbies","location","name",
    "favorite_game","favorite_pokemon","favorite_superhero",
    "favorite_hero","favorite_show","favorite_movie",
    "@type","context","user",
    "relation_to_partner",
    "funny","pretty","smart","alarm_enabled",
}

def _migrate_memory_if_needed(store):
    mem   = store.read()
    stale = [
        k for k in mem
        if k not in ("owner","people","preferences","routines","misc","live_rules")
        and (k in _OLD_KEYS or k in mem.get("owner",{}) or
             k in mem.get("preferences",{}) or k in mem.get("people",{}))
    ]
    if stale:
        logger.info(f"🧹 Removing stale keys: {stale}")
        try:
            store.memory.update_one(
                {"_id": "owner_core_memory"},
                {"$unset": {k: "" for k in stale}}
            )
        except Exception as e:
            logger.error(f"Cleanup: {e}")