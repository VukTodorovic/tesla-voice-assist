"""The model behind Tesla: a long-lived Claude Code session via the Agent SDK.

The SDK runs the `claude` CLI in stream-json mode and keeps it alive between
messages, so only the first message pays the startup cost. It uses whatever
login the CLI has, i.e. the Claude subscription.
"""

import asyncio
import contextlib
import logging
import os
import re
import shutil
import time
import warnings
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    CanUseToolShadowedWarning,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
)

from tesla.config import DATA_DIR, Config
from tesla.desktop import desktop_server
from tesla.prompt import system_prompt

log = logging.getLogger(__name__)
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

WORKSPACE = DATA_DIR / "workspace"
BUILTIN_TOOLS = ["Read", "Glob", "Grep", "Bash", "Edit", "Write", "WebSearch", "WebFetch"]
MODEL_ALIASES = {"haiku": "claude-haiku-5-5", "haiku-4.5": "claude-haiku-4-5",
                 "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
# Models that can run with thinking switched off; on the 5.x models it cannot be
# disabled, so they get the lowest effort instead.
_NO_THINKING_MODELS = ("claude-haiku-4-5",)


def _transcripts_dir() -> Path:
    """Where Claude Code keeps the transcripts of Tesla's sessions (one per cwd)."""
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return config / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(WORKSPACE))


def delete_transcripts(session_ids: set[str] | None = None) -> int:
    """Delete the given sessions' transcripts, or all of Tesla's when None.

    Transcripts hold every message, including attached screenshots; Tesla only
    needs them while a conversation lasts (to resume after a model switch).
    """
    root = _transcripts_dir()
    if not root.is_dir():
        return 0
    if session_ids is None:
        targets = list(root.iterdir())
    else:
        targets = [p for sid in session_ids for p in (root / f"{sid}.jsonl", root / sid)]
    removed = 0
    for path in targets:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink(missing_ok=True)
        else:
            continue
        removed += 1
    return removed


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCall:
    name: str


@dataclass
class TurnDone:
    error: str | None = None


BrainEvent = TextDelta | ToolCall | TurnDone


class ClaudeBrain:
    def __init__(self, cfg: Config, can_use_tool, extra_tools: list | None = None) -> None:
        self.cfg = cfg
        self._extra_tools = extra_tools or []
        self.model = MODEL_ALIASES.get(cfg.brain.model, cfg.brain.model)
        self._can_use_tool = can_use_tool
        self._client: ClaudeSDKClient | None = None
        self._session_id: str | None = None
        # Every session id this conversation used (a model switch may add one).
        self._session_ids: set[str] = set()
        self._lock = asyncio.Lock()
        self._last_used = time.monotonic()
        self._expiry: asyncio.TimerHandle | None = None
        WORKSPACE.mkdir(parents=True, exist_ok=True)

    def _options(self, resume: str | None) -> ClaudeAgentOptions:
        no_thinking = self.model.startswith(_NO_THINKING_MODELS)
        return ClaudeAgentOptions(
            model=self.model,
            system_prompt=system_prompt(self.cfg),
            tools=BUILTIN_TOOLS,
            mcp_servers={"tesla": desktop_server(self._extra_tools)},
            strict_mcp_config=True,
            setting_sources=[],
            can_use_tool=self._can_use_tool,
            cwd=str(WORKSPACE),
            include_partial_messages=True,
            thinking={"type": "disabled"} if no_thinking else None,
            effort=None if no_thinking else self.cfg.brain.effort,
            resume=resume,
            stderr=lambda line: log.debug("claude: %s", line.rstrip()),
        )

    async def start(self, resume: str | None = None) -> None:
        t0 = time.perf_counter()
        client = ClaudeSDKClient(self._options(resume))
        await client.connect()
        self._client = client
        log.info("claude session ready (%s) in %.1fs", self.model, time.perf_counter() - t0)

    async def close(self) -> None:
        if self._client:
            client, self._client = self._client, None
            with contextlib.suppress(Exception):
                await client.disconnect()

    async def _forget(self) -> None:
        """End the conversation: stop the CLI and delete its transcripts."""
        await self.close()
        removed = delete_transcripts(self._session_ids) if self._session_ids else 0
        if removed:
            log.info("deleted %d transcript file(s) of the finished conversation", removed)
        self._session_ids.clear()
        self._session_id = None

    async def reset(self) -> None:
        """Forget the conversation and warm up a fresh session."""
        async with self._lock:
            await self._forget()
            await self.start()

    async def shutdown(self) -> None:
        if self._expiry:
            self._expiry.cancel()
        async with self._lock:
            await self._forget()

    def _schedule_expiry(self) -> None:
        if self._expiry:
            self._expiry.cancel()
        delay = self.cfg.brain.session_idle_reset_min * 60
        self._expiry = asyncio.get_running_loop().call_later(
            delay, lambda: asyncio.create_task(self._expire()))

    async def _expire(self) -> None:
        async with self._lock:
            idle_min = (time.monotonic() - self._last_used) / 60
            if idle_min < self.cfg.brain.session_idle_reset_min or not self._session_ids:
                return
            log.info("idle %.0f min: conversation over", idle_min)
            await self._forget()
            await self.start()  # stay warm for the next question

    async def set_model(self, model: str) -> None:
        """Switch model, keeping the conversation (thinking flags differ, so restart)."""
        async with self._lock:
            self.model = MODEL_ALIASES.get(model, model)
            await self.close()
            await self.start(resume=self._session_id)

    async def ask(self, content: str | list[dict[str, Any]]) -> AsyncIterator[BrainEvent]:
        async with self._lock:
            idle_min = (time.monotonic() - self._last_used) / 60
            if self._session_ids and idle_min > self.cfg.brain.session_idle_reset_min:
                log.info("idle %.0f min, starting a fresh conversation", idle_min)
                await self._forget()
            if not self._client:
                await self.start()
            client = self._client
            if isinstance(content, str):
                await client.query(content)
            else:
                await client.query(_single({"type": "user", "message": {"role": "user", "content": content}}))
            done = False
            try:
                async for msg in client.receive_response():
                    if isinstance(msg, StreamEvent):
                        ev = msg.event
                        if ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                            yield TextDelta(ev["delta"]["text"])
                        elif ev.get("type") == "content_block_start" and ev["content_block"].get("type") == "tool_use":
                            yield ToolCall(ev["content_block"]["name"])
                    elif isinstance(msg, ResultMessage):
                        done = True
                        self._session_id = msg.session_id
                        self._session_ids.add(msg.session_id)
                        yield TurnDone(error=str(msg.result) if msg.is_error else None)
            finally:
                self._last_used = time.monotonic()
                self._schedule_expiry()
                if not done:
                    await self._abort(client)

    async def _abort(self, client: ClaudeSDKClient) -> None:
        """Interrupt the running turn and swallow its tail so the next turn starts clean."""
        try:
            await client.interrupt()
            async with asyncio.timeout(5):
                async for msg in client.receive_response():
                    if isinstance(msg, ResultMessage):
                        self._session_id = msg.session_id
                        self._session_ids.add(msg.session_id)
        except Exception as e:  # a wedged CLI: drop it, the next ask starts a new one
            log.warning("interrupt failed (%s); restarting claude session", e)
            await self.close()


async def _single(message: dict[str, Any]):
    yield message
