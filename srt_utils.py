"""SRT parsing/writing, filename-timestamp parsing, and translation insertion.

The timestamp conversion + SRT regex primitives are adapted from the QwenASR
subtitle_editor.py; the filename-timestamp parser and the insertion logic
(both modes) are new.
"""
from __future__ import annotations

import re
from pathlib import Path

_TS_RE = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")
_RANGE_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})"
)
# final "_HHMMSS.mmm" suffix in the file stem (before extension)
_FN_TS_RE = re.compile(r"_(\d{6})\.(\d{3})(?=[^.]*$)")


def ts_to_sec(ts: str) -> float:
    """'HH:MM:SS,mmm' or 'HH:MM:SS.mmm' → seconds (float)."""
    m = _TS_RE.search(ts)
    if not m:
        raise ValueError(f"bad timestamp: {ts!r}")
    h, mi, s, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000.0


def sec_to_ts(sec: float) -> str:
    """seconds → 'HH:MM:SS,mmm' (SRT style with a comma)."""
    sec = max(0.0, float(sec))
    ms_total = int(round(sec * 1000))
    h = ms_total // 3_600_000
    ms_total %= 3_600_000
    mi = ms_total // 60_000
    ms_total %= 60_000
    s = ms_total // 1_000
    ms = ms_total % 1_000
    return f"{h:02d}:{mi:02d}:{s:02d},{ms:03d}"


def parse_filename_timestamp(path: str | Path) -> float | None:
    """Extract the trailing _HHMMSS.mmm timestamp from a screenshot filename.

    'gachig163-…mp4_000213.789.jpg' → 00:02:13.789 → 133.789 seconds.
    Returns None if the stem has no such suffix.
    """
    stem = Path(path).stem  # filename without extension
    matches = list(_FN_TS_RE.finditer(stem))
    if not matches:
        return None
    digits, millis = matches[-1].group(1), matches[-1].group(2)
    hh = int(digits[0:2])
    mm = int(digits[2:4])
    ss = int(digits[4:6])
    return hh * 3600 + mm * 60 + ss + int(millis) / 1000.0


def parse_srt(path: str | Path) -> list[dict]:
    """Parse an SRT file into a list of {'start':float,'end':float,'text':str}.

    Tolerates a missing index line per block. Multi-line text is joined with
    newlines preserved.
    """
    text = Path(path).read_text(encoding="utf-8-sig")
    blocks = re.split(r"\n\s*\n", text.strip())
    out: list[dict] = []
    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue
        # find the line that carries the 'start --> end' range
        range_idx = None
        for i, ln in enumerate(lines):
            if _RANGE_RE.search(ln):
                range_idx = i
                break
        if range_idx is None:
            continue
        m = _RANGE_RE.search(lines[range_idx])
        start = ts_to_sec(m.group(1))
        end = ts_to_sec(m.group(2))
        # text = everything after the range line
        body = lines[range_idx + 1:]
        content = "\n".join(l.rstrip() for l in body).strip()
        out.append({"start": start, "end": end, "text": content})
    return out


def write_srt(path: str | Path, entries: list[dict]) -> None:
    """Write entries (sorted by start, re-indexed 1-based) to an SRT file."""
    entries = sorted(entries, key=lambda e: e["start"])
    with open(path, "w", encoding="utf-8") as f:
        for i, e in enumerate(entries, 1):
            text = (e.get("text") or "").strip()
            f.write(
                f"{i}\n{sec_to_ts(e['start'])} --> {sec_to_ts(e['end'])}\n{text}\n\n"
            )


def insert_translations(
    entries: list[dict],
    translations: list[tuple[float, str]] | list[tuple[float, str, float]],
    mode: str = "insert",
    duration_mode: str = "auto",
    duration: float = 2.0,
) -> list[dict]:
    """Return a new entry list with translated text spliced in.

    translations: list of (timestamp_seconds, translated_text) or
                  (timestamp_seconds, translated_text, duration_seconds).
    mode: 'insert'  → each becomes a new entry: start=ts, end is determined
                       by duration_mode.
          'append' → append the text to the existing entry whose [start,end)
                     contains ts; if ts falls in a gap, fall back to insert.

    duration_mode:
      'auto'         → end = next entry's start (or ts+2.0 if none).
      'unified'      → end = ts + duration (all subtitles use the same value).
      'per_subtitle' → end = ts + the 3rd element of each translation tuple.

    Empty/whitespace-only translations are skipped. Result is unsorted;
    call write_srt() to sort + re-index, or sort here so the caller can count.
    """
    # work on copies
    result = [dict(e) for e in entries]

    # normalise translations: filter empties, sort, extract optional dur
    raw: list[tuple[float, str, float | None]] = []
    for t in translations:
        txt = t[1].strip() if len(t) > 1 and t[1] else ""
        if not txt:
            continue
        ts = t[0]
        dur = float(t[2]) if len(t) > 2 and t[2] is not None else None
        raw.append((ts, txt, dur))
    raw.sort(key=lambda x: x[0])

    if mode == "append":
        for ts, txt, dur in raw:
            host = None
            for e in result:
                if e["start"] <= ts < e["end"]:
                    host = e
                    break
            if host is not None:
                host["text"] = (host["text"] + "\n" + txt).strip()
            else:
                # gap → fall back to inserting a new entry
                result.append(_make_new_entry(ts, txt, result, dur=dur))
    else:  # insert
        if duration_mode == "unified":
            for ts, txt, _dur in raw:
                result.append({"start": ts, "end": ts + duration, "text": txt})
        elif duration_mode == "per_subtitle":
            for ts, txt, dur in raw:
                dur = dur if dur is not None and dur > 0 else 2.0
                result.append({"start": ts, "end": ts + dur, "text": txt})
        else:  # auto
            for ts, txt, dur in raw:
                result.append(_make_new_entry(ts, txt, result, dur=dur))

    # defensive: never end before start
    for e in result:
        if e["end"] <= e["start"]:
            e["end"] = e["start"] + 0.5

    result.sort(key=lambda e: e["start"])
    return result


def _make_new_entry(ts: float, txt: str, all_entries: list[dict], dur: float | None = None) -> dict:
    """Build a new entry whose end is ts+dur (if provided) or next later start, else ts+2.0."""
    if dur is not None and dur > 0:
        end = ts + dur
    else:
        later_starts = [e["start"] for e in all_entries if e["start"] > ts + 1e-6]
        end = min(later_starts) if later_starts else ts + 2.0
    return {"start": ts, "end": end, "text": txt}
