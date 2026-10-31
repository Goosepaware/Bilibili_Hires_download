"""分片并发下载：断点续传、URL 过期自动换流。"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from typing import Callable

import requests

from .config import REFERER, UA

Progress = Callable[[int, int, float], None]  # done, total, bytes/s
Refresh = Callable[[], list[str]]

CHUNK_MIN = 4 * 1024 * 1024


def _headers() -> dict[str, str]:
    return {
        "User-Agent": UA,
        "Referer": REFERER,
        "Origin": "https://www.bilibili.com",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }


def _probe(session: requests.Session, url: str) -> tuple[int, bool]:
    r = session.get(url, headers={**_headers(), "Range": "bytes=0-0"}, stream=True, timeout=20)
    try:
        if r.status_code == 206:
            cr = r.headers.get("Content-Range", "")
            total = int(cr.rsplit("/", 1)[-1]) if "/" in cr else 0
            return total, True
        if r.status_code == 200:
            return int(r.headers.get("Content-Length") or 0), False
        if r.status_code in (403, 401):
            raise PermissionError("流地址已过期")
        r.raise_for_status()
        return 0, False
    finally:
        r.close()


class _Chunk:
    __slots__ = ("start", "end", "pos")

    def __init__(self, start: int, end: int, pos: int | None = None):
        self.start = start
        self.end = end
        self.pos = start if pos is None else pos


def download(
    urls: list[str],
    dest: Path,
    *,
    threads: int = 4,
    progress: Progress | None = None,
    refresh: Refresh | None = None,
    replace: bool = False,
) -> int:
    """把 urls[0]（失效时自动尝试备用/刷新）下载到 dest，返回字节数。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    state_path = dest.with_name(dest.name + ".part.json")
    part = dest.with_name(dest.name + ".part")

    if dest.exists() and not replace and part.exists() is False:
        return dest.stat().st_size

    sess = requests.Session()
    url_list = list(urls)
    try:
        total, ranged = _probe(sess, url_list[0])
    except Exception:
        if not refresh:
            raise
        url_list = refresh() or url_list
        total, ranged = _probe(sess, url_list[0])
    if total <= 0:
        raise RuntimeError("无法获取文件大小")

    # 续传状态
    chunks: list[_Chunk] = []
    if part.exists() and state_path.exists():
        try:
            st = json.loads(state_path.read_text(encoding="utf-8"))
            if st.get("total") == total and st.get("urls_tail", "") == url_list[0].split("?")[0][-60:]:
                chunks = [_Chunk(*c) for c in st["chunks"]]
        except Exception:
            chunks = []
    if not chunks:
        n = max(1, min(threads, math.ceil(total / CHUNK_MIN)))
        size = math.ceil(total / n)
        chunks = [_Chunk(i * size, min(total - 1, (i + 1) * size - 1)) for i in range(n)]
        with open(part, "wb") as f:
            f.truncate(total)

    lock = threading.Lock()
    done = sum(c.pos - c.start for c in chunks)
    stop = threading.Event()
    start_ts = time.time()
    tail = url_list[0].split("?")[0][-60:]

    def save_state() -> None:
        state_path.write_text(
            json.dumps({"total": total, "urls_tail": tail, "chunks": [[c.start, c.end, c.pos] for c in chunks]}),
            encoding="utf-8",
        )

    def report(force: bool = False) -> None:
        if not progress:
            return
        with lock:
            d = done
        elapsed = max(0.001, time.time() - start_ts)
        progress(d, total, d / elapsed)

    def worker(chunk: _Chunk) -> None:
        nonlocal done, url_list
        attempt = 0
        while chunk.pos <= chunk.end and not stop.is_set():
            idx = 0
            while idx < len(url_list):
                try:
                    h = {**_headers(), "Range": f"bytes={chunk.pos}-{chunk.end}"}
                    with requests.get(url_list[idx], headers=h, stream=True, timeout=30) as r:
                        if r.status_code in (403, 401):
                            raise PermissionError("403")
                        r.raise_for_status()
                        with open(part, "r+b") as f:
                            f.seek(chunk.pos)
                            for block in r.iter_content(256 * 1024):
                                if not block:
                                    continue
                                if stop.is_set():
                                    return
                                f.write(block)
                                chunk.pos += len(block)
                                with lock:
                                    done += len(block)
                                report()
                    attempt = 0
                    break
                except Exception:
                    idx += 1
                    if idx >= len(url_list):
                        attempt += 1
                        if attempt > 4:
                            stop.set()
                            return
                        # 让流程刷新一次取流地址
                        if refresh:
                            try:
                                fresh = refresh()
                                if fresh:
                                    with lock:
                                        url_list = fresh
                                    idx = 0
                                    attempt -= 1
                                    continue
                            except Exception:
                                pass
                        time.sleep(1.5 * attempt)

    ts = [threading.Thread(target=worker, args=(c,), daemon=True) for c in chunks]
    for t in ts:
        t.start()
    try:
        while any(t.is_alive() for t in ts):
            for t in ts:
                t.join(0.3)
            with lock:
                save_state()
    finally:
        with lock:
            save_state()

    if stop.is_set() or any(c.pos <= c.end for c in chunks):
        raise RuntimeError("下载未完成（网络中断或地址失效），已保留 .part 供续传")

    part.replace(dest)
    state_path.unlink(missing_ok=True)
    report(True)
    return total
