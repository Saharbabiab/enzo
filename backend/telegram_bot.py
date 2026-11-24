"""
telegram_bot.py — S.H.A.R.P. Telegram interface.

Uses the SAME pipeline as the web app (agent/pipeline.py):
  instant answers → quick intents → memory recall → hybrid agent
  (local model; hard requests → Claude → Gemini)

Telegram extras:
  • Approvals for risky actions as inline Yes / Always / No buttons
  • Share your location (📎 → Location) and Enzo uses it for weather, "near me", ...
  • Voice messages are transcribed with Whisper
  • Only TELEGRAM_CHAT_ID can talk to the bot

Commands:
  /start   — greeting
  /memory  — show what S.H.A.R.P. knows about you
  /clear   — clear your conversation history with this bot
"""

import os
import io
import asyncio
import uuid
import time

from loguru import logger
from agent import traces as _traces  # noqa: F401  (strips secrets from every log line)
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)

from agent import pipeline
from agent.approvals import APPROVAL_TIMEOUT
from agent.context import Session, use_session
from agent.memory import get_profile_summary
from agent.voice import synthesize

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
MAX_HISTORY = 16

# confirm id → Future resolved by the inline-button callback
_pending: dict[str, asyncio.Future] = {}


# ────────────────────────────────────────────────────────────────
# SESSION (per chat: location, device, approvals)
# ────────────────────────────────────────────────────────────────
def _session_for(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Session:
    s = context.chat_data.get("session")
    if s is None:
        s = Session(device="telegram")
        context.chat_data["session"] = s
    s.confirm = _make_confirm(update, context)
    return use_session(s)


def _make_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    async def confirm(question: str) -> str:
        cid = uuid.uuid4().hex[:8]
        fut = asyncio.get_running_loop().create_future()
        _pending[cid] = fut
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes", callback_data=f"ok:{cid}:yes"),
            InlineKeyboardButton("Always", callback_data=f"ok:{cid}:always"),
            InlineKeyboardButton("❌ No", callback_data=f"ok:{cid}:no"),
        ]])
        await context.bot.send_message(chat_id=chat_id, text=f"🔒 {question}", reply_markup=buttons)
        try:
            return await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            await context.bot.send_message(chat_id=chat_id, text="No answer — I didn't do it.")
            return "no"
        finally:
            _pending.pop(cid, None)

    return confirm


async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    owner = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not owner or str(update.effective_chat.id) != owner:
        await q.answer()
        return  # only the owner's chat can approve actions
    try:
        _, cid, answer = (q.data or "").split(":")
    except ValueError:
        await q.answer()
        return
    fut = _pending.get(cid)
    if fut and not fut.done():
        fut.set_result(answer)
        await q.answer("Got it")
        label = {"yes": "✅ Approved", "always": "✅ Approved (always)", "no": "❌ Declined"}.get(answer, answer)
        try:
            await q.edit_message_text(f"{q.message.text}\n\n{label}")
        except Exception:
            pass
    else:
        await q.answer("This question expired.")


# ────────────────────────────────────────────────────────────────
# COMMANDS
# ────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Telegram interface online. Enzo is at your service, sir.\n"
        "Tip: share your location (📎 → Location) so I can answer weather and 'near me' questions."
    )


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = get_profile_summary("what I know about the user")
    await update.message.reply_text(f"🧠 What I know about you:\n\n{profile}")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    from agent import compaction
    compaction.clear()
    await update.message.reply_text("Conversation history cleared, sir.")


