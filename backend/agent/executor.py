"""
agent/executor.py — S.H.A.R.P. action executor.

All tool handlers live here. execute(step) dispatches to the right handler.
Also contains the intent detector (regex fast-path, zero LLM cost).
"""

import os
import re
import asyncio
from datetime import datetime, timezone, timedelta

import pytz
from loguru import logger

from shared import web_search, fetch_url, ollama_client, OLLAMA_MODEL, OLLAMA_OPTIONS, _repair_json
from shell_executor import execute_shell
from scheduler import reminder_store, parse_reminder_from_plan

# Prefix for internal PowerShell commands (open_app / open_url).
_PS = "powershell -NoProfile -Command "


def _is_skill_action(action: str) -> bool:
    """True when the planner used a skill's function name directly as the action."""
    from agent.skills_manager import is_skill
    return is_skill(action)


def _ps_quote(s: str) -> str:
    """Make text safe inside a single-quoted PowerShell string."""
    # Inside '...' PowerShell treats everything literally except a quote; the
    # whole command is wrapped in "..." for cmd.exe, so drop double quotes too.
    return re.sub(r"[\"%\r\n]", "", s).replace("'", "''")

ISRAEL_TZ = pytz.timezone("Asia/Jerusalem")

_USER_HOME = os.getenv("ENZO_HOME") or os.path.expanduser("~")
_DOWNLOADS = os.path.join(_USER_HOME, "Downloads")
_DESKTOP   = os.path.join(_USER_HOME, "Desktop")
_DOCUMENTS = os.path.join(_USER_HOME, "Documents")

_CODE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".sh",
    ".ps1", ".cpp", ".c", ".cs", ".java", ".go", ".rs", ".rb", ".php", ".sql",
}

_EXT_MAP = {
    "python": ".py", "py": ".py", "javascript": ".js", "js": ".js",
    "typescript": ".ts", "html": ".html", "css": ".css",
    "markdown": ".md", "readme": ".md", "text": ".txt",
    "json": ".json", "bash": ".sh", "powershell": ".ps1",
    "java": ".java", "cpp": ".cpp", "sql": ".sql",
}


# ────────────────────────────────────────────────────────────────
# CONTEXT CARRY — remembers last used folder/file/app
# ────────────────────────────────────────────────────────────────
class _Carry:
    def __init__(self):
        self.folder = _DOWNLOADS
        self.file   = ""
        self.app    = ""

    def set_folder(self, p: str):
        if p and os.path.isabs(p):
            self.folder = p

    def set_file(self, p: str):
        if p:
            self.file = p
            self.set_folder(os.path.dirname(p))


_carry = _Carry()

_SHORTCUTS = {
    "there": _DOWNLOADS, "that folder": _DOWNLOADS,
    "same folder": _DOWNLOADS, "downloads": _DOWNLOADS,
    "desktop": _DESKTOP, "documents": _DOCUMENTS,
    "home": _USER_HOME, "~": _USER_HOME,
}


def _resolve_path(raw: str) -> str:
    raw   = raw.strip().strip("'\"")
    lower = raw.lower().strip()
    if lower in ("there", "that folder", "same folder", "it"):
        return _carry.folder
    for short, full in _SHORTCUTS.items():
        if not full:
            continue
        if lower == short:
            return full
        if lower.startswith(short + "\\") or lower.startswith(short + "/"):
            return full + raw[len(short):]
    if not re.match(r"^[A-Za-z]:\\", raw) and not raw.startswith("\\\\"):
        return os.path.join(_carry.folder, raw)
    return raw


def _guess_ext(hint: str) -> str:
    lower = hint.lower()
    for k, v in _EXT_MAP.items():
        if k in lower:
            return v
    return ".txt"


