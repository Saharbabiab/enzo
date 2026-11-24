"""
shared.py — S.H.A.R.P. core utilities.
Optimised for GTX 1070 (8GB VRAM).

Three-model architecture (Ollama hot-swaps — only one loaded at a time):
  OLLAMA_MODEL  (qwen3:4b)          — planner + judge (structured JSON)
  CHAT_MODEL    (qwen3:4b)          — conversation + responses
  CODE_MODEL    (qwen2.5-coder:7b)   — code generation only

Search pipeline: cache → DuckDuckGo → Tavily → Gemini → OpenRouter
(DDG first — free, local, always available)
"""

import os
import re
import json
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser

import pytz
import httpx
from loguru import logger
from dotenv import load_dotenv
from httpx import Timeout
from ollama import Client
from pymongo import MongoClient, ASCENDING
from openai import OpenAI

# ── Gemini ───────────────────────────────────────────────────────
try:
    from google import genai
    from google.genai.types import Tool, GenerateContentConfig, GoogleSearch
    _GEMINI_OK = True
except ImportError:
    _GEMINI_OK = False

# ── DuckDuckGo ───────────────────────────────────────────────────
try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None

# ── Tavily ───────────────────────────────────────────────────────
try:
    from tavily import TavilyClient
    _TAVILY_OK = True
except ImportError:
    _TAVILY_OK = False

load_dotenv()

# ────────────────────────────────────────────────────────────────
# CONSTANTS
# ────────────────────────────────────────────────────────────────
OLLAMA_HOST        = os.getenv("OLLAMA_HOST",        "http://127.0.0.1:11434")
OLLAMA_MODEL       = os.getenv("OLLAMA_MODEL",       "qwen3.5:9b")
CHAT_MODEL         = os.getenv("CHAT_MODEL",         OLLAMA_MODEL)  # defaults to same model
CODE_MODEL         = os.getenv("CODE_MODEL",         "qwen2.5-coder:7b")
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY",     "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
TAVILY_API_KEY     = os.getenv("TAVILY_API_KEY",     "")
MONGO_URI          = os.getenv("MONGO_URI",          "mongodb://127.0.0.1:27017/")
ISRAEL_TZ          = pytz.timezone("Asia/Jerusalem")

SIR_FREQUENCY = 2   # 1-in-2 chance of adding "sir/boss"

# Knowledge cache TTLs (hours)
TTL_LIVE   = 1
TTL_DAILY  = 24
TTL_WEEKLY = 168
TTL_STABLE = 720

# ── Ollama generation options ────────────────────────────────────
# Applied to every model call so gemma4:e2b doesn't ramble.
OLLAMA_OPTIONS: dict = {
    "num_ctx":     int(os.getenv("LLM_NUM_CTX",     "32768")),   # prompt + tools ~10,800 tokens; 32768 uses 6.6 of the 8 GB GPU (qwen3.5:9b)
    "num_predict": int(os.getenv("LLM_NUM_PREDICT", "512")),
    "temperature": float(os.getenv("LLM_TEMPERATURE", "0.7")),
}

OPENROUTER_MODELS = [
    # "meta-llama/llama-3.3-70b-instruct:free",
    # "google/gemma-3-12b-it:free",
    "openrouter/auto",
]

# ────────────────────────────────────────────────────────────────
# CLIENTS
# ────────────────────────────────────────────────────────────────
class _EnzoOllamaClient(Client):
    """
    Thinking models (qwen3.5, qwen3, gemma4) think by default. With a small
    num_predict they spend the whole budget thinking and return EMPTY content —
    which surfaced as a slow, useless "Ready, sir." reply. Enzo's prompts don't
    need visible reasoning, so thinking is OFF unless a caller asks for it.
    """
    def chat(self, *args, **kwargs):
        kwargs.setdefault("think", os.getenv("OLLAMA_THINK", "false").lower() == "true")
        # Ollama unloads a model after 5 idle minutes and its prompt cache goes with it: the next request then
        # re-reads ~12k tokens (~35 s on the NAS). Keep the chat model loaded; other models (vision) keep the default.
        if kwargs.get("model") in (OLLAMA_MODEL, os.getenv("AGENT_MODEL", OLLAMA_MODEL)):
            kwargs.setdefault("keep_alive", os.getenv("OLLAMA_KEEP_ALIVE", "4h"))
        return super().chat(*args, **kwargs)


ollama_client = _EnzoOllamaClient(
    host=OLLAMA_HOST,
    timeout=Timeout(connect=30.0, read=300.0, write=30.0, pool=300.0),
)

openrouter_client = (
    OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_API_KEY)
    if OPENROUTER_API_KEY else None
)

