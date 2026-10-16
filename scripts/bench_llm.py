"""Measure voice-relevant latency of Claude models through the real `claude` CLI.

Runs one persistent `claude -p` stream-json process per configuration (like the
assistant will) and times several turns: time to first text token, time to the
first complete sentence (when TTS could start speaking) and total turn time.

    uv run scripts/bench_llm.py                # all configs
    uv run scripts/bench_llm.py haiku sonnet   # only configs whose name contains these
"""

import asyncio
import json
import os
import re
import statistics
import sys
import tempfile
import time

PROMPTS = [
    "Reci mi u jednoj rečenici šta je Wayland.",
    "Koliko je 17 puta 23?",
    "Pročitaj /etc/os-release i reci mi samo koja je verzija Ubuntua.",
    "Predloži mi jedno kratko ime za mačku.",
    "Koja je razlika između TCP i UDP, ukratko?",
    "Koliko fajlova sa ekstenzijom .conf ima direktno u /etc?",
    "Daj mi jedan savet za bolji san.",
    "Prevedi na engleski: dobro jutro, kako si?",
]

SYSTEM = (
    "Ti si Tesla, glasovni asistent. Odgovaraš na srpskom (latinica), kratko, "
    "jednom do dve rečenice, bez markdowna, jer se tvoj odgovor izgovara naglas."
)

CONFIGS = [
    {"name": "haiku-4.5-nothink", "args": ["--model", "claude-haiku-4-5"],
     "env": {"MAX_THINKING_TOKENS": "0"}},
    {"name": "haiku-5.5-nothink", "args": ["--model", "claude-haiku-5-5"],
     "env": {"MAX_THINKING_TOKENS": "0"}},
    {"name": "haiku-5.5-low", "args": ["--model", "claude-haiku-5-5", "--effort", "low"]},
    {"name": "haiku-5.5-default", "args": ["--model", "claude-haiku-5-5"]},
    {"name": "sonnet-5.5-low", "args": ["--model", "claude-sonnet-5-5", "--effort", "low"]},
]

# The assistant runs the CLI bundled with claude-agent-sdk; benchmark the same binary.
CLAUDE_BIN = os.environ.get("BENCH_CLAUDE") or os.path.join(
    os.path.dirname(__import__("claude_agent_sdk").__file__), "_bundled", "claude")

SENTENCE_END = re.compile(r"[.!?](\s|$)")


async def run_config(cfg: dict) -> list[dict]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    env.update(cfg.get("env", {}))
    cwd = tempfile.mkdtemp(prefix="tesla-bench-")
    cmd = [
        CLAUDE_BIN, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--tools", "Read,Glob,Grep",
        "--allowedTools", "Read,Glob,Grep",
        "--add-dir", "/etc",
        "--append-system-prompt", SYSTEM,
        *cfg["args"],
    ]
    t_spawn = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=2**24,
    )
    results = []
    for i, prompt in enumerate(PROMPTS):
        msg = {"type": "user", "message": {"role": "user", "content": prompt}}
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        await proc.stdin.drain()
        t0 = time.perf_counter()
        first_token = first_sentence = None
        thinking = False
        text = ""
        tools = []
        error = None
        while True:
            line = await proc.stdout.readline()
            if not line:
                error = (await proc.stderr.read()).decode()[-500:] or "process exited"
                break
            ev = json.loads(line)
            if ev["type"] == "stream_event":
                e = ev["event"]
                if e["type"] == "content_block_start" and e["content_block"]["type"] == "thinking":
                    thinking = True
                if e["type"] == "content_block_start" and e["content_block"]["type"] == "tool_use":
                    tools.append(e["content_block"]["name"])
                if e["type"] == "content_block_delta" and e["delta"]["type"] == "text_delta":
                    now = time.perf_counter()
                    if first_token is None:
                        first_token = now - t0
                    text += e["delta"]["text"]
                    if first_sentence is None and SENTENCE_END.search(text):
                        first_sentence = now - t0
            elif ev["type"] == "result":
                if ev.get("is_error"):
                    error = str(ev.get("result"))[:300]
                break
        total = time.perf_counter() - t0
        results.append({
            "turn": i + 1, "first_token": first_token, "first_sentence": first_sentence,
            "total": total, "thinking": thinking, "tools": tools, "text": text.strip(),
            "error": error,
        })
        if error and not line:
            break
    proc.stdin.close()
    await proc.wait()
    print(f"\n=== {cfg['name']} (process lifetime {time.perf_counter() - t_spawn:.1f}s)")
    for r in results:
        fmt = lambda v: f"{v:5.2f}s" if v is not None else "   -  "
        flags = ("T" if r["thinking"] else " ") + (f" tools={r['tools']}" if r["tools"] else "")
        print(f"  #{r['turn']} first_token={fmt(r['first_token'])} sentence={fmt(r['first_sentence'])}"
              f" total={fmt(r['total'])} {flags}")
        print(f"      {r['error'] or r['text'][:110]}")
    return results


async def main() -> None:
    wanted = sys.argv[1:]
    configs = [c for c in CONFIGS if not wanted or any(w in c["name"] for w in wanted)]
    summary = {}
    for cfg in configs:
        res = await run_config(cfg)
        # Turn 1 includes process startup; voice latency is about warm turns.
        warm = [r for r in res[1:] if r["first_sentence"] is not None]
        if warm:
            summary[cfg["name"]] = (
                statistics.median(r["first_token"] for r in warm),
                statistics.median(r["first_sentence"] for r in warm),
                statistics.median(r["total"] for r in warm),
            )
    print("\n=== median over warm turns (2..n): first_token / first_sentence / total")
    for name, (a, b, c) in summary.items():
        print(f"  {name:16s} {a:5.2f}s  {b:5.2f}s  {c:5.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