# ────────────────────────────────────────────────────────────────
# NATIVE FILE I/O
# ────────────────────────────────────────────────────────────────
def _native_write(path: str, content: str) -> dict:
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _native_read(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return {"success": True, "content": f.read()}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ────────────────────────────────────────────────────────────────
# CODE GENERATOR
# ────────────────────────────────────────────────────────────────
async def _generate_content(description: str, path: str) -> str:
    from shared import CODE_MODEL
    ext   = os.path.splitext(path)[1].lower()
    model = CODE_MODEL if ext in _CODE_EXTS else OLLAMA_MODEL
    if ext in _CODE_EXTS:
        prompt = (
            f"Write {ext} code for this task. "
            "Output ONLY the code, no explanation, no markdown fences:\n\n"
            f"{description}"
        )
    else:
        prompt = f"Write the content for a {ext} file. Output ONLY the content:\n\n{description}"

    resp = await asyncio.to_thread(
        ollama_client.chat,
        model=model,
        messages=[{"role": "user", "content": prompt}],
        options=OLLAMA_OPTIONS,
    )
    content = resp["message"]["content"].strip()
    content = re.sub(r"^```\w*\n?", "", content)
    content = re.sub(r"\n?```$", "", content)
    return content.strip()


# ────────────────────────────────────────────────────────────────
# REALTIME DETECTOR
# ────────────────────────────────────────────────────────────────
# ────────────────────────────────────────────────────────────────
# EXECUTE — dispatches a single step
# ────────────────────────────────────────────────────────────────
async def execute(step: dict) -> str:
    action = step.get("action", "none")
    inp    = step.get("input") or ""

    if action == "none":
        return ""

    if action == "search":
        q = str(inp).strip() or "general query"
        logger.info(f"🔍 Search: {q}")
        r = await asyncio.to_thread(web_search, q)
        return f"[SEARCH RESULTS for '{q}']:\n{r}"

    elif action == "shell":
        cmd = str(inp).strip()
        logger.info(f"⚙️  Shell: {cmd[:80]}")
        # Written by the model → strict read-only allowlist.
        r = await asyncio.to_thread(execute_shell, cmd, True)
        if r.get("success"):
            return f"[SHELL OUTPUT]:\n{r.get('output') or 'Done.'}"
        detail = r.get("output", "")
        return f"[SHELL ERROR]: {r.get('error')}" + (f"\n{detail[:1500]}" if detail else "")

    elif action == "execute_python":
        import tempfile
        import subprocess
        # Native CodeAct implementation
        code = str(inp).strip()
        if code.startswith("```python"):
            code = code[9:]
        code = code.strip("` \n")
        logger.info(f"🐍 Python Execution: {len(code)} bytes")
        
        try:
            with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w", encoding="utf-8") as f:
                f.write(code)
                script_path = f.name
                
            # Run with a 15-second timeout to prevent infinite loops
            result = subprocess.run(
                ["python", script_path],
                capture_output=True,
                text=True,
                timeout=15
            )
            os.remove(script_path)
            
            out = result.stdout.strip()
            err = result.stderr.strip()
            if result.returncode == 0:
                return f"[PYTHON STDOUT]:\n{out if out else 'Success (No output)'}"
            else:
                return f"[PYTHON ERROR]:\n{err if err else 'Unknown error'}"
        except subprocess.TimeoutExpired:
            return "[PYTHON ERROR]: Execution timed out after 15 seconds."
        except Exception as e:
            return f"[PYTHON FATAL]: {e}"

    elif action == "execute_skill" or _is_skill_action(action):
        from agent.skills_manager import execute_skill as run_dynamic_skill, resolve_skill_call
        skill_name, kwargs = resolve_skill_call(action, inp)
        logger.info(f"🧩 Execute Skill: {skill_name}({kwargs})")
        return await run_dynamic_skill(skill_name, kwargs)

    elif action == "create_skill":
        import ast
        from agent.skills_manager import load_skills
        skill_name = inp.get("skill_name")
        code = inp.get("code")
        if not skill_name or not code:
            return "[SKILL ERROR]: missing skill_name or code."
        if not skill_name.endswith(".py"):
            skill_name += ".py"
        file_path = os.path.join(os.path.dirname(__file__), "skills", skill_name)
        try:
            # Syntax validation before saving
            ast.parse(code)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(code)
            load_skills() # Hot reload the agent's brain
            logger.info(f"🧠 SKILL DISCOVERY SUCCESS: {skill_name} authored natively.")
            return f"[SKILL CREATED]: {skill_name} successfully written and loaded into context! You can now use execute_skill."
        except SyntaxError as e:
            return f"[SKILL ERROR]: Syntax Error in your Python code: {e}"
        except Exception as e:
            return f"[SKILL ERROR]: Fatal error creating skill: {e}"

    elif action == "create_folder":
        path = _resolve_path(str(inp))
        try:
            os.makedirs(path, exist_ok=True)
            _carry.set_folder(path)
            return f"[SHELL OUTPUT]: Folder created at {path}"
        except Exception as e:
            return f"[SHELL ERROR]: {e}"

    elif action == "list_files":
        path = _resolve_path(str(inp)) if inp else _carry.folder
        try:
            if not os.path.exists(path):
                return f"[SHELL ERROR]: Path not found: {path}"
            entries = os.listdir(path)
            listing = "\n".join(entries[:50]) if entries else "(empty)"
            return f"[SHELL OUTPUT]:\n{listing}"
        except Exception as e:
            return f"[SHELL ERROR]: {e}"

    elif action == "generate_and_write":
        parts = str(inp).split("|", 1)
        if len(parts) == 2:
            path, desc = _resolve_path(parts[0].strip()), parts[1].strip()
        else:
            desc = str(inp).strip()
            path = os.path.join(_carry.folder, "output" + _guess_ext(desc))
        logger.info(f"✨ Generate: {path}")
        content = await _generate_content(desc, path)
        r = await asyncio.to_thread(_native_write, path, content)
        if r.get("success"):
            _carry.set_file(path)
            preview = content[:120].replace("\n", " ")
            return f"[SHELL OUTPUT]: Written to {path}\nPreview: {preview}"
        return f"[SHELL ERROR]: {r.get('error')}"

    elif action == "write_file":
        parts = str(inp).split("|", 1)
        if len(parts) == 2:
            path, content = _resolve_path(parts[0].strip()), parts[1]
            r = await asyncio.to_thread(_native_write, path, content)
            if r.get("success"):
                _carry.set_file(path)
                return f"[SHELL OUTPUT]: File written at {path}"
            return f"[SHELL ERROR]: {r.get('error')}"
        return "SYSTEM: needs 'path|content'."

    elif action == "read_file":
        path = _resolve_path(str(inp))
        r    = _native_read(path)
        if r.get("success"):
            _carry.set_file(path)
            return f"[FILE CONTENT: {path}]:\n{r['content'][:3000]}"
        return f"[SHELL ERROR]: {r.get('error')}"

    elif action == "delete_file":
        path = _resolve_path(str(inp))
        try:
            import shutil
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
            return f"[SHELL OUTPUT]: Deleted {path}"
        except Exception as e:
            return f"[SHELL ERROR]: {e}"

    elif action == "move_file":
        parts = str(inp).split("|", 1)
        if len(parts) == 2:
            import shutil
            src = _resolve_path(parts[0].strip())
            dst = _resolve_path(parts[1].strip())
            try:
                shutil.move(src, dst)
                _carry.set_file(dst)
                return f"[SHELL OUTPUT]: Moved {src} to {dst}"
            except Exception as e:
                return f"[SHELL ERROR]: {e}"

    elif action == "copy_file":
        parts = str(inp).split("|", 1)
        if len(parts) == 2:
            import shutil
            src = _resolve_path(parts[0].strip())
            dst = _resolve_path(parts[1].strip())
            try:
                shutil.copy2(src, dst)
                return f"[SHELL OUTPUT]: Copied {src} to {dst}"
            except Exception as e:
                return f"[SHELL ERROR]: {e}"

    elif action == "open_app":
        app = str(inp).strip().lower()

        # Known URI protocol apps — fastest and most reliable
        _URI_APPS = {
            "steam":      "steam://open/main",
            "spotify":    "spotify:",
            "discord":    "discord:",
            "slack":      "slack:",
            "vscode":     "vscode:",
            "code":       "vscode:",
            "whatsapp":   "whatsapp:",
            "telegram":   None,  # no URI; use Start-Process
        }

        # Method 1: URI protocol (instant, no AppID lookup needed)
        uri = _URI_APPS.get(app)
        if uri:
            cmd = _PS + f'"Start-Process \'{uri}\'"'
            r   = await asyncio.to_thread(execute_shell, cmd)
            if r.get("success"):
                return f"[SHELL OUTPUT]: {app} opened."

        # Method 2: Start-Process by executable name (works for most installed apps)
        # Strip common filler words for cleaner matching
        app_clean = re.sub(r"\b(app|application|program|software|the|a|an)\b", "", app).strip()
        app_clean = _ps_quote(app_clean)
        if not app_clean:
            return f"[SHELL ERROR]: Could not find or launch '{app}'."
        cmd2 = _PS + f'"Start-Process \'{app_clean}\' -ErrorAction SilentlyContinue"'
        r2   = await asyncio.to_thread(execute_shell, cmd2)
        if r2.get("success") and "cannot find" not in r2.get("output", "").lower():
            return f"[SHELL OUTPUT]: {app} launched."

        # Method 3: Get-StartApps fuzzy match (UWP/Store apps, takes ~1s)
        # Fixed: single backslash in shell:AppsFolder\  (was double-escaped)
        ps_inner = (
            f"$pkg = Get-StartApps | Where-Object {{ $_.Name -like '*{app_clean}*' }} | "
            "Sort-Object { $_.Name.Length } | Select-Object -First 1; "
            "if ($pkg) { Start-Process ('shell:AppsFolder\\' + $pkg.AppID); "
            "Write-Host ('ok:' + $pkg.Name) } else { Write-Host 'NOT_FOUND' }"
        )
        cmd3 = _PS + f'"{ps_inner}"'
        r3   = await asyncio.to_thread(execute_shell, cmd3)
        out3 = r3.get("output", "") + r3.get("error", "")
        if r3.get("success") and "not_found" not in out3.lower():
            return f"[SHELL OUTPUT]: {app} opened."

        return f"[SHELL ERROR]: Could not find or launch '{app}'. Is it installed?"

    elif action == "open_url":
        url = str(inp).strip()
        if not url.startswith("http"):
            url = "https://" + url
        url = _ps_quote(url)
        cmd = _PS + '"Start-Process \'' + url + '\'"'
        r = await asyncio.to_thread(execute_shell, cmd)
        return (
            f"[SHELL OUTPUT]: Opened {url}."
            if r.get("success") else f"[SHELL ERROR]: {r.get('error')}"
        )

    elif action == "fetch_url":
        r = await asyncio.to_thread(fetch_url, str(inp))
        return r if r.startswith("[BLOCKED]") else f"[PAGE CONTENT]:\n{r}"

    elif action == "set_reminder":
        raw    = inp if isinstance(inp, dict) else _repair_json(str(inp))
        parsed = parse_reminder_from_plan(raw)
        if not parsed:
            return "[REMINDER ERROR]: Could not parse reminder."
        if isinstance(raw, dict) and raw.get("command"):
            if not parsed.get("metadata"):
                parsed["metadata"] = {}
            parsed["metadata"]["command"] = raw["command"]
        rid = reminder_store.add(**parsed)
        if not rid:
            return "[REMINDER ERROR]: Failed to save."
        from agent import phone_reminders
        await phone_reminders.sync()
        rtype = parsed.get("reminder_type", "once")
        msg   = parsed.get("message", "")
        if rtype == "recurring":
            secs = parsed.get("interval_seconds", 0)
            mins = secs // 60
            unit = f"{mins} minute{'s' if mins != 1 else ''}" if mins else f"{secs} seconds"
            return f"[SHELL OUTPUT]: Recurring reminder every {unit}: '{msg}' [{rid}]"
        fire = parsed.get("fire_at")
        if fire:
            local = fire.astimezone(ISRAEL_TZ).strftime("%H:%M on %d/%m")
            kind  = "Alarm" if rtype == "alarm" else "Reminder"
            return f"[SHELL OUTPUT]: {kind} set for {local}: '{msg}' [{rid}]"
        secs = parsed.get("interval_seconds", 0)
        unit = f"{secs // 60} minutes" if secs >= 60 else f"{secs} seconds"
        return f"[SHELL OUTPUT]: Timer set for {unit}: '{msg}' [{rid}]"

    elif action == "list_reminders":
        reminders = reminder_store.list_active()
        if not reminders:
            return "[SHELL OUTPUT]: No active reminders."
        lines = []
        for n, r in enumerate(reminders[:10], 1):
            nf     = r.get("next_fire")
            label  = r.get("label", r.get("message", "?"))[:35]
            rtype  = r.get("type", "once")
            rid    = r["_id"]
            nf_str = "?"
            if nf:
                if nf.tzinfo is None:                       # Mongo returns UTC without a label
                    nf = nf.replace(tzinfo=timezone.utc)
                mins = max(0, int((nf - datetime.now(timezone.utc)).total_seconds() // 60))
                left = f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{mins} min"
                nf_str = f"{nf.astimezone(ISRAEL_TZ).strftime('%H:%M %d/%m')} (in {left})"
            lines.append(f"{n}. [{rid}] {label} ({rtype}) -> {nf_str}")
        return "[SHELL OUTPUT]:\n" + "\n".join(lines)

    elif action == "cancel_reminder":
        t = str(inp).strip().lstrip("#")
        from agent import phone_reminders
        if t.isdigit():                                     # "cancel number 2" = 2nd line of list_reminders
            active = reminder_store.list_active()[:10]
            if 1 <= int(t) <= len(active):
                r = active[int(t) - 1]
                if reminder_store.cancel(r["_id"]):
                    await phone_reminders.sync()
                    return f"[SHELL OUTPUT]: Reminder {t} cancelled: '{r.get('label', r.get('message', '?'))[:35]}' [{r['_id']}]."
        if reminder_store.cancel(t):
            await phone_reminders.sync()
            return f"[SHELL OUTPUT]: Reminder {t} cancelled."
        n = reminder_store.cancel_by_label(t)
        if n:
            await phone_reminders.sync()
        return (
            f"[SHELL OUTPUT]: Cancelled {n} reminder(s)."
            if n else f"[SHELL OUTPUT]: No reminder found matching '{t}'."
        )

    elif action == "save_memory":
        from agent.memory import save_explicit
        updates = inp if isinstance(inp, dict) else (_repair_json(str(inp)) if inp else {})
        # If the input is effectively empty (fast-path "I like X" trigger),
        # the background extract_and_save will handle the actual structured save.
        # Just acknowledge for now — the background task already queued.
        if not updates or updates == {}:
            return "[MEMORY]: Noted. Saving what you told me."
        return save_explicit(updates)

    return ""
