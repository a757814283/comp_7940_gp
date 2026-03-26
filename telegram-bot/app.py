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
BACKEND_DISCOVERY_START_ID = int(os.getenv("BACKEND_DISCOVERY_START_ID", "1"))
BACKEND_DISCOVERY_LIMIT = int(os.getenv("BACKEND_DISCOVERY_LIMIT", "20"))
TELEGRAM_SEND_MAX_RETRIES = int(os.getenv("TELEGRAM_SEND_MAX_RETRIES", "3"))
TELEGRAM_SEND_RETRY_DELAY_SECONDS = float(os.getenv("TELEGRAM_SEND_RETRY_DELAY_SECONDS", "1.0"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing env var: TELEGRAM_BOT_TOKEN")
if not MONGODB_URI:
    raise RuntimeError("Missing env var: MONGODB_URI")
if "{id}" not in BACKEND_URL_TEMPLATE:
    raise RuntimeError("BACKEND_URL_TEMPLATE must contain '{id}', e.g. http://backend-{id}:8000")
if BACKEND_DISCOVERY_START_ID < 1:
    raise RuntimeError("BACKEND_DISCOVERY_START_ID must be >= 1")
if BACKEND_DISCOVERY_LIMIT < 1:
    raise RuntimeError("BACKEND_DISCOVERY_LIMIT must be >= 1")


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


async def discover_backend_ids() -> List[int]:
    """
    Discover backends sequentially from BACKEND_DISCOVERY_START_ID by probing /health.
    Stop immediately when an index does not respond to /health.
    """
    ids: List[int] = []
    current = BACKEND_DISCOVERY_START_ID

    for _ in range(BACKEND_DISCOVERY_LIMIT):
        ok = await is_backend_reachable(current)
        if not ok:
            break
        ids.append(current)
        current += 1
    return ids


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
        return BACKEND_DISCOVERY_START_ID
    try:
        pb = int(doc.get("preferred_backend", 1))
        return pb if pb >= BACKEND_DISCOVERY_START_ID else BACKEND_DISCOVERY_START_ID
    except Exception:
        return BACKEND_DISCOVERY_START_ID


async def set_preferred_backend(user_id: int, preferred_backend: int) -> None:
    if preferred_backend < BACKEND_DISCOVERY_START_ID:
        raise ValueError(f"Invalid backend id: {preferred_backend}")
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"preferred_backend": preferred_backend}},
        upsert=True,
    )


async def call_llm_backend(question: str, backend_id: int, ask_url: str) -> str:
    session = await _get_session()

    timeout = aiohttp.ClientTimeout(total=25)
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
    available_ids = await discover_backend_ids()
    if not available_ids:
        raise RuntimeError("No available backend found from /health discovery.")

    for (bid, url) in _backend_order(preferred_backend, available_ids):
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

    if backend < BACKEND_DISCOVERY_START_ID:
        await safe_reply_text(
            update.message,
            f"Invalid backend id: {backend}. x must be >= {BACKEND_DISCOVERY_START_ID}."
        )
        return

    discovered = await discover_backend_ids()
    if backend not in discovered:
        await set_preferred_backend(user.id, previous_backend)
        await safe_reply_text(
            update.message,
            f"Backend {backend} does not exist or /health is unavailable. "
            f"Kept previous backend: {previous_backend}."
        )
        return

    try:
        await set_preferred_backend(user.id, backend)
        await safe_reply_text(update.message, f"OK. Preferred LLM backend set to {backend}.")
    except Exception as e:
        logger.exception("Failed to set backend")
        await safe_reply_text(update.message, "Sorry, failed to save your preference. Please try again later.")


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    preferred_backend = await get_preferred_backend(user.id) if user else BACKEND_DISCOVERY_START_ID
    discovered = await discover_backend_ids()

    if not discovered:
        await safe_reply_text(
            update.message,
            "No backend is currently discoverable (/health failed at the first backend).",
        )
        return

    info = ", ".join([f"backend-{bid}" for bid in discovered])
    await safe_reply_text(
        update.message,
        f"Available backends: {info}\nCurrent preferred backend: {preferred_backend}",
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
