"""输出格式与有损参数预设，以及转码 / 标签。"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import find_ffmpeg, hidden_kwargs


@dataclass
class Preset:
    id: str
    label: str
    desc: str


@dataclass
class FormatSpec:
    id: str
    label: str
    ext: str
    lossless: bool
    presets: list[Preset]

    def preset(self, pid: str | None) -> Preset:
        if not self.presets:
            return Preset("default", "默认", "")
        for p in self.presets:
            if p.id == pid:
                return p
        return self.presets[0]


FORMATS: dict[str, FormatSpec] = {
    "flac": FormatSpec(
        "flac", "FLAC 无损", ".flac", True,
        [Preset("original", "原样", "保持源采样率与位深，转封装不重编码")],
    ),
    "ipod": FormatSpec(
        "ipod", "iPod 专用（ALAC 16bit/44.1k）", ".m4a", True,
        [Preset("fixed", "兼容预设", "Apple Lossless · 44.1kHz · 16bit · 立体声 · 高质量抖动")],
    ),
    "alac": FormatSpec(
        "alac", "ALAC 无损", ".m4a", True,
        [
            Preset("original", "原样", "保持源采样率与位深"),
            Preset("cd", "CD 兼容", "44.1kHz / 16bit，带抖动"),
        ],
    ),
    "wav": FormatSpec(
        "wav", "WAV (PCM)", ".wav", True,
        [
            Preset("cd", "16bit / 44.1kHz", "兼容性最好，体积大"),
            Preset("hires", "24bit / 原采样率", "保留高解析源的信息量"),
        ],
    ),
    "mp3": FormatSpec(
        "mp3", "MP3 (LAME VBR)", ".mp3", False,
        [
            Preset("low", "低 · V5", "约 130 kbps，体积最小"),
            Preset("medium", "中 · V2", "约 190 kbps，推荐"),
            Preset("high", "高 · V0", "约 245 kbps，接近透明"),
        ],
    ),
    "aac": FormatSpec(
        "aac", "AAC (m4a)", ".m4a", False,
        [
            Preset("low", "低 · 128k", "适合手机外放/播客"),
            Preset("medium", "中 · 192k", "推荐，日常听歌"),
            Preset("high", "高 · 256k", "高码率，体积较大"),
        ],
    ),
}

DEFAULT_FORMAT = "flac"
DEFAULT_PRESET = "medium"
# UI 里的默认选项：跟随源（分轨用）
SOURCE_FORMAT = FormatSpec("source", "跟随源格式（推荐）", "", True, [])


def get_format(fmt: str) -> FormatSpec:
    if fmt == "source":
        return SOURCE_FORMAT
    return FORMATS.get(fmt, FORMATS[DEFAULT_FORMAT])


def preset_choices(fmt: str) -> list[Preset]:
    spec = FORMATS.get(fmt)
    return spec.presets if spec else []


def ffmpeg_args(fmt: str, preset_id: str | None) -> list[str]:
    """返回编码参数（不含 -i / 输出路径）。"""
    spec = FORMATS.get(fmt)
    if not spec:
        return ["-c:a", "copy"]
    p = spec.preset(preset_id)
    if fmt == "flac":
        return ["-c:a", "flac", "-compression_level", "8", "-f", "flac"]
    if fmt == "ipod":
        return [
            "-af", "aresample=44100:osf=s16:dither_method=triangular_hp",
            "-ar", "44100", "-sample_fmt", "s16p",
            "-c:a", "alac", "-movflags", "+faststart", "-f", "mp4",
        ]
    if fmt == "alac":
        if p.id == "cd":
            return [
                "-af", "aresample=44100:osf=s16:dither_method=triangular_hp",
                "-ar", "44100", "-sample_fmt", "s16p",
                "-c:a", "alac", "-movflags", "+faststart", "-f", "mp4",
            ]
        return ["-c:a", "alac", "-movflags", "+faststart", "-f", "mp4"]
    if fmt == "wav":
        if p.id == "cd":
            return ["-c:a", "pcm_s16le", "-ar", "44100", "-f", "wav"]
        return ["-c:a", "pcm_s24le", "-f", "wav"]
    if fmt == "mp3":
        q = {"low": "5", "medium": "2", "high": "0"}.get(p.id, "2")
        return ["-c:a", "libmp3lame", "-q:a", q, "-f", "mp3"]
    if fmt == "aac":
        br = {"low": "128k", "medium": "192k", "high": "256k"}.get(p.id, "192k")
        return ["-c:a", "aac", "-b:a", br, "-f", "mp4"]
    return ["-c:a", "copy"]


def describe(fmt: str, preset_id: str | None) -> str:
    spec = get_format(fmt)
    if fmt == "source":
        return "跟随源格式"
    p = spec.preset(preset_id)
    return f"{spec.label} · {p.label}（{p.desc}）"


def convert(src: str | Path, dest: str | Path, fmt: str, preset_id: str | None) -> Path:
    """把已下载的源音频转成目标格式。"""
    ff = find_ffmpeg()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = [
        ff, "-y", "-hide_banner", "-v", "error",
        "-i", str(src), "-map", "0:a:0", "-map_metadata", "-1",
        "-vn", "-sn", "-dn",
        *ffmpeg_args(fmt, preset_id), str(dest),
    ]
    p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", **hidden_kwargs())
    if p.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(f"转码失败（{fmt}）: {p.stderr[-400:]}")
    # 抽样解码验证，避免静默生成损坏文件
    check = subprocess.run(
        [ff, "-v", "error", "-t", "3", "-i", str(dest), "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **hidden_kwargs(),
    )
    if check.returncode != 0:
        raise RuntimeError(f"输出文件无法解码（{fmt}）: {check.stderr[-300:]}")
    return dest


# ---------------- 标签 ----------------
def _cover_mime(data: bytes) -> tuple[str, bool]:
    """返回 (mime, 是否 PNG)。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", True
    return "image/jpeg", False


