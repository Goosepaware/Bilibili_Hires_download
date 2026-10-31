"""从自由文本里识别曲目表（序号 / 歌名 / 时间），用于按时间轴分轨。

支持的常见写法（不要求统一格式）：
    1. Sunset (2019 Y.Nakamura Remastering) 0:00
    02 Syndrome 3:24
    3、I am - 8:17
    04 | Find Your Way | 13:36
    00:00 歌名
    Track 05 歌名 16:45
    1) 歌名 22:52
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_TS = re.compile(r"(?<!\d)(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?!\d)")
_INDEX = re.compile(
    r"^\s*(?:#|no\.?|track\s*)?"
    r"(?:[\[【(（]\s*(\d{1,3})\s*[\]】)）]|(\d{1,3})(?=[.、,，:：\-–—|)\s]))"
    r"\s*(?:[.、,，:：\-–—|)\]}】）]+\s*)?",
    re.I,
)
_URL = re.compile(r"https?://|www\.", re.I)
_BRACKETS = "[]【】()（）<>《》"

_FW = {ord(c): ord("0") + i for i, c in enumerate("０１２３４５６７８９")}
_FW.update({ord("："): ord(":"), ord("．"): ord("."), ord("－"): ord("-"), ord("—"): ord("-"),
            ord("–"): ord("-"), ord("　"): ord(" ")})


@dataclass
class ParsedTrack:
    index: int
    title: str
    start: float

    @property
    def start_text(self) -> str:
        s = int(self.start)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


@dataclass
class ParseResult:
    tracks: list[ParsedTrack]
    warnings: list[str]


def _to_seconds(m: str | None, mm: str, ss: str) -> float:
    return int(m or 0) * 3600 + int(mm) * 60 + int(ss)


def _clean_title(text: str) -> str:
    text = _TS.sub(" ", text)
    text = re.sub(r"[\t\r]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = text.strip()
    # 去掉用于分隔的时间轴符号和空括号
    text = re.sub(r"^[\-–—|:：,，、\s]+", "", text)
    text = re.sub(r"[\-–—|:：,，、\s]+$", "", text)
    text = re.sub(r"[\(（]\s*[\)）]", "", text)
    return text.strip()


def normalize(text: str) -> str:
    return text.translate(_FW)


def parse_tracklist(text: str, duration: float | None = None) -> ParseResult:
    tracks: list[ParsedTrack] = []
    warnings: list[str] = []
    seen_times: set[int] = set()

    for raw in normalize(text).splitlines():
        line = raw.strip()
        if not line or _URL.search(line):
            continue
        matches = list(_TS.finditer(line))
        if not matches:
            continue
        # 一行里有多个时间时，取第一个作为开始时间（常见于 "0:00 - 3:24"）
        first = matches[0]
        start = _to_seconds(first.group(1), first.group(2), first.group(3))
        if start > 24 * 3600:
            continue
        if duration and start >= duration:
            warnings.append(f"跳过超出时长的行：{line[:40]}")
            continue
        key = int(round(start))
        if key in seen_times:
            continue
        seen_times.add(key)

        before = line[: first.start()].strip()
        after = line[first.end():].strip()
        m = _INDEX.match(before)
        idx = int(m.group(1) or m.group(2)) if m else None
        rest = before[m.end():] if m else before
        # 标题可能是「时间在中间」的写法
        title = _clean_title(" ".join(x for x in (rest, after) if x))
        if not title:
            title = "—"
        tracks.append(ParsedTrack(index=idx or (len(tracks) + 1), title=title, start=start))

    if not tracks:
        return ParseResult([], ["没有识别到「歌名 + 时间」的行"])

    # 时间不递增时按时间排序，并保留原序号信息
    times = [t.start for t in tracks]
    if times != sorted(times):
        warnings.append("时间不是递增的，已按时间重新排序")
        tracks.sort(key=lambda t: t.start)

    # 序号重复或缺失时按顺序补全
    if len({t.index for t in tracks}) != len(tracks):
        for i, t in enumerate(tracks, 1):
            t.index = i
        warnings.append("序号有重复，已按时间顺序重新编号")

    if len(tracks) == 1:
        warnings.append("只识别到 1 首，请检查文本格式")

    return ParseResult(tracks, warnings)
