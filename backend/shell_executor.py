"""
shell_executor.py — Secure shell command executor for S.H.A.R.P.

7-layer security: whitelist → blacklist → sandbox → timeout → output cap → audit log → read-only default.
All commands are logged to an audit file for accountability.
"""

import os
import re
import sys
import platform
import subprocess
import shlex
from datetime import datetime
from loguru import logger

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIT_LOG = os.path.join(_SCRIPT_DIR, os.getenv("SHELL_AUDIT_LOG", "enzo_audit.log"))
IS_WINDOWS = platform.system() == "Windows"

# ── Layer 1: WHITELISTED base commands ──────────────────────────
# Only these commands (the first word) are allowed to execute.
# Base commands allowed on all platforms
_BASE_COMMANDS = {
    # System monitoring (Linux/TrueNAS)
    "df", "free", "uptime", "top", "htop", "ps", "who", "hostname",
    "uname", "date", "cat", "head", "tail", "wc", "ls", "du", "find",
    "lsblk", "blkid", "mount", "stat", "file", "env", "printenv",
    "whoami", "id", "groups", "last", "dmesg", "journalctl",
    # Network
    "ping", "traceroute", "curl", "wget", "nslookup", "dig",
    "ip", "ss", "netstat", "ifconfig",
    # Docker
    "docker",
    # ZFS / TrueNAS
    "zpool", "zfs", "smartctl", "midcli",
    # Service management
    "systemctl", "service",
    # Other safe tools
    "echo", "which", "whereis", "type", "grep", "awk", "sed",
    "sort", "uniq", "cut", "tr", "tee", "xargs", "wc",
}

# Windows-specific commands
_WINDOWS_COMMANDS = {
    "powershell", "wmic", "systeminfo", "tasklist", "ipconfig",
    "dir", "where", "hostname", "net", "sc", "get-process",
    "get-service", "get-psdrive", "get-childitem", "get-content",
    "get-computerinfo", "get-disk", "get-volume", "get-partition",
    "get-netadapter", "get-nettcpconnection", "test-connection",
    "measure-object", "select-object", "format-table",
    "get-startapps", "start-process"
}

ALLOWED_COMMANDS = _BASE_COMMANDS | (_WINDOWS_COMMANDS if IS_WINDOWS else set())

# ── Layer 2: BLACKLISTED patterns (blocked even if base command is allowed) ─
# Regex patterns that are NEVER allowed in any command string.
BLACKLISTED_PATTERNS = [
    # Destructive file operations
    r"\brm\b", r"\brmdir\b", r"\bunlink\b",
    r"\bmkfs\b", r"\bdd\b", r"\bformat\b",
    r"\bfdisk\b", r"\bparted\b", r"\bgdisk\b",
    # System power
    r"\bshutdown\b", r"\breboot\b", r"\bhalt\b", r"\bpoweroff\b", r"\binit\b",
    # Dangerous permission changes
    r"chmod\s+777", r"chmod\s+-R", r"chown\s+-R",
    # Process killing (allow docker restart though)
    r"\bkill\s+-9\b", r"\bkillall\b", r"\bpkill\b",
    # Pipe to shell / code execution
    r"\|\s*bash", r"\|\s*sh\b", r"\|\s*zsh", r"\|\s*python",
    r"\beval\b", r"\bexec\b", r"__import__",
    r"curl.*\|\s*bash", r"wget.*\|\s*sh",
    # Privilege escalation
    r"\bsudo\s+su\b", r"\bsu\s+-\b", r"\bsu\s+root\b",
    # File write operators (unless SHELL_WRITE_ENABLED)
    r">\s*/", r">>\s*/",
    # Fork bomb
    r":\(\)\{", r"\./*\s*&",
    # Python/Perl/Ruby inline execution
    r"\bpython\s+-c\b", r"\bperl\s+-e\b", r"\bruby\s+-e\b",
]

# ── Layer 3: SANDBOXED read paths ───────────────────────────────
# File read operations are restricted to these directories.
if IS_WINDOWS:
    ALLOWED_READ_PATHS = [
        os.path.expanduser("~"),
        "C:\\Users\\",
        "C:\\temp\\",
        "C:\\Windows\\Temp\\",
    ]
else:
    ALLOWED_READ_PATHS = [
        "/mnt/",
        "/var/log/",
        "/tmp/",
        "/home/",
        "/root/",
        "/etc/",
        "/usr/local/",
        os.path.expanduser("~"),
    ]