def tag_audio(path: Path, *, title: str, artist: str, album: str,
              track: int = 1, total: int = 1, comment: str = "",
              cover: bytes | None = None) -> None:
    suffix = path.suffix.lower()
    try:
        if suffix == ".flac":
            _tag_flac(path, title, artist, album, track, total, comment, cover)
        elif suffix == ".m4a":
            _tag_m4a(path, title, artist, album, track, total, comment, cover)
        elif suffix == ".mp3":
            _tag_mp3(path, title, artist, album, track, total, comment, cover)
        elif suffix == ".wav":
            _tag_wav(path, title, artist, album, track, comment, cover)
    except Exception:
        pass


def _tag_flac(path, title, artist, album, track, total, comment, cover):
    from mutagen.flac import FLAC, Picture

    f = FLAC(str(path))
    f["title"] = title
    f["artist"] = artist
    f["album"] = album
    f["tracknumber"] = str(track)
    f["tracktotal"] = str(total)
    if comment:
        f["comment"] = comment
    if cover:
        mime, _ = _cover_mime(cover)
        pic = Picture()
        pic.type = 3
        pic.mime = mime
        pic.desc = "Cover"
        pic.data = cover
        f.clear_pictures()
        f.add_picture(pic)
    f.save()


def _tag_m4a(path, title, artist, album, track, total, comment, cover):
    from mutagen.mp4 import MP4, MP4Cover

    f = MP4(str(path))
    f["\xa9nam"] = title
    f["\xa9ART"] = artist
    f["\xa9alb"] = album
    f["trkn"] = [(track, total)]
    if comment:
        f["\xa9cmt"] = comment
    if cover:
        _, is_png = _cover_mime(cover)
        f["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_PNG if is_png else MP4Cover.FORMAT_JPEG)]
    f.save()


def _tag_mp3(path, title, artist, album, track, total, comment, cover):
    from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TRCK

    try:
        tags = ID3(str(path))
    except Exception:
        tags = ID3()
    tags.delall("TIT2")
    tags.delall("TPE1")
    tags.delall("TALB")
    tags.delall("TRCK")
    tags.delall("APIC")
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text=artist))
    tags.add(TALB(encoding=3, text=album))
    tags.add(TRCK(encoding=3, text=f"{track}/{total}"))
    if cover:
        mime, _ = _cover_mime(cover)
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover))
    tags.save(str(path))


def _tag_wav(path, title, artist, album, track, comment, cover=None):
    from mutagen.id3 import APIC, TALB, TIT2, TPE1, TRCK
    from mutagen.wave import WAVE

    f = WAVE(str(path))
    if f.tags is None:
        f.add_tags()
    tags = f.tags
    tags.delall("TIT2")
    tags.delall("TPE1")
    tags.delall("TALB")
    tags.delall("TRCK")
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text=artist))
    tags.add(TALB(encoding=3, text=album))
    tags.add(TRCK(encoding=3, text=str(track)))
    if cover:
        mime, _ = _cover_mime(cover)
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover))
    f.save()
