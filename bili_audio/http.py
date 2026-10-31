"""HTTP 会话、通用请求与 B 站错误码处理。"""
from __future__ import annotations

import json
import random
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from .config import REFERER, UA

API = "https://api.bilibili.com"

HEADERS = {
    "User-Agent": UA,
    "Referer": REFERER,
    "Origin": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

ERRORS = {
    -101: "未登录或登录已过期",
    -111: "csrf 校验失败",
    -400: "请求参数错误",
    -403: "访问权限不足（可能需要大会员或登录）",
    -404: "资源不存在",
    -10403: "地区限制，当前网络无法访问",
    87007: "充电专属视频，需要充电后才能观看",
    87008: "充电专属视频（非充电用户）",
    600200: "稿件审核中",
    62002: "稿件不可见",
    62004: "稿件审核中",
    62012: "仅 UP 主自己可见",
}


class BiliError(RuntimeError):
    def __init__(self, code: int, message: str = ""):
        self.code = code
        super().__init__(f"[{code}] {ERRORS.get(code, message or '未知错误')}")


def new_session(cookies: dict[str, str] | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    if cookies:
        s.cookies.update(cookies)
    return s


def ensure_buvid(session: requests.Session) -> None:
    """补一个 buvid3/4，降低被风控（-352 / 412）的概率。"""
    if session.cookies.get("buvid3"):
        return
    try:
        r = session.get(f"{API}/x/frontend/finger/spi", timeout=10)
        d = r.json().get("data") or {}
        if d.get("b_3"):
            session.cookies.set("buvid3", d["b_3"], domain=".bilibili.com")
        if d.get("b_4"):
            session.cookies.set("buvid4", d["b_4"], domain=".bilibili.com")
    except Exception:
        pass


def api_get(
    session: requests.Session,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    retries: int = 3,
    timeout: float = 15,
) -> dict:
    """GET 一个 B 站 JSON 接口，带退避重试；业务错误码抛 BiliError。"""
    url = path if path.startswith("http") else API + path
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 412:
                raise BiliError(412, "请求被风控拦截，请降低并发或稍后再试")
            r.raise_for_status()
            data = r.json()
            code = data.get("code", 0)
            if code != 0:
                raise BiliError(code, data.get("message", ""))
            return data
        except BiliError:
            raise
        except Exception as e:  # 网络错误
            last = e
            time.sleep(0.8 * (attempt + 1) + random.random() * 0.4)
    raise BiliError(-1, f"网络请求失败: {last}")


def json_dump(path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
