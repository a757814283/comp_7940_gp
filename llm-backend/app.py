import os
import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import requests

from openai import OpenAI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("llm-backend")

# Preferred variable names: API_KEY / BASE_URL
# Backward-compatible with OPENAI_API_KEY / OPENAI_BASE_URL
API_KEY = (os.getenv("API_KEY", "").strip() or os.getenv("OPENAI_API_KEY", "").strip())
BASE_URL = (os.getenv("BASE_URL", "").strip() or os.getenv("OPENAI_BASE_URL", "").strip())
MODEL = os.getenv("MODEL", "").strip()
LLM_NAME = os.getenv("LLM_NAME", "LLM").strip()
API_VER = os.getenv("API_VER", "").strip() or os.getenv("OPENAI_API_VERSION", "").strip()
USE_HKBU_ROUTE = bool(API_VER and API_VER.lower() != "none")
USE_SYSTEM_PROMPT = os.getenv("USE_SYSTEM_PROMPT", "false").strip().lower() in ("1", "true", "yes")

if not API_KEY:
    raise RuntimeError("Missing env var: API_KEY")
if not MODEL:
    raise RuntimeError("Missing env var: MODEL")

if BASE_URL and not USE_HKBU_ROUTE:
    # DeepSeek / OpenAI-compatible providers.
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
else:
    client = OpenAI(api_key=API_KEY)


def call_hkbu_chat_completions(user_message: str) -> str:
    """
    Use the HKBU route format:
    POST {base_url}/deployments/{model}/chat/completions?api-version={api_ver}
    """
    if not BASE_URL:
        raise RuntimeError("BASE_URL is required for HKBU mode")
    if not API_VER:
        raise RuntimeError("API_VER is required for HKBU mode")

    base = BASE_URL.rstrip("/")
    url = f"{base}/deployments/{MODEL}/chat/completions?api-version={API_VER}"
    headers = {
        "accept": "application/json",
        "Content-Type": "application/json",
        "api-key": API_KEY,
    }

    # By default, align with the verified sample:
    # send only the user message unless system prompt is explicitly enabled.
    messages = [{"role": "user", "content": user_message}]
    if USE_SYSTEM_PROMPT:
        system_prompt = os.getenv(
            "SYSTEM_PROMPT",
            "You are a helper! Your users are university students. "
            "Your replies should be conversational, informative, use simple words, and be straightforward.",
        )
        messages.insert(0, {"role": "system", "content": system_prompt})

    payload = {
        "messages": messages,
        "temperature": float(os.getenv("TEMPERATURE", "1")),
        "max_tokens": int(os.getenv("MAX_TOKENS", "150")),
        "top_p": float(os.getenv("TOP_P", "1")),
        "stream": False,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
    except Exception as e:
        raise RuntimeError(f"HKBU request failed: {str(e)}")

    if resp.status_code != 200:
        raise RuntimeError(f"HKBU API HTTP {resp.status_code}: {resp.text}")

    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError(f"HKBU API invalid response: {data}")

app = FastAPI(title="LLM Backend API")


class AskRequest(BaseModel):
    message: str


class AskResponse(BaseModel):
    answer: str


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "model": MODEL,
        "llm_name": LLM_NAME,
        "mode": "hkbu" if USE_HKBU_ROUTE else "openai-compatible",
        "api_ver": API_VER,
        "base_url": BASE_URL,
    }


@app.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest) -> AskResponse:
    user_message = (req.message or "").strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="message is required")

    try:
        if USE_HKBU_ROUTE:
            content = call_hkbu_chat_completions(user_message)
        else:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": user_message}],
            )

            content = ""
            if resp and getattr(resp, "choices", None):
                choice0 = resp.choices[0]
                msg = getattr(choice0, "message", None)
                content = getattr(msg, "content", "") or ""

        if not content.strip():
            raise RuntimeError("OpenAI returned empty content")

        prefix = f"[{LLM_NAME}]"
        return AskResponse(answer=f"{prefix} {content.strip()}")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("OpenAI call failed")
        raise HTTPException(status_code=500, detail=f"LLM request failed: {str(e)}")


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