gemini_client = None
if _GEMINI_OK and GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info("🟢 Gemini connected.")
    except Exception as e:
        logger.warning(f"Gemini: {e}")

tavily_client = None
if _TAVILY_OK and TAVILY_API_KEY:
    try:
        tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
        logger.info("🟢 Tavily connected.")
    except Exception as e:
        logger.warning(f"Tavily: {e}")


# ────────────────────────────────────────────────────────────────
# MONGODB
# ────────────────────────────────────────────────────────────────
class MongoMemoryStore:
    """
    user_memory     — permanent categorised personal facts
    knowledge_cache — web facts with TTL auto-expiry
    prompt_log      — self-improvement notes
    """

    def __init__(self, uri: str):
        self._ok = False
        try:
            self.client     = MongoClient(uri, serverSelectionTimeoutMS=5000)
            self.db         = self.client["sharp_db"]
            self.memory     = self.db["user_memory"]
            self.knowledge  = self.db["knowledge_cache"]
            self.prompt_log = self.db["prompt_log"]
            self._doc_id    = "owner_core_memory"

            if self.memory.count_documents({"_id": self._doc_id}) == 0:
                self.memory.insert_one({
                    "_id": self._doc_id,
                    "owner": {}, "people": {},
                    "preferences": {}, "routines": {}, "misc": {},
                })

            self.knowledge.create_index(
                [("expires_at", ASCENDING)],
                expireAfterSeconds=0, background=True,
            )
            self._ok = True
            logger.info("🟢 MongoDB connected.")
        except Exception as e:
            logger.error(f"🔴 MongoDB: {e}")

    def read(self) -> dict:
        if not self._ok:
            return {}
        try:
            doc = self.memory.find_one({"_id": self._doc_id}) or {}
            doc.pop("_id", None)
            return doc
        except Exception as e:
            logger.error(f"Memory read: {e}")
            return {}

    def add_to_list(self, path: str, items: list) -> None:
        """Append items to a list field (e.g. 'preferences.interests') without duplicates."""
        if not self._ok or not items:
            return
        try:
            doc = self.memory.find_one({"_id": self._doc_id}, {path: 1}) or {}
            cur = doc
            for part in path.split("."):
                cur = cur.get(part, {}) if isinstance(cur, dict) else {}
            if not isinstance(cur, list):
                # scalar or missing → turn into a list first (keep the old value)
                existing = [cur] if isinstance(cur, str) and cur else []
                self.memory.update_one({"_id": self._doc_id}, {"$set": {path: existing}}, upsert=True)
            self.memory.update_one({"_id": self._doc_id},
                                   {"$addToSet": {path: {"$each": items}}}, upsert=True)
            logger.info(f"🧠 Memory: +{items} → {path}")
        except Exception as e:
            logger.error(f"Memory add_to_list: {e}")

    def write(self, updates: dict):
        if not self._ok or not updates:
            return
        try:
            dot = _flatten_to_dot(updates)
            self.memory.update_one(
                {"_id": self._doc_id},
                {"$set": dot},
                upsert=True,
            )
            logger.info(f"🧠 Memory: {list(dot.keys())}")
        except Exception as e:
            logger.error(f"Memory write: {e}")

    def cache_fact(self, key: str, value: str, ttl_hours: int = TTL_WEEKLY):
        if not self._ok:
            return
        try:
            self.knowledge.update_one(
                {"_id": key},
                {"$set": {
                    "value":      value,
                    "expires_at": datetime.utcnow() + timedelta(hours=ttl_hours),
                    "updated_at": datetime.utcnow(),
                }},
                upsert=True,
            )
        except Exception as e:
            logger.error(f"Cache write: {e}")

    def fuzzy_search_cache(self, query: str) -> str | None:
        if not self._ok:
            return None
        try:
            term = query[:50].strip()
            docs = list(self.knowledge.find(
                {"$or": [
                    {"_id":   {"$regex": term, "$options": "i"}},
                    {"value": {"$regex": term, "$options": "i"}},
                ]},
                limit=3,
            ))
            if docs:
                return " | ".join(d.get("value", "") for d in docs)
        except Exception:
            pass
        return None

    def log_improvement(self, note: str):
        if not self._ok:
            return
        try:
            self.prompt_log.insert_one({"note": note, "created_at": datetime.utcnow()})
        except Exception as e:
            logger.error(f"Prompt log: {e}")

    def get_improvements(self, limit: int = 5) -> list[str]:
        if not self._ok:
            return []
        try:
            docs = list(
                self.prompt_log.find({}, {"note": 1})
                .sort("created_at", -1).limit(limit)
            )
            return [d.get("note", "") for d in docs]
        except Exception:
            return []


