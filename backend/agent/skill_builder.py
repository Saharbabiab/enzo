"""
agent/skill_builder.py — Enzo writes new skills (tools) for itself.

Flow when the user says "learn how to …" / "make yourself a tool that …":
  1. The model (Claude for these requests) writes a Python module.
  2. check_skill_code() rejects anything dangerous (static AST checks) and
     requires documented, type-hinted public functions.
  3. trial_import() loads it in a SEPARATE Python process with a timeout to
     catch syntax/import errors before it touches Enzo.
  4. The user approves (agent/approvals.py — "create_skill").
  5. It's saved to agent/skills/<name>.py and the tool registry reloads, so
     the new tool can be used in the very same conversation.

Not a sandbox: an approved skill runs with Enzo's permissions. The checks
block the obvious foot-guns; the approval step is the real gate.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tempfile

SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")

# Modules a skill may not import (process control, raw OS access, persistence tricks)
_BANNED_MODULES = {
    "subprocess", "ctypes", "winreg", "_winreg", "shutil", "multiprocessing", "pickle",
    "marshal", "importlib", "socketserver", "pty", "signal", "sys", "builtins",
    # Enzo internals / credentials — accounts are reachable ONLY via agent.skill_api
    "shared", "scheduler", "bot", "server", "telegram_bot", "heartbeat", "rules", "google_setup",
    "google", "googleapiclient", "google_auth_oauthlib", "oauthlib", "pymongo", "dotenv",
    "anthropic", "ollama", "elevenlabs", "keyring",
}
# The one Enzo module a skill may import: the approval-gated door to Google.
_ALLOWED_AGENT_MODULES = {"agent.skill_api"}


def _agent_import_ok(module: str, names: list[str] | None = None) -> bool:
    if module in _ALLOWED_AGENT_MODULES:
        return True
    if module == "agent" and names is not None:     # "from agent import skill_api"
        return all(f"agent.{n}" in _ALLOWED_AGENT_MODULES for n in names)
    return False
# Calls that are never OK in a skill
_BANNED_CALLS = {"eval", "exec", "compile", "__import__", "globals", "locals", "setattr", "delattr", "open"}
# Attribute calls like os.system(...), os.remove(...)
_BANNED_ATTRS = {
    "system", "popen", "spawn", "spawnl", "spawnv", "execv", "execl", "execvp", "fork", "kill",
    "remove", "unlink", "rmdir", "removedirs", "rmtree", "rename", "replace", "chmod", "chown",
    "startfile", "putenv", "unsetenv", "environ", "getenv", "write_text", "write_bytes", "mkdir", "makedirs",
}
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")


def _safety_problems(tree: ast.AST) -> list[str]:
    """Banned imports, calls and attributes anywhere in the code (skill or its tests)."""
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root in _BANNED_MODULES or (root == "agent" and not _agent_import_ok(a.name)):
                    problems.append(f"Not allowed to import '{a.name}'"
                                    + (" — use agent.skill_api for Google." if root in ("agent", "google", "googleapiclient") else "."))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            root = mod.split(".")[0]
            if node.level:                       # relative imports
                problems.append("Relative imports aren't allowed in skills.")
            elif root in _BANNED_MODULES or (root == "agent" and not _agent_import_ok(mod, [a.name for a in node.names])):
                problems.append(f"Not allowed to import from '{mod}'"
                                + (" — use agent.skill_api for Google." if root in ("agent", "google", "googleapiclient") else "."))
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in _BANNED_CALLS:
                problems.append(f"Not allowed to call {f.id}().")
            if isinstance(f, ast.Attribute) and f.attr in _BANNED_ATTRS:
                problems.append(f"Not allowed to call .{f.attr}().")
        elif isinstance(node, ast.Attribute) and node.attr in ("environ", "__globals__", "__builtins__", "__subclasses__"):
            problems.append(f"Not allowed to access .{node.attr}.")
    return problems


def check_skill_code(code: str) -> tuple[list[str], list[str]]:
    """Return (problems, public_function_names). Empty problems = OK."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"Syntax error: {e}"], []
    problems = _safety_problems(tree)
    functions = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            if not ast.get_docstring(node):
                problems.append(f"Function '{node.name}' needs a docstring (the model reads it to know when to use the tool).")
            missing = [a.arg for a in node.args.args if a.annotation is None]
            if missing:
                problems.append(f"Function '{node.name}' needs type hints for: {', '.join(missing)}.")
            functions.append(node.name)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue  # module docstring
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and not any(isinstance(n, ast.Call) for n in ast.walk(node)):
            continue  # simple constants (URLs, tables) — no calls at import time
        elif isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        else:
            problems.append(f"Top-level '{type(node).__name__}' statements aren't allowed — put logic inside functions.")
    if not functions:
        problems.append("The skill must define at least one public function (the tool).")
    return problems, functions


