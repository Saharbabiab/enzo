"""
pc_code_jobs.py - coding jobs Enzo hands to Claude Code on the user's PC (runs inside pc_helper.py).

Enzo (agent/code_jobs.py) sends: folders | start | apply | discard. A job:
  1. a git worktree of the project on a new branch  enzo/job-<id>  (his own checkout is never touched),
  2. Claude Code runs headless there (`claude -p`) with a fixed tool list: read/edit files, run tests, git read-only;
     no web, no push, no .env. The child gets a minimal environment (none of Enzo's keys),
  3. the changes are committed on that branch, the worktree is removed, and the result (summary, diff) is saved in
     data/code_jobs/<id>.json until Enzo acknowledges it (pc_helper resends unacknowledged results).
Nothing reaches his real branch until he says apply (Enzo asks him every time); discard deletes the branch.

Projects: git repositories inside WORK_ROOT (CODE_WORK_ROOT, default ~/Desktop/work), up to two levels deep.
Login: Claude Code's own (CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`, in this PC's .env). CLAUDE_BIN overrides
where claude.exe is; by default PATH, then the copy the Claude desktop app keeps.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

try:    # pc_helper does not load .env: take only the keys code jobs need, never the other secrets
    from dotenv import dotenv_values
    for _k, _v in dotenv_values(Path(__file__).parent / ".env").items():
        if _k in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_BIN", "CODE_WORK_ROOT", "CODE_JOB_TIMEOUT") and _v:
            os.environ.setdefault(_k, _v)
except ImportError:
    pass

WORK_ROOT = Path(os.getenv("CODE_WORK_ROOT", str(Path.home() / "Desktop" / "work"))).resolve()
JOBS_DIR = Path(__file__).parent / "data" / "code_jobs"
TIMEOUT = int(os.getenv("CODE_JOB_TIMEOUT", "1200"))
MAX_DIFF = 20_000
ID_RE = re.compile(r"^[a-z0-9]{6,16}$")

ALLOWED_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "TodoWrite",
                 "Bash(python -m pytest:*)", "Bash(pytest:*)", "Bash(python -m py_compile:*)",
                 "Bash(npm test:*)", "Bash(npm run test:*)", "Bash(npm run lint:*)", "Bash(npx tsc:*)",
                 "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)"]
DENIED_TOOLS = ["WebFetch", "WebSearch", "Bash(git push:*)", "Bash(git commit:*)", "Bash(git checkout:*)",
                "Bash(git reset:*)", "Bash(curl:*)", "Read(**/.env)", "Read(**/.env.*)", "Read(**/*.pem)"]
RULES = ("You are fixing code for the user. His home assistant Enzo sent this job; he is not watching, so do not ask "
         "questions: make the best reasonable choice and say what you assumed. Work only inside this folder. Make the "
         "smallest change that fully does the job, keep the project's style, and run its tests if it has any. Do not "
         "commit, push, deploy or install anything, and never read or print secrets (.env files, keys, tokens). "
         "Finish with a short plain-English summary for him: what was wrong, what you changed (files), and the test "
         "result.")
SAFE_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "HOME", "HOMEDRIVE",
            "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "USERNAME",
            "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "LANG", "CLAUDE_CODE_OAUTH_TOKEN")

_busy = threading.Lock()


def claude_cmd() -> list[str]:
    exe = os.getenv("CLAUDE_BIN") or shutil.which("claude")
    if not exe:      # the copy the Claude desktop app keeps (the path has its version in it)
        found = glob.glob(os.path.join(os.getenv("LOCALAPPDATA", ""), "Claude-Data", "*", "claude-code", "*", "*", "claude.exe"))
        exe = max(found, key=os.path.getmtime) if found else ""
    return [exe] if exe else []


def _git(repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def folders() -> list[str]:
    """His projects: git repositories under WORK_ROOT (one or two levels down), as paths relative to it."""
    out = []
    for pattern in ("*/.git", "*/*/.git"):
        for g in sorted(WORK_ROOT.glob(pattern)):
            rel = g.parent.relative_to(WORK_ROOT).as_posix()
            if not rel.startswith("."):
                out.append(rel)
    return out


def resolve(folder: str) -> tuple[Path | None, str]:
    """(the repository, "") or (None, why not). Only repositories inside WORK_ROOT."""
    name = str(folder or "").strip().strip("/\\")
    if not name:
        return None, "Say which project (folder)."
    p = (WORK_ROOT / name).resolve()
    if p != WORK_ROOT and WORK_ROOT not in p.parents:
        return None, f"'{folder}' is outside the work folder."
    if not p.is_dir():
        known = [f for f in folders() if f.split("/")[-1].lower() == name.split("/")[-1].lower()]
        if len(known) == 1:
            p = (WORK_ROOT / known[0]).resolve()
        else:
            return None, f"There is no project '{folder}'. Projects: {', '.join(folders()) or 'none'}."
    if not (p / ".git").exists():
        return None, f"'{folder}' is not a git project, so its changes could not be undone. Projects: {', '.join(folders())}."
    return p, ""


def _save(job_id: str, result: dict) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = JOBS_DIR / f"{job_id}.tmp"
    tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    tmp.replace(JOBS_DIR / f"{job_id}.json")


def unsent() -> list[dict]:
    """Finished jobs Enzo has not acknowledged yet. A job left 'running' by a helper that stopped is reported failed."""
    out = []
    for f in sorted(JOBS_DIR.glob("*.json")) if JOBS_DIR.exists() else []:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if r.get("state") == "running" and not _busy.locked():
            r = {**r, "state": "failed", "error": "The PC helper stopped while the job was running."}
            _save(r["id"], r)
        if r.get("state") != "running":
            out.append(r)
    return out


def ack(job_id: str) -> None:
    if ID_RE.match(job_id or ""):
        (JOBS_DIR / f"{job_id}.json").unlink(missing_ok=True)


def child_env() -> dict:
    env = {k: os.environ[k] for k in SAFE_ENV if k in os.environ}
    if os.getenv("CODE_JOB_USE_API", "").lower() == "true" and os.getenv("ANTHROPIC_API_KEY"):
        env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]      # pay with the API key instead of the plan
    return env


def _run(job_id: str, repo: Path, task: str) -> dict:
    branch = f"enzo/job-{job_id}"
    tree = WORK_ROOT / ".enzo_jobs" / job_id
    base = {"id": job_id, "folder": repo.relative_to(WORK_ROOT).as_posix(), "branch": branch}
    r = _git(repo, "worktree", "add", "-b", branch, str(tree), "HEAD")
    if r.returncode:
        return {**base, "state": "failed", "error": f"Could not make a copy to work on: {r.stderr.strip()[:300]}"}
    try:
        cmd = claude_cmd() + ["-p", task, "--output-format", "json", "--permission-mode", "acceptEdits",
                              "--append-system-prompt", RULES, "--no-session-persistence",
                              "--allowedTools", *ALLOWED_TOOLS, "--disallowedTools", *DENIED_TOOLS]
        if os.getenv("CODE_JOB_MODEL"):
            cmd += ["--model", os.environ["CODE_JOB_MODEL"]]
        try:
            p = subprocess.run(cmd, cwd=tree, env=child_env(), capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=TIMEOUT, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return {**base, "state": "failed", "error": f"Claude Code ran longer than {TIMEOUT // 60} minutes and was stopped."}
        try:
            out = json.loads(p.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {**base, "state": "failed", "error": f"Claude Code did not answer properly: {(p.stderr or p.stdout).strip()[:400]}"}
        summary = str(out.get("result", "")).strip()
        if out.get("is_error"):
            hint = " Run 'claude setup-token' on the PC and put CLAUDE_CODE_OAUTH_TOKEN in its .env." if "login" in summary.lower() else ""
            return {**base, "state": "failed", "error": f"Claude Code: {summary[:400]}{hint}"}
        _git(tree, "add", "-A")
        if not _git(tree, "status", "--porcelain").stdout.strip():
            _git(repo, "worktree", "remove", "--force", str(tree))
            _git(repo, "branch", "-D", branch)
            return {**base, "state": "done", "changed": False, "summary": summary, "cost_usd": out.get("total_cost_usd")}
        c = _git(tree, "commit", "-m", f"Enzo code job {job_id}: {task.splitlines()[0][:60]}")
        if c.returncode:
            return {**base, "state": "failed", "error": f"Could not save the changes: {c.stderr.strip()[:300]}"}
        stat = _git(tree, "diff", "--stat", "HEAD~1", "HEAD").stdout.strip()
        diff = _git(tree, "diff", "HEAD~1", "HEAD").stdout
        return {**base, "state": "done", "changed": True, "summary": summary, "diffstat": stat,
                "diff": diff[:MAX_DIFF] + ("\n[...diff cut...]" if len(diff) > MAX_DIFF else ""),
                "cost_usd": out.get("total_cost_usd")}
    finally:
        if tree.exists():
            _git(repo, "worktree", "remove", "--force", str(tree))
        _git(repo, "worktree", "prune")


def start(job_id: str, folder: str, task: str) -> str:
    if not ID_RE.match(job_id or ""):
        return "[ERROR]: bad job id."
    if not str(task or "").strip():
        return "[ERROR]: The job has no task."
    repo, why = resolve(folder)
    if not repo:
        return f"[ERROR]: {why}"
    if not claude_cmd():
        return "[ERROR]: Claude Code is not installed on the PC (or set CLAUDE_BIN)."
    if not _busy.acquire(blocking=False):
        return "[BUSY]: Another job is running on the PC."
    _save(job_id, {"id": job_id, "folder": folder, "state": "running", "started": time.time()})

    def work():
        try:
            try:
                result = _run(job_id, repo, task.strip())
            except Exception as e:  # noqa: BLE001 - always report back
                result = {"id": job_id, "folder": folder, "state": "failed", "error": f"{type(e).__name__}: {e}"}
            _save(job_id, {**result, "finished": time.time()})
        finally:
            _busy.release()

    threading.Thread(target=work, daemon=True).start()
    return f"[OK]: Started in {repo.relative_to(WORK_ROOT).as_posix()}."


def apply(job_id: str, folder: str) -> str:
    repo, why = resolve(folder)
    if not repo or not ID_RE.match(job_id or ""):
        return f"[ERROR]: {why or 'bad job id.'}"
    branch = f"enzo/job-{job_id}"
    if _git(repo, "rev-parse", "--verify", "--quiet", branch).returncode:
        return f"[ERROR]: The job's branch {branch} is gone (applied or thrown away already?)."
    on = _git(repo, "branch", "--show-current").stdout.strip() or "HEAD"
    m = _git(repo, "merge", "--no-ff", "--no-edit", branch)
    if m.returncode:
        _git(repo, "merge", "--abort")
        return (f"[ERROR]: Could not apply it to {on}: {(m.stdout + m.stderr).strip()[:400]} Nothing was changed; the "
                f"job stays on branch {branch} for him to look at.")
    _git(repo, "branch", "-D", branch)
    return f"[OK]: Applied to {on} in {repo.relative_to(WORK_ROOT).as_posix()} (merged {branch}). Not pushed."


def discard(job_id: str, folder: str) -> str:
    repo, why = resolve(folder)
    if not repo or not ID_RE.match(job_id or ""):
        return f"[ERROR]: {why or 'bad job id.'}"
    r = _git(repo, "branch", "-D", f"enzo/job-{job_id}")
    return "[OK]: Thrown away." if r.returncode == 0 else "[OK]: Nothing to throw away (no branch left)."


def push(folder: str) -> str:
    """git push of his project's current branch (Enzo asked him first). Claude Code itself can never push."""
    repo, why = resolve(folder)
    if not repo:
        return f"[ERROR]: {why}"
    on = _git(repo, "branch", "--show-current").stdout.strip()
    if not on:
        return "[ERROR]: The project is not on a branch (detached HEAD); nothing was pushed."
    p = _git(repo, "push", "origin", on, timeout=120)
    said = (p.stdout + p.stderr).strip()
    if p.returncode:
        return f"[ERROR]: git push failed, nothing was pushed: {said[-400:]}"
    return f"[OK]: Pushed {on} of {repo.relative_to(WORK_ROOT).as_posix()} to origin. git said: {said.splitlines()[-1] if said else 'done'}"


def handle(args: dict) -> str:
    """pc_helper's entry point for the "code_job" call."""
    a = str(args.get("action", "")).lower()
    if a == "folders":
        return json.dumps({"root": str(WORK_ROOT), "folders": folders()})
    if a == "start":
        return start(str(args.get("id", "")), str(args.get("folder", "")), str(args.get("task", "")))
    if a == "apply":
        return apply(str(args.get("id", "")), str(args.get("folder", "")))
    if a == "discard":
        return discard(str(args.get("id", "")), str(args.get("folder", "")))
    if a == "push":
        return push(str(args.get("folder", "")))
    return "[ERROR]: action must be folders, start, apply, discard or push."
