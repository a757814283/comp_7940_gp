import os
import re
import logging
import asyncio
from typing import Optional, Tuple, List

import aiohttp
from motor.motor_asyncio import AsyncIOMotorClient

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import NetworkError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("telegram-bot")


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
BACKEND_URL_TEMPLATE = os.getenv("BACKEND_URL_TEMPLATE", "http://backend-{id}:8000").strip()
BACKEND_START_ID = int(os.getenv("BACKEND_START_ID", "1"))
# Safety bound for message-time failover (avoid infinite retries).
# The bot does NOT use /health during message sending.
BACKEND_FAILOVER_MAX_NEXT = int(os.getenv("BACKEND_FAILOVER_MAX_NEXT", "20"))
BACKEND_HEALTH_TIMEOUT_SECONDS = float(os.getenv("BACKEND_HEALTH_TIMEOUT_SECONDS", "2.0"))
BACKEND_ASK_TIMEOUT_SECONDS = float(os.getenv("BACKEND_ASK_TIMEOUT_SECONDS", "8.0"))
TELEGRAM_SEND_MAX_RETRIES = int(os.getenv("TELEGRAM_SEND_MAX_RETRIES", "3"))
TELEGRAM_SEND_RETRY_DELAY_SECONDS = float(os.getenv("TELEGRAM_SEND_RETRY_DELAY_SECONDS", "1.0"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing env var: TELEGRAM_BOT_TOKEN")
if not MONGODB_URI:
    raise RuntimeError("Missing env var: MONGODB_URI")
if "{id}" not in BACKEND_URL_TEMPLATE:
    raise RuntimeError("BACKEND_URL_TEMPLATE must contain '{id}', e.g. http://backend-{id}:8000")
if BACKEND_START_ID < 1:
    raise RuntimeError("BACKEND_START_ID must be >= 1")
if BACKEND_FAILOVER_MAX_NEXT < 1:
    raise RuntimeError("BACKEND_FAILOVER_MAX_NEXT must be >= 1")


MODEL_PREFIX_RE = re.compile(r"^\s*\[([^\]]+)\]")


mongo_client = AsyncIOMotorClient(MONGODB_URI)
db = mongo_client.get_default_database()
users_col = db["user_settings"]
chats_col = db["chat_records"]

_aiohttp_session: Optional[aiohttp.ClientSession] = None


def _backend_base_url(backend_id: int) -> str:
    return BACKEND_URL_TEMPLATE.format(id=backend_id).rstrip("/")


def _backend_ask_url(backend_id: int) -> str:
    return f"{_backend_base_url(backend_id)}/ask"


def _backend_health_url(backend_id: int) -> str:
    return f"{_backend_base_url(backend_id)}/health"


def _backend_order(preferred_backend: int, ids: List[int]) -> List[Tuple[int, str]]:
    if preferred_backend in ids:
        ordered = [preferred_backend] + [i for i in ids if i != preferred_backend]
    else:
        ordered = ids
    return [(bid, _backend_ask_url(bid)) for bid in ordered]


async def _get_session() -> aiohttp.ClientSession:
    global _aiohttp_session
    if _aiohttp_session is None or _aiohttp_session.closed:
        _aiohttp_session = aiohttp.ClientSession()
    return _aiohttp_session


async def is_backend_reachable(backend_id: int, timeout_seconds: float = 2.0) -> bool:
    try:
        session = await _get_session()
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with session.get(_backend_health_url(backend_id), timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


async def safe_reply_text(message, text: str) -> bool:
    """
    Retry Telegram message sending on network errors.
    """
    for attempt in range(1, TELEGRAM_SEND_MAX_RETRIES + 1):
        try:
            await message.reply_text(text)
            return True
        except NetworkError as e:
            logger.warning(
                "Telegram reply failed (attempt %s/%s): %s",
                attempt,
                TELEGRAM_SEND_MAX_RETRIES,
                str(e),
            )
            if attempt < TELEGRAM_SEND_MAX_RETRIES:
                await asyncio.sleep(TELEGRAM_SEND_RETRY_DELAY_SECONDS * attempt)
    return False


def _extract_model_from_answer(answer: str) -> Optional[str]:
    if not answer:
        return None
    m = MODEL_PREFIX_RE.match(answer)
    if not m:
        return None
    return m.group(1).strip()


async def get_preferred_backend(user_id: int) -> int:
    doc = await users_col.find_one({"user_id": user_id}, {"preferred_backend": 1})
    if not doc:
        return BACKEND_START_ID
    try:
        pb = int(doc.get("preferred_backend", 1))
        if pb < BACKEND_START_ID:
            return BACKEND_START_ID
        return pb
    except Exception:
        return BACKEND_START_ID


async def set_preferred_backend(user_id: int, preferred_backend: int) -> None:
    if preferred_backend < BACKEND_START_ID:
        raise ValueError(f"Invalid backend id: {preferred_backend}")
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"preferred_backend": preferred_backend}},
        upsert=True,
    )


