import os
import sys

import tempfile
os.environ.setdefault("TRACE_STORE", "file")            # tests never write traces to the real database
os.environ.setdefault("AGENT_REST_CHECK", "off")        # the real check asks Ollama; tests that need it fake it
os.environ.setdefault("FACTS_STORE", "file")           # memory facts / tool outputs never go to the real database
os.environ.setdefault("FACTS_FILE", os.path.join(tempfile.mkdtemp(prefix="enzo_facts_"), "facts.json"))
os.environ.setdefault("AGENT_IDLE_SECONDS", "0")         # background jobs never wait for "he is idle" in tests
os.environ.setdefault("EMBED_MODEL", "off")              # no 118 MB download in tests; test_facts.py fakes the embedder
os.environ.setdefault("TRACE_FILE", os.path.join(tempfile.mkdtemp(prefix="enzo_traces_"), "traces.jsonl"))

os.environ.setdefault("AUDIT_STORE", "file")             # audit records of risky actions stay out of the real database
os.environ.setdefault("AUDIT_FILE", os.path.join(tempfile.mkdtemp(prefix="enzo_audit_"), "audit.jsonl"))
os.environ.setdefault("CLAUDE_SPEND_FILE", os.path.join(tempfile.mkdtemp(prefix="enzo_spend_"), "spend.json"))
os.environ["MAIN_BRAIN"] = "local"                     # tests cover the local-first pipeline; Claude-first tests set it themselves

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


import pytest


@pytest.fixture(autouse=True)
def _no_real_free_brain(monkeypatch):
    """A real FREE_LLM_* in .env (load_dotenv) must never make a test call Groq; test_free_brain.py sets its own."""
    for k in ("FREE_LLM_BASE_URL", "FREE_LLM_API_KEY", "FREE_LLM_MODELS"):
        monkeypatch.delenv(k, raising=False)
