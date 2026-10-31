"""用 ffmpeg 做封装转换与真实参数读取（不依赖 ffprobe）。"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import find_ffmpeg, hidden_kwargs

_DUR = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_CODEC = re.compile(r"Stream #\d+:\d+.*?Audio:\s*([A-Za-z0-9_]+)")
_SR = re.compile(r"(\d+)\s*Hz")
_LAYOUT = re.compile(r"\bHz,\s*([^,]+),")
_BITS = re.compile(r"\((\d+)\s*bit\)")
_BR = re.compile(r"(\d+)\s*kb/s")
_SFMT = re.compile(r",\s*(s16|s32|s64|flt|dbl|u8|fltp|dblp)(?:p)?\b")

CODEC_ALIASES = {
    "flac": "flac",
    "alac": "alac",
    "aac": "aac",
    "mp3": "mp3",
    "eac3": "eac3",
    "ac3": "ac3",
    "opus": "opus",
}

LOSSLESS = {"flac", "alac", "wav", "ape", "wavpack"}


def probe(path: str | Path) -> dict:
    ff = find_ffmpeg()
    p = subprocess.run(
        [ff, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **hidden_kwargs(),
    )
    text = p.stderr
    mline = re.search(r"Stream #\d+:\d+.*?Audio:.*", text)
    audio_line = mline.group(0) if mline else text
    info: dict = {
        "codec": None,
        "sample_rate": None,
        "channels": None,
        "bit_depth": None,
        "bitrate_kbps": None,
        "duration": None,
    }
    m = _CODEC.search(audio_line)
    if m:
        raw = m.group(1).lower()
        info["codec"] = CODEC_ALIASES.get(raw, raw)
    m = _SR.search(audio_line)
    if m:
        info["sample_rate"] = int(m.group(1))
    m = _LAYOUT.search(audio_line)
    if m:
        info["channels"] = m.group(1).strip()
    m = _BITS.search(audio_line)
    if m:
        info["bit_depth"] = int(m.group(1))
    else:
        m = _SFMT.search(audio_line)
        if m:
            info["bit_depth"] = {"s16": 16, "s32": 32, "s64": 64, "flt": 32, "fltp": 32, "dbl": 64, "dblp": 64, "u8": 8}.get(m.group(1))
    m = _BR.search(audio_line)
    if m:
        info["bitrate_kbps"] = int(m.group(1))
    m = _DUR.search(text)
    if m:
        info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    if info["bitrate_kbps"] is None and info["duration"]:
        try:
            info["bitrate_kbps"] = int(Path(path).stat().st_size * 8 / info["duration"] / 1000)
        except Exception:
            pass
    if info["codec"] is None:
        raise RuntimeError(f"无法识别音频流: {text[-400:]}")
    return info


def remux(src: str | Path, dest: str | Path, *, ext: str | None = None) -> Path:
    """只换封装不重新编码（-c:a copy）。"""
    ff = find_ffmpeg()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = [
        ff, "-y", "-hide_banner", "-v", "error",
        "-i", str(src),
        "-map", "0:a:0",
        "-c:a", "copy",
        "-vn", "-sn", "-dn",
    ]
    fmt = {
        "flac": "flac",
        "eac3": "eac3",
        "m4a": "mp4",
    }.get((ext or dest.suffix.lstrip(".")).lower())
    if fmt:
        args += ["-f", fmt]
    args.append(str(dest))
    p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", **hidden_kwargs())
    if p.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(f"转封装失败: {p.stderr[-500:]}")
    return dest