def _flatten_to_dot(d: dict, prefix: str = "") -> dict:
    result = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and v:
            result.update(_flatten_to_dot(v, key))
        else:
            result[key] = v
    return result


memory_store = MongoMemoryStore(MONGO_URI)


# ────────────────────────────────────────────────────────────────
# SMART QUERY BUILDER
# ────────────────────────────────────────────────────────────────
def build_search_query(raw: str) -> str:
    q     = raw.strip()
    lower = q.lower()
    if any(w in lower for w in (
        "most viewed", "most watched", "all time record", "highest ever",
        "richest", "tallest", "most disliked", "most liked", "most popular ever",
    )):
        if "wikipedia" not in lower:
            return f"{q} site:wikipedia.org"
    current_year = str(datetime.now().year)
    if any(w in lower for w in ("latest", "current", "today", "news", "price", "score")):
        if current_year not in q:
            return f"{q} {current_year}"
    return q


# ────────────────────────────────────────────────────────────────
# SEARCH BACKENDS
# ────────────────────────────────────────────────────────────────
def _openrouter_search(query: str) -> str:
    if not openrouter_client:
        return ""
    for model in OPENROUTER_MODELS:
        for attempt in range(2):
            try:
                logger.info(f"🌐 OpenRouter [{model}]: {query[:60]}")
                resp = openrouter_client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": (
                            "You are a factual web search assistant. "
                            "Search the web and return a concise accurate answer "
                            "with specific facts, numbers and dates. 2-3 sentences max."
                        )},
                        {"role": "user", "content": query},
                    ],
                    tools=[{"type": "function", "function": {
                        "name": "openrouter:web_search",
                        "description": "Search the web",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    }}],
                    extra_body={"tools": [{"type": "openrouter:web_search"}]},
                    max_tokens=500,
                )
                answer = resp.choices[0].message.content
                if answer and len(answer.strip()) > 10:
                    return answer.strip()
            except Exception as e:
                err = str(e)
                logger.warning(f"⚠️ OpenRouter [{model}]: {err[:80]}")
                if any(c in err for c in ("402", "404", "No endpoints")):
                    break
                if any(c in err for c in ("429", "500", "503")):
                    time.sleep(3)
                    continue
                break
    return ""


def _gemini_search(query: str) -> str:
    if not gemini_client:
        return ""
    try:
        resp = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=f"Search the web and answer with specific facts, numbers and dates. 2-3 sentences: {query}",
            config=GenerateContentConfig(
                tools=[Tool(google_search=GoogleSearch())],
                max_output_tokens=400,
            ),
        )
        return resp.text.strip()
    except Exception as e:
        logger.warning(f"Gemini: {e}")
        return ""


def _tavily_search(query: str) -> str:
    if not tavily_client:
        return ""
    try:
        r = tavily_client.search(query=query, search_depth="basic", max_results=3)
        if r and r.get("results"):
            return "\n".join(
                res.get("content", "") for res in r["results"] if res.get("content")
            )
    except Exception as e:
        logger.warning(f"Tavily: {e}")
    return ""


def _ddg_search(query: str) -> str:
    if not DDGS:
        return ""
    try:
        results = list(DDGS().text(query, max_results=5))
        if results:
            return "\n".join(
                f"{r.get('title','')}: {r.get('body','')}" for r in results
            )
    except Exception as e:
        logger.warning(f"DDG: {e}")
    return ""


def web_search(raw_query: str) -> str:
    query = build_search_query(raw_query)
    if query != raw_query:
        logger.info(f"🔧 Query: '{raw_query}' → '{query}'")

    cached = memory_store.fuzzy_search_cache(query)
    if cached:
        logger.info(f"📚 Cache hit: {query[:50]}")
        return f"[CACHED]: {cached}"


    # DDG first — it's free and always available
    # Paid/quota APIs are tried as fallbacks only if DDG comes up empty
    for fn, name in [
        (_ddg_search,        "DuckDuckGo"),
        (_tavily_search,     "Tavily"),
        (_gemini_search,     "Gemini"),
        (_openrouter_search, "OpenRouter"),
    ]:
        result = fn(query)
        if result and "RESOURCE_EXHAUSTED" not in result and "All search" not in result:
            logger.info(f"\u2705 {name} returned results for: {query[:50]}")
            return result
        if result:
            logger.warning(f"{name} exhausted — next")

    # All providers failed — return a sentinel the responder can detect
    logger.warning(f"🔴 All search providers failed for: {query[:60]}")
    return "[NO SEARCH RESULTS]: All search providers are currently unavailable. Answer from your own knowledge."


# ────────────────────────────────────────────────────────────────
# URL FETCHER
# ────────────────────────────────────────────────────────────────
class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "header", "aside", "meta"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "header", "aside", "meta"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip and data.strip():
            self._parts.append(data.strip())

    @property
    def text(self) -> str:
        return " ".join(self._parts)


