"""真实音质核验：容器参数 + FFT 频谱带宽，识别假无损 / 升频 / 有损转码。

不依赖 numpy/scipy：用 ffmpeg 解出原始 PCM，再用纯 Python 的 radix-2 FFT 分析。
"""
from __future__ import annotations

import array
import math
import subprocess
from pathlib import Path

from .config import find_ffmpeg, hidden_kwargs
from .remux import LOSSLESS, probe

RATIO_OK = 0.90        # 有效带宽 >= 90% Nyquist => 带宽充足
RATIO_LIMIT = 0.72     # 低于 72% => 基本可判定存在明显截断
RELDROP_DB = 55.0      # 相对峰值的动态范围阈值

VERDICT_TEXT = {
    "LOSSLESS": "无损（带宽充足，未见明显截断）",
    "LIMITED": "无损封装，但高频带宽受限（源头可能是 44.1kHz 或经过降频处理）",
    "UPSAMPLED": "无损封装，但高频有硬截断：源头很可能是 44.1kHz 有损音源或被升频，不是真正的 Hi-Res",
    "SUSPECT": "疑似假无损：有效带宽明显低于当前采样率，高频被截断，很可能来自有损音源或升频",
    "TRANSCODE": "疑似假无损：高频存在明显截断，源头很可能是有损编码",
    "LOSSY_CODEC": "非无损编码（AAC / EC-3 等），不能称为无损",
}

COMMON_RATES = [8000, 11025, 16000, 22050, 32000, 44100, 48000, 88200, 96000, 176400, 192000]
N = 1 << 15


def _fft(a: list[complex]) -> None:
    n = len(a)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    length = 2
    while length <= n:
        ang = -2.0 * math.pi / length
        wl = complex(math.cos(ang), math.sin(ang))
        half = length >> 1
        for i in range(0, n, length):
            w = 1.0 + 0j
            for k in range(i, i + half):
                u = a[k]
                v = a[k + half] * w
                a[k] = u + v
                a[k + half] = u - v
                w *= wl
        length <<= 1


