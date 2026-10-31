"""数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Page:
    index: int
    cid: int
    title: str
    duration: int = 0


@dataclass
class Video:
    bvid: str
    aid: int
    title: str
    owner: str
    cover: str
    pages: list[Page]
    pubdate: int = 0
    desc: str = ""


@dataclass
class Track:
    kind: str            # flac / dolby / aac
    id: int              # 30251 / 30250 / 30280...
    codec: str           # fLaC / ec-3 / mp4a.40.2
    bandwidth: int
    urls: list[str]
    label: str
    ext: str             # flac / eac3 / m4a
    sample_rate: int | None = None
    channels: int | None = None

    @property
    def kbps(self) -> int:
        return round(self.bandwidth / 1000)


@dataclass
class DownloadResult:
    video: Video
    page: Page
    track: Track
    path: str
    bytes: int
    analysis: dict = field(default_factory=dict)
    extra_paths: list[str] = field(default_factory=list)
