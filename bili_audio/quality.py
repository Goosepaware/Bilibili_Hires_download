"""音源质量检测：远程采样 + 分段频谱 + 1~100 评分 + 分段评语。

不需要把整首歌下载下来：直接用 ffmpeg 通过 HTTP Range 读取若干时间点的音频，
每段做 FFT 得到有效带宽/截断陡峭度/峰值/削波，再综合打分。
"""
from __future__ import annotations

import math
import re
import statistics
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .analyze import N, _cutoff, _fft, _smooth
from .config import REFERER, UA, find_ffmpeg, hidden_kwargs
from .models import Track
from .remux import LOSSLESS, probe

HEADERS = {"Referer": REFERER, "User-Agent": UA}


def _ffmpeg_headers(headers: dict[str, str] | None) -> list[str]:
    if not headers:
        return []
    raw = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    return ["-headers", raw]


def decode_mono(src: str, start: float, dur: float, sr: int, headers: dict[str, str] | None = None) -> list[int]:
    import array

    ff = find_ffmpeg()
    cmd = [ff, "-v", "error", *_ffmpeg_headers(headers), "-ss", f"{max(0.0, start):.3f}",
           "-t", f"{dur:.3f}", "-i", src, "-map", "0:a:0", "-ac", "1", "-ar", str(sr),
           "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    p = subprocess.run(cmd, capture_output=True, **hidden_kwargs())
    a = array.array("h")
    a.frombytes(p.stdout[: len(p.stdout) // 2 * 2])
    return a.tolist()


def probe_remote(src: str, headers: dict[str, str] | None = None) -> dict:
    ff = find_ffmpeg()
    p = subprocess.run(
        [ff, "-hide_banner", *_ffmpeg_headers(headers), "-i", src],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **hidden_kwargs(),
    )
    text = p.stderr
    info = {"codec": None, "sample_rate": None, "channels": None, "bit_depth": None,
            "bitrate_kbps": None, "duration": None}
    m = re.search(r"Stream #\d+:\d+.*?Audio:\s*([A-Za-z0-9_]+)", text)
    if m:
        info["codec"] = m.group(1).lower()
    m = re.search(r"(\d+)\s*Hz", text)
    if m:
        info["sample_rate"] = int(m.group(1))
    m = re.search(r"\bHz,\s*([^,]+),", text)
    if m:
        info["channels"] = m.group(1).strip()
    m = re.search(r"\((\d+)\s*bit\)", text)
    if m:
        info["bit_depth"] = int(m.group(1))
    m = re.search(r"(\d+)\s*kb/s", text)
    if m:
        info["bitrate_kbps"] = int(m.group(1))
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    if not info["codec"]:
        raise RuntimeError(f"无法读取音频流: {text[-300:]}")
    return info


@dataclass
class Segment:
    index: int
    start: float
    end: float
    cutoff_hz: float = 0.0
    ratio: float = 0.0
    steepness: float = 0.0
    peak_db: float = -120.0
    clipped: float = 0.0
    valid: bool = True


@dataclass
class TrackQuality:
    track: Track
    codec: str
    sample_rate: int
    bit_depth: int | None
    channels: str | None
    duration: float
    cutoff_hz: float
    ratio: float
    steepness: float
    score: int
    grade: str
    summary: str
    segments: list[Segment] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "track_id": self.track.id,
            "kind": self.track.kind,
            "label": self.track.label,
            "codec": self.codec,
            "sample_rate": self.sample_rate,
            "bit_depth": self.bit_depth,
            "channels": self.channels,
            "duration": self.duration,
            "cutoff_hz": round(self.cutoff_hz),
            "ratio": round(self.ratio, 3),
            "steepness": round(self.steepness, 2),
            "score": self.score,
            "grade": self.grade,
            "summary": self.summary,
            "segments": [
                {
                    "start": round(s.start, 1),
                    "end": round(s.end, 1),
                    "cutoff_hz": round(s.cutoff_hz),
                    "ratio": round(s.ratio, 3),
                    "steepness": round(s.steepness, 2),
                    "peak_db": round(s.peak_db, 1),
                    "clipped": round(s.clipped, 6),
                    "valid": s.valid,
                }
                for s in self.segments
            ],
            "descriptions": self.descriptions,
        }


@dataclass
class QualityReport:
    video_title: str
    bvid: str
    owner: str
    duration: float
    tracks: list[TrackQuality]
    score: int
    grade: str
    summary: str

    def to_dict(self) -> dict:
        return {
            "bvid": self.bvid,
            "title": self.video_title,
            "owner": self.owner,
            "duration": self.duration,
            "score": self.score,
            "grade": self.grade,
            "summary": self.summary,
            "tracks": [t.to_dict() for t in self.tracks],
        }


def _mmss(t: float) -> str:
    t = int(max(0, t))
    return f"{t // 60:02d}:{t % 60:02d}"


GRADE = [
    (90, "优秀 · 真无损"),
    (80, "良好 · 基本无损"),
    (65, "一般 · 带宽受限"),
    (45, "存疑 · 疑似有损/升频"),
    (0, "差 · 假无损或有损"),
]


def _grade(score: int) -> str:
    for th, name in GRADE:
        if score >= th:
            return name
    return "差"


def _seg_verdict(s: Segment, nyquist: int) -> str:
    if not s.valid:
        return "该段几乎无音频内容（静音或读取失败）"
    if s.cutoff_hz >= 22000:
        return "高频完整，达到 Hi-Res 水平"
    if s.cutoff_hz >= 20000:
        return "高频基本完整，接近 CD/44.1kHz 上限"
    if s.cutoff_hz >= 18000:
        return "高频偏窄，疑似有损来源或升频"
    if s.cutoff_hz >= 15000:
        return "高频明显缺失，大概率来自有损音源"
    return "高频严重缺失，可以确定是有损音源"


def _segments_for(duration: float, count: int) -> list[tuple[float, float]]:
    if duration <= 0:
        return [(0.0, 0.0)]
    count = max(1, min(count, int(duration // 5) or 1))
    seg_len = duration / count
    out = []
    for i in range(count):
        start = i * seg_len
        out.append((start, start + seg_len))
    return out


def _analyse_segment(src: str, sr: int, start: float, end: float, index: int,
                     headers: dict[str, str] | None) -> Segment:
    nyquist = sr / 2
    seg = Segment(index=index, start=start, end=end)
    span = max(0.0, end - start)
    # 段内取三个位置，选最响的一段，避免前奏/尾奏淡出导致误判
    offsets = [0.15, 0.5, 0.85] if span > 3 else [0.5]
    best = None
    try:
        for frac in offsets:
            at = start + span * frac
            samples = decode_mono(src, at, N / sr + 0.15, sr, headers)
            if len(samples) < N // 6:
                continue
            samples = samples[:N] + [0] * max(0, N - len(samples))
            peak = max(abs(v) for v in samples) / 32768.0
            clipped = sum(1 for v in samples if abs(v) >= 32760) / len(samples)
            win = [samples[i] * (0.5 - 0.5 * math.cos(2 * math.pi * i / (N - 1))) for i in range(N)]
            buf = [complex(v, 0.0) for v in win]
            _fft(buf)
            power = [10 * math.log10((buf[i].real ** 2 + buf[i].imag ** 2) * 4.0 / (N * N) + 1e-12)
                     for i in range(N // 2)]
            cutoff, steep = _cutoff(power, int(nyquist))
            cand = (peak, cutoff, steep, clipped)
            if best is None or peak > best[0]:
                best = cand
        if best is None:
            seg.valid = False
            return seg
        peak, cutoff, steep, clipped = best
        seg.peak_db = 20 * math.log10(peak) if peak > 0 else -120.0
        seg.clipped = clipped
        seg.cutoff_hz = cutoff
        seg.ratio = cutoff / nyquist if nyquist else 0
        seg.steepness = steep
        # 太安静的段落不足以判断带宽
        if seg.peak_db < -45:
            seg.valid = False
    except Exception:
        seg.valid = False
    return seg


def _lossless_score(segments: list[Segment], info: dict, nyquist: float) -> tuple[int, str]:
    score = 100.0
    valid = [s for s in segments if s.valid]
    if not valid:
        return 5, "拿不到有效的音频段，无法判断"

    cutoff = statistics.median(s.cutoff_hz for s in valid)
    steep = statistics.median(s.steepness for s in valid)
    clip = max(s.clipped for s in valid)

    if cutoff >= 22000:
        pass
    else:
        score -= min(62.0, (22000 - cutoff) / 6000.0 * 55.0)
        if cutoff < 16000:
            score -= (16000 - cutoff) / 1000.0 * 4.0

    if steep >= 0.85:
        score -= 18
    elif steep >= 0.7:
        score -= 8

    if info.get("bit_depth") and info["bit_depth"] <= 16:
        score -= 3
    if info.get("sample_rate", 0) >= 96000 and cutoff >= 30000:
        score += 3

    if clip > 0.001:
        score -= 10
    elif clip > 0.00005:
        score -= 5

    cvals = [s.cutoff_hz for s in valid]
    if len(cvals) >= 3 and max(cvals) - min(cvals) > 8000:
        score -= 6
    low = sorted(cvals)[max(0, len(cvals) // 5)] if cvals else 22000
    if low < 16000:
        score -= 5

    score = int(max(1, min(100, round(score))))
    return score, _grade(score)


def _lossy_score(track: Track) -> tuple[int, str]:
    if track.kind == "dolby":
        return 52, "杜比全景声：有损编码，但码率高、环绕声，观影优先"
    kbps = track.kbps
    if kbps >= 180:
        return 42, f"AAC {kbps}K：有损编码，日常够用，但不能称为无损"
    if kbps >= 120:
        return 34, f"AAC {kbps}K：有损编码，高频和细节有损失"
    return 24, f"AAC {kbps}K：有损编码，音质较差"


def analyse_track(track: Track, duration: float, segments: int = 8, workers: int = 4) -> TrackQuality:
    src = track.urls[0]
    info = probe_remote(src, HEADERS)
    sr = info.get("sample_rate") or 44100
    nyquist = sr / 2
    is_lossless = (info.get("codec") in LOSSLESS) or (track.kind == "flac")

    if not is_lossless:
        score, grade = _lossy_score(track)
        return TrackQuality(
            track=track, codec=info.get("codec") or track.codec,
            sample_rate=sr, bit_depth=info.get("bit_depth"),
            channels=info.get("channels"), duration=duration,
            cutoff_hz=0, ratio=0, steepness=0,
            score=score, grade=grade,
            summary=grade,
            descriptions=[],
        )

    # 长视频自动增加分段，保证每段不超过约 3 分钟
    if duration > 0:
        segments = max(segments, min(12, int(duration // 180)))

    spans = _segments_for(duration, segments)
    segs: list[Segment] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(spans)))) as ex:
        futures = [ex.submit(_analyse_segment, src, sr, a, b, i, HEADERS)
                   for i, (a, b) in enumerate(spans)]
        segs = [f.result() for f in futures]

    score, grade = _lossless_score(segs, info, nyquist)
    valid = [s for s in segs if s.valid]
    med_cut = statistics.median(s.cutoff_hz for s in valid) if valid else 0
    med_steep = statistics.median(s.steepness for s in valid) if valid else 0

    if med_cut >= 22000:
        head = "高频完整，达到 Hi-Res 水平"
    elif med_cut >= 20000:
        head = "高频基本完整，接近 44.1kHz 上限"
    elif med_cut >= 18000:
        head = "高频偏窄，疑似有损来源或升频"
    else:
        head = "高频明显缺失，大概率来自有损音源"
    summary = f"{head}（中位有效带宽 {med_cut / 1000:.1f} kHz，陡峭度 {med_steep:.2f}）"

    descriptions = []
    for s in segs:
        if not s.valid:
            descriptions.append(f"{_mmss(s.start)}–{_mmss(s.end)}  该段几乎无声，未参与判断。")
            continue
        extra = []
        if s.steepness >= 0.8:
            extra.append("存在硬截断")
        if s.clipped > 0.001:
            extra.append("出现削波")
        elif s.clipped > 0.00005:
            extra.append("个别采样接近满刻度")
        tail = ("；" + "，".join(extra)) if extra else ""
        descriptions.append(
            f"{_mmss(s.start)}–{_mmss(s.end)}  有效带宽 {s.cutoff_hz / 1000:.1f} kHz"
            f"（Nyquist 的 {s.ratio * 100:.0f}%），陡峭度 {s.steepness:.2f}{tail}。"
            f"{_seg_verdict(s, int(nyquist))}。"
        )

    return TrackQuality(
        track=track, codec=info.get("codec") or track.codec,
        sample_rate=sr, bit_depth=info.get("bit_depth"),
        channels=info.get("channels"), duration=duration,
        cutoff_hz=med_cut, ratio=med_cut / nyquist if nyquist else 0,
        steepness=med_steep, score=score, grade=grade,
        summary=summary, segments=segs, descriptions=descriptions,
    )


def inspect_url(session, url: str, *, segments: int = 8, workers: int = 4) -> QualityReport:
    from . import api
    from .pipeline import Options, select_tracks

    vid = api.resolve_url(session, url)
    video = api.get_video(session, vid)
    page = video.pages[0]
    dash = (api.get_playurl(session, video.bvid, page.cid).get("dash") or {})
    tracks = select_tracks(dash, Options())

    picked: list[Track] = []
    flac = next((t for t in tracks if t.kind == "flac"), None)
    dolby = next((t for t in tracks if t.kind == "dolby"), None)
    aac = next((t for t in tracks if t.kind == "aac"), None)
    if flac:
        picked.append(flac)
    if dolby:
        picked.append(dolby)
    if aac:
        picked.append(aac)
    if not picked:
        raise RuntimeError("这个视频没有可取用的音轨")

    qualities: list[TrackQuality] = []
    for t in picked:
        try:
            qualities.append(analyse_track(t, float(page.duration or 0), segments, workers))
        except Exception as e:
            qualities.append(
                TrackQuality(track=t, codec=t.codec, sample_rate=0, bit_depth=None, channels=None,
                             duration=float(page.duration or 0), cutoff_hz=0, ratio=0, steepness=0,
                             score=0, grade="无法检测", summary=str(e))
            )

    flac_q = next((q for q in qualities if q.track.kind == "flac"), None)
    best = flac_q if (flac_q and flac_q.score > 0) else max(qualities, key=lambda q: q.score)
    score = best.score
    grade = best.grade

    if flac:
        c = best.cutoff_hz
        if c >= 30000:
            summary = "Hi-Res 母带级带宽，高频完整，可以放心收藏。"
        elif c >= 22000:
            summary = "带宽达到 Hi-Res 水平（22kHz 以上），可作为无损收藏。"
        elif c >= 20000:
            summary = "有效带宽约 CD 级（20~22kHz），是无损封装，但不算真正的 Hi-Res。"
        elif c >= 18000:
            summary = "高频偏窄（18~20kHz），疑似有损来源或升频，建议试听后决定是否收藏。"
        else:
            summary = "高频明显缺失（低于 18kHz），基本可以判定为有损转码或升频，不建议当无损收藏。"
    else:
        summary = "该视频没有 Hi-Res 无损音轨，最高只有 AAC；以下评分按有损音轨给出。"

    if len(picked) > 1:
        summary += f" 已同时分析 {len(picked)} 条音轨。"

    return QualityReport(
        video_title=video.title, bvid=video.bvid, owner=video.owner,
        duration=float(page.duration or 0), tracks=qualities,
        score=score, grade=grade, summary=summary,
    )


def format_report(r: QualityReport) -> str:
    lines = [
        f"■ {r.video_title}",
        f"  {r.bvid} · {r.owner} · {_mmss(r.duration)}",
        "",
        f"综合评分: {r.score}/100  [{r.grade}]",
        f"{r.summary}",
    ]
    for tq in r.tracks:
        lines += ["", "─" * 46, f"{tq.track.label}  ({tq.track.kbps} kb/s)"]
        if tq.segments:
            lines.append(f"得分 {tq.score}/100 · {tq.grade}")
            lines.append(f"{tq.codec} · {tq.sample_rate} Hz · "
                         f"{tq.bit_depth or '-'}-bit · {tq.channels or '-'}")
            lines.append("")
            lines.extend(tq.descriptions)
        else:
            lines.append(tq.summary)
    return "\n".join(lines)
