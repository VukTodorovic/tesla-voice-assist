"""Command line entry point: `tesla run`, `tesla toggle`, and test helpers."""

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from tesla.config import CONFIG_FILE, load_config

log = logging.getLogger("tesla")

HOTKEY_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/tesla/"
MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
# Prefer the shell extension (captures screen context, hold-to-talk); fall back to the daemon.
TOGGLE_CMD = (
    "sh -c 'gdbus call --session --dest dev.vuk.TeslaShell --object-path /dev/vuk/TeslaShell "
    "--method dev.vuk.TeslaShell.Activate || gdbus call --session --dest dev.vuk.Tesla "
    "--object-path /dev/vuk/Tesla --method dev.vuk.Tesla.Toggle'"
)


def _load_env_files() -> None:
    """Secrets such as GROQ_API_KEY: ~/.config/tesla/env (written by scripts/install.sh)
    and, when running from the repo, ./.env. Variables already set win."""
    for path in (CONFIG_FILE.parent / "env", Path.cwd() / ".env"):
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#"):
                os.environ.setdefault(key.removeprefix("export ").strip(), value.strip().strip("\"'"))


def _clean_env() -> None:
    # Inherited Claude Code session variables confuse the CLI we spawn, and an
    # API key would silently switch billing away from the subscription.
    for key in list(os.environ):
        if (key.startswith("CLAUDE") and key != "CLAUDE_CONFIG_DIR") or key == "ANTHROPIC_API_KEY":
            os.environ.pop(key)


async def _call(member: str, signature: str = "", body: list | None = None) -> None:
    from dbus_fast import BusType, Message, MessageType
    from dbus_fast.aio import MessageBus

    from tesla.daemon import BUS_NAME, OBJECT_PATH

    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    reply = await bus.call(Message(destination=BUS_NAME, path=OBJECT_PATH, interface=BUS_NAME,
                                   member=member, signature=signature, body=body or []))
    if reply.message_type == MessageType.ERROR:
        sys.exit(f"tesla: {reply.error_name} (is `tesla run` running?)")
    if reply.body:
        print(reply.body[0])


async def _ask(cfg, text: str, speak: bool) -> None:
    from tesla import notify
    from tesla.brain import ClaudeBrain, TextDelta, ToolCall, TurnDone
    from tesla.policy import Policy
    from tesla.prompt import message_context
    from tesla.text import SentenceChunker

    brain = ClaudeBrain(cfg, Policy(notify.ask_approval).can_use_tool)
    voice = player = None
    if speak:
        from tesla.audio import Player
        from tesla.tts import Voice
        voice = Voice(cfg.tts)
        player = Player(voice.sample_rate, cfg.audio.output_device)
    chunker = SentenceChunker()
    try:
        async for ev in brain.ask(f"{message_context()} {text}"):
            if isinstance(ev, TextDelta):
                print(ev.text, end="", flush=True)
                if player:
                    for s in chunker.feed(ev.text):
                        player.play(await voice.synthesize(s))
            elif isinstance(ev, ToolCall):
                print(f"\n  [{ev.name}]", flush=True)
            elif isinstance(ev, TurnDone) and ev.error:
                print(f"\n  error: {ev.error}")
        print()
        if player:
            for s in chunker.flush():
                player.play(await voice.synthesize(s))
            await player.drained()
            player.close()
    finally:
        await brain.shutdown()


async def _say(cfg, text: str) -> None:
    from tesla.audio import Player
    from tesla.tts import Voice

    voice = Voice(cfg.tts)
    player = Player(voice.sample_rate, cfg.audio.output_device)
    player.play(await voice.synthesize(text))
    await player.drained()
    await asyncio.sleep(0.2)
    player.close()


