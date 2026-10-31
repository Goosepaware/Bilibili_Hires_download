"""整轨分轨：静音检测、波形峰值、按时间轴导出（含标签与序号）。"""
from __future__ import annotations

import array
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import find_ffmpeg, hidden_kwargs

ANALYSIS_SR = 2000


@dataclass
class Segment:
    index: int
    start: float
    end: float
    title: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def _headers_args(headers: dict[str, str] | None) -> list[str]:
    if not headers:
        return []
    raw = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    return ["-headers", raw]


def decode_analysis_pcm(source: str, headers: dict[str, str] | None = None) -> array.array:
    """解码成 2kHz 单声道，用于波形和静音检测。"""
    ff = find_ffmpeg()
    p = subprocess.run(
        [ff, "-v", "error", *_headers_args(headers), "-i", source,
         "-map", "0:a:0", "-ac", "1", "-ar", str(ANALYSIS_SR),
         "-f", "s16le", "-acodec", "pcm_s16le", "-"],
        capture_output=True,
        **hidden_kwargs(),
    )
    if not p.stdout:
        raise RuntimeError(f"解码失败: {p.stderr.decode('utf-8', 'replace')[-300:]}")
    data = array.array("h")
    data.frombytes(p.stdout[: len(p.stdout) // 2 * 2])
    return data


def waveform_peaks(samples: array.array, buckets: int = 2000) -> list[tuple[float, float]]:
    n = len(samples)
    if n == 0:
        return []
    buckets = max(1, min(buckets, n))
    size = max(1, n // buckets)
    peaks: list[tuple[float, float]] = []
    for i in range(buckets):
        a = i * size
        b = n if i == buckets - 1 else min(n, a + size)
        if a >= b:
            peaks.append((0.0, 0.0))
            continue
        seg = samples[a:b]
        peaks.append((min(seg) / 32768.0, max(seg) / 32768.0))
    return peaks


def detect_silence(
    samples: array.array,
    *,
    threshold_db: float = -35.0,
    min_silence: float = 1.5,
    window: float = 0.05,
) -> list[tuple[float, float]]:
    """返回静音区间 [(start, end), ...]（秒）。"""
    sr = ANALYSIS_SR
    win = max(1, int(sr * window))
    thr = 32768.0 * (10 ** (threshold_db / 20.0))
    nwin = len(samples) // win
    flags = []
    for i in range(nwin):
        seg = samples[i * win:(i + 1) * win]
        rms = math.sqrt(sum(v * v for v in seg) / max(1, len(seg)))
        flags.append(rms < thr)

    gaps: list[tuple[float, float]] = []
    i = 0
    while i < nwin:
        if flags[i]:
            j = i
            while j < nwin and flags[j]:
                j += 1
            dur = (j - i) * window
            if dur >= min_silence:
                gaps.append((i * window, j * window))
            i = j
        else:
            i += 1
    return gaps


def splits_from_gaps(gaps: list[tuple[float, float]], duration: float, min_gap: float = 10.0) -> list[float]:
    """把静音区间换成切点（取中点），并合并过近的切点。"""
    pts = []
    for a, b in gaps:
        mid = (a + b) / 2
        if 3.0 < mid < duration - 3.0:
            pts.append((mid, b - a))
    pts.sort()
    merged: list[tuple[float, float]] = []
    for p, w in pts:
        if merged and p - merged[-1][0] < min_gap:
            if w > merged[-1][1]:
                merged[-1] = (p, w)
        else:
            merged.append((p, w))
    return [p for p, _ in merged]


def segments_from_points(points: list[float], duration: float, titles: dict[int, str] | None = None) -> list[Segment]:
    points = sorted(p for p in points if 0 < p < duration)
    starts = [0.0] + points
    ends = points + [duration]
    out = []
    for i, (a, b) in enumerate(zip(starts, ends), 1):
        if b - a < 0.5:
            continue
        out.append(Segment(index=i, start=a, end=b, title=(titles or {}).get(i, "")))
    return out


def segments_from_tracks(tracks, duration: float) -> list[Segment]:
    segs = []
    for i, t in enumerate(tracks):
        end = tracks[i + 1].start if i + 1 < len(tracks) else duration
        if end - t.start < 0.5:
            continue
        segs.append(Segment(index=t.index, start=t.start, end=end, title=t.title))
    return segs


_SAFE = re.compile(r'[\\/:*?"<>|\r\n\t]')


def safe_name(name: str, limit: int = 100) -> str:
    name = _SAFE.sub("_", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:limit] or "untitled"


def export_segments(
    source: str,
    segments: list[Segment],
    out_dir: str | Path,
    *,
    album: str,
    artist: str = "",
    ext: str = "flac",
    fmt: str = "source",
    preset: str = "medium",
    cover: bytes | None = None,
    headers: dict[str, str] | None = None,
    titles: bool = True,
    progress: Callable[[int, int, str], None] | None = None,
    refresh: Callable[[], str] | None = None,
) -> list[Path]:
    """逐个导出分轨。

    fmt="source" 时跟随源：FLAC 无损重编码，AAC/E-AC3 直接 copy；
    否则按 formats 里的目标格式与预设转码。
    """
    from . import formats

    ff = find_ffmpeg()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    total = len(segments)
    results: list[Path] = []
    cur_source = source
    if fmt != "source":
        ext = formats.get_format(fmt).ext.lstrip(".")

    for i, seg in enumerate(segments, 1):
        label = safe_name(seg.title) if (titles and seg.title) else ""
        name = f"{seg.index:02d} {label}.{ext}" if label else f"{seg.index:02d}.{ext}"
        dest = out_dir / name
        args = [ff, "-y", "-hide_banner", "-v", "error",
                "-ss", f"{seg.start:.3f}", "-t", f"{seg.duration:.3f}",
                *_headers_args(headers), "-i", cur_source,
                "-map", "0:a:0", "-map_metadata", "-1", "-vn", "-sn", "-dn"]
        if fmt != "source":
            args += formats.ffmpeg_args(fmt, preset)
        elif ext == "flac":
            args += ["-c:a", "flac", "-compression_level", "8", "-f", "flac"]
        elif ext == "m4a":
            args += ["-c:a", "copy", "-movflags", "+faststart", "-f", "mp4"]
        elif ext == "eac3":
            args += ["-c:a", "copy", "-f", "eac3"]
        else:
            args += ["-c:a", "copy"]
        args.append(str(dest))

        ok = False
        p = None
        for attempt in range(2):
            p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", **hidden_kwargs())
            if p.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                ok = True
                break
            if attempt == 0 and refresh:
                try:
                    cur_source = refresh()
                    args[args.index("-i") + 1] = cur_source
                except Exception:
                    pass
        if not ok:
            raise RuntimeError(f"导出第 {seg.index} 轨失败: {p.stderr[-300:] if p else ''}")

        formats.tag_audio(
            dest,
            title=seg.title or f"Track {seg.index:02d}",
            artist=artist,
            album=album,
            track=seg.index,
            total=total,
            cover=cover,
        )
        results.append(dest)
        if progress:
            progress(i, total, dest.name)
    return results
