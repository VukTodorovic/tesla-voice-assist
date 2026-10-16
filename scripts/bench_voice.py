"""Benchmark Serbian STT (faster-whisper sizes) and record your voice for other benches.

Synthesises reference sentences with Tesla's voice, then transcribes them back with
several Whisper sizes and reports speed and word error rate. Synthetic speech is
cleaner than a real voice, so treat WER as optimistic; use --record to also test
your own voice.

    uv run scripts/bench_voice.py
    uv run scripts/bench_voice.py --models tiny base small --record
"""

import argparse
import re
import time
import wave
from pathlib import Path

import numpy as np

from tesla.config import DATA_DIR
from tesla.config import TtsConfig
from tesla.models import whisper_dir
from tesla.tts import Voice
from tesla.text import to_latin

SENTENCES = [
    "Tesla, otvori mi terminal u projektu prozone.",
    "Pronađi koja je vrednost porta u konfiguraciji za DJS bekend.",
    "Koliko je sati u Njujorku?",
    "Podseti me za dvadeset minuta da proverim veš mašinu.",
    "Otvori Firefox i potraži recept za palačinke.",
    "Zapamti da više volim kratke odgovore.",
]

OUT = DATA_DIR / "bench"


def words(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", to_latin(text).lower()).split()


def wer(ref: str, hyp: str) -> float:
    r, h = words(ref), words(hyp)
    d = np.zeros((len(r) + 1, len(h) + 1), dtype=int)
    d[:, 0] = range(len(r) + 1)
    d[0, :] = range(len(h) + 1)
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i, j] = min(d[i - 1, j] + 1, d[i, j - 1] + 1,
                          d[i - 1, j - 1] + (r[i - 1] != h[j - 1]))
    return d[len(r), len(h)] / max(len(r), 1)


def load_16k(path: Path) -> np.ndarray:
    # faster-whisper's own file decoding is broken with the installed PyAV; the
    # assistant feeds it 16 kHz arrays from the mic anyway.
    with wave.open(str(path)) as w:
        rate = w.getframerate()
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    audio = audio.astype(np.float32) / 32768
    if rate != 16000:
        n = int(len(audio) * 16000 / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio)
    return audio.astype(np.float32)


def bench_tts() -> list[Path]:
    voice = Voice(TtsConfig())
    OUT.mkdir(parents=True, exist_ok=True)
    paths = []
    print(f"\n=== TTS {voice.voice}")
    for i, text in enumerate(SENTENCES):
        t0 = time.perf_counter()
        audio = voice.synthesize_sync(text)
        print(f"  {time.perf_counter() - t0:.2f}s for {len(audio) / voice.sample_rate:.1f}s of audio")
        path = OUT / f"tts_{i}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(voice.sample_rate)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        paths.append(path)
    return paths


def record(seconds: float) -> Path:
    import sounddevice as sd

    path = OUT / "my_voice.wav"
    print(f"\nRecording {seconds:.0f}s - read this aloud:\n  {' '.join(SENTENCES)}")
    for n in (3, 2, 1):
        print(f"  {n}...")
        time.sleep(1)
    audio = sd.rec(int(seconds * 16000), samplerate=16000, channels=1, dtype="int16")
    sd.wait()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(audio.tobytes())
    return path


def bench_stt(models: list[str], tts_paths: list[Path], my_voice: Path | None) -> None:
    from faster_whisper import WhisperModel

    for name in models:
        t0 = time.perf_counter()
        model = WhisperModel(name, device="cpu", compute_type="int8",
                             download_root=str(whisper_dir()))
        load_s = time.perf_counter() - t0
        for beam in (1, 5):
            total_s = 0.0
            errs = []
            samples = []
            for ref, path in zip(SENTENCES, tts_paths):
                t0 = time.perf_counter()
                segs, _ = model.transcribe(load_16k(path), language="sr", beam_size=beam,
                                           condition_on_previous_text=False)
                hyp = " ".join(s.text for s in segs).strip()
                total_s += time.perf_counter() - t0
                errs.append(wer(ref, hyp))
                samples.append(to_latin(hyp))
            print(f"\n=== whisper {name} beam={beam}: load {load_s:.1f}s, "
                  f"avg {total_s / len(tts_paths):.2f}s/utterance, WER {np.mean(errs):.0%}")
            for s in samples[:3]:
                print(f"    {s}")
            if my_voice:
                t0 = time.perf_counter()
                segs, _ = model.transcribe(load_16k(my_voice), language="sr", beam_size=beam)
                hyp = " ".join(s.text for s in segs).strip()
                print(f"  your voice: {time.perf_counter() - t0:.2f}s, "
                      f"WER {wer(' '.join(SENTENCES), hyp):.0%}\n    {to_latin(hyp)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=["tiny", "base", "small"])
    ap.add_argument("--record", action="store_true", help="also record and test your voice")
    ap.add_argument("--seconds", type=float, default=25)
    args = ap.parse_args()
    paths = bench_tts()
    my_voice = record(args.seconds) if args.record else None
    bench_stt(args.models, paths, my_voice)


if __name__ == "__main__":
    main()
