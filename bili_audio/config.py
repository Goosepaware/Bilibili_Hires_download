"""全局配置与本地路径、ffmpeg 定位、Windows DPAPI 加解密。"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

APP_NAME = "bili-audio"
APP_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
APP_DIR.mkdir(parents=True, exist_ok=True)

SESSION_FILE = APP_DIR / "session.bin"
CONFIG_FILE = APP_DIR / "config.json"
CACHE_DIR = APP_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_DOWNLOAD_DIR = Path.home() / "Music" / "bilibili"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REFERER = "https://www.bilibili.com/"


def find_ffmpeg() -> str:
    """按 环境变量 -> PATH -> 项目目录 -> 常见安装位置 查找 ffmpeg。"""
    env = os.environ.get("BILI_AUDIO_FFMPEG")
    if env and Path(env).exists():
        return str(Path(env))
    which = shutil.which("ffmpeg")
    if which:
        return which
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / "ffmpeg.exe",
        here / "ffmpeg.exe",
        Path(sys.executable).parent / "ffmpeg.exe",
        Path(getattr(sys, "_MEIPASS", here)) / "ffmpeg.exe",
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        Path(r"D:\ffmpeg\bin\ffmpeg.exe"),
        Path(r"D:\FormatFactory\ffmpeg.exe"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    raise FileNotFoundError(
        "找不到 ffmpeg。请安装后加入 PATH，或设置环境变量 BILI_AUDIO_FFMPEG 指向 ffmpeg.exe"
    )


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("DPAPI 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def dpapi_encrypt(text: str) -> str:
    return base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")


def dpapi_decrypt(b64: str) -> str:
    return _dpapi(base64.b64decode(b64), False).decode("utf-8")


def hidden_kwargs() -> dict:
    """让子进程（ffmpeg）不要弹出控制台窗口。"""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(data: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def human_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GiB"


def human_duration(sec: float | None) -> str:
    if not sec:
        return "-"
    sec = int(sec)
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def ensure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
