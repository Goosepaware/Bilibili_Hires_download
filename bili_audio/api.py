"""B 站接口：短链解析、视频信息、取流。"""
from __future__ import annotations

import re

from .http import BiliError, api_get
from .models import Page, Video
from .wbi import sign

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
AV_RE = re.compile(r"av(\d+)", re.I)
EP_RE = re.compile(r"(?:ep|ss)(\d+)", re.I)


def resolve_url(session, url: str) -> str:
    """接受 BV 号 / av 号 / 完整链接 / b23.tv 短链，返回 BV 号或 ep/ss 标识。"""
    url = url.strip()
    if BV_RE.fullmatch(url):
        return BV_RE.fullmatch(url).group(1)
    m = AV_RE.fullmatch(url)
    if m:
        return "av" + m.group(1)
    if url.startswith("http"):
        if "b23.tv" in url or "bili2233" in url:
            try:
                r = session.get(url, allow_redirects=True, timeout=15)
                url = r.url
            except Exception as e:
                raise BiliError(-1, f"短链展开失败: {e}")
        m = BV_RE.search(url)
        if m:
            return m.group(1)
        m = AV_RE.search(url)
        if m:
            return "av" + m.group(1)
        m = EP_RE.search(url)
        if m:
            raise BiliError(-1, "番剧/影视（ep/ss）暂不支持，当前版本只处理普通投稿视频")
    raise BiliError(-1, f"无法识别的链接或 ID: {url}")


def get_video(session, vid: str) -> Video:
    params = {"bvid": vid} if vid.startswith("BV") else {"aid": vid[2:]}
    data = api_get(session, "/x/web-interface/view", params)["data"]
    pages = [
        Page(index=p["page"], cid=p["cid"], title=p.get("part") or f"P{p['page']}", duration=p.get("duration", 0))
        for p in data.get("pages") or [{"page": 1, "cid": data["cid"], "part": data["title"], "duration": data.get("duration", 0)}]
    ]
    return Video(
        bvid=data["bvid"],
        aid=data["aid"],
        title=data["title"],
        owner=(data.get("owner") or {}).get("name", ""),
        cover=data.get("pic", ""),
        pages=pages,
        pubdate=data.get("pubdate", 0),
        desc=data.get("desc", ""),
    )


def get_playurl(session, bvid: str, cid: int, *, qn: int = 127) -> dict:
    """取 DASH 流；fnval=4048 才会带上杜比/Hi-Res 等全部音轨。"""
    params = sign(
        session,
        {
            "bvid": bvid,
            "cid": cid,
            "qn": qn,
            "fnval": 4048,
            "fnver": 0,
            "fourk": 1,
            "otype": "json",
            "platform": "pc",
        },
    )
    data = api_get(session, "/x/player/wbi/playurl", params)
    return data["data"]
