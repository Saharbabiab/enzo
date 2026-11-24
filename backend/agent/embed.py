"""
agent/embed.py - sentence embeddings on the CPU for memory recall (Phase 4).

Model: multilingual-e5-small, quantized ONNX (~118 MB, Hebrew + English), kept in data/models like Whisper.
Runs with onnxruntime + tokenizers, which faster-whisper already installs: no extra packages, never the GPU.
embed() returns unit vectors (numpy float32), so a dot product is the cosine similarity. None if the model
cannot load (offline first start): callers fall back to word overlap.
"""

from __future__ import annotations

import os
import threading

import numpy as np
from loguru import logger

REPO = os.getenv("EMBED_MODEL", "Xenova/multilingual-e5-small")
_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "models")
_lock = threading.Lock()
_model = None          # (session, tokenizer) once loaded; False after a failed load


def _load():
    global _model
    with _lock:
        if _model is None:
            try:
                import onnxruntime as ort
                from huggingface_hub import hf_hub_download
                from tokenizers import Tokenizer
                os.makedirs(_ROOT, exist_ok=True)
                onnx = hf_hub_download(REPO, "onnx/model_quantized.onnx", cache_dir=_ROOT)
                tok = Tokenizer.from_file(hf_hub_download(REPO, "tokenizer.json", cache_dir=_ROOT))
                tok.enable_truncation(256)
                tok.enable_padding()
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = int(os.getenv("EMBED_THREADS", "2"))
                _model = (ort.InferenceSession(onnx, opts, providers=["CPUExecutionProvider"]), tok)
                logger.info("🧠 Embedding model ready")
            except Exception as e:
                logger.warning(f"Embedding model not available, memory recall uses word overlap: {e}")
                _model = False
    return _model or None


def embed(texts: list[str], query: bool = False) -> np.ndarray | None:
    """One unit vector per text. e5 wants "query: " on a search and "passage: " on a stored fact."""
    if REPO == "off":            # tests, or a machine without room for it: word overlap instead
        return None
    m = _model if _model is not None else _load()
    if not m or not texts:
        return None
    sess, tok = m
    enc = tok.encode_batch([("query: " if query else "passage: ") + (t or "") for t in texts])
    ids = np.array([e.ids for e in enc], dtype=np.int64)
    mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
    feed = {"input_ids": ids, "attention_mask": mask}
    if any(i.name == "token_type_ids" for i in sess.get_inputs()):
        feed["token_type_ids"] = np.zeros_like(ids)
    hidden = sess.run(None, feed)[0]                                   # (batch, tokens, 384)
    vec = (hidden * mask[..., None]).sum(1) / np.maximum(mask.sum(1, keepdims=True), 1)
    return (vec / np.linalg.norm(vec, axis=1, keepdims=True)).astype(np.float32)


def preload() -> None:
    """Load at start-up (in a thread) so the first request does not wait for it."""
    _load()
