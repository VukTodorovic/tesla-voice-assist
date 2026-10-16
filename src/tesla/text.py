"""Text helpers shared by STT and TTS: script normalisation and speech chunking."""

import re

_CYR_TO_LAT = {
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Ђ": "Đ", "Е": "E", "Ж": "Ž",
    "З": "Z", "И": "I", "Ј": "J", "К": "K", "Л": "L", "Љ": "Lj", "М": "M", "Н": "N",
    "Њ": "Nj", "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T", "Ћ": "Ć", "У": "U",
    "Ф": "F", "Х": "H", "Ц": "C", "Ч": "Č", "Џ": "Dž", "Ш": "Š",
}
_CYR_TO_LAT.update({k.lower(): v.lower() for k, v in _CYR_TO_LAT.items()})
_CYR_TABLE = str.maketrans(_CYR_TO_LAT)


def to_latin(text: str) -> str:
    """Whisper writes Serbian in either script; normalise to Latin."""
    return text.translate(_CYR_TABLE)


# Abbreviations that end in a dot but do not end a sentence.
_ABBREVIATIONS = ("npr", "tj", "itd", "sl", "br", "str", "dr", "mr", "god", "e.g", "i.e", "etc")
_SENTENCE_END = re.compile(r"([.!?…]+)[\"')\]]*\s+")
_MIN_CHUNK = 12


# A trailing "Sources:" / "Izvori:" list (WebSearch asks the model to add one).
SOURCES_HEADER = re.compile(r"(?im)^[ \t*#]*(sources|izvori|izvor|reference|linkovi)\**[ \t]*:")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)|(?<![(\[])(https?://[^\s)\]>\"']+)")


def extract_links(text: str) -> list[dict[str, str]]:
    """Markdown links and bare URLs, in order of appearance, without duplicates."""
    links, seen = [], set()
    for m in _LINK.finditer(text):
        url = (m.group(2) or m.group(3)).rstrip(".,;:")
        domain = re.sub(r"^www\.", "", url.split("/")[2])
        title = (m.group(1) or domain).strip()
        # The same page often comes back twice with a slightly different URL.
        keys = {re.sub(r"^https?://(www\.)?|[#?].*$|/$", "", url).lower(), title.lower()}
        if keys & seen:
            continue
        seen |= keys
        links.append({"title": title, "url": url, "domain": domain})
    return links


class SentenceChunker:
    """Collects streamed text deltas and yields complete sentences for TTS.

    Text inside ``` fences is "show, don't say": it is routed to `shown` and
    never spoken. Neither is a trailing sources list.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_code = False
        self._in_sources = False
        self.shown: list[str] = []
        self._code_buf = ""

    def feed(self, delta: str) -> list[str]:
        if self._in_sources:
            return []
        self._buf += delta
        out: list[str] = []
        if not self._in_code:
            header = SOURCES_HEADER.search(self._buf)
            if header and "```" not in self._buf[:header.start()]:
                self._buf = self._buf[:header.start()]
                self._in_sources = True
                return self.flush()
        while True:
            if self._in_code:
                end = self._buf.find("```")
                if end < 0:
                    return out
                self._code_buf += self._buf[:end]
                self.shown.append(self._code_buf.split("\n", 1)[-1].strip("\n"))
                self._code_buf = ""
                self._buf = self._buf[end + 3:]
                self._in_code = False
                continue
            fence = self._buf.find("```")
            speakable = self._buf if fence < 0 else self._buf[:fence]
            sentences, rest = _split_sentences(speakable)
            out.extend(sentences)
            if fence < 0:
                self._buf = rest
                return out
            if rest.strip():
                out.append(rest)
            self._buf = self._buf[fence + 3:]
            self._in_code = True

    @property
    def pending(self) -> int:
        """Characters fed but not yet turned into sentences or shown blocks."""
        return len(self._buf) + len(self._code_buf)

    def end_block(self) -> list[str]:
        """The model moved on to a tool call: speak what is pending, start fresh."""
        out = self.flush()
        self._in_sources = False
        return out

    def flush(self) -> list[str]:
        out = []
        if self._in_code:
            self.shown.append((self._code_buf + self._buf).split("\n", 1)[-1].strip())
        elif self._buf.strip():
            out.append(self._buf)
        self._buf = self._code_buf = ""
        self._in_code = False
        return [s for s in (clean_for_speech(x) for x in out) if s]


def _split_sentences(text: str) -> tuple[list[str], str]:
    sentences = []
    start = 0
    for m in _SENTENCE_END.finditer(text):
        candidate = text[start:m.end()]
        before = text[start:m.start()].rsplit(None, 1)
        last_word = before[-1].lower() if before else ""
        if m.group(1) == "." and (last_word in _ABBREVIATIONS or last_word.isdigit()):
            continue
        if len(candidate.strip()) < _MIN_CHUNK:
            continue
        sentences.append(candidate)
        start = m.end()
    cleaned = [s for s in (clean_for_speech(x) for x in sentences) if s]
    return cleaned, text[start:]


_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_INLINE_CODE = re.compile(r"`([^`]*)`")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")
_MD_MARKS = re.compile(r"(\*\*|__|\*|#+\s|^\s*[-•]\s+|^\s*\d+\.\s+)", re.M)


def clean_for_speech(text: str) -> str:
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub("link", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _MD_MARKS.sub("", text)
    text = _EMOJI.sub("", text)
    return " ".join(text.split())


END_MARKER = "[[kraj]]"


class MarkerFilter:
    """Removes the model's end-of-conversation marker from a delta stream.

    Holds back a trailing partial marker ("[[kr") until it is clear what it is.
    """

    def __init__(self, marker: str = END_MARKER) -> None:
        self.marker = marker
        self.found = False
        self._held = ""

    def feed(self, text: str) -> str:
        buf = self._held + text
        if self.marker in buf:
            self.found = True
            buf = buf.replace(self.marker, "")
        keep = next((n for n in range(min(len(self.marker) - 1, len(buf)), 0, -1)
                     if self.marker.startswith(buf[-n:])), 0)
        self._held = buf[len(buf) - keep:] if keep else ""
        return buf[:len(buf) - keep] if keep else buf

    def flush(self) -> str:
        held, self._held = self._held, ""
        return held
