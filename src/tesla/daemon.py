"""The assistant loop: hotkey -> listen -> transcribe -> Claude -> speak.

    idle --activate--> listening --silence/release--> transcribing --> thinking --> speaking
      ^                                                                              |
      +-------------- no follow-up within a few seconds <-- listening (follow-up) <--+

Activating while Tesla thinks or speaks interrupts it and listens again.
The GNOME Shell extension drives this over D-Bus and renders `Event` signals.
"""

import asyncio
import contextlib
import enum
import itertools
import json
import logging
import random
import re
import signal as signals
import time

import numpy as np
from dbus_fast import BusType, Message, MessageType, NameFlag, RequestNameReply
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method, signal

from tesla import notify
from tesla.audio import FRAME, MIC_RATE, Microphone, Player, Sounds
from tesla.brain import ClaudeBrain, TextDelta, ToolCall, TurnDone, delete_transcripts
from tesla.config import DATA_DIR, Config
from tesla.policy import Policy
from tesla.prompt import message_context
from tesla.screen import DEICTIC, ScreenContext, image_block, jpeg_b64, make_screen_tool
from tesla.stt import Transcriber
from tesla.desktop import open_link
from tesla.text import MarkerFilter, SentenceChunker, extract_links, to_latin
from tesla.tts import Voice
from tesla.vad import StreamingVad
from tesla.wake import WakeWord, strip_wake

log = logging.getLogger(__name__)

BUS_NAME = "dev.vuk.Tesla"
OBJECT_PATH = "/dev/vuk/Tesla"
SHELL_BUS_NAME = "dev.vuk.TeslaShell"
SHELL_PATH = "/dev/vuk/TeslaShell"
FRAME_MS = FRAME * 1000 // MIC_RATE
PREROLL_FRAMES = 10
APPROVAL_TIMEOUT_S = 120
# Exit status when another daemon owns the bus name (the service does not restart on it).
ALREADY_RUNNING = 3

TOOL_LABELS = {
    "Read": "Čitam fajl", "Glob": "Tražim fajlove", "Grep": "Pretražujem fajlove",
    "Bash": "Pokrećem komandu", "Edit": "Menjam fajl", "Write": "Pišem fajl",
    "WebSearch": "Pretražujem web", "WebFetch": "Otvaram stranicu",
    "mcp__tesla__open_app": "Pokrećem aplikaciju", "mcp__tesla__open_url": "Otvaram link",
    "mcp__tesla__open_path": "Otvaram", "mcp__tesla__open_terminal": "Otvaram terminal",
    "mcp__tesla__open_in_vscode": "Otvaram VS Code", "mcp__tesla__notify": "Prikazujem",
    "mcp__tesla__look_at_screen": "Gledam ekran",
}