async def call_llm_backend(question: str, backend_id: int, ask_url: str) -> str:
    session = await _get_session()

    timeout = aiohttp.ClientTimeout(total=BACKEND_ASK_TIMEOUT_SECONDS)
    async with session.post(ask_url, json={"message": question}, timeout=timeout) as resp:
        text = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"Backend {backend_id} HTTP {resp.status}: {text}")

        data = await resp.json()
        answer = data.get("answer", "")
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError(f"Backend {backend_id} returned empty/invalid answer: {data}")
        return answer


async def ask_with_failover(question: str, preferred_backend: int) -> Tuple[str, int, str]:
    errors = []
    # Do not probe /health here. We only fail over by trying the next backend.
    first = max(BACKEND_START_ID, preferred_backend)
    ordered = [(first + i, _backend_ask_url(first + i)) for i in range(BACKEND_FAILOVER_MAX_NEXT)]
    for (bid, url) in ordered:
        try:
            answer = await call_llm_backend(question, bid, url)
            model_name = _extract_model_from_answer(answer)
            model_used = model_name if model_name else f"backend-{bid}"
            return answer, bid, model_used
        except Exception as e:
            logger.warning("Backend attempt failed: backend_id=%s url=%s err=%s", bid, url, str(e))
            errors.append(f"backend {bid}: {e}")

    raise RuntimeError("All backends failed. " + " | ".join(errors))


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_reply_text(
        update.message,
        "Welcome! Send a message to ask the bot.\n"
        "Use /setllm x to choose your preferred LLM backend (x is backend index).",
    )


async def cmd_setllm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    previous_backend = await get_preferred_backend(user.id)

    if not context.args or len(context.args) < 1:
        await safe_reply_text(update.message, "Usage: /setllm <x>")
        return

    try:
        backend = int(context.args[0])
    except Exception:
        await safe_reply_text(update.message, "Usage: /setllm <x>")
        return

    if backend < BACKEND_START_ID:
        await safe_reply_text(
            update.message,
            f"Invalid backend id: {backend}. x must be >= {BACKEND_START_ID}.",
        )
        return
    # Only here we probe /health (as requested).
    if not await is_backend_reachable(backend, timeout_seconds=BACKEND_HEALTH_TIMEOUT_SECONDS):
        await safe_reply_text(
            update.message,
            f"Backend {backend} is not reachable (health check failed). "
            f"Kept previous backend: {previous_backend}.",
        )
        return

    await set_preferred_backend(user.id, backend)
    await safe_reply_text(update.message, f"OK. Preferred LLM backend set to {backend}.")


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # No /health probing here; health is checked only inside /setllm.
    user = update.effective_user
    preferred_backend = await get_preferred_backend(user.id) if user else BACKEND_START_ID
    await safe_reply_text(
        update.message,
        f"Configured backends: backend-{BACKEND_START_ID} .. (unknown)\n"
        f"Current preferred backend: {preferred_backend}\n"
        "Note: /health is checked only when you run /setllm x. "
        f"Message failover tries up to {BACKEND_FAILOVER_MAX_NEXT} next backend ids.",
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not message or not user:
        return

    question = (message.text or "").strip()
    if not question:
        return

    preferred_backend = await get_preferred_backend(user.id)

    # Persist the user question first, then update this record with final result.
    record = await chats_col.insert_one(
        {
            "user_id": user.id,
            "chat_id": update.effective_chat.id if update.effective_chat else None,
            "question": question,
            "preferred_backend": preferred_backend,
            "status": "pending",
        }
    )

    try:
        answer, used_backend_id, model_used = await ask_with_failover(question, preferred_backend)
        final_answer = answer
        if used_backend_id != preferred_backend:
            final_answer = f"[fallback to backend-{used_backend_id}] {answer}"
        await chats_col.update_one(
            {"_id": record.inserted_id},
            {
                "$set": {
                    "answer": final_answer,
                    "used_backend_id": used_backend_id,
                    "model_used": model_used,
                    "status": "done",
                }
            },
        )
        await safe_reply_text(update.message, final_answer)
    except Exception as e:
        logger.exception("LLM request failed")
        await chats_col.update_one(
            {"_id": record.inserted_id},
            {
                "$set": {
                    "answer": "",
                    "used_backend_id": None,
                    "model_used": None,
                    "status": "failed",
                    "error": str(e),
                }
            },
        )
        await safe_reply_text(
            update.message,
            "Sorry, I cannot reach the LLM backends right now. Please try again later.",
        )


def main() -> None:
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("setllm", cmd_setllm))
    application.add_handler(CommandHandler("health", cmd_health))

    # Handle plain text messages (exclude commands)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Bot started. Polling...")
    application.run_polling()


if __name__ == "__main__":
    main()