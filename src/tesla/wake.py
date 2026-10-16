"""Wake word "Hej Tesla".

Vosk (small English model, free decoding, ~2% of one core) watches the shared
mic while Tesla is idle and fires as soon as its hypothesis contains "tesla".
In tests on Serbian speech it never produced "tesla" for other words, so its
trigger is trusted. Whisper then transcribes pre-roll plus request, and
`strip_wake` removes the name from the front when Whisper wrote it.
"""

import asyncio
import collections
import difflib
import json
import logging
import re
from collections.abc import Callable

import numpy as np

from tesla.audio import FRAME, MIC_RATE, Microphone
from tesla.config import WakeConfig
from tesla.models import ensure_vosk_model

log = logging.getLogger(__name__)

# How Whisper writes the name (incl. typical mishearings), optionally after "hej".
WAKE_IN_TRANSCRIPT = re.compile(
    r"^\W*(?:(?:hej|hey|ej|aj|e|o)\W+)?(?:t[eé]s+l\w*|tess?va|desla|tezla|te\s?s[lv]a)\b\W*", re.I)
_BATCH = 4  # frames per recognizer call (128 ms)
_COOLDOWN_S = 2.0


_FUZZY_MIN = 0.6


def strip_wake(text: str) -> str | None:
    """The request without the leading "Hej Tesla", or None if the name is missing.

    Runs only after Vosk heard "tesla", so a garbled name ("Paite sva") among the
    first three words still counts.
    """
    m = WAKE_IN_TRANSCRIPT.match(text)
    if m:
        return text[m.end():].strip()
    words = list(re.finditer(r"\w+", text.lower()))[:3]
    spans = [(w.group(), w.end()) for w in words]
    spans += [(a.group() + b.group(), b.end()) for a, b in zip(words, words[1:])]
    score, end = max(((difflib.SequenceMatcher(None, w, "tesla").ratio(), e) for w, e in spans),
                     default=(0.0, 0))
    if score < _FUZZY_MIN:
        return None
    return re.sub(r"^\W+", "", text[end:]).strip()


class WakeWord:
    def __init__(self, cfg: WakeConfig, mic: Microphone,
                 on_wake: Callable[[np.ndarray, asyncio.Queue], None], armed: Callable[[], bool]) -> None:
        from vosk import KaldiRecognizer, Model, SetLogLevel

        SetLogLevel(-1)
        self.cfg = cfg
        self.mic = mic
        self._on_wake = on_wake
        self._armed = armed
        self._model = Model(str(ensure_vosk_model(cfg.model)))
        self._new_recognizer = lambda: KaldiRecognizer(self._model, MIC_RATE)
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._run())
            log.info("wake word on")

    def stop(self) -> None:
        if self.running:
            self._task.cancel()
            log.info("wake word off")

    async def _run(self) -> None:
        recognizer = self._new_recognizer()
        ring: collections.deque[np.ndarray] = collections.deque(
            maxlen=int(self.cfg.preroll_s * MIC_RATE / FRAME))
        batch: list[np.ndarray] = []
        last_wake = 0.0
        loop = asyncio.get_running_loop()
        async with self.mic.stream() as queue:
            while True:
                frame = await queue.get()
                ring.append(frame)
                batch.append(frame)
                if len(batch) < _BATCH:
                    continue
                pcm = (np.concatenate(batch) * 32767).astype(np.int16).tobytes()
                batch.clear()
                heard = await asyncio.to_thread(_feed, recognizer, pcm)
                if "tesla" not in heard.split():
                    continue
                recognizer.Reset()
                if not self._armed() or loop.time() - last_wake < _COOLDOWN_S:
                    continue
                last_wake = loop.time()
                log.info("wake word heard (%r)", heard)
                # Hand over a fresh subscription now, so nothing said after the name is lost.
                self._on_wake(np.concatenate(ring), self.mic.subscribe())
                ring.clear()


def _feed(recognizer, pcm: bytes) -> str:
    if recognizer.AcceptWaveform(pcm):
        return json.loads(recognizer.Result()).get("text", "")
    return json.loads(recognizer.PartialResult()).get("partial", "")