def _decode_mono(path: Path, ff: str, start: float, dur: float, sr: int) -> list[int]:
    p = subprocess.run(
        [
            ff, "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-t", f"{dur:.3f}",
            "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(sr),
            "-f", "s16le", "-acodec", "pcm_s16le", "-",
        ],
        capture_output=True,
        **hidden_kwargs(),
    )
    raw = p.stdout
    a = array.array("h")
    a.frombytes(raw[: len(raw) // 2 * 2])
    return a.tolist()


def _spectrum(path: Path, ff: str, sr: int, duration: float) -> list[float]:
    """返回每个频点的平均功率(dB)，长度 N//2。取多个时间窗口，逐频点取最大值。"""
    if duration and duration > 10:
        starts = [duration * f for f in (0.1, 0.3, 0.5, 0.7, 0.9)]
    else:
        starts = [0.0]
    best: list[float] | None = None
    for st in starts:
        samples = _decode_mono(path, ff, st, N / sr + 0.2, sr)
        if len(samples) < N // 4:
            continue
        samples = samples[:N] + [0] * max(0, N - len(samples))
        win = [samples[i] * (0.5 - 0.5 * math.cos(2 * math.pi * i / (N - 1))) for i in range(N)]
        buf = [complex(v, 0.0) for v in win]
        _fft(buf)
        cur = [buf[i].real ** 2 + buf[i].imag ** 2 for i in range(N // 2)]
        if best is None:
            best = cur
        else:
            for i, v in enumerate(cur):
                if v > best[i]:
                    best[i] = v
    if best is None:
        raise RuntimeError("无法解码音频用于频谱分析")
    return [10.0 * math.log10(v * 4.0 / (N * N) + 1e-12) for v in best]


def _smooth(vals: list[float], k: int = 15) -> list[float]:
    if k <= 1:
        return vals
    half = k // 2
    out = []
    for i in range(len(vals)):
        lo = max(0, i - half)
        hi = min(len(vals), i + half + 1)
        seg = vals[lo:hi]
        m = sum(seg) / len(seg)
        out.append(m)
    return out


def _cutoff(power_db: list[float], nyquist: int) -> tuple[float, float]:
    """从 Nyquist 往下找第一个仍有能量的频点；返回 (带宽Hz, 截断陡峭度 0~1)。"""
    sm = _smooth(power_db, 15)
    peak = max(sm)
    thr = peak - RELDROP_DB
    bins = len(sm)
    hz_per_bin = nyquist / bins
    idx = bins - 1
    while idx > 0 and sm[idx] < thr:
        idx -= 1
    cutoff = idx * hz_per_bin

    # 陡峭度：截断点下方(低频侧)与上方(高频侧)的能量差，单位 dB
    span = max(4, int(2000 / hz_per_bin))
    pb = sm[max(0, idx - 3 * span): max(1, idx - 2)]
    sb = sm[idx + 2: min(bins, idx + 2 + span)]
    if not pb:
        return cutoff, 0.0
    drop = sum(pb) / len(pb) - (sum(sb) / len(sb) if sb else sum(pb) / len(pb))
    return cutoff, min(1.0, max(0.0, drop / 40.0))


def _nearest_rate(hz: float) -> int | None:
    if hz <= 0:
        return None
    rate = int(round(hz * 2 / 1000.0)) * 1000
    return min(COMMON_RATES, key=lambda r: abs(r - rate))


def _spectrogram_png(path: Path, ff: str, start: float, dur: float, out: Path) -> None:
    subprocess.run(
        [
            ff, "-y", "-v", "error",
            "-ss", f"{max(0.0, start):.3f}", "-t", f"{dur:.3f}", "-i", str(path),
            "-lavfi", "showspectrumpic=s=1200x600:scale=lin:legend=disabled:color=fire",
            "-frames:v", "1", str(out),
        ],
        check=True,
        **hidden_kwargs(),
    )


def analyze(path: str | Path, *, spectrogram: str | Path | None = None, keep_png: bool = True) -> dict:
    path = Path(path)
    info = probe(path)
    nyquist = int((info["sample_rate"] or 44100) / 2)
    result = {
        **info,
        "nyquist": nyquist,
        "is_lossless_codec": info["codec"] in LOSSLESS,
        "cutoff_hz": None,
        "cutoff_ratio": None,
        "estimated_source_rate": None,
        "steepness": None,
        "spectrogram": None,
        "verdict": None,
        "verdict_text": None,
    }
    if info["codec"] not in LOSSLESS:
        result["verdict"] = "LOSSY_CODEC"
        result["verdict_text"] = VERDICT_TEXT["LOSSY_CODEC"]
        return result

    ff = find_ffmpeg()
    try:
        power = _spectrum(path, ff, info["sample_rate"] or 44100, info["duration"] or 0)
        cutoff, steep = _cutoff(power, nyquist)
        ratio = cutoff / nyquist
        result.update(
            cutoff_hz=round(cutoff),
            cutoff_ratio=round(ratio, 3),
            steepness=round(steep, 2),
            estimated_source_rate=_nearest_rate(cutoff),
        )
        est = result["estimated_source_rate"] or 0
        sr = info["sample_rate"] or 0
        if steep >= 0.85:
            if est >= sr * 0.95 or (est >= 44100 and cutoff >= 0.90 * nyquist):
                verdict = "LOSSLESS"
            elif est >= 44100:
                verdict = "UPSAMPLED"
            else:
                verdict = "TRANSCODE"
        elif cutoff >= 0.88 * nyquist:
            verdict = "LOSSLESS"
        elif cutoff >= 22000:
            verdict = "LOSSLESS"
        elif cutoff >= 19000:
            verdict = "LIMITED"
        elif cutoff >= 16000:
            verdict = "SUSPECT"
        else:
            verdict = "TRANSCODE"
        result["verdict"] = verdict
        result["verdict_text"] = VERDICT_TEXT[verdict]

        if keep_png:
            out_png = Path(spectrogram) if spectrogram else path.with_suffix(path.suffix + ".spectrum.png")
            total = info["duration"] or 0
            dur = min(60.0, total) if total else 60.0
            _spectrogram_png(path, ff, max(0.0, total * 0.4), dur, out_png)
            result["spectrogram"] = str(out_png)
    except Exception as e:
        result["verdict"] = "UNKNOWN"
        result["verdict_text"] = f"频谱分析失败: {e}"
    return result


def format_report(a: dict) -> str:
    bits = f"{a.get('bit_depth')}-bit" if a.get("is_lossless_codec") and a.get("bit_depth") else "-"
    lines = [
        f"  编码: {a.get('codec')} | 采样率: {a.get('sample_rate')} Hz | 位深: {bits} | 声道: {a.get('channels')}",
        f"  码率: {a.get('bitrate_kbps')} kb/s | 时长: {(a.get('duration') or 0):.1f}s",
    ]
    if a.get("cutoff_hz"):
        lines.append(
            f"  有效带宽: {a['cutoff_hz'] / 1000:.1f} kHz / Nyquist {a['nyquist'] / 1000:.1f} kHz"
            f" ({a['cutoff_ratio'] * 100:.0f}%) | 截断陡峭度 {a['steepness']}"
        )
        if a.get("estimated_source_rate"):
            lines.append(f"  推测原始采样率: ≥ {a['estimated_source_rate'] / 1000:.1f} kHz")
    lines.append(f"  结论: {a.get('verdict_text')}")
    if a.get("spectrogram"):
        lines.append(f"  频谱图: {a['spectrogram']}")
    return "\n".join(lines)
