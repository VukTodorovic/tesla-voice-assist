"""Locations of downloaded speech models (fetched on first use)."""

from pathlib import Path

from tesla.config import DATA_DIR

MODELS_DIR = DATA_DIR / "models"


def whisper_dir() -> Path:
    path = MODELS_DIR / "whisper"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_vosk_model(name: str) -> Path:
    import io
    import urllib.request
    import zipfile

    root = MODELS_DIR / "vosk"
    path = root / name
    if not path.is_dir():
        root.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(f"https://alphacephei.com/vosk/models/{name}.zip") as r:
            zipfile.ZipFile(io.BytesIO(r.read())).extractall(root)
    return path


def ensure_whispercpp_model(name: str) -> Path:
    """A ggml model by name (downloaded from ggerganov/whisper.cpp) or an existing path."""
    import urllib.request

    if Path(name).expanduser().is_file():
        return Path(name).expanduser()
    path = MODELS_DIR / "whispercpp" / f"ggml-{name}.bin"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(
            f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{name}.bin", tmp)
        tmp.rename(path)
    return path
