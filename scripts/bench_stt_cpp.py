"""Compare faster-whisper with whisper.cpp (full vs shortened audio window) on real speech.

Uses ~/.local/share/tesla/bench/my_voice.wav (from bench_voice.py --record), split
into utterances with Tesla's VAD, so timings are per request like in real use.

    uv run --with pywhispercpp scripts/bench_stt_cpp.py
"""

import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from bench_voice import SENTENCES, load_16k, wer  # noqa: E402

from tesla.stt import ENGINE_DEFAULT_PROMPT  # noqa: E402
from tesla.models import MODELS_DIR, whisper_dir  # noqa: E402
from tesla.text import to_latin  # noqa: E402
from tesla.vad import StreamingVad  # noqa: E402

PROMPT = ENGINE_DEFAULT_PROMPT["faster-whisper"]
CPP_DIR = MODELS_DIR / "whispercpp"


def utterances(audio: np.ndarray) -> list[np.ndarray]:
    vad = StreamingVad()
    speech = [vad(audio[i:i + 512]) >= 0.5 for i in range(0, len(audio) - 512, 512)]
    segs, start, silence = [], None, 0
    for i, s in enumerate(speech):
        if s:
            start = i if start is None else start
            silence = 0
        elif start is not None:
            silence += 1
            if silence * 32 >= 500:
                segs.append((start, i - silence + 1))
                start = None
    return [audio[max(0, a - 10) * 512:(b + 3) * 512] for a, b in segs if (b - a) * 32 > 800]


def report(name: str, results: list[tuple[float, str]], refs: list[str]) -> None:
    times = [t for t, _ in results]
    errors = [wer(r, h) for (_, h), r in zip(results, refs)]
    print(f"\n{name:38s} avg {np.mean(times):.2f}s  max {max(times):.2f}s  WER {np.mean(errors):.0%}")
    for (t, h), r in zip(results, refs):
        print(f"   {t:4.2f}s  {h}")


def main() -> None:
    audio = load_16k(Path.home() / ".local/share/tesla/bench/my_voice.wav")
    clips = utterances(audio)[:len(SENTENCES)]
    # The recording started late: the first sentence lost "Tesla, o-".
    refs = ["otvori mi terminal u projektu prozone."] + SENTENCES[1:len(clips)]
    print(f"{len(clips)} utterances: {[round(len(c) / 16000, 1) for c in clips]} s")

    from faster_whisper import WhisperModel
    fw = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=8,
                      download_root=str(whisper_dir()))
    def run_fw(clip):
        segs, _ = fw.transcribe(clip, language="sr", beam_size=1, initial_prompt=PROMPT,
                                condition_on_previous_text=False, without_timestamps=True)
        return " ".join(s.text.strip() for s in segs)
    run_fw(clips[0])
    res = []
    for c in clips:
        t0 = time.perf_counter()
        text = run_fw(c)
        res.append((time.perf_counter() - t0, to_latin(text)))
    report("faster-whisper small int8 (current)", res, refs)

    from pywhispercpp.model import Model
    for model in sys.argv[1:] or ["small-q8_0", "medium-q5_0", "large-v3-turbo-q5_0", "large-v3-turbo-q8_0"]:
        path = CPP_DIR / f"ggml-{model}.bin"
        if not path.exists():
            print(f"\n(skipping {model}: {path} missing)")
            continue
        m = Model(str(path), n_threads=8, language="sr", print_progress=False, print_realtime=False,
                  no_timestamps=True, single_segment=True, initial_prompt=PROMPT, no_context=True,
                  redirect_whispercpp_logs_to=None)
        for mode in ("full", "fit"):
            def opts(clip):
                dur = len(clip) / 16000
                # 50 encoder frames per second of audio, with headroom; a token cap stops
                # the repetition loops a shortened window can fall into.
                return {"audio_ctx": 0 if mode == "full" else min(1500, math.ceil(dur * 50 * 1.3) + 128),
                        "max_tokens": int(dur * 15) + 20}
            m.transcribe(clips[0], **opts(clips[0]))
            res = []
            for c in clips:
                t0 = time.perf_counter()
                segs = m.transcribe(c, **opts(c))
                res.append((time.perf_counter() - t0, to_latin(" ".join(s.text.strip() for s in segs))))
            report(f"whisper.cpp {model} {'30s window' if mode == 'full' else 'fitted window'}", res, refs)


if __name__ == "__main__":
    main()
