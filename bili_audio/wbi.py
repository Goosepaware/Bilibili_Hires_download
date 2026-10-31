"""WBI 签名（2023 年后 playurl 等接口的硬性要求）。"""
from __future__ import annotations

import hashlib
import time
import urllib.parse
from typing import Any

from .http import API, BiliError

_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

_cache: dict[str, Any] = {"keys": None, "ts": 0.0}


def get_wbi_keys(session, force: bool = False) -> tuple[str, str]:
    now = time.time()
    if not force and _cache["keys"] and now - _cache["ts"] < 600:
        return _cache["keys"]
    # 未登录时 nav 返回 code=-101，但 data.wbi_img 仍然可用，所以这里不能走 api_get
    r = session.get(API + "/x/web-interface/nav", timeout=15)
    data = r.json()
    wbi = (data.get("data") or {}).get("wbi_img") or {}
    if not wbi.get("img_url"):
        raise BiliError(data.get("code", -1), data.get("message", "无法获取 WBI 密钥"))
    img_key = wbi["img_url"].rsplit("/", 1)[-1].split(".")[0]
    sub_key = wbi["sub_url"].rsplit("/", 1)[-1].split(".")[0]
    _cache["keys"] = (img_key, sub_key)
    _cache["ts"] = now
    return img_key, sub_key


def _mixin_key(img_key: str, sub_key: str) -> str:
    orig = img_key + sub_key
    return "".join(orig[i] for i in _MIXIN_TAB)[:32]


def sign(session, params: dict[str, Any]) -> dict[str, Any]:
    img_key, sub_key = get_wbi_keys(session)
    mixin = _mixin_key(img_key, sub_key)
    p = {k: str(v) for k, v in params.items() if v is not None}
    p["wts"] = str(int(time.time()))
    for k, v in list(p.items()):
        p[k] = "".join(ch for ch in v if ch not in "!'()*")
    query = urllib.parse.urlencode(sorted(p.items()))
    p["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return p