# ── Layer 7: READ-ONLY default ──────────────────────────────────
WRITE_ENABLED = os.getenv("SHELL_WRITE_ENABLED", "false").lower() == "true"

# ── Constants ───────────────────────────────────────────────────
MAX_TIMEOUT = 30          # seconds
MAX_OUTPUT_CHARS = 3000   # characters returned to the LLM


def _audit_log(command: str, result: str, blocked: bool = False) -> None:
    """Append every command (allowed or blocked) to the audit log."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status = "BLOCKED" if blocked else "EXECUTED"
    entry = f"[{timestamp}] [{status}] {command}\n"
    if not blocked:
        # Log first 200 chars of output
        entry += f"  → {result[:200]}\n"
    try:
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception:
        pass  # Never crash on audit failure


def _get_base_command(command: str) -> str:
    """Extract the first word (base command) from a shell command string."""
    command = command.strip()
    # Handle sudo prefix
    if command.lower().startswith("sudo "):
        parts = command.split(None, 2)
        return parts[1].lower() if len(parts) > 1 else ""
    # Handle 'powershell -Command "..."' pattern
    if command.lower().startswith("powershell"):
        return "powershell"
    return command.split(None, 1)[0].lower() if command else ""


def _check_whitelist(command: str) -> tuple[bool, str]:
    """Layer 1: Check if the base command is in the whitelist (case-insensitive)."""
    base = _get_base_command(command)
    # Check against lowercased whitelist
    allowed_lower = {c.lower() for c in ALLOWED_COMMANDS}
    if base not in allowed_lower:
        return False, f"Command '{base}' is not in the allowed list."
    return True, ""


def _check_blacklist(command: str) -> tuple[bool, str]:
    """Layer 2: Check if any blacklisted pattern matches."""
    for pattern in BLACKLISTED_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return False, f"Command blocked by security policy (matched: {pattern})."
    return True, ""


def _check_write_ops(command: str) -> tuple[bool, str]:
    """Layer 7: Block write operations unless explicitly enabled."""
    if not WRITE_ENABLED:
        # Check for output redirection
        if re.search(r"[>|].*>", command):
            return False, "Write operations are disabled. Set SHELL_WRITE_ENABLED=true to allow."
        # Check for docker rm/stop/kill
        docker_destructive = re.search(
            r"docker\s+(rm|rmi|stop|kill|prune|volume\s+rm|network\s+rm|system\s+prune)",
            command, re.IGNORECASE
        )
        if docker_destructive:
            return False, f"Destructive Docker operation '{docker_destructive.group(1)}' is blocked in read-only mode."
    return True, ""


# ── Auto-translate Linux → Windows ──────────────────────────────
# The LLM always outputs Linux commands; we transparently convert
# them on Windows so the model doesn't need to know the OS.
_LINUX_TO_WINDOWS = {
    "df -h":     'powershell -Command "Get-PSDrive -PSProvider FileSystem | Format-Table Name, @{n=\'Used(GB)\';e={[math]::Round($_.Used/1GB,2)}}, @{n=\'Free(GB)\';e={[math]::Round($_.Free/1GB,2)}}"',
    "df":        'powershell -Command "Get-PSDrive -PSProvider FileSystem | Format-Table Name, Used, Free"',
    "free -h":   'powershell -Command "$os = Get-CimInstance Win32_OperatingSystem; Write-Host (\'Total: \' + [math]::Round($os.TotalVisibleMemorySize/1MB,2) + \' GB\'); Write-Host (\'Free: \' + [math]::Round($os.FreePhysicalMemory/1MB,2) + \' GB\'); Write-Host (\'Used: \' + [math]::Round(($os.TotalVisibleMemorySize - $os.FreePhysicalMemory)/1MB,2) + \' GB\')"',
    "free":      'powershell -Command "$os = Get-CimInstance Win32_OperatingSystem; Write-Host (\'Total: \' + [math]::Round($os.TotalVisibleMemorySize/1MB,2) + \' GB  Free: \' + [math]::Round($os.FreePhysicalMemory/1MB,2) + \' GB\')"',
    "uptime":    'powershell -Command "$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime; $up = (Get-Date) - $boot; Write-Host (\'Up \' + $up.Days + \'d \' + $up.Hours + \'h \' + $up.Minutes + \'m\')"',
    "uname -a":  'powershell -Command "[System.Environment]::OSVersion | Format-List"',
    "uname":     'powershell -Command "Write-Host $env:OS"',
    "whoami":    "whoami",
    "hostname":  "hostname",
    "top":       'powershell -Command "Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 Name,CPU,WorkingSet | Format-Table"',
    "ps aux":    'powershell -Command "Get-Process | Select-Object -First 20 Name,CPU,WorkingSet,Id | Format-Table"',
    "ps":        'powershell -Command "Get-Process | Select-Object -First 20 Name,CPU,Id | Format-Table"',
    "netstat":   "netstat -an",
    "ifconfig":  "ipconfig",
    "ip a":      "ipconfig /all",
    "ip addr":   "ipconfig /all",
}


def _translate_command(command: str) -> str:
    """On Windows, translate common Linux commands to their equivalents."""
    if not IS_WINDOWS:
        return command
    # Exact match first
    cmd_stripped = command.strip()
    if cmd_stripped in _LINUX_TO_WINDOWS:
        translated = _LINUX_TO_WINDOWS[cmd_stripped]
        logger.info(f"🔄 Translated: {cmd_stripped} → {translated}")
        return translated
    # Partial matches (e.g. "ls /home" → "dir C:\\Users")
    if cmd_stripped.lower().startswith("get-startapps"):
        return f'powershell -Command "{cmd_stripped}"'
    if cmd_stripped.startswith("ls "):
        return f"dir {cmd_stripped[3:]}"
    if cmd_stripped.startswith("cat "):
        return f'powershell -Command "Get-Content {cmd_stripped[4:]}"'
    if cmd_stripped.startswith("ping "):
        return cmd_stripped  # ping works on both
    if cmd_stripped.startswith("docker "):
        return cmd_stripped  # docker works on both
    return command


# ── Strict mode for commands written by the LLM ─────────────────
# Internal callers (heartbeat, open_app) build fixed commands and use the
# normal path. Anything the model writes goes through _check_untrusted:
# every command word in the pipeline must be on a read-only allowlist.

_SAFE_PS = {
    # read-only cmdlets
    "get-process", "get-service", "get-psdrive", "get-childitem", "get-content",
    "get-computerinfo", "get-disk", "get-volume", "get-partition", "get-netadapter",
    "get-nettcpconnection", "get-netipaddress", "get-ciminstance", "get-wmiobject",
    "get-date", "get-item", "get-itemproperty", "get-location", "get-hotfix",
    "get-uptime", "get-startapps", "get-winevent", "get-eventlog", "resolve-dnsname",
    "test-connection", "test-path",
    # pipeline / formatting
    "measure-object", "select-object", "sort-object", "where-object", "group-object",
    "foreach-object", "format-table", "format-list", "out-string", "select-string",
    "write-host", "write-output",
    # aliases / native read-only tools
    "?", "%", "where", "select", "sort", "ft", "fl", "dir", "ls", "gci", "gc", "cat",
    "echo", "ipconfig", "systeminfo", "tasklist", "hostname", "whoami", "netstat",
    "ping", "nslookup", "tracert", "docker",
}

_SAFE_LINUX = {
    "df", "free", "uptime", "top", "ps", "who", "hostname", "uname", "date", "cat",
    "head", "tail", "wc", "ls", "du", "find", "lsblk", "blkid", "stat", "file",
    "whoami", "id", "groups", "last", "dmesg", "journalctl", "ping", "traceroute",
    "nslookup", "dig", "ip", "ss", "netstat", "ifconfig", "zpool", "zfs", "smartctl",
    "systemctl", "docker", "echo", "which", "whereis", "grep", "sort", "uniq", "cut",
    "tr",
}

# Commands that are only safe with certain subcommands.
_SUBCOMMANDS_RO = {
    "docker":    r"(ps|logs|stats|images|inspect|version|info|top)\b",
    "systemctl": r"(status|is-active|is-enabled|list-units|list-timers)\b",
    "zfs":       r"(list|get)\b",
    "zpool":     r"(list|status|iostat|get)\b",
}
_SUBCOMMANDS_RW = {
    "docker":    r"(restart|start|stop)\b",
    "systemctl": r"(restart|start|stop)\b",
}

_UNTRUSTED_BLOCK = [
    (r"`",                         "backticks"),
    (r"\$\(",                      "command substitution"),
    (r"[<>]",                      "redirection"),
    (r"(?<!\[math)\]\s*::",        ".NET static calls"),
    (r"\.\s*[A-Za-z_]\w*\s*\(",    ".NET method calls"),
    (r"-enc",                      "encoded commands"),
    (r"\.env\b|sharp_memory",      "reading secrets"),
    (r"\s-(exec|execdir|delete|ok)\b", "find actions"),
]

_SPLIT_RE = re.compile(r"(\|\||&&|[;|&\n{}()=])")
_STRING_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


def _ps_payload(command: str) -> str:
    """Return the script inside 'powershell -Command "..."' (or the command as-is)."""
    m = re.match(r'^\s*powershell(?:\.exe)?\s+(?:-\w+\s+)*?-c(?:ommand)?\s+(.*)$',
                 command, re.IGNORECASE | re.DOTALL)
    if not m:
        return command
    body = m.group(1).strip()
    if len(body) >= 2 and body[0] == body[-1] and body[0] in "\"'":
        body = body[1:-1]
    return body


def _check_untrusted(payload: str) -> tuple[bool, str]:
    """Validate an LLM-written command. `payload` is the bare script (no powershell wrapper)."""
    for pattern, why in _UNTRUSTED_BLOCK:
        if re.search(pattern, payload, re.IGNORECASE):
            return False, f"Blocked: {why} are not allowed in assistant shell commands."

    allowed = _SAFE_PS if IS_WINDOWS else _SAFE_LINUX
    # Blank out quoted text so its contents can't be mistaken for commands.
    parts = _SPLIT_RE.split(_STRING_RE.sub("''", payload))
    for i in range(0, len(parts), 2):
        seg = parts[i].strip()
        next_delim = parts[i + 1] if i + 1 < len(parts) else ""
        if not seg or next_delim == "=":
            continue  # empty, or a name being assigned (`$x =`, `@{n=...}`)
        # Expressions (variables, strings, numbers, [math]::, flags) are not commands.
        if seg[0] in "$'\"-+*/![@0123456789.":
            continue
        word, _, rest = seg.partition(" ")
        word = word.lower()
        if word not in allowed:
            return False, f"Command '{word}' is not allowed for the assistant (read-only allowlist)."
        if word in _SUBCOMMANDS_RO:
            pattern = _SUBCOMMANDS_RO[word]
            if WRITE_ENABLED and word in _SUBCOMMANDS_RW:
                pattern = f"(?:{pattern}|{_SUBCOMMANDS_RW[word]})"
            if not re.match(pattern, rest.strip(), re.IGNORECASE):
                return False, f"'{word} {rest.strip().split(' ')[0]}' is not allowed (read-only)."
    return True, ""


def _child_env() -> dict:
    """Environment for subprocesses, without API keys, tokens or DB credentials."""
    secret = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|MONGO|URI", re.IGNORECASE)
    env = {k: v for k, v in os.environ.items() if not secret.search(k)}
    env["TERM"] = "dumb"  # Prevent color codes
    return env


def execute_shell(command: str, untrusted: bool = False) -> dict:
    """
    Execute a shell command with full security stack.

    untrusted=True is for commands written by the LLM: they must pass the
    strict read-only allowlist (_check_untrusted). On Windows they are run
    through PowerShell automatically.

    Returns:
        {
            "success": bool,
            "output": str,     # stdout + stderr (truncated)
            "error": str,      # security violation message if blocked
            "command": str,    # the command that was (or wasn't) executed
        }
    """
    command = command.strip()

    if not command:
        return {"success": False, "output": "", "error": "Empty command.", "command": ""}

    # Auto-translate Linux commands on Windows
    original_command = command
    command = _translate_command(command)
    was_translated = (command != original_command)

    # ── Strict mode for LLM-written commands ──
    if untrusted and not was_translated:
        payload = _ps_payload(command) if IS_WINDOWS else command
        ok, msg = _check_untrusted(payload)
        if not ok:
            _audit_log(command, msg, blocked=True)
            logger.warning(f"🛡️ BLOCKED (untrusted): {command}")
            return {"success": False, "output": "", "error": msg, "command": command}
        if IS_WINDOWS:
            command = 'powershell -NoProfile -Command "' + payload.replace('"', '\\"') + '"'

    # ── Layer 1: Whitelist ──
    ok, msg = _check_whitelist(command)
    if not ok:
        _audit_log(command, msg, blocked=True)
        logger.warning(f"🛡️ BLOCKED (whitelist): {command}")
        return {"success": False, "output": "", "error": msg, "command": command}

    # ── Layer 2: Blacklist (skip for auto-translated commands — they're safe) ──
    if not was_translated:
        ok, msg = _check_blacklist(command)
        if not ok:
            _audit_log(command, msg, blocked=True)
            logger.warning(f"🛡️ BLOCKED (blacklist): {command}")
            return {"success": False, "output": "", "error": msg, "command": command}

    # ── Layer 7: Read-only check (skip for auto-translated) ──
    if not was_translated:
        ok, msg = _check_write_ops(command)
        if not ok:
            _audit_log(command, msg, blocked=True)
            logger.warning(f"🛡️ BLOCKED (read-only): {command}")
            return {"success": False, "output": "", "error": msg, "command": command}

    # ── Layer 4: Execute with timeout ──
    logger.info(f"⚙️ Executing: {command}")
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=MAX_TIMEOUT,
            env=_child_env(),
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[STDERR]: {result.stderr}"

        # ── Layer 5: Output cap ──
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + f"\n\n[... output truncated at {MAX_OUTPUT_CHARS} chars]"

        _audit_log(command, output)
        # A non-zero exit code means the command failed (e.g. Start-Process on an
        # unknown app) — report it so callers can try another approach.
        ok = result.returncode == 0
        err = "" if ok else (result.stderr.strip()[:500] or f"Command exited with code {result.returncode}.")
        return {"success": ok, "output": output.strip(), "error": err, "command": command}

    except subprocess.TimeoutExpired:
        msg = f"Command timed out after {MAX_TIMEOUT} seconds."
        _audit_log(command, msg)
        logger.warning(f"⏰ Timeout: {command}")
        return {"success": False, "output": "", "error": msg, "command": command}

    except Exception as e:
        msg = f"Execution error: {str(e)}"
        _audit_log(command, msg)
        logger.error(f"❌ Shell error: {e}")
        return {"success": False, "output": "", "error": msg, "command": command}


def read_file(filepath: str) -> dict:
    """
    Read a text file with path sandboxing.

    Returns:
        {"success": bool, "content": str, "error": str}
    """
    filepath = os.path.abspath(filepath.strip())

    # ── Layer 3: Path sandbox ──
    # Block path traversal
    if ".." in filepath:
        _audit_log(f"read_file({filepath})", "Path traversal blocked", blocked=True)
        return {"success": False, "content": "", "error": "Path traversal (..) is not allowed."}

    # Check if path is under allowed directories
    allowed = any(filepath.startswith(p) for p in ALLOWED_READ_PATHS)
    if not allowed:
        _audit_log(f"read_file({filepath})", "Outside sandbox", blocked=True)
        logger.warning(f"🛡️ BLOCKED read_file: {filepath} (not in allowed paths)")
        return {"success": False, "content": "", "error": f"Access denied. File must be under: {', '.join(ALLOWED_READ_PATHS)}"}

    if not os.path.exists(filepath):
        return {"success": False, "content": "", "error": f"File not found: {filepath}"}

    if not os.path.isfile(filepath):
        return {"success": False, "content": "", "error": f"Not a file: {filepath}"}

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(MAX_OUTPUT_CHARS + 100)

        # Check for binary content
        if "\x00" in content:
            return {"success": False, "content": "", "error": "Cannot read binary files."}

        if len(content) > MAX_OUTPUT_CHARS:
            content = content[:MAX_OUTPUT_CHARS] + f"\n\n[... truncated at {MAX_OUTPUT_CHARS} chars]"

        _audit_log(f"read_file({filepath})", f"{len(content)} chars read")
        return {"success": True, "content": content, "error": ""}

    except Exception as e:
        return {"success": False, "content": "", "error": f"Read error: {str(e)}"}


def write_file(filepath: str, content: str) -> dict:
    """
    Write to a file (only if SHELL_WRITE_ENABLED=true).
    Restricted to sandboxed paths.

    Returns:
        {"success": bool, "message": str, "error": str}
    """
    if not WRITE_ENABLED:
        return {"success": False, "message": "", "error": "Write operations are disabled. Set SHELL_WRITE_ENABLED=true."}

    filepath = os.path.abspath(filepath.strip())

    if ".." in filepath:
        return {"success": False, "message": "", "error": "Path traversal (..) is not allowed."}

    # Only allow writing under /home/, /tmp/, /mnt/ (not /etc, /var/log)
    write_paths = ["/home/", "/tmp/", "/mnt/"]
    allowed = any(filepath.startswith(p) for p in write_paths)
    if not allowed:
        _audit_log(f"write_file({filepath})", "Outside write sandbox", blocked=True)
        return {"success": False, "message": "", "error": f"Write access denied. Must be under: {', '.join(write_paths)}"}

    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        _audit_log(f"write_file({filepath})", f"{len(content)} chars written")
        logger.info(f"📝 File written: {filepath} ({len(content)} chars)")
        return {"success": True, "message": f"File written: {filepath}", "error": ""}

    except Exception as e:
        return {"success": False, "message": "", "error": f"Write error: {str(e)}"}