async def _hear(cfg) -> None:
    from tesla.daemon import Assistant

    assistant = await asyncio.to_thread(Assistant, cfg)
    print("Pričaj (završava se kad ućutiš)...", flush=True)
    audio = await assistant._listen(cfg.listen.no_speech_timeout_s)
    if audio is None:
        print("Ništa nisam čuo.")
        return
    print(await assistant.stt.transcribe(audio))


def _gsettings(*args: str) -> str:
    return subprocess.run(["gsettings", *args], check=True, capture_output=True, text=True).stdout.strip()


def _setup_hotkey(binding: str, remove: bool) -> None:
    current = _gsettings("get", MEDIA_KEYS, "custom-keybindings")
    paths = [] if current.startswith("@as") else json.loads(current.replace("'", '"'))
    paths = [p for p in paths if p != HOTKEY_PATH]
    if not remove:
        paths.append(HOTKEY_PATH)
        schema = f"{MEDIA_KEYS}.custom-keybinding:{HOTKEY_PATH}"
        _gsettings("set", schema, "name", "Tesla")
        _gsettings("set", schema, "command", TOGGLE_CMD)
        _gsettings("set", schema, "binding", binding)
    _gsettings("set", MEDIA_KEYS, "custom-keybindings", json.dumps(paths).replace('"', "'"))
    print("Prečica uklonjena." if remove else f"Prečica {binding} -> Tesla toggle.")


def main() -> None:
    ap = argparse.ArgumentParser(prog="tesla", description="Tesla, lokalni glasovni asistent.")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--config", type=Path, default=CONFIG_FILE)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="pokreni asistenta (podrazumevano)")
    sub.add_parser("toggle", help="počni/prekini slušanje")
    sub.add_parser("cancel", help="prekini sve")
    sub.add_parser("state", help="trenutno stanje")
    sub.add_parser("version", help="instalirana verzija")
    p = sub.add_parser("wake", help="uključi/isključi buđenje glasom (\"Hej Tesla\")")
    p.add_argument("onoff", choices=["on", "off"])
    p = sub.add_parser("send", help="pošalji kucani zahtev pokrenutoj Tesli")
    p.add_argument("text")
    p = sub.add_parser("ask", help="pošalji tekst modelu (bez mikrofona)")
    p.add_argument("text")
    p.add_argument("--speak", action="store_true")
    p = sub.add_parser("say", help="izgovori tekst (test glasa)")
    p.add_argument("text")
    p.add_argument("--voice", help="nicholas (muški) ili sophie (ženski)")
    p.add_argument("--rate", help='brzina, npr. "+10%%" ili "-5%%"')
    sub.add_parser("hear", help="jedno slušanje i transkripcija (test mikrofona i STT-a)")
    p = sub.add_parser("setup-hotkey", help="GNOME prečica za toggle")
    p.add_argument("--binding", default="<Super><Alt>space")
    p.add_argument("--remove", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname).1s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "huggingface_hub", "faster_whisper", "pywhispercpp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _load_env_files()
    _clean_env()
    cfg = load_config(args.config)

    match args.cmd:
        case None | "run":
            from tesla.daemon import run
            log.info("config: %s", args.config if args.config.exists() else "defaults")
            asyncio.run(run(cfg))
        case "toggle":
            asyncio.run(_call("Toggle"))
        case "cancel":
            asyncio.run(_call("Cancel"))
        case "state":
            asyncio.run(_call("GetState"))
        case "version":
            from tesla.daemon import installed_version
            print(installed_version())
        case "wake":
            asyncio.run(_call("SetWake", "b", [args.onoff == "on"]))
        case "send":
            asyncio.run(_call("Ask", "s", [args.text]))
        case "ask":
            asyncio.run(_ask(cfg, args.text, args.speak))
        case "say":
            if args.voice:
                cfg.tts.voice = args.voice
            if args.rate:
                cfg.tts.rate = args.rate
            asyncio.run(_say(cfg, args.text))
        case "hear":
            asyncio.run(_hear(cfg))
        case "setup-hotkey":
            _setup_hotkey(args.binding, args.remove)
