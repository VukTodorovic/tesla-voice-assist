"""Microphone capture and speaker playback (PipeWire through PortAudio)."""

import asyncio
import collections
import contextlib
import logging
import threading

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

MIC_RATE = 16000
# 32 ms at 16 kHz: the frame size Silero VAD expects.
FRAME = 512


class Microphone:
    """One shared input stream, fanned out to subscribers.

    It is open only while someone listens: during a request, or all the time
    when the wake word is on (GNOME then shows the mic indicator permanently).
    """

    def __init__(self, device: str | None = None) -> None:
        self.device = device
        self._subscribers: set[asyncio.Queue] = set()
        self._stream: sd.InputStream | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self._subscribers.add(queue)
        if self._stream is None:
            self._loop = asyncio.get_running_loop()
            self._stream = sd.InputStream(samplerate=MIC_RATE, blocksize=FRAME, channels=1,
                                          dtype="float32", device=self.device, callback=self._callback)
            self._stream.start()
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)
        if not self._subscribers and self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    @contextlib.asynccontextmanager
    async def stream(self, queue: asyncio.Queue | None = None):
        """Subscribe for the duration of a block (or adopt an existing subscription)."""
        queue = queue or self.subscribe()
        try:
            yield queue
        finally:
            self.unsubscribe(queue)

    def _callback(self, indata, frames, time_info, status):
        if status:
            log.debug("mic status: %s", status)
        frame = indata[:, 0].copy()
        for queue in list(self._subscribers):
            self._loop.call_soon_threadsafe(queue.put_nowait, frame)


class Player:
    """Gapless queue of float32 mono chunks; `stop()` silences immediately.

    A chunk may carry an `on_start` callback, run on the event loop when that
    chunk actually starts sounding (not when it was queued).
    """

    def __init__(self, rate: int, device: str | None = None) -> None:
        self.rate = rate
        self.device = device
        # [audio, on_start or None]; on_start is cleared once it has been scheduled.
        self._chunks: collections.deque[list] = collections.deque()
        self._lock = threading.Lock()
        self._stream: sd.OutputStream | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._drained = asyncio.Event()
        self._drained.set()

    def open(self) -> None:
        if self._stream is None:
            self._loop = asyncio.get_running_loop()
            self._stream = sd.OutputStream(samplerate=self.rate, channels=1, dtype="float32",
                                           device=self.device, callback=self._callback)
            self._stream.start()

    def close(self) -> None:
        self.stop()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def play(self, audio: np.ndarray, on_start=None) -> None:
        if len(audio) == 0:
            if on_start:
                on_start()
            return
        self.open()
        with self._lock:
            self._chunks.append([audio.astype(np.float32, copy=False), on_start])
        self._drained.clear()

    def stop(self) -> None:
        with self._lock:
            self._chunks.clear()
        self._drained.set()

    async def drained(self) -> None:
        await self._drained.wait()

    def _callback(self, outdata, frames, time_info, status):
        out = outdata[:, 0]
        filled = 0
        with self._lock:
            while filled < frames and self._chunks:
                entry = self._chunks[0]
                chunk, on_start = entry
                if on_start is not None:
                    entry[1] = None
                    self._loop.call_soon_threadsafe(on_start)
                n = min(frames - filled, len(chunk))
                out[filled:filled + n] = chunk[:n]
                if n == len(chunk):
                    self._chunks.popleft()
                else:
                    entry[0] = chunk[n:]
                filled += n
            empty = not self._chunks
        out[filled:] = 0
        if empty and not self._drained.is_set() and self._loop is not None:
            self._loop.call_soon_threadsafe(self._drained.set)


class Sounds:
    """Short earcons so state changes are audible without looking at the screen."""

    def __init__(self, player: Player, enabled: bool, volume: float) -> None:
        self.player = player
        self.enabled = enabled
        rate = player.rate
        self._sounds = {
            "listen": _tones(rate, [660, 880], volume),
            "heard": _tones(rate, [880, 660], volume),
            "followup": _tones(rate, [740], volume * 0.6),
            "cancel": _tones(rate, [440, 330], volume * 0.8),
            "error": _tones(rate, [220, 196], volume, dur=0.15),
            "approval": _tones(rate, [988, 988], volume, dur=0.06),
        }

    def play(self, name: str) -> None:
        if self.enabled:
            self.player.play(self._sounds[name])


def _tones(rate: int, freqs: list[float], volume: float, dur: float = 0.07) -> np.ndarray:
    t = np.arange(int(rate * dur)) / rate
    fade = np.minimum(1.0, np.minimum(t, t[::-1]) / 0.01)
    parts = [np.sin(2 * np.pi * f * t) * fade * volume for f in freqs]
    return np.concatenate(parts).astype(np.float32)
