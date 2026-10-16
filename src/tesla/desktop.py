"""Small desktop actions exposed to Claude as MCP tools (GNOME / Ubuntu)."""

import asyncio
import configparser
import difflib
import logging
import os
import subprocess
from pathlib import Path
from typing import Annotated, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from tesla.text import to_latin

log = logging.getLogger(__name__)

APP_DIRS = [
    Path.home() / ".local/share/applications",
    Path("/usr/share/applications"),
    Path("/var/lib/snapd/desktop/applications"),
    Path.home() / ".local/share/flatpak/exports/share/applications",
    Path("/var/lib/flatpak/exports/share/applications"),
]


def _spawn(*argv: str) -> None:
    """Start a GUI program fully detached from the daemon."""
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def open_link(url: str) -> None:
    _spawn("xdg-open", url)


def _desktop_entries() -> dict[str, tuple[str, list[str]]]:
    """desktop-id -> (display name, search names)."""
    entries: dict[str, tuple[str, list[str]]] = {}
    for d in APP_DIRS:
        if not d.is_dir():
            continue
        for f in d.glob("*.desktop"):
            if f.name in entries:
                continue
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(f, encoding="utf-8")
                e = cp["Desktop Entry"]
            except (configparser.Error, KeyError, UnicodeDecodeError):
                continue
            if e.get("NoDisplay", "false") == "true" or e.get("Type") != "Application":
                continue
            names = [e.get(k, "") for k in ("Name", "Name[sr]", "Name[sr@latin]", "GenericName",
                                             "GenericName[sr]", "GenericName[sr@latin]")]
            names += e.get("Keywords", "").split(";") + e.get("Keywords[sr]", "").split(";")
            names = [to_latin(n).strip().lower() for n in names if n.strip()]
            entries[f.name] = (e.get("Name", f.stem), names)
    return entries


def find_app(query: str) -> tuple[str, str] | None:
    q = to_latin(query).strip().lower()
    entries = _desktop_entries()
    for desktop_id, (name, names) in entries.items():
        if q in names or q == desktop_id.removesuffix(".desktop").lower():
            return desktop_id, name
    for desktop_id, (name, names) in entries.items():
        if any(q in n for n in names[:1]):
            return desktop_id, name
    flat = {n: did for did, (_, names) in entries.items() for n in names}
    close = difflib.get_close_matches(q, flat, n=1, cutoff=0.6)
    if close:
        did = flat[close[0]]
        return did, entries[did][0]
    return None


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _existing_path(path: str) -> Path | None:
    p = Path(os.path.expanduser(path))
    return p if p.exists() else None


@tool("open_app", "Pokreće desktop aplikaciju po imenu. Koristi englesko sistemsko ime (Firefox, Files, Settings, Calculator, Spotify).",
      {"name": Annotated[str, "Ime aplikacije"]})
async def open_app(args: dict[str, Any]) -> dict[str, Any]:
    found = await asyncio.to_thread(find_app, args["name"])
    if not found:
        return _err(f"Nisam našao aplikaciju '{args['name']}'.")
    desktop_id, name = found
    _spawn("gtk-launch", desktop_id)
    return _ok(f"Pokrenuto: {name}")


@tool("open_url", "Otvara URL u podrazumevanom browseru.", {"url": Annotated[str, "Pun URL"]})
async def open_url(args: dict[str, Any]) -> dict[str, Any]:
    url = args["url"]
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    _spawn("xdg-open", url)
    return _ok(f"Otvoreno: {url}")


@tool("open_path", "Otvara fajl ili folder podrazumevanom aplikacijom (folder u Files).",
      {"path": Annotated[str, "Apsolutna putanja ili ~/..."]})
async def open_path(args: dict[str, Any]) -> dict[str, Any]:
    p = _existing_path(args["path"])
    if not p:
        return _err(f"Putanja ne postoji: {args['path']}")
    _spawn("xdg-open", str(p))
    return _ok(f"Otvoreno: {p}")


@tool("open_terminal", "Otvara novi GNOME Terminal u datom folderu.",
      {"path": Annotated[str, "Folder; podrazumevano ~"]})
async def open_terminal(args: dict[str, Any]) -> dict[str, Any]:
    p = _existing_path(args.get("path") or "~")
    if not p or not p.is_dir():
        return _err(f"Folder ne postoji: {args.get('path')}")
    _spawn("gnome-terminal", f"--working-directory={p}")
    return _ok(f"Terminal otvoren u {p}")


@tool("open_in_vscode", "Otvara folder ili fajl u VS Code-u, opciono na liniji.",
      {"path": Annotated[str, "Fajl ili folder"], "line": Annotated[int, "Linija, 0 ako nije bitna"]})
async def open_in_vscode(args: dict[str, Any]) -> dict[str, Any]:
    p = _existing_path(args["path"])
    if not p:
        return _err(f"Putanja ne postoji: {args['path']}")
    line = int(args.get("line") or 0)
    if p.is_file() and line > 0:
        _spawn("code", "--goto", f"{p}:{line}")
    else:
        _spawn("code", str(p))
    return _ok(f"Otvoreno u VS Code-u: {p}")


@tool("notify", "Prikazuje desktop obaveštenje (za tekst koji Vuk treba da vidi, npr. vrednost ili putanju).",
      {"title": str, "body": str})
async def notify(args: dict[str, Any]) -> dict[str, Any]:
    _spawn("notify-send", "-a", "Tesla", args["title"], args["body"])
    return _ok("Prikazano.")


def desktop_server(extra_tools: list | None = None):
    return create_sdk_mcp_server(
        name="tesla",
        tools=[open_app, open_url, open_path, open_terminal, open_in_vscode, notify, *(extra_tools or [])],
    )