# Hosts on the user's LAN that web-page tools may still reach, e.g. FETCH_ALLOW=192.168.1.10:30020,nas.local
FETCH_ALLOW = {h.strip().lower() for h in os.getenv("FETCH_ALLOW", "").split(",") if h.strip()}


def url_blocked(url: str) -> str:
    """Why Enzo must not fetch this URL (an internal or odd address), or "" when it is a normal public site.
    The host name is resolved and EVERY address it gives must be public (no loopback, LAN, link-local/metadata, ...)."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url.strip())
        host, port = (u.hostname or "").lower(), u.port
    except ValueError:
        return "bad URL"
    if u.scheme not in ("http", "https") or not host:
        return "only http(s) links to a named site are allowed"
    if host in FETCH_ALLOW or f"{host}:{port}" in FETCH_ALLOW:
        return ""
    try:
        infos = socket.getaddrinfo(host, port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except OSError:
        return ""          # does not resolve: the request itself will fail, nothing internal is reached
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            return f"{host} points to an internal address ({ip})"
    return ""


def fetch_url(url: str, max_chars: int = 4000) -> str:
    # shortcut: the host is resolved again by httpx after the check (DNS rebinding window of a few ms);
    # pin the checked IP if Enzo ever fetches for untrusted users.
    try:
        for _ in range(6):                      # follow redirects by hand so every hop is checked
            why = url_blocked(url)
            if why:
                logger.warning(f"🛡️ Blocked fetch of {url[:120]}: {why}")
                return f"[BLOCKED]: {why}. Enzo does not open internal addresses from web tools. Do not try another tool for it; tell the user it was blocked for safety."
            resp = httpx.get(url, timeout=12, follow_redirects=False,
                             headers={"User-Agent": "Mozilla/5.0 (compatible; Enzo/5.0)"})
            if not resp.is_redirect:
                break
            url = str(resp.next_request.url) if resp.next_request else str(resp.url.join(resp.headers.get("location", "")))
        else:
            return "Fetch failed: too many redirects"
        resp.raise_for_status()
        p = _TextExtractor()
        p.feed(resp.text)
        text = p.text
        return text[:max_chars] if len(text) > max_chars else text
    except Exception as e:
        return f"Fetch failed: {e}"


# ────────────────────────────────────────────────────────────────
# TIME
# ────────────────────────────────────────────────────────────────
def get_time_context() -> str:
    now = datetime.now(ISRAEL_TZ).strftime("%A, %B %d, %Y at %H:%M")
    return f"Current time (Israel): {now}"


# ────────────────────────────────────────────────────────────────
# STRIP THINKING — qwen3 wraps reasoning in <think>...</think>
# ────────────────────────────────────────────────────────────────
def strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks from qwen3 output before processing."""
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()
    # Handle unclosed <think> (model ran out of tokens mid-thought)
    if "<think>" in cleaned.lower():
        idx = cleaned.lower().find("<think>")
        cleaned = cleaned[:idx].strip()
    return cleaned or text  # fallback to original if stripping removed everything


# ────────────────────────────────────────────────────────────────
# JSON REPAIR — shared utility used by all agent modules
# ────────────────────────────────────────────────────────────────
def _repair_json(text: str) -> dict:
    """
    Parse JSON from LLM output, tolerating:
    - <think>...</think> blocks (qwen3)
    - markdown fences (```json ... ```)
    - trailing commas
    - missing outer }
    """
    import ast

    # Strip thinking blocks first (qwen3 wraps reasoning in <think> tags)
    text = strip_thinking(text)

    # Strip markdown fences
    text = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).strip()

    s = text.find("{")
    if s == -1:
        return {}

    # Try from the last } going backwards — if gemma4 cut off the outer }
    # we try auto-completing with a } appended
    candidates = []
    e = text.rfind("}")
    if e != -1:
        candidates.append(text[s:e+1])

    # Also try appending a closing brace (handles the missing outer } case)
    inner = text[s:].rstrip()
    if inner and not inner.endswith("}"):
        candidates.append(inner + "}")

    for candidate in candidates:
        for fn in [
            lambda t: json.loads(t),
            lambda t: json.loads(re.sub(r",\s*([}\]])", r"\1", t)),
        ]:
            try:
                result = fn(candidate)
                if isinstance(result, dict):
                    return result
            except Exception:
                pass
        try:
            result = ast.literal_eval(candidate)
            if isinstance(result, dict):
                return result
        except Exception:
            pass

    return {}



# ────────────────────────────────────────────────────────────────
# WARMUP
# ────────────────────────────────────────────────────────────────
