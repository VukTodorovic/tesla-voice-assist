"""Speech to text: Groq's hosted Whisper, backed by a local model (whisper.cpp or
faster-whisper, chosen by `stt.engine`) that takes over whenever Groq fails."""

import asyncio
import dataclasses
import io
import logging
import math
import os
import re
import time
import wave

import httpx
import numpy as np

from tesla.config import SttConfig
from tesla.models import ensure_whispercpp_model, whisper_dir
from tesla.text import to_latin

log = logging.getLogger(__name__)

# Whisper "hears" subtitle credits in near-silence; drop them on short clips. A plain
# "Hvala." stays: VAD only lets real speech through, and Vuk says it to end a conversation.
_HALLUCINATIONS = re.compile(
    r"^(hvala (vam )?(na gledanju|što ste gledali|što ste pratili|što pratite( kanal)?)|"
    r"titl(ovi|ovao).*|prevod.*|pretplatite se.*|thank you for watching)[.!]*$", re.I)


def _dedupe_sentences(text: str) -> str:
    """turbo sometimes writes a short utterance twice in a row; keep one copy."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [p for i, p in enumerate(parts) if i == 0 or p.lower() != parts[i - 1].lower()]
    return " ".join(kept)


def _looks_hallucinated(text: str) -> bool:
    words = re.findall(r"\w+", text.lower())
    if sum(len(w) for w in words) < 2 or all(len(w) == 1 for w in words):
        return True  # "A.", "S.O.S." on silence
    # A phrase of 3+ words repeated 3 times is a decoding loop, not speech.
    for n in range(3, 9):
        grams = [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]
        if any(grams.count(g) >= 3 for g in set(grams)):
            return True
    return False


ENGINE_DEFAULT_MODEL = {"faster-whisper": "small", "whisper.cpp": "large-v3-turbo-q8_0"}
# The prompt biases spelling and vocabulary. small gains from a longer one; turbo
# echoes a longer prompt back on silence, and with none it "hears" "Hvala." there.
ENGINE_DEFAULT_PROMPT = {
    "faster-whisper": "Tesla, otvori terminal. Pronađi vrednost u konfiguraciji projekta. "
                      "Podseti me za deset minuta.",
    "whisper.cpp": "Tesla.",
}


def vulkan_available() -> bool:
    """Whether the installed whisper.cpp was built with the Vulkan (GPU) backend."""
    import importlib.util
    from pathlib import Path

    try:
        spec = importlib.util.find_spec("_pywhispercpp")
    except (ImportError, ValueError):
        return False
    return bool(spec and spec.origin and list(Path(spec.origin).parent.glob("libggml-vulkan*")))


class GroqWhisper:
    """Groq's OpenAI-compatible transcription endpoint (free tier: ~2000 requests/day)."""

    URL = "https://api.groq.com/openai/v1/audio/transcriptions"

    def __init__(self, cfg: SttConfig, key: str) -> None:
        self.cfg = cfg
        self._key = key
        # One client, so requests reuse the TLS connection.
        self._client = httpx.AsyncClient(timeout=cfg.groq_timeout_s)
        self._paused_until = 0.0

    @property
    def available(self) -> bool:
        return time.monotonic() >= self._paused_until

    async def transcribe(self, audio: np.ndarray) -> str:
        data = {"model": self.cfg.groq_model, "language": self.cfg.language,
                "temperature": "0", "response_format": "json", "prompt": "Tesla."}
        r = await self._client.post(
            self.URL, headers={"Authorization": f"Bearer {self._key}"}, data=data,
            files={"file": ("speech.wav", _wav(audio), "audio/wav")})
        if r.status_code == 429:
            # Rate limited: stay on the local model as long as Groq asks.
            wait = float(r.headers.get("retry-after") or 60)
            self._paused_until = time.monotonic() + wait
            raise RuntimeError(f"Groq rate limit, using the local model for {wait:.0f}s")
        r.raise_for_status()
        return r.json()["text"]