def trial_import(code: str, timeout: float = 15) -> str | None:
    """Import the module in a separate, isolated Python process. Returns an error or None."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "candidate_skill.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = (
            "import importlib.util, sys\n"
            f"sys.path.append({root!r})  # so 'from agent.skill_api import google_call' resolves\n"
            f"spec = importlib.util.spec_from_file_location('candidate_skill', {path!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "print('ok')\n"
        )
        try:
            # -E: ignore PYTHON* env vars; cwd = temp dir holding only the candidate.
            # (Not -I: that would hide user-installed packages like httpx.)
            r = subprocess.run([sys.executable, "-E", "-c", script], cwd=d, capture_output=True,
                               text=True, timeout=timeout, env=_clean_env())
        except subprocess.TimeoutExpired:
            return "Importing the skill took too long (it must not do work at import time)."
        if r.returncode != 0 or "ok" not in r.stdout:
            return (r.stderr or r.stdout).strip()[-800:] or "Import failed."
    return None


# Self-written skills live in data/skills/<name>/ (kept across container updates), next to their tests and any
# playbook of the same name. New ones are staged in data/skills/_proposed/<name>/ until their tests pass.
from agent.playbooks import OWN_DIR  # noqa: E402
_SELF_WRITTEN = "# Created by S.H.A.R.P."


def _own_file(name: str) -> str:
    return os.path.join(OWN_DIR, name, f"{name}.py")


def _legacy_file(name: str) -> str:
    return os.path.join(SKILLS_DIR, f"{name}.py")


def existing_skills() -> list[tuple[str, bool]]:
    """[(module, self_written)] from agent/skills and data/skills."""
    out = {}
    for f in os.listdir(SKILLS_DIR):
        if f.endswith(".py") and not f.startswith("_"):
            with open(os.path.join(SKILLS_DIR, f), encoding="utf-8") as fh:
                out[f[:-3]] = fh.read(len(_SELF_WRITTEN)) == _SELF_WRITTEN
    if os.path.isdir(OWN_DIR):
        for d in os.listdir(OWN_DIR):
            if not d.startswith("_") and os.path.exists(_own_file(d)):
                out[d] = True
    return sorted(out.items())


def skill_functions(name: str) -> list[str]:
    """Public functions of an installed skill module (so updating a skill is not a name clash)."""
    for path in (_own_file(name), _legacy_file(name)):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return check_skill_code(f.read())[1]
    return []


def check_tests(tests: str) -> list[str]:
    try:
        tree = ast.parse(tests or "")
    except SyntaxError as e:
        return [f"Tests: syntax error: {e}"]
    problems = ["Tests: " + p for p in _safety_problems(tree)]
    if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_") for n in tree.body):
        problems.append("Write tests: at least one 'def test_...():' that imports the skill's function and uses assert.")
    return problems


def check_proposal(code: str, tests: str) -> tuple[list[str], list[str]]:
    """Static checks for a new skill and its tests. (problems, public function names)."""
    problems, funcs = check_skill_code(code)
    return problems + check_tests(tests), funcs


def stage(name: str, code: str, tests: str) -> str:
    """Write the skill and its tests to data/skills/_proposed/<name>/. Returns the folder."""
    folder = os.path.join(OWN_DIR, "_proposed", name)
    os.makedirs(folder, exist_ok=True)
    for fname, text in ((f"{name}.py", code), (f"test_{name}.py", tests.rstrip() + "\n")):
        with open(os.path.join(folder, fname), "w", encoding="utf-8") as f:
            f.write(text)
    return folder


def _clean_env() -> dict:
    return {k: os.environ.get(k, "") for k in ("PATH", "SYSTEMROOT", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME")}


def run_tests(folder: str, name: str, timeout: float = 60) -> str | None:
    """Run test_<name>.py's test_ functions in a separate process. Returns what failed, or None when all pass."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = (
        "import sys, traceback, inspect, asyncio\n"
        f"sys.path[:0] = [{folder!r}, {root!r}]\n"
        f"import test_{name} as t\n"
        "tests = [(n, f) for n, f in vars(t).items() if n.startswith('test_') and callable(f)]\n"
        "bad = 0\n"
        "for n, f in tests:\n"
        "    try:\n"
        "        r = f()\n"
        "        if inspect.isawaitable(r): asyncio.run(r)\n"
        "    except Exception:\n"
        "        bad += 1; print('FAILED', n); print(traceback.format_exc(limit=3)[-600:])\n"
        "print(f'{len(tests) - bad} of {len(tests)} tests passed')\n"
        "sys.exit(1 if bad or not tests else 0)\n"
    )
    try:
        r = subprocess.run([sys.executable, "-E", "-B", "-c", script], cwd=folder, capture_output=True,
                           text=True, timeout=timeout, env=_clean_env())
    except subprocess.TimeoutExpired:
        return f"The tests took longer than {timeout:.0f} s (no network calls in tests)."
    return None if r.returncode == 0 else ((r.stdout + r.stderr).strip()[-1500:] or "The tests failed.")


def install(name: str) -> None:
    """Move a staged skill (with its tests) into data/skills/<name>/; drop an older copy in agent/skills."""
    src = os.path.join(OWN_DIR, "_proposed", name)
    dst = os.path.join(OWN_DIR, name)
    os.makedirs(dst, exist_ok=True)
    for fname in (f"{name}.py", f"test_{name}.py"):
        os.replace(os.path.join(src, fname), os.path.join(dst, fname))
    import shutil
    shutil.rmtree(src, ignore_errors=True)
    legacy = _legacy_file(name)
    if os.path.exists(legacy):
        with open(legacy, encoding="utf-8") as f:
            if f.read(len(_SELF_WRITTEN)) == _SELF_WRITTEN:
                os.remove(legacy)


def remove(name: str) -> bool:
    """Delete a self-written skill and/or the user's playbook of this name. False if there is none (built-ins stay)."""
    import shutil
    gone = False
    own = os.path.join(OWN_DIR, name)
    if os.path.isdir(own):
        shutil.rmtree(own)
        gone = True
    legacy = _legacy_file(name)
    if os.path.exists(legacy):
        with open(legacy, encoding="utf-8") as f:
            mine = f.read(len(_SELF_WRITTEN)) == _SELF_WRITTEN
        if mine:
            os.remove(legacy)
            gone = True
    return gone
