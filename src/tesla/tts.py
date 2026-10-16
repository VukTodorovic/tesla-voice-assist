"""Text to speech with Microsoft's Serbian neural voices (via edge-tts, online)."""

import asyncio
import logging
import re
from collections.abc import AsyncIterator

import av
import edge_tts
import numpy as np

from tesla.config import TtsConfig

log = logging.getLogger(__name__)

VOICES = {"nicholas": "sr-RS-NicholasNeural", "sophie": "sr-RS-SophieNeural"}
SAMPLE_RATE = 24000
TIMEOUT_S = 8.0

# The Serbian voices read Latin script as gibberish, so everything is spoken in
# Cyrillic. English words are spelled the Serbian way first; a short lexicon
# covers the ones that come up all the time.
_LEXICON = {
    "vs code": "vi es kod", "visual studio code": "vižual studio kod", "code": "kod",
    "firefox": "fajerfoks", "chrome": "krom", "chromium": "kromijum", "google": "gugl",
    "github": "githab", "gitlab": "gitlab", "slack": "slek", "youtube": "jutjub",
    "claude": "klod", "chatgpt": "čet dži pi ti", "gpt": "dži pi ti", "openai": "open ej aj",
    "backend": "bekend", "frontend": "frontend", "docker": "doker", "compose": "kompouz",
    "install": "instol", "npm": "en pi em", "api": "ej pi aj", "json": "džejson", "yaml": "jaml",
    "config": "konfig", "pipeline": "pajplajn", "commit": "komit", "push": "puš", "pull": "pul",
    "branch": "branč", "merge": "merdž", "deploy": "diploj", "cloud": "klaud", "batch": "beč",
    "size": "sajz", "update": "apdejt", "email": "imejl", "online": "onlajn", "offline": "oflajn",
    "python": "pajton", "linux": "linuks", "ubuntu": "ubuntu", "wayland": "vejlend",
    "gnome": "gnom", "file": "fajl", "folder": "folder", "chat": "čet", "link": "link",
    "spotify": "spotifaj", "notch": "noč", "prozone": "prozon",
}
_LEXICON_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, _LEXICON), key=len, reverse=True)) + r")\b",
                         re.I)
_LAT_DIGRAPHS = [("dž", "џ"), ("Dž", "Џ"), ("DŽ", "Џ"), ("lj", "љ"), ("Lj", "Љ"), ("LJ", "Љ"),
                 ("nj", "њ"), ("Nj", "Њ"), ("NJ", "Њ")]
_LAT_TO_CYR = str.maketrans(  # x is spelled "ks" beforehand
    "abvgdđežzijklmnoprstćufhcčšwqyABVGDĐEŽZIJKLMNOPRSTĆUFHCČŠWQY",
    "абвгдђежзијклмнопрстћуфхцчшвкјАБВГДЂЕЖЗИЈКЛМНОПРСТЋУФХЦЧШВКЈ")


def to_spoken_cyrillic(text: str) -> str:
    text = _LEXICON_RE.sub(lambda m: _LEXICON[m.group().lower()], text)
    text = re.sub(r"x", "ks", text).replace("X", "Ks")
    for lat, cyr in _LAT_DIGRAPHS:
        text = text.replace(lat, cyr)
    return text.translate(_LAT_TO_CYR)


class Voice:
    def __init__(self, cfg: TtsConfig) -> None:
        self.voice = VOICES.get(cfg.voice.lower(), cfg.voice)
        self.rate = cfg.rate
        self._warned = False

    @property
    def sample_rate(self) -> int:
        return SAMPLE_RATE

    async def stream(self, text: str) -> AsyncIterator[np.ndarray]:
        """Audio for `text`, yielded piece by piece as it arrives from the service."""
        spoken = to_spoken_cyrillic(text) if self.voice.startswith("sr-") else text
        decoder = _Mp3Stream()
        try:
            async with asyncio.timeout(TIMEOUT_S):
                async for chunk in edge_tts.Communicate(spoken, self.voice, rate=self.rate).stream():
                    if chunk["type"] == "audio" and len(pcm := decoder.feed(chunk["data"])):
                        yield pcm
        except Exception as e:  # offline or the service changed: stay silent, the notch still shows text
            if not self._warned:
                hint = ""
                if "403" in str(e):
                    # Microsoft changes the token check now and then; edge-tts releases follow.
                    hint = (" - likely an outdated edge-tts: uv lock --upgrade-package edge-tts && "
                            "uv sync && scripts/build-whispercpp-vulkan.sh --install")
                log.warning("TTS unavailable (%s); answers are only shown, not spoken%s", e, hint)
                self._warned = True
            return
        self._warned = False
        if len(pcm := decoder.flush()):
            yield pcm

    async def synthesize(self, text: str) -> np.ndarray:
        parts = [pcm async for pcm in self.stream(text)]
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

    def synthesize_sync(self, text: str) -> np.ndarray:
        """For scripts outside an event loop."""
        return asyncio.run(self.synthesize(text))


class _Mp3Stream:
    """Incremental MP3 decoding, so playback can start before the download ends."""

    def __init__(self) -> None:
        self._codec = av.CodecContext.create("mp3", "r")

    def feed(self, data: bytes | None) -> np.ndarray:
        frames = []
        for packet in self._codec.parse(data):
            frames += self._codec.decode(packet)
        return _to_mono(frames)

    def flush(self) -> np.ndarray:
        frames = [f for p in self._codec.parse(None) for f in self._codec.decode(p)]
        return _to_mono(frames + self._codec.decode(None))


# Full-scale value per decoder sample format (the streaming decoder gives s16p).
_FULL_SCALE = {"s16": 32768.0, "s16p": 32768.0, "s32": 2147483648.0, "s32p": 2147483648.0}


def _to_mono(frames) -> np.ndarray:
    if not frames:
        return np.zeros(0, dtype=np.float32)
    scale = _FULL_SCALE.get(frames[0].format.name, 1.0)
    audio = np.concatenate([f.to_ndarray().mean(axis=0) for f in frames]).astype(np.float32)
    return audio / scale if scale != 1.0 else audio