def _wav(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


class Transcriber:
    def __init__(self, cfg: SttConfig) -> None:
        self.groq: GroqWhisper | None = None
        if cfg.service == "groq":
            if key := os.environ.get("GROQ_API_KEY"):
                self.groq = GroqWhisper(cfg, key)
            else:
                log.warning("stt.service is groq but GROQ_API_KEY is not set; using the local model")
        if cfg.engine == "auto":
            engine = "whisper.cpp" if vulkan_available() else "faster-whisper"
            cfg = dataclasses.replace(cfg, engine=engine)
        if not cfg.model:
            cfg = dataclasses.replace(cfg, model=ENGINE_DEFAULT_MODEL[cfg.engine])
        if cfg.initial_prompt is None:
            cfg = dataclasses.replace(cfg, initial_prompt=ENGINE_DEFAULT_PROMPT[cfg.engine])
        if cfg.engine == "whisper.cpp" and not vulkan_available():
            log.warning("whisper.cpp is not installed with Vulkan (scripts/build-whispercpp-vulkan.sh); "
                        "large models will be slow or fail to load")
        self.cfg = cfg
        t0 = time.perf_counter()
        if cfg.engine == "whisper.cpp":
            from pywhispercpp.model import Model

            prompt = {"initial_prompt": cfg.initial_prompt} if cfg.initial_prompt else {}
            self._cpp = Model(
                str(ensure_whispercpp_model(cfg.model)), n_threads=cfg.cpu_threads,
                language=cfg.language, **prompt, no_context=True,
                no_timestamps=True, single_segment=True, print_progress=False, print_realtime=False,
                redirect_whispercpp_logs_to=os.devnull)
            self._run = self._run_cpp
        elif cfg.engine == "faster-whisper":
            from faster_whisper import WhisperModel

            self._fw = WhisperModel(cfg.model, device="cpu", compute_type="int8",
                                    cpu_threads=cfg.cpu_threads, download_root=str(whisper_dir()))
            self._run = self._run_fw
        else:
            raise ValueError(f"unknown stt.engine {cfg.engine!r}")
        log.info("%s %s loaded in %.1fs%s", cfg.engine, cfg.model, time.perf_counter() - t0,
                 " (Vulkan GPU)" if cfg.engine == "whisper.cpp" and vulkan_available() else "")
        if self.groq:
            log.info("stt: Groq %s, local %s as fallback", cfg.groq_model, cfg.model)

    def _run_fw(self, audio: np.ndarray) -> str:
        segments, _ = self._fw.transcribe(
            audio, language=self.cfg.language, beam_size=self.cfg.beam_size,
            initial_prompt=self.cfg.initial_prompt or None,
            condition_on_previous_text=False, without_timestamps=True)
        return " ".join(s.text.strip() for s in segments)

    def _run_cpp(self, audio: np.ndarray) -> str:
        seconds = len(audio) / 16000
        # The encoder normally always runs over 30 s (1500 frames, 50 per second). "fit"
        # shrinks it to the clip plus headroom, but not below min_window_s. A token cap
        # stops the repetition loops a short window can cause.
        window = 0 if self.cfg.audio_window == "full" else min(
            1500, max(int(self.cfg.min_window_s * 50), math.ceil(seconds * 50) + 64))
        segments = self._cpp.transcribe(audio, audio_ctx=window, max_tokens=int(seconds * 15) + 20)
        return " ".join(s.text.strip() for s in segments)

    def transcribe_sync(self, audio: np.ndarray) -> str:
        """Local model only (benchmarks and scripts)."""
        t0 = time.perf_counter()
        text = self._run(audio.astype(np.float32, copy=False))
        log.info("stt local %.2fs for %.1fs of audio", time.perf_counter() - t0, len(audio) / 16000)
        return self._clean(text, audio)

    async def transcribe(self, audio: np.ndarray) -> str:
        if self.groq and self.groq.available:
            t0 = time.perf_counter()
            try:
                text = await self.groq.transcribe(audio)
            except Exception as e:  # network, timeout, 5xx, rate limit: the local model takes over
                log.warning("Groq STT failed (%s); using the local model", str(e) or type(e).__name__)
            else:
                log.info("stt groq %.2fs for %.1fs of audio", time.perf_counter() - t0, len(audio) / 16000)
                return self._clean(text, audio)
        return await asyncio.to_thread(self.transcribe_sync, audio)

    def _clean(self, text: str, audio: np.ndarray) -> str:
        text = _dedupe_sentences(to_latin(text))
        if (len(audio) < 16000 * 2.5 and _HALLUCINATIONS.match(text)) or _looks_hallucinated(text):
            log.info("dropping likely hallucination: %r", text)
            return ""
        return text
