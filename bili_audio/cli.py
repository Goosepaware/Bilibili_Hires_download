"""命令行入口。"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import auth
from .analyze import analyze, format_report
from .config import APP_DIR, DEFAULT_DOWNLOAD_DIR, ensure_stdout, find_ffmpeg, load_config, save_config
from .http import BiliError
from .pipeline import Options, process_url


def _qr_to_terminal(url: str) -> None:
    try:
        import qrcode

        qr = qrcode.QRCode(border=2, box_size=12)
        qr.add_data(url)
        qr.make(fit=True)
        target = APP_DIR / "login_qr.png"
        qr.make_image().save(target)
        print(f"二维码图片: {target}")
        qr.print_ascii(invert=True)
    except Exception:
        print("二维码生成失败，请重试")


def cmd_login(args) -> int:
    if getattr(args, "sessdata", None):
        cookies = {"SESSDATA": args.sessdata.strip()}
        auth.save_cookies(cookies)
        print("已保存手动填写的 SESSDATA")
    elif args.cookie_file:
        cookies = auth.import_netscape(args.cookie_file)
        print("已从 cookies.txt 导入")
    else:
        print("正在生成登录二维码…")
        cookies = auth.qr_login(on_qr=_qr_to_terminal)
    session, account = auth.current_session()
    if not account:
        print("登录失败：未能获取账号信息")
        return 1
    vip = f"大会员（到期 {account['vip_due']}）" if account["vip"] else "非大会员（无法获取 Hi-Res 无损）"
    print(f"登录成功：{account['uname']} (uid {account['mid']}) | {vip}")
    return 0


def cmd_whoami(args) -> int:
    session, account = auth.current_session()
    if not account:
        print("未登录，请先运行: ba login")
        return 1
    vip = f"大会员，到期 {account['vip_due']}" if account["vip"] else "非大会员"
    print(f"{account['uname']} (uid {account['mid']}) | Lv{account['level']} | {vip}")
    return 0


def _opts(args) -> Options:
    return Options(
        out=Path(getattr(args, "out", None) or load_config().get("last_out") or DEFAULT_DOWNLOAD_DIR),
        atmos=getattr(args, "atmos", False),
        aac_fallback=not getattr(args, "no_aac", False),
        verify=not getattr(args, "no_verify", False),
        tag=not getattr(args, "no_tag", False),
        threads=getattr(args, "threads", 4),
        pages=getattr(args, "pages", "all"),
        keep_temp=getattr(args, "keep_temp", False),
        fmt=getattr(args, "fmt", "flac"),
        preset=getattr(args, "preset", "medium"),
    )


def cmd_dl(args) -> int:
    find_ffmpeg()
    session, account = auth.current_session()
    if account:
        print(f"账号: {account['uname']}" + ("（大会员）" if account["vip"] else "（非大会员）"))
    else:
        print("未登录：只能下载 AAC，无法获取 Hi-Res 无损。请先运行 ba login")

    opts = _opts(args)
    cfg = load_config()
    cfg["last_out"] = str(opts.out)
    save_config(cfg)
    failed = 0
    for url in args.urls:
        t0 = time.time()
        try:
            results = process_url(session, url, opts, log=print)
            print(f"—— 完成 {len(results)} 个音频，用时 {time.time() - t0:.1f}s\n")
        except BiliError as e:
            print(f"✗ {url}: {e}\n")
            failed += 1
        except KeyboardInterrupt:
            print("已中断")
            return 130
        except Exception as e:
            print(f"✗ {url}: {e}\n")
            failed += 1
    return 1 if failed else 0


def cmd_info(args) -> int:
    from . import api
    from .pipeline import choose_tracks, select_tracks

    session, _ = auth.current_session()
    opts = _opts(args)
    for url in args.urls:
        vid = api.resolve_url(session, url)
        video = api.get_video(session, vid)
        print(f"■ {video.title}\n  {video.bvid} / av{video.aid} | {video.owner} | {len(video.pages)}P")
        for page in video.pages[: int(args.limit)]:
            dash = (api.get_playurl(session, video.bvid, page.cid).get("dash") or {})
            tracks = select_tracks(dash, opts)
            chosen, note = choose_tracks(tracks, opts)
            print(f"  P{page.index} cid={page.cid} {page.title}")
            for t in tracks:
                mark = "*" if t in chosen else " "
                print(f"    {mark} {t.label:24} id={t.id:<6} {t.kbps:>5} kb/s  {t.codec}")
            if note:
                print(f"    ! {note}")
    return 0


def cmd_verify(args) -> int:
    rc = 0
    for f in args.files:
        p = Path(f)
        print(f"=== {p}")
        a = analyze(p)
        print(format_report(a))
        if a.get("verdict") not in ("LOSSLESS",):
            rc = 2
    return rc


def cmd_score(args) -> int:
    from . import quality

    session, account = auth.current_session()
    if not account:
        print("! 未登录：只能看到 AAC，无法检测 Hi-Res 无损音轨。请先运行 ba login")
    for url in args.urls:
        print(f"检测中（{args.segments} 段采样）…")
        report = quality.inspect_url(session, url, segments=args.segments, workers=args.workers)
        print(quality.format_report(report))
        if args.json:
            from pathlib import Path

            p = Path(args.json)
            p.parent.mkdir(parents=True, exist_ok=True)
            import json

            p.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\nJSON 已写入: {p}")
        print()
    return 0


def cmd_split(args) -> int:
    from . import api, split
    from .config import REFERER, UA
    from .download import download
    from .pipeline import Options, select_tracks
    from .tracklist import parse_tracklist

    session, _ = auth.current_session()
    vid = api.resolve_url(session, args.url)
    video = api.get_video(session, vid)
    page = video.pages[0]
    dash = (api.get_playurl(session, video.bvid, page.cid).get("dash") or {})
    tracks = select_tracks(dash, Options())
    track = next((t for t in tracks if t.kind == "flac"), None) or next((t for t in tracks if t.kind == "dolby"), None) or next((t for t in tracks if t.kind == "aac"), None)
    if not track:
        print("没有可用音轨")
        return 1
    duration = float(dash.get("duration") or page.duration or 0)
    print(f"■ {video.title}\n  {video.bvid} · {track.label} · {duration:.0f}s")
    headers = {"Referer": REFERER, "User-Agent": UA}
    out_root = Path(args.out or load_config().get("last_split_out") or DEFAULT_DOWNLOAD_DIR)

    def refresh() -> str:
        d = (api.get_playurl(session, video.bvid, page.cid).get("dash") or {})
        ts = select_tracks(d, Options())
        t = next((x for x in ts if x.kind == track.kind), None)
        return t.urls[0] if t else track.urls[0]

    if args.list:
        text = Path(args.list).read_text(encoding="utf-8", errors="replace") if Path(args.list).exists() else args.list
        parsed = parse_tracklist(text, duration)
        if not parsed.tracks:
            print("! 没有识别到曲目：" + "；".join(parsed.warnings))
            return 1
        for w in parsed.warnings:
            print("  !", w)
        print(f"识别到 {len(parsed.tracks)} 首：")
        for t in parsed.tracks:
            print(f"  {t.index:02d}  {t.start_text:>7}  {t.title}")
        segments = split.segments_from_tracks(parsed.tracks, duration)
        if getattr(args, "limit", None):
            segments = segments[: args.limit]
        source = track.urls[0]
        temp = None
    else:
        temp_dir = out_root / ".bili_audio_tmp"
        temp = temp_dir / f"{video.bvid}_{page.cid}.{track.ext}"
        print("下载临时整轨…")
        download(track.urls, temp, threads=args.threads, refresh=refresh, replace=True)
        print("分析静音与波形…")
        pcm = split.decode_analysis_pcm(str(temp))
        gaps = split.detect_silence(pcm, threshold_db=args.threshold, min_silence=args.min_silence)
        points = split.splits_from_gaps(gaps, duration, min_gap=args.min_gap)
        print(f"检测到 {len(gaps)} 段静音，切点 {len(points)} 个")
        for p in points:
            print("  ", f"{int(p) // 60:02d}:{int(p) % 60:02d}")
        segments = split.segments_from_points(points, duration)
        source = str(temp)

    album_dir = out_root / split.safe_name(args.album or video.title)
    from . import formats

    cover = None
    if args.cover:
        try:
            cover = Path(args.cover).read_bytes()
        except Exception as e:
            print(f"! 读取封面失败: {e}")
    print(f"导出到 {album_dir} · {formats.describe(args.format, args.preset)}")
    results = split.export_segments(
        source, segments, album_dir, album=args.album or video.title,
        artist=args.artist or video.owner,
        ext=track.ext, fmt=args.format, preset=args.preset, cover=cover,
        headers=headers if not temp else None,
        titles=not args.no_titles, refresh=refresh,
        progress=lambda i, n, name: print(f"  [{i}/{n}] {name}"),
    )
    print(f"完成，共 {len(results)} 个文件")
    cfg = load_config()
    cfg["last_split_out"] = str(out_root)
    save_config(cfg)
    if temp and not args.keep_temp:
        temp.unlink(missing_ok=True)
        for suffix in (".part.json", ".part"):
            temp.with_name(temp.name + suffix).unlink(missing_ok=True)
    return 0


def cmd_gui(args) -> int:
    try:
        from .gui.app import main as gui_main
    except Exception:
        print("此版本未包含图形界面，请运行 bili-audio.exe")
        return 1
    return gui_main()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ba", description="B 站 Hi-Res 无损音频下载与真伪核验")
    sub = p.add_subparsers(dest="cmd")

    lp = sub.add_parser("login", help="扫码登录（或导入 cookies.txt）")
    lp.add_argument("--cookie-file", help="浏览器扩展导出的 cookies.txt")
    lp.add_argument("--sessdata", help="手动填入浏览器 Cookie 里的 SESSDATA")
    lp.set_defaults(func=cmd_login)

    wp = sub.add_parser("whoami", help="查看当前账号与大会员状态")
    wp.set_defaults(func=cmd_whoami)

    dl = sub.add_parser("dl", help="下载视频音频")
    dl.add_argument("urls", nargs="+")
    dl.add_argument("-o", "--out", help="输出目录")
    dl.add_argument("--atmos", action="store_true", help="同时下载杜比全景声（E-AC3）")
    dl.add_argument("--no-aac", action="store_true", help="没有无损时不要回退 AAC")
    dl.add_argument("--no-verify", action="store_true", help="跳过频谱核验")
    dl.add_argument("--no-tag", action="store_true", help="不写标签/封面")
    dl.add_argument("-t", "--threads", type=int, default=4, help="分片下载线程数")
    dl.add_argument("-p", "--pages", default="all", help="分 P，如 1 或 1,3-5（默认 all）")
    dl.add_argument("--keep-temp", action="store_true")
    dl.add_argument("-f", "--format", dest="fmt", default="flac",
                    choices=["flac", "ipod", "alac", "wav", "mp3", "aac", "source"],
                    help="输出格式：flac/ipod/alac/wav/mp3/aac/source")
    dl.add_argument("--preset", default="medium", help="有损格式预设：low/medium/high")
    dl.set_defaults(func=cmd_dl)

    ip = sub.add_parser("info", help="只解析，查看可用音轨")
    ip.add_argument("urls", nargs="+")
    ip.add_argument("--atmos", action="store_true")
    ip.add_argument("--limit", default=3, help="最多显示几个分 P")
    ip.set_defaults(func=cmd_info)

    vp = sub.add_parser("verify", help="核验本地音频文件是否为真无损")
    vp.add_argument("files", nargs="+")
    vp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("score", help="在线检测视频音源质量并打分（1-100）")
    sp.add_argument("urls", nargs="+")
    sp.add_argument("-s", "--segments", type=int, default=8, help="分段数量（默认 8）")
    sp.add_argument("-w", "--workers", type=int, default=4)
    sp.add_argument("--json", help="把结果写成 JSON 文件")
    sp.set_defaults(func=cmd_score)

    tp = sub.add_parser("split", help="整轨自动分轨（静音检测 / 曲目文本）")
    tp.add_argument("url")
    tp.add_argument("--list", help="曲目表文本（文件路径或直接文本）")
    tp.add_argument("--threshold", type=float, default=-35.0, help="静音阈值 dB，默认 -35")
    tp.add_argument("--min-silence", type=float, default=1.5, help="最短静音秒数，默认 1.5")
    tp.add_argument("--min-gap", type=float, default=10.0, help="切点最小间隔秒数，默认 10")
    tp.add_argument("-o", "--out", default=None)
    tp.add_argument("--no-titles", action="store_true", help="文件名不写歌名，只用序号")
    tp.add_argument("-t", "--threads", type=int, default=4)
    tp.add_argument("--keep-temp", action="store_true")
    tp.add_argument("--limit", type=int, help="只导出前 N 首（用于预览）")
    tp.add_argument("-f", "--format", default="source",
                    choices=["source", "flac", "ipod", "alac", "wav", "mp3", "aac"],
                    help="导出格式，默认跟随源")
    tp.add_argument("--preset", default="medium", help="有损格式预设：low/medium/high")
    tp.add_argument("--album", help="专辑名，默认视频标题")
    tp.add_argument("--artist", help="歌手，默认 UP 主名")
    tp.add_argument("--cover", help="封面图片路径，嵌入每个分轨")
    tp.set_defaults(func=cmd_split)

    gp = sub.add_parser("gui", help="打开图形界面")
    gp.set_defaults(func=cmd_gui)
    return p


def main(argv: list[str] | None = None) -> int:
    ensure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except BiliError as e:
        print(f"✗ {e}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
