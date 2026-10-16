"""Configuration: built-in defaults overridden by ~/.config/tesla/config.toml."""

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tesla"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "tesla"
CONFIG_FILE = CONFIG_DIR / "config.toml"


@dataclass
class AudioConfig:
    input_device: str | None = None
    output_device: str | None = None
    feedback_sounds: bool = True
    feedback_volume: float = 0.2


@dataclass
class ListenConfig:
    vad_threshold: float = 0.5
    # Silence after speech that ends the utterance.
    end_silence_ms: int = 800
    # Give up if nothing is said this long after activation.
    no_speech_timeout_s: float = 6.0
    max_utterance_s: float = 30.0
    # After an answer, keep listening this long for a follow-up (0 = off).
    followup_s: float = 4.0


@dataclass
class WakeConfig:
    # "Hej Tesla" without the hotkey. Keeps the mic open while idle.
    enabled: bool = True
    # Small English Vosk model in free decoding: hears "tesla" reliably and almost
    # never invents it; Whisper then confirms it in the transcript.
    model: str = "vosk-model-small-en-us-0.15"
    # Audio kept from before the trigger, so "Hej Tesla, otvori…" in one breath works.
    preroll_s: float = 2.0


@dataclass
class SttConfig:
    # "groq": Groq's hosted Whisper (needs GROQ_API_KEY; audio leaves the machine), with
    # the local model as fallback whenever Groq fails or is slow. "local": local only.
    service: str = "groq"
    groq_model: str = "whisper-large-v3"
    # Past this, the local model transcribes instead.
    groq_timeout_s: float = 3.0
    # Local engine. "auto": whisper.cpp + large-v3-turbo on the GPU when the Vulkan build is installed
    # (scripts/build-whispercpp-vulkan.sh), otherwise faster-whisper small on the CPU.
    # Or force "faster-whisper" / "whisper.cpp".
    engine: str = "auto"
    # Empty = the engine's default. faster-whisper: size name or CTranslate2 model dir;
    # whisper.cpp: ggml model name (e.g. "large-v3-turbo-q8_0") or path to a .bin.
    model: str = ""
    # whisper.cpp: "full" always runs the encoder over 30 s; "fit" shrinks the window
    # to the utterance but never below min_window_s (large models break below ~15 s).
    audio_window: str = "fit"
    min_window_s: float = 15.0
    language: str = "sr"
    beam_size: int = 1
    cpu_threads: int = 8
    # None = the engine's default (see stt.ENGINE_DEFAULT_PROMPT); "" = no prompt.
    initial_prompt: str | None = None

    def __post_init__(self) -> None:
        if self.service not in ("groq", "local"):
            raise ValueError(f"stt.service must be groq|local, not {self.service!r}")


@dataclass
class ScreenConfig:
    # Which screen a screenshot shows: "pointer" (monitor under the mouse), "focused"
    # (monitor of the active window), "primary" (main monitor) or "all" (every monitor
    # in one image; small text gets hard to read).
    capture: str = "pointer"

    def __post_init__(self) -> None:
        if self.capture not in ("pointer", "focused", "primary", "all"):
            raise ValueError(f"screen.capture must be pointer|focused|primary|all, not {self.capture!r}")


@dataclass
class TtsConfig:
    # false = silent mode: answers only appear in the notch.
    enabled: bool = True
    # "nicholas" (male) or "sophie" (female), or any edge-tts voice name.
    voice: str = "nicholas"
    # Speaking rate relative to normal, e.g. "+10%", "-5%".
    rate: str = "+10%"


@dataclass
class BrainConfig:
    # haiku (Haiku 5.5, fastest) | haiku-4.5 | sonnet | opus, or a full model id.
    model: str = "haiku"
    effort: str = "low"
    # Start a fresh conversation after this many idle minutes.
    session_idle_reset_min: float = 15.0


@dataclass
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    listen: ListenConfig = field(default_factory=ListenConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    screen: ScreenConfig = field(default_factory=ScreenConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    # Folders whose subfolders are projects, grouped by kind.
    projects: dict[str, list[str]] = field(default_factory=lambda: {
        "privatni": ["~/coding"],
        "poslovni": ["~/3nity_solutions", "~/prozone-ai", "~/corporate_momentum"],
    })


def load_config(path: Path = CONFIG_FILE) -> Config:
    cfg = Config()
    if not path.exists():
        return cfg
    with path.open("rb") as f:
        data = tomllib.load(f)
    for section in dataclasses.fields(cfg):
        value = data.get(section.name)
        if value is None:
            continue
        current = getattr(cfg, section.name)
        if dataclasses.is_dataclass(current):
            known = {f.name for f in dataclasses.fields(current)}
            unknown = set(value) - known
            if unknown:
                raise ValueError(f"{path}: unknown keys in [{section.name}]: {sorted(unknown)}")
            setattr(cfg, section.name, dataclasses.replace(current, **value))
        else:
            setattr(cfg, section.name, value)
    return cfg
