from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CaptionEvent:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class CaptionBuildResult:
    source: str
    events: list[CaptionEvent]
    warnings: list[str]


@dataclass(frozen=True)
class CaptionStyle:
    font_name: str = "Arial"
    font_size: int = 54
    margin_v: int = 64
    outline: int = 3
    primary_color: str = "&H00FFFFFF"
    outline_color: str = "&H00000000"
    max_chars_per_line: int = 42
    max_lines: int = 2


_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def split_sentences(text: str) -> list[str]:
    normalized = " ".join(text.strip().split())
    if not normalized:
        return []
    return [part.strip() for part in _SENTENCE_RE.split(normalized) if part.strip()] or [normalized]


def wrap_caption_text(text: str, *, max_chars_per_line: int, max_lines: int) -> list[str]:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return []
    wrapped = textwrap.wrap(cleaned, width=max(8, max_chars_per_line), break_long_words=False, break_on_hyphens=False)
    if len(wrapped) <= max_lines:
        return ["\\N".join(wrapped)]
    pages: list[str] = []
    for index in range(0, len(wrapped), max_lines):
        pages.append("\\N".join(wrapped[index:index + max_lines]))
    return pages


def ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def escape_ass_text(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\r\n", r"\N").replace("\n", r"\N")


def escape_ass_filter_path(path: Path) -> str:
    normalized = path.resolve().as_posix()
    normalized = re.sub(r"^([A-Za-z]):", r"\1\\:", normalized)
    for char in ("'", "[", "]", ","):
        normalized = normalized.replace(char, "\\" + char)
    return normalized


def load_micro_events(path: Path) -> list[CaptionEvent]:
    data = read_json(path)
    events: list[CaptionEvent] = []
    for item in data:
        text = str(item.get("narration") or "").strip()
        if not text:
            continue
        events.append(CaptionEvent(start=float(item["tl_start"]), end=float(item["tl_end"]), text=text))
    return events


def load_tts_align_events(path: Path) -> list[CaptionEvent]:
    data = read_json(path)
    events: list[CaptionEvent] = []
    for beat in data.get("beats", []):
        for sentence in beat.get("sentences", []):
            text = str(sentence.get("text") or "").strip()
            if not text:
                continue
            events.append(CaptionEvent(start=float(sentence["tl_start"]), end=float(sentence["tl_end"]), text=text))
    return events


def load_parent_events(review_script: Path, beats_timing: Path) -> list[CaptionEvent]:
    beats = read_json(review_script)
    timings = {int(item["beat_id"]): item for item in read_json(beats_timing)}
    events: list[CaptionEvent] = []
    for beat in beats:
        timing = timings.get(int(beat["beat_id"]))
        if timing is None:
            continue
        text = str(beat.get("narration") or "").strip()
        if text:
            events.append(CaptionEvent(start=float(timing["tl_start"]), end=float(timing["tl_end"]), text=text))
    return events


def paginate_events(events: list[CaptionEvent], style: CaptionStyle) -> list[CaptionEvent]:
    output: list[CaptionEvent] = []
    for event in events:
        sentences = split_sentences(event.text)
        pages: list[str] = []
        for sentence in sentences:
            pages.extend(wrap_caption_text(sentence, max_chars_per_line=style.max_chars_per_line, max_lines=style.max_lines))
        if not pages:
            continue
        duration = max(0.001, event.end - event.start)
        weights = [max(1, len(page.replace("\\N", " "))) for page in pages]
        total = sum(weights)
        cursor = event.start
        for index, (page, weight) in enumerate(zip(pages, weights)):
            end = event.end if index == len(pages) - 1 else cursor + duration * weight / total
            if end - cursor < 0.8 and output:
                previous = output.pop()
                output.append(CaptionEvent(previous.start, end, previous.text + r"\N" + page))
            else:
                output.append(CaptionEvent(cursor, end, page))
            cursor = end
    return output


def build_caption_events(*, review_script: Path | None, beats_timing: Path | None, review_micro: Path | None, tts_align: Path | None, style: CaptionStyle) -> CaptionBuildResult:
    warnings: list[str] = []
    source = "none"
    events: list[CaptionEvent] = []
    if review_micro and review_micro.is_file():
        events = load_micro_events(review_micro)
        source = "review_micro"
    elif tts_align and tts_align.is_file():
        events = load_tts_align_events(tts_align)
        source = "tts_align"
    elif review_script and beats_timing and review_script.is_file() and beats_timing.is_file():
        events = load_parent_events(review_script, beats_timing)
        source = "beats"
    else:
        warnings.append("caption source files are missing")
    paginated = paginate_events(events, style)
    if not paginated:
        warnings.append("no caption events generated")
    return CaptionBuildResult(source=source, events=paginated, warnings=warnings)


def write_ass(path: Path, *, events: list[CaptionEvent], width: int, height: int, style: CaptionStyle) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{style.font_name},{style.font_size},{style.primary_color},&H000000FF,{style.outline_color},&H64000000,1,0,0,0,100,100,0,0,1,{style.outline},0,2,80,80,{style.margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for event in events:
        if event.end <= event.start:
            continue
        lines.append(f"Dialogue: 0,{ass_timestamp(event.start)},{ass_timestamp(event.end)},Default,,0,0,0,,{escape_ass_text(event.text)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
