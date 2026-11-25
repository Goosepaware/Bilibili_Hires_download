"""读取播放器的字幕轨，导出 SRT 并按时间对照文字。"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .http import BiliError, api_get, new_session
from .wbi import sign


@dataclass(frozen=True)
class Cue:
    start: int
    end: int
    text: str


def get_tracks(session, bvid: str, cid: int) -> list[dict]:
    params = sign(session, {"bvid": bvid, "cid": cid})
    data = api_get(session, "/x/player/wbi/v2", params)["data"]
    return (data.get("subtitle") or {}).get("subtitles") or []


def fetch_cues(session, url: str) -> list[Cue]:
    """字幕 CDN 使用单独的无登录会话。"""
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username is not None or
            not any(host == domain or host.endswith("." + domain)
                    for domain in ("hdslb.com", "bilibili.com"))):
        raise BiliError(-1, "字幕地址不属于受支持的 B 站 HTTPS 域名")

    with session.get(url, timeout=15, allow_redirects=False) as response:
        if response.status_code != 200:
            raise BiliError(response.status_code, "读取字幕文件失败")
        body = response.json().get("body") or []
    cues = []
    for item in body:
        text = item.get("content")
        start, end = item.get("from"), item.get("to")
        if not isinstance(text, str) or not text.strip():
            continue
        if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (start, end)):
            continue
        start, end = round(start * 1000), round(end * 1000)
        if 0 <= start < end:
            cues.append(Cue(start, end, text.strip()))
    if not cues:
        raise BiliError(-1, "字幕文件没有有效的文本和时间轴")
    return sorted(cues, key=lambda cue: cue.start)


def _srt_time(ms: int) -> str:
    seconds, ms = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def render_srt(cues: list[Cue]) -> str:
    blocks = []
    for i, cue in enumerate(cues, 1):
        text = "\n".join(line for line in cue.text.splitlines() if line.strip())
        blocks.append(f"{i}\n{_srt_time(cue.start)} --> {_srt_time(cue.end)}\n{text}")
    return "\n\n".join(blocks) + "\n"


def track_label(track: dict) -> str:
    language = track.get("lan") or "und"
    label = track.get("lan_doc") or language
    ai = " · AI" if language.startswith("ai-") or track.get("ai_type") else ""
    return f"{label} [{language}]{ai}"


def _script_hint(cues: list[Cue]) -> str:
    text = "".join(cue.text for cue in cues)
    if re.search(r"[\u3041-\u3096\u30a1-\u30fa]", text):
        return "检测到日文假名"
    if re.search(r"[\u3400-\u9fff]", text):
        return "检测到汉字"
    return ""


def _cue_dict(cue: Cue) -> dict:
    return {"start_ms": cue.start, "end_ms": cue.end, "text": cue.text}


def _compare(exported: list[tuple[Path, list[Cue]]]) -> dict:
    """以第一轨为参照，保留切分不同造成的一对多与未重叠条目。"""
    reference = exported[0][1]
    comparisons = []
    for path, cues in exported[1:]:
        used = set()
        rows = []
        first = 0
        for cue in reference:
            while first < len(cues) and cues[first].end <= cue.start:
                first += 1
            matches = []
            for i in range(first, len(cues)):
                other = cues[i]
                if other.start >= cue.end:
                    break
                if other.end > cue.start:
                    matches.append(_cue_dict(other))
                    used.add(i)
            rows.append({"reference": _cue_dict(cue), "matches": matches})
        comparisons.append({
            "file": path.name,
            "rows": rows,
            "unmatched": [_cue_dict(cue) for i, cue in enumerate(cues) if i not in used],
        })
    return {"reference": exported[0][0].name, "comparisons": comparisons}


def export_page(session, bvid: str, cid: int, stem: Path, *, list_only: bool = False,
                compare: bool = False, track_number: int | None = None, log=print) -> int:
    """返回实际失败的轨数。接口未提供地址的轨道单独提示。"""
    tracks = get_tracks(session, bvid, cid)
    if track_number is not None and not 1 <= track_number <= len(tracks):
        log(f"    没有第 {track_number} 条字幕；当前返回 {len(tracks)} 条，可用 --list 确认编号")
        return 1
    if not tracks:
        log("    未返回字幕轨；可登录后重试")
        return 0
    failed = 0
    exported = []
    with new_session() as cdn:
        for i, track in enumerate(tracks, 1):
            if track_number is not None and i != track_number:
                continue
            label = f"#{i} {track_label(track)}"
            url = track.get("subtitle_url")
            if not url:
                log(f"    {label}：没有可用的下载地址")
                if track_number is not None:
                    failed += 1
                continue
            if list_only:
                log(f"    {label}：可下载")
                continue
            language = re.sub(r"[^a-zA-Z0-9-]", "", track.get("lan") or "") or "und"
            path = stem.with_name(f"{stem.name}.subtitle.{i:02d}.{language}.srt")
            try:
                cues = fetch_cues(cdn, url)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(render_srt(cues), encoding="utf-8")
            except Exception as error:
                # 网络异常中可能含临时字幕地址，只输出错误类型或可读业务错误。
                message = str(error) if isinstance(error, BiliError) else type(error).__name__
                log(f"    {label}：导出失败（{message}）")
                failed += 1
                continue
            hint = _script_hint(cues)
            log(f"    {label} → {path.name}，{len(cues)} 条" + (f"，{hint}" if hint else ""))
            exported.append((path, cues))
    if compare and len(exported) >= 2:
        path = stem.with_name(f"{stem.name}.subtitle.compare.json")
        path.write_text(json.dumps(_compare(exported), ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"    时间轴对照 → {path.name}")
    return failed