# ────────────────────────────────────────────────────────────────
# MESSAGES
# ────────────────────────────────────────────────────────────────
async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    s = _session_for(update, context)
    s.update({"location": {"latitude": loc.latitude, "longitude": loc.longitude,
                           "accuracy": loc.horizontal_accuracy or 0}})
    logger.info(f"📍 [TG] Location shared: {s.describe()}")
    await update.message.reply_text("Got your location, sir. I'll use it for weather and anything nearby.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str | None = None):
    user_text = (text or update.message.text or "").strip()
    if not user_text:
        return

    chat_id = update.effective_chat.id
    logger.info(f"📱 [TG] {chat_id}: {user_text}")
    _session_for(update, context)

    chat_history: list = context.user_data.setdefault("history", [])
    chat_history.append({"role": "user", "content": user_text})

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    async def status(msg: str):
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        await context.bot.send_message(chat_id=chat_id, text=f"⏳ {msg}")

    try:
        final, _ = await pipeline.answer(user_text, chat_history, status=status)
    except Exception as e:
        logger.error(f"[TG] pipeline error: {e.__class__.__name__}: {e}")
        final = "Something went wrong on my side, sir. Try again."

    chat_history.append({"role": "assistant", "content": final})
    if len(chat_history) > MAX_HISTORY:
        context.user_data["history"] = chat_history[-MAX_HISTORY:]

    logger.info(f"🤖 [TG] Enzo: {final}")
    await update.message.reply_text(final)

    if os.getenv("TELEGRAM_VOICE_REPLIES", "true").lower() == "true":
        try:
            audio = await synthesize(final)
            if audio:
                await context.bot.send_audio(
                    chat_id=chat_id, audio=io.BytesIO(audio),
                    filename="enzo_reply.mp3", title="Enzo",
                )
        except Exception as e:
            logger.debug(f"TG voice send: {e}")


_whisper = None


def _transcribe(path: str) -> str:
    global _whisper
    if _whisper is None:  # load once, not per voice message
        from faster_whisper import WhisperModel
        _whisper = WhisperModel("base", device="cpu", compute_type="int8")
    segs, _ = _whisper.transcribe(path, language=None)  # auto: Hebrew or English
    return "".join(s.text for s in segs).strip()


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Transcribe a Telegram voice message using Whisper, then process it."""
    import tempfile

    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    tmp_path = None
    try:
        file = await update.message.voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await file.download_to_drive(tmp_path)
        user_text = await asyncio.to_thread(_transcribe, tmp_path)
        if user_text:
            await update.message.reply_text(f"🎙️ “{user_text}”")
            await handle_message(update, context, text=user_text)
        else:
            await update.message.reply_text("I didn't catch that, sir.")
    except Exception as e:
        logger.error(f"Voice transcription: {e}")
        await update.message.reply_text("Couldn't transcribe that, sir. Try typing it.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


# ────────────────────────────────────────────────────────────────
# HEALTH (Docker healthcheck: the image's default one checks the web port,
# which this process does not serve, so the container showed "unhealthy")
# ────────────────────────────────────────────────────────────────
HEALTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "telegram_alive")
HEALTH_INTERVAL = 30   # seconds between beats; compose treats > ~120 s as unhealthy


def beat(path: str = HEALTH_FILE) -> None:
    """Touch the heartbeat file (best effort)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(str(int(time.time())))
    except OSError as e:
        logger.warning(f"health beat failed: {e}")


async def _health_loop(app) -> None:
    # Runs on the bot's own event loop, so a hung loop stops the beats.
    while True:
        beat()
        await asyncio.sleep(HEALTH_INTERVAL)


async def _post_init(app) -> None:
    app.bot_data["health_task"] = asyncio.create_task(_health_loop(app))


# ────────────────────────────────────────────────────────────────
# ENTRY POINT
# ────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        logger.warning("⚠️ TELEGRAM_TOKEN not set — Telegram bot disabled.")
        return

    # Only answer the owner's chat — anyone else who finds the bot is ignored.
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id.lstrip("-").isdigit():
        logger.warning("⚠️ TELEGRAM_CHAT_ID not set — Telegram bot disabled (it would answer anyone).")
        return
    owner = filters.Chat(chat_id=int(chat_id))

    # concurrent_updates: an approval button press must be handled while the
    # message that asked for it is still waiting.
    app = Application.builder().token(TELEGRAM_TOKEN).concurrent_updates(True).post_init(_post_init).build()
    app.add_handler(CommandHandler("start",  cmd_start,  filters=owner))
    app.add_handler(CommandHandler("memory", cmd_memory, filters=owner))
    app.add_handler(CommandHandler("clear",  cmd_clear,  filters=owner))
    app.add_handler(CallbackQueryHandler(handle_approval, pattern=r"^ok:"))
    app.add_handler(MessageHandler(owner & filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(owner & filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(owner & filters.VOICE, handle_voice))

    logger.info("📱 Telegram bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
