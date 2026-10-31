"""登录：扫码 / 手动 Cookie / cookies.txt 导入，本地 DPAPI 加密存储。"""
from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path
from typing import Callable

from .config import SESSION_FILE, dpapi_decrypt, dpapi_encrypt
from .http import BiliError, api_get, ensure_buvid, new_session

PASSPORT = "https://passport.bilibili.com"

COOKIE_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid", "buvid3", "buvid4")


def load_cookies() -> dict[str, str]:
    if not SESSION_FILE.exists():
        return {}
    try:
        raw = dpapi_decrypt(SESSION_FILE.read_text(encoding="ascii").strip())
        data = json.loads(raw)
        return {k: v for k, v in data.items() if isinstance(v, str)}
    except Exception:
        return {}


def save_cookies(cookies: dict[str, str]) -> None:
    payload = {k: v for k, v in cookies.items() if k in COOKIE_KEYS or k.startswith("buvid")}
    SESSION_FILE.write_text(dpapi_encrypt(json.dumps(payload, ensure_ascii=False)), encoding="ascii")


def clear_cookies() -> None:
    SESSION_FILE.unlink(missing_ok=True)


def apply_cookies(session, cookies: dict[str, str]) -> None:
    for k, v in cookies.items():
        session.cookies.set(k, v, domain=".bilibili.com")
    ensure_buvid(session)


def _parse_cookie_url(url: str) -> dict[str, str]:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return {k: v[0] for k, v in q.items() if k in COOKIE_KEYS}


def qr_login(
    on_qr: Callable[[str], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    timeout: float = 600,
) -> dict[str, str]:
    """扫码登录，返回 cookie 字典。on_qr 收到二维码内容（URL）。"""
    session = new_session()
    ensure_buvid(session)
    say = on_status or (lambda _m: None)

    deadline = time.time() + timeout
    while time.time() < deadline:
        gen = api_get(session, PASSPORT + "/x/passport-login/web/qrcode/generate")
        qr_url = gen["data"]["url"]
        key = gen["data"]["qrcode_key"]
        if on_qr:
            on_qr(qr_url)

        scanned = False
        while time.time() < deadline:
            r = session.get(
                PASSPORT + "/x/passport-login/web/qrcode/poll",
                params={"qrcode_key": key},
                timeout=10,
            )
            data = r.json()
            d = data.get("data") or {}
            code = d.get("code")
            if code == 0:
                cookies = {c.name: c.value for c in session.cookies}
                cookies.update(_parse_cookie_url(d.get("url", "")))
                cookies = {k: v for k, v in cookies.items() if k in COOKIE_KEYS}
                if not cookies.get("SESSDATA"):
                    raise BiliError(-1, "登录成功但没拿到 SESSDATA")
                save_cookies(cookies)
                return cookies
            if code == 86101:
                if not scanned:
                    say("请用手机 B 站扫描二维码…")
                    scanned = True
            elif code == 86090:
                say("已扫码，请在手机上确认登录")
            elif code == 86038:
                say("二维码已过期，重新生成")
                break
            else:
                say(f"等待扫码… ({code})")
            time.sleep(1.2)
        else:
            break
    raise BiliError(-1, "登录超时")


def import_netscape(path: str | Path) -> dict[str, str]:
    """从浏览器扩展导出的 cookies.txt 导入。"""
    cookies: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and "bilibili" in parts[0]:
            cookies[parts[5]] = parts[6]
    cookies = {k: v for k, v in cookies.items() if k in COOKIE_KEYS}
    if not cookies.get("SESSDATA"):
        raise BiliError(-1, "该 cookies.txt 里没有 SESSDATA")
    save_cookies(cookies)
    return cookies


def fetch_account(session) -> dict:
    data = api_get(session, "/x/web-interface/nav")
    d = data.get("data") or {}
    if not d.get("isLogin"):
        raise BiliError(-101, "未登录")
    vip = d.get("vipStatus") == 1
    due = d.get("vipDueDate")
    return {
        "mid": d.get("mid"),
        "uname": d.get("uname"),
        "vip": vip,
        "vip_type": d.get("vipType"),
        "vip_due": time.strftime("%Y-%m-%d", time.localtime(due / 1000)) if due else None,
        "level": (d.get("level_info") or {}).get("current_level"),
    }


def current_session():
    """返回 (session, account|None)。"""
    cookies = load_cookies()
    session = new_session(cookies)
    if not cookies.get("SESSDATA"):
        ensure_buvid(session)
        return session, None
    try:
        return session, fetch_account(session)
    except Exception:
        ensure_buvid(session)
        return session, None
