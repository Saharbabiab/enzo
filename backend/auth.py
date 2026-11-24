"""
auth.py — login for the web app (PC and phone).

How it works:
  1. The user sets a password once:  python set_password.py   (stores only a hash in .env)
  2. The web app POSTs the password to /api/login → gets a random session token
     for that device (valid SESSION_DAYS days, default 30).
  3. The token is the first message on /ws.  Each device has its own token, so a
     lost phone can be signed out alone:  python sessions.py list / revoke <id>
     (or from the app: "Sign out").

Only a SHA-256 of each token is stored (data/sessions.json), never the token itself.
Wrong passwords / tokens are rate-limited per IP and globally.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

from loguru import logger

SESSIONS_FILE = Path(os.getenv("ENZO_SESSIONS_FILE", Path(__file__).parent / "data" / "sessions.json"))
PBKDF2_ITERATIONS = 600_000

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
_lock = threading.Lock()


# ── Password ────────────────────────────────────────────────

def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
        return secrets.compare_digest(digest.hex(), hash_hex)
    except Exception:
        return False


def password_configured() -> bool:
    return bool(os.getenv("ENZO_PASSWORD_HASH", "").strip())


def check_password(password: str) -> bool:
    stored = os.getenv("ENZO_PASSWORD_HASH", "").strip()
    return bool(stored) and verify_password(password, stored)


# ── Sessions (one per device) ───────────────────────────────

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _load() -> list[dict]:
    try:
        return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.error(f"sessions file unreadable ({e}) — treating as empty")
        return []


def _save(sessions: list[dict]) -> None:
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SESSIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sessions, indent=2), encoding="utf-8")
    os.replace(tmp, SESSIONS_FILE)


def _session_days(role: str = "user") -> float:
    key, default = ("SESSION_DAYS_HELPER", "90") if role == "helper" else ("SESSION_DAYS", "30")
    try:
        return max(0.01, float(os.getenv(key, default)))
    except ValueError:
        return float(default)


ROLES = ("user", "helper")  # "helper" = the program on the PC (only allowed on /pc, never on /ws)


def create_session(device: str, ip: str = "", role: str = "user") -> dict:
    """Returns {"token", "id", "expires"}. The token is shown once and never stored."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    role = role if role in ROLES else "user"
    entry = {
        "id": secrets.token_hex(4),
        "token_hash": _token_hash(token),
        "device": (device or "unknown device")[:80],
        "ip": ip,
        "role": role,
        "created": now,
        "last_seen": now,
        "expires": now + _session_days(role) * 86400,
    }
    with _lock:
        sessions = [s for s in _load() if s.get("expires", 0) > now]
        sessions.append(entry)
        _save(sessions)
    return {"token": token, "id": entry["id"], "expires": entry["expires"]}


def validate_token(token: str) -> dict | None:
    """Returns the session (without its hash) if the token is valid, else None."""
    if not token or not isinstance(token, str):
        return None
    h = _token_hash(token)
    now = time.time()
    with _lock:
        sessions = _load()
        for s in sessions:
            if secrets.compare_digest(s.get("token_hash", ""), h):
                if s.get("expires", 0) <= now:
                    return None
                if now - s.get("last_seen", 0) > 300:  # don't rewrite the file on every connect
                    s["last_seen"] = now
                    _save(sessions)
                return {"role": "user", **{k: v for k, v in s.items() if k != "token_hash"}}
    return None


def list_sessions() -> list[dict]:
    now = time.time()
    return [{k: v for k, v in s.items() if k != "token_hash"} for s in _load() if s.get("expires", 0) > now]


def revoke_session(session_id: str) -> bool:
    with _lock:
        sessions = _load()
        kept = [s for s in sessions if s.get("id") != session_id]
        if len(kept) == len(sessions):
            return False
        _save(kept)
    return True


def revoke_all() -> int:
    with _lock:
        n = len(_load())
        _save([])
    return n


# ── Rate limiting ───────────────────────────────────────────

class RateLimiter:
    """
    After `per_ip` failures from one IP within `window` seconds → that IP is blocked
    until the window passes.  After `global_max` failures from anyone → everyone is
    blocked (protects against many IPs guessing).  A success clears the IP's count.
    """

    def __init__(self, per_ip: int = 5, global_max: int = 30, window: float = 900):
        self.per_ip, self.global_max, self.window = per_ip, global_max, window
        self._fails: dict[str, list[float]] = {}
        self._global: list[float] = []
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        self._global = [t for t in self._global if t > cutoff]
        for ip in list(self._fails):
            self._fails[ip] = [t for t in self._fails[ip] if t > cutoff]
            if not self._fails[ip]:
                del self._fails[ip]

    def blocked(self, ip: str) -> bool:
        with self._lock:
            self._prune(time.time())
            return len(self._fails.get(ip, [])) >= self.per_ip or len(self._global) >= self.global_max

    def fail(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            self._fails.setdefault(ip, []).append(now)
            self._global.append(now)

    def success(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


limiter = RateLimiter()


def _trusted_proxies() -> set[str]:
    """This PC plus any reverse proxy we trust (TRUSTED_PROXIES=192.168.1.10 for Nginx Proxy Manager)."""
    extra = {p.strip() for p in os.getenv("TRUSTED_PROXIES", "").split(",") if p.strip()}
    return _LOCAL_HOSTS | extra


def client_ip(peer: str, headers) -> str:
    """
    The real client IP. Behind a reverse proxy (`tailscale serve`, Nginx Proxy Manager)
    the peer is the proxy and the real address is in X-Forwarded-For — trust that
    header only when the connection comes from this PC or a TRUSTED_PROXIES address.
    The LAST entry is the one our proxy added; earlier ones can be faked by the client.
    """
    if peer in _trusted_proxies():
        fwd = headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[-1].strip()
    return peer


def is_direct_local(peer: str, headers) -> bool:
    """True only for a browser on this PC — not something proxied in from outside."""
    if peer not in _LOCAL_HOSTS or headers.get("forwarded") or headers.get("tailscale-user-login"):
        return False
    # The Vite dev proxy forwards the PC's own browser as 127.0.0.1 — that's still local.
    fwd = [h.strip() for h in headers.get("x-forwarded-for", "").split(",") if h.strip()]
    return all(h in _LOCAL_HOSTS for h in fwd)
