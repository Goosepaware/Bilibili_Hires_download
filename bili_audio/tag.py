"""写入标签与封面，并输出核验报告。"""
from __future__ import annotations

import json
from pathlib import Path


def _base_meta(video, page, track, analysis) -> dict[str, str]:
    fmt = analysis.get("codec", "").upper()
    if analysis.get("is_lossless_codec") and analysis.get("bit_depth"):
        detail = f"{analysis.get('sample_rate') or '?'}Hz/{analysis.get('bit_depth')}bit"
    else:
        detail = f"{analysis.get('sample_rate') or '?'}Hz/{analysis.get('bitrate_kbps') or '?'}kbps"
    return {
        "title": page.title if len(video.pages) > 1 else video.title,
        "artist": video.owner,
        "album": video.title,
        "date": str(video.pubdate or ""),
        "comment": f"bilibili {video.bvid} P{page.index} | {fmt} {detail} | {analysis.get('verdict_text', '')}",
        "bilibili_id": video.bvid,
        "bilibili_cid": str(page.cid),
        "audio_track": f"{track.label} (id={track.id}, {track.kbps} kb/s)",
    }


def tag_flac(path: str | Path, video, page, track, analysis, cover: bytes | None = None) -> None:
    from mutagen.flac import FLAC, Picture

    f = FLAC(str(path))
    meta = _base_meta(video, page, track, analysis)
    for k, v in meta.items():
        if v:
            f[k] = v
    f["tracknumber"] = str(page.index)
    f["tracktotal"] = str(len(video.pages))
    if cover:
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        pic.data = cover
        f.clear_pictures()
        f.add_picture(pic)
    f.save()


def tag_m4a(path: str | Path, video, page, track, analysis, cover: bytes | None = None) -> None:
    from mutagen.mp4 import MP4, MP4Cover

    f = MP4(str(path))
    meta = _base_meta(video, page, track, analysis)
    f["\xa9nam"] = meta["title"]
    f["\xa9ART"] = meta["artist"]
    f["\xa9alb"] = meta["album"]
    f["\xa9cmt"] = meta["comment"]
    f["trkn"] = [(page.index, len(video.pages))]
    if cover:
        f["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    f.save()


def write_report(path: Path, video, page, track, analysis) -> Path:
    out = path.with_suffix(path.suffix + ".json")
    payload = {
        "video": {"bvid": video.bvid, "aid": video.aid, "title": video.title, "owner": video.owner},
        "page": {"index": page.index, "cid": page.cid, "title": page.title},
        "track": {
            "kind": track.kind,
            "id": track.id,
            "codec": track.codec,
            "bandwidth": track.bandwidth,
            "label": track.label,
        },
        "audio": analysis,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