class State(enum.StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    APPROVAL = "approval"


# Handled locally, without a model round trip.
_NEW_TOPIC = re.compile(r"^(nova (tema|konverzacija|razgovor)|počnimo ispočetka|zaboravi (ovaj )?razgovor)\b")
_SWITCH_MODEL = re.compile(r"\b(pređi|predji|prebaci( se)?) na (haiku|hajku|sonet|sonnet|sonetu|opus)\b")
_STOP = re.compile(r"^(stani|stop|prekini|ćuti|tišina|nema veze|zaboravi)[.!]*$")
# A whole utterance that only closes the conversation: "hvala, ćao", "to je sve", "laku noć".
_CLOSER = (r"(ok(ej)?|u redu|super|odlično|dobro|važi|vazi|to je to|to je sve|to bi bilo (sve|to)|"
           r"hval\w*( ti| vam)?( puno| lepo| najlepše| mnogo)?|fal\w*|ćao|cao|čao|doviđenja|dovidjenja|"
           r"laku noć|laku noc|vidimo se|čujemo se|pozdrav|zbogom|tesla|ništa više|to je bilo sve|"
           # fillers that come with a goodbye: "ništa brate, hvala, ćao"
           r"ništa|ništo|nista|brate|brat|eto|aj|ajde|ajd|e|pa|onda|baš|i|mnogo|puno|ti|vam)")
_FAREWELL = re.compile(rf"^{_CLOSER}([ ,.!]+{_CLOSER})*$")
# Closes even as the first sentence after waking; a bare "ćao" then is a greeting,
# however much conversation history is still kept.
_DEFINITE_CLOSER = re.compile(r"hval|fala|laku no|to je sve|to bi bilo|vidimo se|čujemo se|"
                              r"doviđenja|dovidjenja|zbogom|ništa više|to je bilo sve")
_FAREWELL_REPLIES = {
    "noć": ["Laku noć!", "Laku noć, Vuče!"],
    "hvala": ["Nema na čemu, ćao!", "Nema na čemu!", "Uvek, ćao!"],
    "": ["Ćao!", "Vidimo se!", "Ćao, Vuče!"],
}
_WAKE_OFF = re.compile(r"\b(isključi|iskljuci|ugasi)( mi)? (buđenje|budjenje|wake word|slušanje|osluškivanje)\b")
_WAKE_ON = re.compile(r"\b(uključi|ukljuci|upali)( mi)? (buđenje|budjenje|wake word|slušanje|osluškivanje)\b")
_OPEN_SOURCE = re.compile(
    r"^(?:otvori|otvoriti|pokaži|prikaži)(?: mi)?(?: (?P<which>\w+))?(?: broj)?"
    r" (?:link|linkove|linka|izvor|izvore|izvora|sajt|sajtove|stranicu|rezultat)(?: broj)?(?: (?P<num>\w+))?$")
_ORDINALS = {
    "prvi": 1, "prvu": 1, "prvo": 1, "jedan": 1, "1": 1, "drugi": 2, "drugu": 2, "dva": 2, "2": 2,
    "treći": 3, "treci": 3, "treću": 3, "tri": 3, "3": 3, "četvrti": 4, "cetvrti": 4, "četiri": 4,
    "4": 4, "peti": 5, "pet": 5, "5": 5, "poslednji": -1, "zadnji": -1,
}


class _Reveal:
    """Releases the reply to the notch as speech reaches it.

    Text appears when the sentence it belongs to starts sounding, so Vuk is never
    reading ahead of Tesla. Shown blocks and sources follow the same clock.
    """

    def __init__(self, emit, show) -> None:
        self._emit = emit
        self._show = show
        self.text = ""
        self.sources: list[dict[str, str]] = []
        self._revealed = 0
        self._blocks: list[tuple[int, str]] = []
        self._waiting = 0  # queued sentences that have not started sounding yet
        self._finished = False

    def add(self, delta: str) -> None:
        self.text += delta

    def block(self, pos: int, block: str) -> None:
        self._blocks.append((pos, block))

    def expect(self) -> None:
        self._waiting += 1

    def started(self, upto: int) -> None:
        self._waiting -= 1
        self.upto(upto)
        if self._finished and self._waiting == 0:
            self._all()

    def finish(self) -> None:
        self._finished = True
        if self._waiting == 0:
            self._all()

    def upto(self, pos: int) -> None:
        if pos > self._revealed:
            self._emit("delta", self.text[self._revealed:pos])
            self._revealed = pos
        while self._blocks and self._blocks[0][0] <= pos:
            asyncio.create_task(self._show(self._blocks.pop(0)[1]))

    def _all(self) -> None:
        self.upto(len(self.text))
        while self._blocks:
            asyncio.create_task(self._show(self._blocks.pop(0)[1]))
        if self.sources:
            self._emit("sources", json.dumps(self.sources, ensure_ascii=False))
            self.sources = []


class Assistant:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.mic = Microphone(cfg.audio.input_device)
        self.vad = StreamingVad()
        self.voice = Voice(cfg.tts)
        self.player = Player(self.voice.sample_rate, cfg.audio.output_device)
        self.sounds = Sounds(self.player, cfg.audio.feedback_sounds, cfg.audio.feedback_volume)
        self.stt = Transcriber(cfg.stt)
        screen_tool = make_screen_tool(lambda: self.context, self._capture_context)
        self.brain = ClaudeBrain(cfg, Policy(self._approve).can_use_tool, [screen_tool])
        self.bus: MessageBus | None = None
        self.state = State.IDLE
        self.context = ScreenContext()
        self.listeners: list = []
        self._turn: asyncio.Task | None = None
        self._stop_listening = asyncio.Event()
        self._hold = False
        # Set by a farewell: answer, then go idle without follow-up listening.
        self._end_session = False
        self._approvals: dict[int, asyncio.Future] = {}
        self._approval_ids = itertools.count(1)
        # Links from the last answer, as numbered in the notch.
        self.last_sources: list[dict[str, str]] = []
        # Reply length when an approval was last denied in the current turn.
        self._reply_len = 0
        self._denied_at: int | None = None
        # Wake word: audio from before the trigger and the mic subscription to continue on.
        self._wake_audio: np.ndarray | None = None
        self._wake_queue: asyncio.Queue | None = None
        self.wake: WakeWord | None = None
        try:
            self.wake = WakeWord(cfg.wake, self.mic, self._on_wake, lambda: self.state == State.IDLE)
        except Exception:
            log.exception("wake word unavailable; use the hotkey")

    # ----- control (D-Bus) -----

    def activate(self, context: ScreenContext | None = None) -> None:
        """Hotkey pressed: listen, finish listening, or interrupt and listen again."""
        if self.state == State.LISTENING:
            self._stop_listening.set()
        elif self.state != State.APPROVAL:
            self.context = context or ScreenContext()
            self._start(followup=False)

    def _on_wake(self, preroll: np.ndarray, queue: asyncio.Queue) -> None:
        if self.state != State.IDLE:
            self.mic.unsubscribe(queue)
            return
        self._wake_audio, self._wake_queue = preroll, queue
        self.context = ScreenContext()
        self._start(followup=False, wake=True)
        asyncio.create_task(self._capture_context())

    def set_wake(self, enabled: bool) -> None:
        if self.wake:
            self.wake.start() if enabled else self.wake.stop()

    def ask_text(self, text: str) -> None:
        """A typed request: same as speaking it, minus the microphone."""
        if self.state != State.APPROVAL:
            self._start(followup=False, text=text)

    def stop_listening(self) -> None:
        self._hold = False
        self._stop_listening.set()

    def set_hold(self, hold: bool) -> None:
        self._hold = hold

    def cancel(self) -> None:
        for fut in self._approvals.values():
            if not fut.done():
                fut.set_result(False)
        if self._turn and not self._turn.done():
            self._turn.cancel()

    def approve(self, approval_id: int, allow: bool) -> None:
        fut = self._approvals.get(approval_id)
        if fut and not fut.done():
            fut.set_result(allow)

    def _start(self, followup: bool, text: str | None = None, wake: bool = False) -> None:
        previous = self._turn
        self._turn = asyncio.create_task(self._session(previous, followup, text, wake))

    def _set_state(self, state: State) -> None:
        if state != self.state:
            self.state = state
            log.info("state: %s", state)
            self._emit("state", str(state))

    def _emit(self, kind: str, value: str) -> None:
        for listener in self.listeners:
            listener(kind, value)

    # ----- one activation, possibly with follow-ups -----

    async def _session(self, previous: asyncio.Task | None, followup: bool, text: str | None,
                       wake: bool = False) -> None:
        if previous and not previous.done():
            previous.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await previous
        try:
            if text is not None:
                self._emit("transcript", text)
                if not await self._local_command(text):
                    await self._capture_context()
                    await self._respond(text)
                return
            self._end_session = False
            while True:
                if followup and self.cfg.listen.followup_s <= 0:
                    break
                if not await self._turn_once(followup, wake):
                    break
                followup, wake = True, False
        except asyncio.CancelledError:
            self.player.stop()
            self.sounds.play("cancel")
            raise
        except Exception as e:
            log.exception("turn failed")
            self._emit("error", str(e))
            self.player.stop()
            self.sounds.play("error")
        finally:
            self._hold = False
            if self._wake_queue is not None:
                self.mic.unsubscribe(self._wake_queue)
                self._wake_queue = None
            self._set_state(State.IDLE)
            await asyncio.sleep(0.3)  # let the last earcon finish
            if self.state == State.IDLE:
                self.player.close()

    async def _turn_once(self, followup: bool, wake: bool = False) -> bool:
        self._set_state(State.LISTENING)
        if wake:
            text = await self._wake_request()
            if text is None:
                return False
        else:
            self.sounds.play("followup" if followup else "listen")
            await self.player.drained()  # keep the earcon out of the recording
            timeout = self.cfg.listen.followup_s if followup else self.cfg.listen.no_speech_timeout_s
            audio = await self._listen(timeout)
            if audio is None:
                if not followup:
                    self.sounds.play("cancel")
                return False
            self.sounds.play("heard")
            self._set_state(State.TRANSCRIBING)
            # The activation captured the screen already; a follow-up needs a fresh
            # look. Taken while Whisper runs, so it adds no delay.
            capture = asyncio.create_task(self._capture_context()) if followup else None
            text = await self.stt.transcribe(audio)
            if capture:
                with contextlib.suppress(Exception):
                    await capture
        log.info("Vuk: %s", text)
        self._emit("transcript", text)
        if not text:
            return False
        if await self._local_command(text, followup):
            return not self._end_session
        await self._respond(text, first=not followup)
        return not self._end_session

    async def _wake_request(self) -> str | None:
        """After "Hej Tesla": the request, or None for a false wake or silence."""
        queue, self._wake_queue = self._wake_queue, None
        preroll, self._wake_audio = self._wake_audio, None
        # No waiting for the earcon: the request may already be underway.
        self.sounds.play("listen")
        audio = await self._listen(self.cfg.listen.no_speech_timeout_s, queue)
        if audio is None:
            self._emit("dismiss", "")
            return None
        self.sounds.play("heard")
        self._set_state(State.TRANSCRIBING)
        gap = np.zeros(MIC_RATE // 4, dtype=np.float32)
        raw = await self.stt.transcribe(np.concatenate([preroll, gap, audio]))
        text = strip_wake(raw)
        if text is None:
            # Whisper sometimes drops the name ("Čao." for "Hej Tesla, ćao"). Vosk has not
            # misfired in testing, so trust it rather than ignore Vuk.
            log.info("name not in transcript %r; trusting the wake detector", raw)
            text = raw.strip()
        if not text and not raw.strip():
            self._emit("dismiss", "")
            return None
        if text:
            return text
        # Just "Hej Tesla" and a pause: now listen for the request itself.
        self._set_state(State.LISTENING)
        audio = await self._listen(self.cfg.listen.no_speech_timeout_s)
        if audio is None:
            self.sounds.play("cancel")
            return None
        self.sounds.play("heard")
        self._set_state(State.TRANSCRIBING)
        return await self.stt.transcribe(audio)

    async def _listen(self, start_timeout: float, queue: asyncio.Queue | None = None) -> np.ndarray | None:
        cfg = self.cfg.listen
        self.vad.reset()
        self._stop_listening.clear()
        frames: list[np.ndarray] = []
        speech_at: int | None = None
        silence_ms = 0
        t0 = time.monotonic()
        async with self.mic.stream(queue) as queue:
            while True:
                try:
                    async with asyncio.timeout(0.1):
                        frame = await queue.get()
                except TimeoutError:
                    frame = None
                if self._stop_listening.is_set():
                    break
                if frame is None:
                    continue
                frames.append(frame)
                if len(frames) % 3 == 0:
                    rms = float(np.sqrt(np.mean(frame ** 2)))
                    self._emit("level", f"{min(1.0, rms * 12):.2f}")
                elapsed = time.monotonic() - t0
                if self.vad(frame) >= cfg.vad_threshold:
                    if speech_at is None:
                        speech_at = len(frames) - 1
                    silence_ms = 0
                elif speech_at is not None:
                    silence_ms += FRAME_MS
                    if silence_ms >= cfg.end_silence_ms and not self._hold:
                        break
                if speech_at is None and elapsed > start_timeout and not self._hold:
                    return None
                if elapsed > cfg.max_utterance_s:
                    break
        if speech_at is None:
            # Stopped by hand before VAD fired: still try if there is enough audio.
            if not self._stop_listening.is_set() or len(frames) * FRAME_MS < 600:
                return None
            speech_at = 0
        return np.concatenate(frames[max(0, speech_at - PREROLL_FRAMES):])

    async def _local_command(self, text: str, followup: bool = False) -> bool:
        t = to_latin(text).lower().strip(" .!?,")
        t = re.sub(r"^((hej|hey|ej)\s+)?(tesla|tessla|tesva)[,!]?\s*", "", t)
        if _STOP.match(t):
            return True
        if self.wake and _WAKE_OFF.search(t):
            self.wake.stop()
            await self._say("Buđenje glasom je isključeno, zovi me prečicom.")
            return True
        if self.wake and _WAKE_ON.search(t):
            self.wake.start()
            await self._say("Buđenje glasom je uključeno. Reci hej Tesla.")
            return True
        if _FAREWELL.match(t) and (followup or _DEFINITE_CLOSER.search(t)):
            key = "noć" if "noć" in t or "noc" in t else "hvala" if ("hvala" in t or "fala" in t) else ""
            self._end_session = True
            self._emit("dismiss", "")
            await self._say(random.choice(_FAREWELL_REPLIES[key]))
            return True
        if await self._open_sources(t):
            return True
        if _NEW_TOPIC.match(t):
            self.last_sources = []
            await self._say("U redu, nova tema.")
            await self.brain.reset()
            return True
        m = _SWITCH_MODEL.search(t)
        if m:
            target = {"haiku": "haiku", "hajku": "haiku", "opus": "opus"}.get(m.group(3), "sonnet")
            await self._say(f"Prelazim na {target}.")
            await self.brain.set_model(target)
            return True
        return False

    async def _open_sources(self, t: str) -> bool:
        """'otvori drugi link', 'otvori sve izvore': instant, no model round trip."""
        m = _OPEN_SOURCE.match(t)
        if not m or not self.last_sources:
            return False
        words = {m.group("which"), m.group("num")} - {None}
        if words & {"sve", "svi", "sva", "ih"}:
            chosen = self.last_sources
        else:
            numbers = [_ORDINALS[w] for w in words if w in _ORDINALS]
            if not numbers and words - {"mi"}:
                return False  # "otvori link za X": let the model pick
            index = numbers[0] if numbers else 1
            if index > len(self.last_sources):
                return False
            chosen = [self.last_sources[index - 1 if index > 0 else -1]]
        for link in chosen:
            open_link(link["url"])
        n = len(chosen)
        await self._say("Otvaram." if n == 1 else f"Otvaram {n} {'linka' if n < 5 else 'linkova'}.")
        return True

    async def _build_message(self, text: str, first: bool) -> str | list[dict]:
        ctx = self.context.describe()
        head = message_context()
        if first:
            # Earlier turns may still be in context; a "ćao" now opens, not closes.
            head += " [nova aktivacija: Vuk te je upravo pozvao]"
        prompt = f"{head}\n{ctx}\n\n{text}" if ctx else f"{head} {text}"
        if not DEICTIC.search(text):
            return prompt
        b64 = await asyncio.to_thread(jpeg_b64, self.context.screenshot, self.context.mask)
        if not b64:
            return prompt
        log.info("attaching screenshot")
        return [image_block(b64), {"type": "text", "text": f"{prompt}\n(priložen snimak ekrana)"}]

    async def _respond(self, text: str, first: bool = True) -> None:
        self._set_state(State.THINKING)
        chunker = SentenceChunker()
        reveal = _Reveal(self._emit, self._show)
        sentences: asyncio.Queue[tuple[str, int] | None] = asyncio.Queue()
        speaker = asyncio.create_task(self._speak_worker(sentences, reveal))
        shown = 0
        marker = MarkerFilter()
        self._reply_len = 0
        self._denied_at = None

        def queue(new_sentences: list[str]) -> None:
            # Each sentence carries how far into the reply its text reaches.
            for sentence in new_sentences:
                sentences.put_nowait((sentence, len(reveal.text) - chunker.pending))

        def say_extra(line: str) -> None:
            reveal.add(f"\n{line}" if reveal.text else line)
            sentences.put_nowait((line, len(reveal.text)))

        try:
            async for ev in self.brain.ask(await self._build_message(text, first)):
                if isinstance(ev, TextDelta):
                    ev.text = marker.feed(to_latin(ev.text))  # models occasionally slip into Cyrillic
                    if not ev.text:
                        continue
                    self._reply_len += len(ev.text)
                    reveal.add(ev.text)
                    queue(chunker.feed(ev.text))
                elif isinstance(ev, ToolCall):
                    log.info("tool: %s", ev.name)
                    self._emit("tool", TOOL_LABELS.get(ev.name, ev.name))
                    if reveal.text and not reveal.text[-1:].isspace():
                        # Text before and after a tool call are separate blocks.
                        reveal.add("\n")
                    # Say the preamble ("Proveravam...") now, while the tool runs.
                    queue(chunker.end_block())
                elif isinstance(ev, TurnDone) and ev.error:
                    log.error("claude error: %s", ev.error)
                    self._emit("error", ev.error)
                    say_extra("Izvini, došlo je do greške u komunikaciji sa modelom.")
                for block in chunker.shown[shown:]:
                    reveal.block(len(reveal.text) - chunker.pending, block)
                shown = len(chunker.shown)
            tail = marker.flush()
            if tail:
                reveal.add(tail)
                queue(chunker.feed(tail))
            queue(chunker.flush())
            for block in chunker.shown[shown:]:
                reveal.block(len(reveal.text), block)
            if marker.found and first:
                log.info("ignoring end marker on the first message after waking")
            elif marker.found:
                # The model judged that Vuk said goodbye: no follow-up listening.
                self._end_session = True
                self._emit("dismiss", "")
            if self._denied_at is not None and self._reply_len == self._denied_at:
                # The model went quiet after a denial; never leave "Gotovo." as the last word.
                say_extra("Ta radnja nije izvršena jer nije odobrena.")
            log.info("Tesla: %s", reveal.text.strip())
            links = extract_links(reveal.text)
            if links:
                self.last_sources = links
            reveal.sources = links
            sentences.put_nowait(None)
            await speaker
        finally:
            speaker.cancel()

    async def _show(self, block: str) -> None:
        self._emit("shown", block)
        if not await self._ui_present():
            await notify.notify("Tesla", block)

    async def _speak_worker(self, sentences: asyncio.Queue, reveal: "_Reveal") -> None:
        while (item := await sentences.get()) is not None:
            sentence, upto = item
            if not self.cfg.tts.enabled:
                reveal.upto(upto)
                continue

            def started(upto=upto) -> None:
                if self.state not in (State.SPEAKING, State.APPROVAL):
                    self._set_state(State.SPEAKING)
                reveal.started(upto)

            # Play pieces as they arrive; the text shows when the first one sounds.
            first = True
            async for pcm in self.voice.stream(sentence):
                if first:
                    reveal.expect()
                    self.player.play(pcm, on_start=started)
                    first = False
                else:
                    self.player.play(pcm)
            if first:  # no audio (TTS down): show the text anyway
                reveal.upto(upto)
        # Whatever is left (code blocks, sources) shows once the last sentence has begun.
        reveal.finish()
        await self.player.drained()

    async def _say(self, text: str, show: bool = True) -> None:
        on_start = (lambda: self._emit("delta", text)) if show else None
        if self.cfg.tts.enabled:
            async for pcm in self.voice.stream(text):
                self.player.play(pcm, on_start=on_start)
                on_start = None
        if on_start:  # silent mode, or no audio came back
            on_start()
        await self.player.drained()

    async def _approve(self, description: str) -> bool:
        previous = self.state
        self._set_state(State.APPROVAL)
        self.sounds.play("approval")
        approval_id = next(self._approval_ids)
        allowed = False
        try:
            if await self._ui_present():
                fut = asyncio.get_running_loop().create_future()
                self._approvals[approval_id] = fut
                self._emit("approval", json.dumps({"id": approval_id, "description": description}))
                await self._say("Treba mi tvoja dozvola.", show=False)
                async with asyncio.timeout(APPROVAL_TIMEOUT_S):
                    allowed = await fut
            else:
                await self._say("Treba mi tvoja dozvola.", show=False)
                allowed = await notify.ask_approval(description, APPROVAL_TIMEOUT_S)
            log.info("approval %d %s: %s", approval_id, "allowed" if allowed else "denied", description)
            return allowed
        except TimeoutError:
            return False
        finally:
            if not allowed:
                self._denied_at = self._reply_len
            self._approvals.pop(approval_id, None)
            self._emit("approval_done", str(approval_id))
            self._set_state(previous)

    # ----- talking to the shell extension -----

    async def _shell_call(self, member: str, signature: str = "", body: list | None = None):
        if not self.bus:
            return None
        reply = await self.bus.call(Message(destination=SHELL_BUS_NAME, path=SHELL_PATH,
                                            interface=SHELL_BUS_NAME, member=member,
                                            signature=signature, body=body or []))
        if reply.message_type == MessageType.ERROR:
            log.debug("shell %s failed: %s %s", member, reply.error_name, reply.body)
            return None
        return reply.body

    async def _ui_present(self) -> bool:
        if not self.bus:
            return False
        reply = await self.bus.call(Message(
            destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus", member="NameHasOwner", signature="s",
            body=[SHELL_BUS_NAME]))
        return reply.message_type != MessageType.ERROR and bool(reply.body[0])

    async def _capture_context(self) -> ScreenContext:
        """Fresh screen context from the extension (wake word, follow-ups, typed
        requests, look_at_screen). Keeps the previous one if the shell is away."""
        try:
            async with asyncio.timeout(2):
                body = await self._shell_call("CaptureContext")
        except TimeoutError:
            body = None
        if body:
            self.context = ScreenContext.from_dbus(body[0])
        return self.context


class TeslaInterface(ServiceInterface):
    """D-Bus API used by the GNOME Shell extension and the `tesla` CLI."""

    def __init__(self, assistant: Assistant) -> None:
        super().__init__(BUS_NAME)
        self.assistant = assistant
        assistant.listeners.append(self._on_event)

    @method()
    def Toggle(self) -> None:
        self.assistant.activate()

    @method()
    def Activate(self, context: "a{ss}") -> None:
        self.assistant.activate(ScreenContext.from_dbus(context))

    @method()
    def Ask(self, text: "s") -> None:
        self.assistant.ask_text(text)

    @method()
    def StopListening(self) -> None:
        self.assistant.stop_listening()

    @method()
    def SetHold(self, hold: "b") -> None:
        self.assistant.set_hold(hold)

    @method()
    def Cancel(self) -> None:
        self.assistant.cancel()

    @method()
    def Approve(self, approval_id: "u", allow: "b") -> None:
        self.assistant.approve(approval_id, allow)

    @method()
    def SetWake(self, enabled: "b") -> None:
        self.assistant.set_wake(enabled)

    @method()
    def GetState(self) -> "s":
        return str(self.assistant.state)

    @method()
    def GetUiSettings(self) -> "a{ss}":
        return {"capture": self.assistant.cfg.screen.capture}

    @signal()
    def Event(self, kind: str, value: str) -> "ss":
        return [kind, value]

    def _on_event(self, kind: str, value: str) -> None:
        self.Event(kind, value)


async def run(cfg: Config) -> None:
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    owner = await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                   interface="org.freedesktop.DBus", member="NameHasOwner",
                                   signature="s", body=[BUS_NAME]))
    if owner.body and owner.body[0]:
        # Two daemons would both listen for "Hej Tesla" and answer over each other.
        log.error("Tesla već radi (npr. kao servis: systemctl --user status tesla).")
        raise SystemExit(ALREADY_RUNNING)
    log.info("version: %s", installed_version())
    log.info("loading models...")
    assistant = await asyncio.to_thread(Assistant, cfg)
    bus.export(OBJECT_PATH, TeslaInterface(assistant))
    if await bus.request_name(BUS_NAME, NameFlag.DO_NOT_QUEUE) != RequestNameReply.PRIMARY_OWNER:
        log.error("Tesla već radi.")
        raise SystemExit(ALREADY_RUNNING)
    assistant.bus = bus
    # A fresh daemon has no conversation to resume: drop leftover transcripts.
    if removed := delete_transcripts():
        log.info("deleted %d leftover transcript file(s)", removed)
    await assistant.brain.start()
    if cfg.wake.enabled and assistant.wake:
        assistant.wake.start()
    ui = "notch" if await assistant._ui_present() else "notifikacije (ekstenzija nije aktivna)"
    log.info("Tesla je spremna. UI: %s", ui)

    # systemd stops the service with SIGTERM; shut down cleanly so the conversation's
    # transcripts get deleted and the claude CLI exits with us.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signals.SIGTERM, signals.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    waiters = [asyncio.create_task(stop.wait()), asyncio.create_task(bus.wait_for_disconnect())]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        log.info("shutting down")
        for w in waiters:
            w.cancel()
        assistant.cancel()
        if assistant.wake:
            assistant.wake.stop()
        await assistant.brain.shutdown()
        assistant.player.close()
        bus.disconnect()


def installed_version() -> str:
    """What scripts/install.sh recorded, or "dev" when running from the repo."""
    path = DATA_DIR / "installed.json"
    try:
        info = json.loads(path.read_text())
        return f"{info['commit']} ({info['installed_at']})"
    except (OSError, ValueError, KeyError):
        return "dev"
