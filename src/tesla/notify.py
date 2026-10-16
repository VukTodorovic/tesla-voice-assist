"""Desktop notifications, including the Allow/Deny prompt for risky actions.

Stopgap UI until the GNOME Shell extension shows approvals in the notch.
"""

import asyncio
import contextlib
import logging

log = logging.getLogger(__name__)


async def notify(title: str, body: str) -> None:
    proc = await asyncio.create_subprocess_exec("notify-send", "-a", "Tesla", title, body)
    await proc.wait()


async def ask_approval(body: str, timeout: float = 90) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "notify-send", "-a", "Tesla", "-u", "critical", "-i", "dialog-warning",
        "-A", "allow=Dozvoli", "-A", "deny=Odbij", "Tesla traži dozvolu", body,
        stdout=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(timeout):
            out, _ = await proc.communicate()
    except (TimeoutError, asyncio.CancelledError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    answer = out.decode().strip()
    log.info("approval for %r: %s", body, answer or "dismissed")
    return answer == "allow"
