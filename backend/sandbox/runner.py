"""
sandbox/runner.py - runs Enzo's Python snippets inside the locked sandbox container (Hermes plan H5).

Standard library only. POST /run  {"code": "...", "timeout": 20}  with header  X-Sandbox-Token: <SANDBOX_TOKEN>
-> {"ok": bool, "exit": int, "stdout": "...", "stderr": "...", "timed_out": bool}

Each run is a new `python -I` process in a fresh temp folder with an empty environment, and on Linux these limits:
memory 512 MB, CPU = timeout, 64 processes, files up to 10 MB, 32 open files. Output is cut to MAX_OUT characters.
The container itself adds: no internet (an internal-only network), read-only root, tmpfs /tmp, non-root user,
all capabilities dropped (sandbox/compose.example.yml). Runs are one at a time.
"""

from __future__ import annotations

import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_CODE = 20_000
MAX_OUT = 20_000
MAX_TIMEOUT = 60
TOKEN = os.getenv("SANDBOX_TOKEN", "")
_one_at_a_time = threading.Lock()


def _limits(timeout: int):
    def apply():                                  # runs in the child before python starts (Linux only)
        import resource
        mb = 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (512 * mb, 512 * mb))
        resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 1))
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
        resource.setrlimit(resource.RLIMIT_FSIZE, (10 * mb, 10 * mb))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
        os.setsid()
    return apply if sys.platform.startswith("linux") else None


def _cut(text: str) -> str:
    return text if len(text) <= MAX_OUT else text[: MAX_OUT // 2] + "\n[...cut...]\n" + text[-MAX_OUT // 2:]


def run(code: str, timeout: int = 20) -> dict:
    timeout = max(1, min(int(timeout or 20), MAX_TIMEOUT))
    work = tempfile.mkdtemp(prefix="run_")
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": work, "TMPDIR": work, "PYTHONIOENCODING": "utf-8",
           "PYTHONDONTWRITEBYTECODE": "1"}
    if os.name == "nt":                           # tests on Windows: python needs these to start
        env.update({k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR") if k in os.environ})
    with _one_at_a_time:
        try:
            p = subprocess.run([sys.executable, "-I", "-c", code], cwd=work, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout, preexec_fn=_limits(timeout))
            return {"ok": p.returncode == 0, "exit": p.returncode, "stdout": _cut(p.stdout), "stderr": _cut(p.stderr),
                    "timed_out": False}
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            return {"ok": False, "exit": -9, "stdout": _cut(out), "stderr": f"Stopped after {timeout} s.", "timed_out": True}
        finally:
            shutil.rmtree(work, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200 if self.path == "/health" else 404, {"ok": self.path == "/health"})

    def do_POST(self):
        if self.path != "/run":
            return self._send(404, {"error": "not found"})
        if not TOKEN or not hmac.compare_digest(self.headers.get("X-Sandbox-Token", ""), TOKEN):
            return self._send(401, {"error": "bad token"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_CODE * 4:
                return self._send(413, {"error": "too big"})
            req = json.loads(self.rfile.read(n) or b"{}")
            code = str(req.get("code", ""))
        except Exception:
            return self._send(400, {"error": "bad request"})
        if not code.strip() or len(code) > MAX_CODE:
            return self._send(400, {"error": f"code must be 1-{MAX_CODE} characters"})
        self._send(200, run(code, req.get("timeout", 20)))

    def log_message(self, *a):
        pass


def main() -> None:
    if not TOKEN:
        sys.exit("SANDBOX_TOKEN is not set")
    port = int(os.getenv("SANDBOX_PORT", "8800"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
