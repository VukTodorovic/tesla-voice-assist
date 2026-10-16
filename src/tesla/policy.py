"""Which tool calls run freely, which need Vuk's approval, and which never run.

Read-only work (reading files, searching, web, opening apps) is free. Anything
that changes state goes through the approver. A few things are never allowed.
"""

import logging
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

log = logging.getLogger(__name__)

HOME = Path.home()

# Never sent to the model, even read-only.
SECRET_PATHS = [HOME / p for p in (
    ".ssh", ".gnupg", ".password-store", ".local/share/keyrings", ".aws", ".netrc",
    ".docker/config.json", ".kube", ".claude/.credentials.json", ".config/gh/hosts.yml",
    ".mozilla", ".config/google-chrome", ".config/chromium", "snap/chromium",
)]

FREE_TOOLS = {"Read", "Glob", "Grep", "WebSearch", "WebFetch"}
# Our own MCP tools that only open things or show notifications.
FREE_PREFIX = "mcp__tesla__"

READONLY_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "fd", "pwd", "date", "which",
    "whoami", "uname", "df", "du", "free", "uptime", "ps", "stat", "file", "tree", "jq",
    "sort", "uniq", "cut", "tr", "basename", "dirname", "realpath", "lsblk", "ip",
    "hostname", "id", "nproc", "lscpu", "git",
}
READONLY_GIT = {"status", "log", "diff", "show", "ls-files", "rev-parse", "blame", "describe",
                "shortlog", "branch", "remote"}
FIND_WRITES = {"-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
_SHELL_META = re.compile(r"[;&<>`\n]|\$\(|\|\|")
# Redirections that only silence or merge stderr.
_HARMLESS_REDIRECTS = re.compile(r"\s2>\s*/dev/null|\s2>&1")

NEVER = re.compile(
    r"(^|[\s;&|(])(sudo|su|pkexec|doas|mkfs[.\w]*|shutdown|reboot|poweroff|halt)\b"
    r"|\brm\s+(-\w*\s+)*(/|~|\$HOME)/?(\s|$)"
    r"|\bdd\s+.*\bof=/dev/"
    r"|:\(\)\s*\{")

Approver = Callable[[str], Awaitable[bool]]


@dataclass
class Decision:
    action: str  # "allow" | "ask" | "deny"
    reason: str = ""


def _is_secret(path: str) -> bool:
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        pass
    return any(p == s or s in p.parents for s in SECRET_PATHS)


def _readonly_shell(command: str) -> bool:
    command = _HARMLESS_REDIRECTS.sub("", command)
    if _SHELL_META.search(command):
        return False
    for segment in command.split("|"):
        try:
            argv = shlex.split(segment)
        except ValueError:
            return False
        if not argv or argv[0] not in READONLY_COMMANDS:
            return False
        if argv[0] == "git":
            sub = next((a for a in argv[1:] if not a.startswith("-")), "")
            if sub not in READONLY_GIT or any(a in ("-d", "-D", "--delete", "-m", "-M") for a in argv):
                return False
        if argv[0] in ("find", "fd") and FIND_WRITES & set(argv):
            return False
    return True


def _touches_secrets(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    tokens = [t.replace("$HOME", str(HOME)) for t in tokens]
    return any(_is_secret(t) for t in tokens if "/" in t)


def decide(tool: str, args: dict[str, Any]) -> Decision:
    if tool in FREE_TOOLS:
        path = args.get("file_path") or args.get("path") or ""
        if path and _is_secret(path):
            return Decision("deny", "Pristup tajnim fajlovima (ključevi, lozinke) nije dozvoljen.")
        if path and Path(path).name.startswith(".env"):
            return Decision("ask", f"Čitanje fajla sa tajnama: {path}")
        return Decision("allow")
    if tool.startswith(FREE_PREFIX):
        return Decision("allow")
    if tool == "Bash":
        command = args.get("command", "")
        if NEVER.search(command):
            return Decision("deny", "Ova komanda nikad nije dozvoljena asistentu.")
        if _touches_secrets(command):
            return Decision("deny", "Pristup tajnim fajlovima (ključevi, lozinke) nije dozvoljen.")
        if _readonly_shell(command):
            return Decision("allow")
        return Decision("ask", f"Komanda: {command}")
    if tool in ("Edit", "MultiEdit"):
        return Decision("ask", f"Izmena fajla: {args.get('file_path', '?')}")
    if tool == "Write":
        return Decision("ask", f"Upis u fajl: {args.get('file_path', '?')}")
    return Decision("ask", f"Alat {tool}: {str(args)[:200]}")


class Policy:
    def __init__(self, approver: Approver) -> None:
        self.approver = approver

    async def can_use_tool(self, tool: str, args: dict[str, Any], ctx: ToolPermissionContext):
        d = decide(tool, args)
        log.info("tool %s -> %s %s", tool, d.action, d.reason)
        if d.action == "allow":
            return PermissionResultAllow()
        if d.action == "deny":
            return PermissionResultDeny(message=d.reason)
        if await self.approver(d.reason):
            return PermissionResultAllow()
        return PermissionResultDeny(message="Vuk nije odobrio ovu radnju. Ne pokušavaj ponovo "
                                            "na drugi način; reci mu kratko šta nije urađeno.")
