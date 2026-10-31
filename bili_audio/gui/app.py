"""PySide6 图形界面：下载、音源质检、整轨自动分轨。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import api, auth, formats, quality
from .. import split as splitmod
from ..config import DEFAULT_DOWNLOAD_DIR, REFERER, UA, human_size, load_config, save_config
from ..download import download
from ..pipeline import Options, choose_tracks, process_url, select_tracks
from ..tracklist import parse_tracklist
from .waveform import WaveformView

APP_TITLE = "B 站 Hi-Res 音频下载器 / 音源质检 / 自动分轨"


def _fmt(t: float) -> str:
    t = int(max(0, t))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class LoginWorker(QThread):
    qr = Signal(str)
    status = Signal(str)
    done = Signal(bool, str)

    def run(self) -> None:
        try:
            path = Path(tempfile.gettempdir()) / "bili_audio_qr.png"

            def on_qr(url: str) -> None:
                import qrcode

                qr = qrcode.QRCode(border=2, box_size=10)
                qr.add_data(url)
                qr.make(fit=True)
                qr.make_image().save(path)
                self.qr.emit(str(path))

            auth.qr_login(on_qr=on_qr, on_status=self.status.emit)
            _, account = auth.current_session()
            if account:
                vip = f"大会员，到期 {account['vip_due']}" if account["vip"] else "非大会员"
                self.done.emit(True, f"{account['uname']}（{vip}）")
            else:
                self.done.emit(False, "登录后仍无法获取账号信息")
        except Exception as e:
            self.done.emit(False, str(e))


class LoginDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("扫码登录")
        self.resize(380, 500)
        layout = QVBoxLayout(self)
        self.hint = QLabel("正在生成二维码…")
        self.hint.setAlignment(Qt.AlignCenter)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setMinimumSize(300, 300)
        layout.addWidget(self.hint)
        layout.addWidget(self.image, 1)
        self.cancel = QPushButton("取消")
        self.cancel.clicked.connect(self.reject)
        layout.addWidget(self.cancel)

        self.worker = LoginWorker()
        self.worker.qr.connect(self._show_qr)
        self.worker.status.connect(self.hint.setText)
        self.worker.done.connect(self._finish)
        self.worker.start()

    def _show_qr(self, path: str) -> None:
        pix = QPixmap(path)
        self.image.setPixmap(pix.scaled(300, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _finish(self, ok: bool, msg: str) -> None:
        if ok:
            QMessageBox.information(self, "登录成功", msg)
            self.accept()
        else:
            QMessageBox.warning(self, "登录失败", msg)
            self.reject()


class ParseWorker(QThread):
    done = Signal(object, object, str)
    failed = Signal(str)

    def __init__(self, url: str, opts: Options):
        super().__init__()
        self.url = url
        self.opts = opts

    def run(self) -> None:
        try:
            session, _ = auth.current_session()
            vid = api.resolve_url(session, self.url)
            video = api.get_video(session, vid)
            dash = (api.get_playurl(session, video.bvid, video.pages[0].cid).get("dash") or {})
            self.done.emit(video, select_tracks(dash, self.opts), vid)
        except Exception as e:
            self.failed.emit(str(e))


class DownloadWorker(QThread):
    log = Signal(str)
    progress = Signal(int, int, float, float)
    finished_ok = Signal(int, str)
    failed = Signal(str)

    def __init__(self, url: str, opts: Options):
        super().__init__()
        self.url = url
        self.opts = opts

    def run(self) -> None:
        try:
            session, _ = auth.current_session()
            results = process_url(
                session, self.url, self.opts,
                log=self.log.emit, progress=self.progress.emit,
            )
            folder = str(Path(results[0].path).parent) if results else str(self.opts.out)
            self.finished_ok.emit(len(results), folder)
        except Exception as e:
            self.failed.emit(f"{e}\n{traceback.format_exc(limit=2)}")


class ScoreWorker(QThread):
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, url: str, segments: int):
        super().__init__()
        self.url = url
        self.segments = segments

    def run(self) -> None:
        try:
            session, _ = auth.current_session()
            self.finished_ok.emit(quality.inspect_url(session, self.url, segments=self.segments))
        except Exception as e:
            self.failed.emit(f"{e}")


class LoadSplitWorker(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self) -> None:
        try:
            session, _ = auth.current_session()
            vid = api.resolve_url(session, self.url)
            video = api.get_video(session, vid)
            page = video.pages[0]
            dash = api.get_playurl(session, video.bvid, page.cid).get("dash") or {}
            tracks = select_tracks(dash, Options())
            track = (next((t for t in tracks if t.kind == "flac"), None)
                     or next((t for t in tracks if t.kind == "dolby"), None)
                     or next((t for t in tracks if t.kind == "aac"), None))
            if not track:
                raise RuntimeError("没有可用音轨")
            self.done.emit({
                "video": video,
                "page": page,
                "track": track,
                "duration": float(dash.get("duration") or page.duration or 0),
                "bvid": video.bvid,
                "cid": page.cid,
            })
        except Exception as e:
            self.failed.emit(str(e))


class AnalyzeWorker(QThread):
    log = Signal(str)
    progress = Signal(int, int, float, float)
    done = Signal(object, object, str)
    failed = Signal(str)

    def __init__(self, ctx: dict, out_dir: Path, threshold: float, min_silence: float, min_gap: float):
        super().__init__()
        self.ctx = ctx
        self.out_dir = out_dir
        self.threshold = threshold
        self.min_silence = min_silence
        self.min_gap = min_gap

    def _refresh(self) -> list[str]:
        session, _ = auth.current_session()
        video = self.ctx["video"]
        page = self.ctx["page"]
        kind = self.ctx["track"].kind
        dash = api.get_playurl(session, video.bvid, page.cid).get("dash") or {}
        t = next((x for x in select_tracks(dash, Options()) if x.kind == kind), None)
        return t.urls if t else self.ctx["track"].urls

    def run(self) -> None:
        try:
            temp_dir = self.out_dir / ".bili_audio_tmp"
            temp = temp_dir / f"{self.ctx['bvid']}_{self.ctx['cid']}.{self.ctx['track'].ext}"
            self.log.emit(f"下载整轨到临时文件：{temp}")
            last = [0.0]

            def cb(done: int, total: int, speed: float) -> None:
                import time

                now = time.time()
                eta = (total - done) / speed if speed > 0 else 0.0
                self.progress.emit(done, total, speed, eta)
                if now - last[0] < 2 and done < total:
                    return
                last[0] = now
                pct = done / total * 100 if total else 0
                self.log.emit(
                    f"  {pct:5.1f}%  {human_size(done)}/{human_size(total)}  "
                    f"{speed / 1048576:.2f} MB/s  剩余 {_fmt(eta)}"
                )

            download(self.ctx["track"].urls, temp, threads=4, refresh=self._refresh, progress=cb, replace=False)
            self.log.emit("解码并分析静音点…")
            pcm = splitmod.decode_analysis_pcm(str(temp))
            peaks = splitmod.waveform_peaks(pcm, 2000)
            gaps = splitmod.detect_silence(pcm, threshold_db=self.threshold, min_silence=self.min_silence)
            points = splitmod.splits_from_gaps(gaps, self.ctx["duration"], min_gap=self.min_gap)
            self.log.emit(f"检测到 {len(gaps)} 段静音，{len(points)} 个切点")
            self.done.emit(peaks, points, str(temp))
        except Exception as e:
            self.failed.emit(f"{e}\n{traceback.format_exc(limit=2)}")


class SplitExportWorker(QThread):
    log = Signal(str)
    progress = Signal(int, int)
    done = Signal(int, str)
    failed = Signal(str)

    def __init__(self, ctx: dict, segments, out_parent: Path, source: str, local: bool,
                 titles: bool, keep_temp: bool, fmt: str = "source", preset: str = "medium",
                 album: str = "", artist: str = "", cover_path: str = ""):
        super().__init__()
        self.ctx = ctx
        self.segments = segments
        self.out_parent = out_parent
        self.source = source
        self.local = local
        self.titles = titles
        self.keep_temp = keep_temp
        self.fmt = fmt
        self.preset = preset
        self.album = album
        self.artist = artist
        self.cover_path = cover_path

    def _refresh(self) -> str:
        session, _ = auth.current_session()
        video = self.ctx["video"]
        page = self.ctx["page"]
        kind = self.ctx["track"].kind
        dash = api.get_playurl(session, video.bvid, page.cid).get("dash") or {}
        t = next((x for x in select_tracks(dash, Options()) if x.kind == kind), None)
        return t.urls[0] if t else self.source

    def run(self) -> None:
        try:
            album_name = (self.album or "").strip() or self.ctx["video"].title
            artist_name = (self.artist or "").strip() or self.ctx["video"].owner
            cover = None
            if self.cover_path:
                try:
                    cover = Path(self.cover_path).read_bytes()
                except Exception:
                    cover = None
            album_dir = self.out_parent / splitmod.safe_name(album_name)
            self.log.emit(f"导出到：{album_dir} · {formats.describe(self.fmt, self.preset)}")

            def on_progress(i: int, n: int, name: str) -> None:
                self.log.emit(f"  [{i}/{n}] {name}")
                self.progress.emit(i, n)

            results = splitmod.export_segments(
                self.source,
                self.segments,
                album_dir,
                album=album_name,
                artist=artist_name,
                ext=self.ctx["track"].ext,
                fmt=self.fmt,
                preset=self.preset,
                headers=None if self.local else {"Referer": REFERER, "User-Agent": UA},
                titles=self.titles,
                refresh=None if self.local else self._refresh,
                progress=on_progress,
                cover=cover,
            )
            if not self.keep_temp and self.local:
                p = Path(self.source)
                p.unlink(missing_ok=True)
                for suffix in (".part.json", ".part"):
                    p.with_name(p.name + suffix).unlink(missing_ok=True)
            self.done.emit(len(results), str(album_dir))
        except Exception as e:
            self.failed.emit(f"{e}\n{traceback.format_exc(limit=2)}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1080, 820)
        self.video = None
        self.tracks = []
        self.worker = None
        self.last_report = None

        self.split_ctx = None
        self.split_temp: str | None = None
        self.split_segments = []
        self.split_mode = "silence"
        self.split_analyzed = False
        self._cfg = load_config()

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        top = QHBoxLayout()
        self.btn_home = QPushButton("作者主页")
        self.btn_home.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl("https://space.bilibili.com/521582772?spm_id_from=333.788.0.0")
            )
        )
        self.account = QLabel("未登录")
        self.btn_login = QPushButton("扫码登录")
        self.btn_login.clicked.connect(self.login)
        top.addWidget(self.btn_home)
        top.addWidget(QLabel("账号:"))
        top.addWidget(self.account, 1)
        top.addWidget(self.btn_login)
        outer.addLayout(top)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self.tabs.addTab(self._build_download_tab(), "下载无损音频")
        self.tabs.addTab(self._build_check_tab(), "音质检测 / 打分")
        self.tabs.addTab(self._build_split_tab(), "自动分轨")

        self._load_account()

    # ---------------- 下载页 ----------------
    def _build_download_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        row = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("粘贴视频链接 / BV 号，支持 b23.tv 短链")
        self.btn_parse = QPushButton("解析")
        self.btn_parse.clicked.connect(self.parse)
        row.addWidget(self.url, 1)
        row.addWidget(self._paste_button(self.url))
        row.addWidget(self.btn_parse)
        layout.addLayout(row)

        self.title = QLabel("")
        self.title.setWordWrap(True)
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.title)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["选择", "音轨", "音轨 ID", "码率", "编码"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setMaximumHeight(140)
        layout.addWidget(self.table)

        opts_box = QFormLayout()
        line = QHBoxLayout()
        self.out = QLineEdit(str(Path(self._cfg.get("last_out") or DEFAULT_DOWNLOAD_DIR)))
        pick = QPushButton("选择…")
        pick.clicked.connect(self.pick_dir)
        line.addWidget(self.out, 1)
        line.addWidget(self._paste_button(self.out))
        line.addWidget(pick)
        opts_box.addRow("输出目录", line)

        line2 = QHBoxLayout()
        self.pages = QLineEdit("all")
        self.pages.setMaximumWidth(150)
        self.threads = QComboBox()
        self.threads.addItems(["2", "4", "8", "12"])
        self.threads.setCurrentText("4")
        self.cb_atmos = QCheckBox("同时下载杜比全景声")
        self.cb_verify = QCheckBox("核验真无损")
        self.cb_verify.setChecked(True)
        self.cb_tag = QCheckBox("写入标签/封面")
        self.cb_tag.setChecked(True)
        line2.addWidget(QLabel("分 P"))
        line2.addWidget(self.pages)
        line2.addWidget(QLabel("线程"))
        line2.addWidget(self.threads)
        line2.addWidget(self.cb_atmos)
        line2.addWidget(self.cb_verify)
        line2.addWidget(self.cb_tag)
        line2.addStretch(1)
        opts_box.addRow("下载设置", line2)

        fmt_row = QHBoxLayout()
        self.fmt_box = QComboBox()
        for fid in ("flac", "ipod", "alac", "wav", "mp3", "aac"):
            self.fmt_box.addItem(formats.FORMATS[fid].label, fid)
        self.fmt_box.currentIndexChanged.connect(self._fmt_changed)
        self.preset_box = QComboBox()
        fmt_row.addWidget(QLabel("输出格式"))
        fmt_row.addWidget(self.fmt_box)
        fmt_row.addWidget(QLabel("参数"))
        fmt_row.addWidget(self.preset_box)
        self.fmt_hint = QLabel("")
        self.fmt_hint.setStyleSheet("color: #64748b;")
        fmt_row.addWidget(self.fmt_hint, 1)
        opts_box.addRow("格式 / 参数", fmt_row)
        self._fmt_changed()
        layout.addLayout(opts_box)

        self.btn_download = QPushButton("开始下载")
        self.btn_download.setEnabled(False)
        self.btn_download.setMinimumHeight(38)
        self.btn_download.clicked.connect(self.download)
        layout.addWidget(self.btn_download)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setVisible(False)
        self.progress_label = QLabel("")
        self.progress_label.setMinimumWidth(240)
        prow = QHBoxLayout()
        prow.addWidget(self.progress, 1)
        prow.addWidget(self.progress_label)
        layout.addLayout(prow)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)
        return w

    # ---------------- 检测页 ----------------
    def _build_check_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        row = QHBoxLayout()
        self.check_url = QLineEdit()
        self.check_url.setPlaceholderText("粘贴视频链接，检测音源质量并打分（不会下载整首歌）")
        self.btn_check = QPushButton("开始检测")
        self.btn_check.setMinimumHeight(34)
        self.btn_check.clicked.connect(self.check_quality)
        row.addWidget(self.check_url, 1)
        row.addWidget(self._paste_button(self.check_url))
        row.addWidget(self.btn_check)
        layout.addLayout(row)

        cfg = QHBoxLayout()
        cfg.addWidget(QLabel("分段数"))
        self.seg_count = QComboBox()
        self.seg_count.addItems(["4", "6", "8", "12"])
        self.seg_count.setCurrentText("8")
        cfg.addWidget(self.seg_count)
        self.btn_save_json = QPushButton("保存 JSON")
        self.btn_save_json.setEnabled(False)
        self.btn_save_json.clicked.connect(self.save_json)
        cfg.addWidget(self.btn_save_json)
        cfg.addStretch(1)
        layout.addLayout(cfg)

        score_row = QHBoxLayout()
        self.score_label = QLabel("—")
        self.score_label.setStyleSheet("font-size: 40px; font-weight: 700; color: #888;")
        self.grade_label = QLabel("粘贴链接后点“开始检测”")
        self.grade_label.setWordWrap(True)
        score_row.addWidget(self.score_label)
        score_row.addWidget(self.grade_label, 1)
        layout.addLayout(score_row)

        self.check_result = QPlainTextEdit()
        self.check_result.setReadOnly(True)
        layout.addWidget(self.check_result, 1)
        return w

    # ---------------- 分轨页 ----------------
    def _build_split_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        row = QHBoxLayout()
        self.split_url = QLineEdit()
        self.split_url.setPlaceholderText("粘贴整轨专辑 / 合集视频链接")
        self.btn_load_split = QPushButton("载入音轨")
        self.btn_load_split.clicked.connect(self.load_split_track)
        row.addWidget(self.split_url, 1)
        row.addWidget(self._paste_button(self.split_url))
        row.addWidget(self.btn_load_split)
        layout.addLayout(row)

        self.split_info = QLabel("先载入音轨。时间列表模式不需要下载；静音检测模式会把整轨下载到临时目录，导出后自动删除。")
        self.split_info.setWordWrap(True)
        layout.addWidget(self.split_info)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("分轨方式"))
        self.split_mode_box = QComboBox()
        self.split_mode_box.addItems(["静音检测（显示波形）", "时间列表（粘贴曲目文本）"])
        self.split_mode_box.currentIndexChanged.connect(self._split_mode_changed)
        mode_row.addWidget(self.split_mode_box)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        self.split_stack = QStackedWidget()
        self.split_stack.addWidget(self._build_silence_page())
        self.split_stack.addWidget(self._build_list_page())
        layout.addWidget(self.split_stack, 1)

        exp = QHBoxLayout()
        self.split_out = QLineEdit(str(Path(self._cfg.get("last_split_out") or DEFAULT_DOWNLOAD_DIR)))
        pick = QPushButton("选择…")
        pick.clicked.connect(self._pick_split_dir)
        self.cb_keep_temp = QCheckBox("保留临时整轨")
        exp.addWidget(QLabel("导出到"))
        exp.addWidget(self.split_out, 1)
        exp.addWidget(self._paste_button(self.split_out))
        exp.addWidget(pick)
        exp.addWidget(self.cb_keep_temp)
        layout.addLayout(exp)

        fmt_row = QHBoxLayout()
        self.split_fmt = QComboBox()
        self.split_fmt.addItem("跟随源格式（推荐）", "source")
        for fid in ("flac", "ipod", "alac", "wav", "mp3", "aac"):
            self.split_fmt.addItem(formats.FORMATS[fid].label, fid)
        self.split_fmt.currentIndexChanged.connect(self._split_fmt_changed)
        self.split_preset = QComboBox()
        fmt_row.addWidget(QLabel("导出格式"))
        fmt_row.addWidget(self.split_fmt)
        fmt_row.addWidget(QLabel("参数"))
        fmt_row.addWidget(self.split_preset)
        self.split_fmt_hint = QLabel("")
        self.split_fmt_hint.setStyleSheet("color: #64748b;")
        fmt_row.addWidget(self.split_fmt_hint, 1)
        layout.addLayout(fmt_row)
        self._split_fmt_changed()

        meta = QHBoxLayout()
        self.split_artist = QLineEdit()
        self.split_artist.setPlaceholderText("默认：UP 主名")
        self.split_album = QLineEdit()
        self.split_album.setPlaceholderText("默认：视频标题")
        meta.addWidget(QLabel("歌手"))
        meta.addWidget(self.split_artist, 1)
        meta.addWidget(QLabel("专辑"))
        meta.addWidget(self.split_album, 1)
        layout.addLayout(meta)

        cover_row = QHBoxLayout()
        self.split_cover = QLineEdit()
        self.split_cover.setReadOnly(True)
        self.split_cover.setPlaceholderText("可选：给每首分轨嵌入封面")
        btn_cover = QPushButton("选择封面…")
        btn_cover.clicked.connect(self._pick_cover)
        btn_clear = QPushButton("清除")
        btn_clear.clicked.connect(lambda: self.split_cover.clear())
        cover_row.addWidget(QLabel("封面"))
        cover_row.addWidget(self.split_cover, 1)
        cover_row.addWidget(btn_cover)
        cover_row.addWidget(btn_clear)
        layout.addLayout(cover_row)

        self.btn_export = QPushButton("导出分轨")
        self.btn_export.setMinimumHeight(38)
        self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(self.export_split)
        layout.addWidget(self.btn_export)

        self.split_progress = QProgressBar()
        self.split_progress.setRange(0, 1000)
        self.split_progress.setVisible(False)
        self.split_progress_label = QLabel("")
        self.split_progress_label.setMinimumWidth(240)
        sprow = QHBoxLayout()
        sprow.addWidget(self.split_progress, 1)
        sprow.addWidget(self.split_progress_label)
        layout.addLayout(sprow)

        self.split_log = QPlainTextEdit()
        self.split_log.setReadOnly(True)
        self.split_log.setMaximumHeight(120)
        layout.addWidget(self.split_log)
        return w

    def _build_silence_page(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)

        ctl = QHBoxLayout()
        ctl.addWidget(QLabel("静音阈值"))
        self.sp_threshold = QDoubleSpinBox()
        self.sp_threshold.setRange(-60, -10)
        self.sp_threshold.setValue(-35)
        self.sp_threshold.setSuffix(" dB")
        ctl.addWidget(self.sp_threshold)
        ctl.addWidget(QLabel("最短静音"))
        self.sp_min_sil = QDoubleSpinBox()
        self.sp_min_sil.setRange(0.3, 10.0)
        self.sp_min_sil.setSingleStep(0.1)
        self.sp_min_sil.setValue(1.5)
        self.sp_min_sil.setSuffix(" 秒")
        ctl.addWidget(self.sp_min_sil)
        self.btn_analyze = QPushButton("检测静音并显示波形")
        self.btn_analyze.clicked.connect(self.analyze_silence)
        self.btn_analyze.setEnabled(False)
        ctl.addWidget(self.btn_analyze)
        ctl.addStretch(1)
        layout.addLayout(ctl)

        self.wave = WaveformView()
        self.wave.markersChanged.connect(self._markers_changed)
        layout.addWidget(self.wave)

        btns = QHBoxLayout()
        self.btn_add_mark = QPushButton("在最大空白处加分割点")
        self.btn_add_mark.clicked.connect(self.wave.add_marker)
        self.btn_del_mark = QPushButton("删除选中")
        self.btn_del_mark.clicked.connect(self.wave.remove_selected)
        self.btn_clear_mark = QPushButton("清空切点")
        self.btn_clear_mark.clicked.connect(lambda: self.wave.set_markers([]))
        btns.addWidget(self.btn_add_mark)
        btns.addWidget(self.btn_del_mark)
        btns.addWidget(self.btn_clear_mark)
        btns.addWidget(QLabel("提示：拖动橙色标记可微调；选中后按 Delete 也能删除"))
        btns.addStretch(1)
        layout.addLayout(btns)

        self.split_table = QTableWidget(0, 5)
        self.split_table.setHorizontalHeaderLabels(["序号", "歌名", "开始", "结束", "时长"])
        self.split_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.split_table.verticalHeader().setVisible(False)
        self.split_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.split_table, 1)
        return w

    def _build_list_page(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)

        self.list_text = QPlainTextEdit()
        self.list_text.setPlaceholderText(
            "把简介/评论里的曲目表整段粘贴进来，例如：\n"
            "1. 忘れられないの  0:00\n"
            "2. マッチとピーナッツ  3:58\n"
            "3. 陽炎  8:28\n"
            "软件会自动识别序号、歌名和时间，不要求格式统一。"
        )
        self.list_text.setMinimumHeight(140)
        layout.addWidget(self.list_text)

        row = QHBoxLayout()
        self.btn_parse_list = QPushButton("识别曲目")
        self.btn_parse_list.clicked.connect(self.parse_list_text)
        self.cb_use_titles = QCheckBox("用识别到的歌名命名文件")
        self.cb_use_titles.setChecked(True)
        row.addWidget(self.btn_parse_list)
        row.addWidget(self._paste_button(self.list_text, insert=True))
        row.addWidget(self.cb_use_titles)
        row.addStretch(1)
        layout.addLayout(row)

        self.list_table = QTableWidget(0, 5)
        self.list_table.setHorizontalHeaderLabels(["序号", "歌名", "开始", "结束", "时长"])
        self.list_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.list_table.verticalHeader().setVisible(False)
        self.list_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.list_table, 1)
        return w

    def _fmt_changed(self) -> None:
        fid = self.fmt_box.currentData() or "flac"
        self.preset_box.blockSignals(True)
        self.preset_box.clear()
        for p in formats.preset_choices(fid):
            self.preset_box.addItem(f"{p.label} — {p.desc}", p.id)
        for i in range(self.preset_box.count()):
            if self.preset_box.itemData(i) == "medium":
                self.preset_box.setCurrentIndex(i)
                break
        self.preset_box.blockSignals(False)
        self.preset_box.setEnabled(self.preset_box.count() > 1)
        self._update_fmt_hint()

    def _update_fmt_hint(self) -> None:
        if not hasattr(self, "fmt_hint"):
            return
        fid = self.fmt_box.currentData() or "flac"
        self.fmt_hint.setText("无损" if formats.FORMATS[fid].lossless else "有损")

    # ---------------- 通用 ----------------
    def _opts(self) -> Options:
        return Options(
            out=Path(self.out.text().strip() or DEFAULT_DOWNLOAD_DIR),
            atmos=self.cb_atmos.isChecked(),
            aac_fallback=True,
            verify=self.cb_verify.isChecked(),
            tag=self.cb_tag.isChecked(),
            threads=int(self.threads.currentText()),
            pages=self.pages.text().strip() or "all",
            fmt=self.fmt_box.currentData() or "flac",
            preset=self.preset_box.currentData() or "medium",
        )

    def _load_account(self) -> None:
        _, account = auth.current_session()
        if account:
            vip = f"大会员（到期 {account['vip_due']}）" if account["vip"] else "非大会员"
            self.account.setText(f"{account['uname']} · {vip}")
            self.btn_login.setText("重新登录")
        else:
            self.account.setText("未登录（只能看 AAC，无法检测无损音轨）")

    def login(self) -> None:
        dlg = LoginDialog(self)
        if dlg.exec():
            self._load_account()
            self.log.appendPlainText("登录成功")
            self.check_result.appendPlainText("登录成功")

    def pick_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择输出目录", self.out.text())
        if d:
            self.out.setText(d)
            self._save_state()

    def _save_state(self) -> None:
        cfg = load_config()
        if self.out.text().strip():
            cfg["last_out"] = self.out.text().strip()
        if self.split_out.text().strip():
            cfg["last_split_out"] = self.split_out.text().strip()
        save_config(cfg)

    def closeEvent(self, event) -> None:
        self._save_state()
        super().closeEvent(event)

    def _paste_button(self, target, insert: bool = False) -> QPushButton:
        btn = QPushButton("从剪贴板粘贴")
        btn.setToolTip("把剪贴板内容粘贴到左侧输入框")

        def do_paste() -> None:
            text = QApplication.clipboard().text()
            if not text:
                return
            if insert and isinstance(target, QPlainTextEdit):
                target.insertPlainText(text)
            else:
                target.setText(text)

        btn.clicked.connect(do_paste)
        return btn

    def _split_fmt_changed(self) -> None:
        fid = self.split_fmt.currentData() or "source"
        self.split_preset.blockSignals(True)
        self.split_preset.clear()
        for p in formats.preset_choices(fid):
            self.split_preset.addItem(f"{p.label} — {p.desc}", p.id)
        for i in range(self.split_preset.count()):
            if self.split_preset.itemData(i) == "medium":
                self.split_preset.setCurrentIndex(i)
                break
        self.split_preset.blockSignals(False)
        self.split_preset.setEnabled(self.split_preset.count() > 1)
        self.split_fmt_hint.setText(
            "跟随源：FLAC 无损重编码，AAC/E-AC3 直接复制" if fid == "source"
            else ("无损" if formats.FORMATS[fid].lossless else "有损")
        )

    def _pick_split_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择导出目录", self.split_out.text())
        if d:
            self.split_out.setText(d)
            self._save_state()

    def _pick_cover(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择封面图片", "",
            "图片 (*.jpg *.jpeg *.png *.webp *.bmp);;所有文件 (*.*)",
        )
        if path:
            self.split_cover.setText(path)

    # ---------------- 解析 / 下载 ----------------
    def parse(self) -> None:
        url = self.url.text().strip()
        if not url:
            return
        self.btn_parse.setEnabled(False)
        self.table.setRowCount(0)
        self.title.setText("解析中…")
        self.worker = ParseWorker(url, self._opts())
        self.worker.done.connect(self._parsed)
        self.worker.failed.connect(self._parse_failed)
        self.worker.start()

    def _parsed(self, video, tracks, vid) -> None:
        self.btn_parse.setEnabled(True)
        self.video = video
        self.tracks = tracks
        self.pages.setPlaceholderText(f"1-{len(video.pages)}")
        self.title.setText(f"{video.title}\n{video.bvid} · {video.owner} · {len(video.pages)}P")
        chosen, note = choose_tracks(tracks, self._opts())
        self.table.setRowCount(len(tracks))
        for i, t in enumerate(tracks):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Checked if t in chosen else Qt.Unchecked)
            self.table.setItem(i, 0, chk)
            for col, val in enumerate([t.label, str(t.id), f"{t.kbps} kb/s", t.codec], start=1):
                self.table.setItem(i, col, QTableWidgetItem(val))
        if note:
            self.log.appendPlainText(f"! {note}")
        self.btn_download.setEnabled(bool(chosen))

    def _parse_failed(self, msg: str) -> None:
        self.btn_parse.setEnabled(True)
        self.log.appendPlainText(f"✗ {msg}")
        QMessageBox.warning(self, "出错了", msg)

    def download(self) -> None:
        url = self.url.text().strip()
        if not url:
            return
        self._save_state()
        self.btn_download.setEnabled(False)
        self.btn_parse.setEnabled(False)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.progress_label.setText("准备中…")
        self.log.appendPlainText("—— 开始 ——")
        self.worker = DownloadWorker(url, self._opts())
        self.worker.log.connect(self.log.appendPlainText)
        self.worker.progress.connect(self._download_progress)
        self.worker.finished_ok.connect(self._download_done)
        self.worker.failed.connect(self._download_failed)
        self.worker.start()

    def _download_progress(self, done: int, total: int, speed: float, eta: float) -> None:
        if total:
            self.progress.setValue(int(done / total * 1000))
        self.progress_label.setText(
            f"{speed / 1048576:.2f} MB/s · 剩余 {_fmt(eta)} · "
            f"{human_size(done)}/{human_size(total)}"
        )

    def _done_dialog(self, title: str, text: str, folder: str) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Information)
        box.setText(text)
        open_btn = box.addButton("打开文件夹", QMessageBox.ActionRole)
        box.addButton("确定", QMessageBox.AcceptRole)
        box.exec()
        if box.clickedButton() is open_btn:
            try:
                if folder and Path(folder).exists():
                    os.startfile(folder)
                else:
                    QMessageBox.warning(self, "无法打开", f"文件夹不存在：\n{folder}")
            except Exception as e:
                QMessageBox.warning(self, "无法打开", str(e))

    def _download_done(self, n: int, folder: str) -> None:
        self.progress.setVisible(False)
        self.progress_label.setText("")
        self.btn_download.setEnabled(True)
        self.btn_parse.setEnabled(True)
        self.log.appendPlainText(f"—— 完成，共 {n} 个音频 ——")
        self._done_dialog("完成", f"已下载 {n} 个音频", folder)

    def _download_failed(self, msg: str) -> None:
        self.progress.setVisible(False)
        self.progress_label.setText("")
        self.btn_download.setEnabled(True)
        self.btn_parse.setEnabled(True)
        self.log.appendPlainText(f"✗ {msg}")
        QMessageBox.critical(self, "下载失败", msg)

    # ---------------- 检测 ----------------
    def check_quality(self) -> None:
        url = self.check_url.text().strip()
        if not url:
            return
        self.btn_check.setEnabled(False)
        self.score_label.setText("…")
        self.score_label.setStyleSheet("font-size: 40px; font-weight: 700; color: #888;")
        self.grade_label.setText("正在远程采样分析，请稍候（不会下载整首歌）")
        self.check_result.setPlainText("")
        self.last_report = None
        self.worker = ScoreWorker(url, int(self.seg_count.currentText()))
        self.worker.finished_ok.connect(self._check_done)
        self.worker.failed.connect(self._check_failed)
        self.worker.start()

    def _check_done(self, report) -> None:
        self.btn_check.setEnabled(True)
        self.last_report = report
        self.btn_save_json.setEnabled(True)
        self.score_label.setText(f"{report.score}")
        if report.score >= 80:
            color = "#16a34a"
        elif report.score >= 60:
            color = "#d97706"
        else:
            color = "#dc2626"
        self.score_label.setStyleSheet(f"font-size: 40px; font-weight: 700; color: {color};")
        self.grade_label.setText(f"{report.grade}\n{report.summary}")
        self.check_result.setPlainText(quality.format_report(report))

    def _check_failed(self, msg: str) -> None:
        self.btn_check.setEnabled(True)
        self.score_label.setText("—")
        self.grade_label.setText("检测失败")
        self.check_result.setPlainText(msg)
        QMessageBox.warning(self, "检测失败", msg)

    def save_json(self) -> None:
        if not self.last_report:
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存检测报告", f"{self.last_report.bvid}.json", "JSON (*.json)")
        if not path:
            return
        Path(path).write_text(json.dumps(self.last_report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        QMessageBox.information(self, "已保存", path)

    # ---------------- 分轨 ----------------
    def _split_mode_changed(self, idx: int) -> None:
        self.split_mode = "silence" if idx == 0 else "list"
        self.split_stack.setCurrentIndex(idx)
        self._update_export_state()

    def load_split_track(self) -> None:
        url = self.split_url.text().strip()
        if not url:
            return
        self.btn_load_split.setEnabled(False)
        self.split_info.setText("载入中…")
        self.worker = LoadSplitWorker(url)
        self.worker.done.connect(self._split_loaded)
        self.worker.failed.connect(self._split_load_failed)
        self.worker.start()

    def _split_loaded(self, ctx: dict) -> None:
        self.btn_load_split.setEnabled(True)
        self.split_ctx = ctx
        self.split_temp = None
        self.split_segments = []
        self.split_analyzed = False
        self.split_artist.setText(ctx["video"].owner)
        self.split_album.setText(ctx["video"].title)
        self.split_cover.clear()
        self.wave.set_data([], ctx["duration"], [])
        self.split_table.setRowCount(0)
        self.list_table.setRowCount(0)
        t = ctx["track"]
        self.split_info.setText(
            f"已载入：{ctx['video'].title}\n"
            f"{t.label} · {t.kbps} kb/s · {_fmt(ctx['duration'])} · {ctx['video'].owner}"
        )
        self.btn_analyze.setEnabled(True)
        self.btn_export.setEnabled(bool(self.split_segments) or bool(self.wave.markers))
        self.split_log.appendPlainText("音轨已就绪。时间列表模式可直接识别后导出；静音检测模式先分析。")

    def _split_load_failed(self, msg: str) -> None:
        self.btn_load_split.setEnabled(True)
        self.split_info.setText("载入失败")
        QMessageBox.warning(self, "载入失败", msg)

    def analyze_silence(self) -> None:
        if not self.split_ctx:
            return
        out = Path(self.split_out.text().strip() or DEFAULT_DOWNLOAD_DIR)
        self.btn_analyze.setEnabled(False)
        self.btn_export.setEnabled(False)
        self.split_progress.setValue(0)
        self.split_progress.setVisible(True)
        self.split_progress_label.setText("准备中…")
        self.split_log.appendPlainText("—— 开始分析 ——")
        self.worker = AnalyzeWorker(
            self.split_ctx, out,
            self.sp_threshold.value(), self.sp_min_sil.value(), 10.0,
        )
        self.worker.log.connect(self.split_log.appendPlainText)
        self.worker.progress.connect(self._split_download_progress)
        self.worker.done.connect(self._analyze_done)
        self.worker.failed.connect(self._analyze_failed)
        self.worker.start()

    def _split_download_progress(self, done: int, total: int, speed: float, eta: float) -> None:
        if total:
            self.split_progress.setValue(int(done / total * 1000))
        self.split_progress_label.setText(
            f"{speed / 1048576:.2f} MB/s · 剩余 {_fmt(eta)} · "
            f"{human_size(done)}/{human_size(total)}"
        )

    def _analyze_done(self, peaks, points, temp: str) -> None:
        self.btn_analyze.setEnabled(True)
        self.split_progress.setVisible(False)
        self.split_progress_label.setText("")
        self.split_temp = temp
        self.split_analyzed = True
        self.wave.set_data(peaks, self.split_ctx["duration"], points)
        self.split_log.appendPlainText(f"分析完成：{len(points) + 1} 段。可拖动标记微调。")
        self._rebuild_silence_table()
        self._update_export_state()

    def _analyze_failed(self, msg: str) -> None:
        self.btn_analyze.setEnabled(True)
        self.split_progress.setVisible(False)
        self.split_progress_label.setText("")
        self.split_log.appendPlainText(f"✗ {msg}")
        QMessageBox.critical(self, "分析失败", msg)

    def _markers_changed(self) -> None:
        if self.split_mode == "silence":
            self._rebuild_silence_table()
        self._update_export_state()

    def _rebuild_silence_table(self) -> None:
        if not self.split_ctx:
            return
        segs = splitmod.segments_from_points(self.wave.markers, self.split_ctx["duration"])
        self.split_segments = segs
        self._fill_table(self.split_table, segs)

    def parse_list_text(self) -> None:
        if not self.split_ctx:
            QMessageBox.information(self, "提示", "请先载入音轨")
            return
        result = parse_tracklist(self.list_text.toPlainText(), self.split_ctx["duration"])
        if not result.tracks:
            QMessageBox.warning(self, "识别失败", "；".join(result.warnings) or "没有识别到曲目")
            return
        segs = splitmod.segments_from_tracks(result.tracks, self.split_ctx["duration"])
        self.split_segments = segs
        self._fill_table(self.list_table, segs)
        for w in result.warnings:
            self.split_log.appendPlainText(f"! {w}")
        self.split_log.appendPlainText(f"识别到 {len(segs)} 首，总时长 {_fmt(self.split_ctx['duration'])}")
        if self.wave.peaks:
            self.wave.set_markers([t.start for t in result.tracks[1:]])
        self._update_export_state()

    def _fill_table(self, table: QTableWidget, segs) -> None:
        table.setRowCount(len(segs))
        for i, s in enumerate(segs):
            vals = [f"{s.index:02d}", s.title or "—", _fmt(s.start), _fmt(s.end), _fmt(s.duration)]
            for col, v in enumerate(vals):
                table.setItem(i, col, QTableWidgetItem(v))

    def _update_export_state(self) -> None:
        if not self.split_ctx:
            self.btn_export.setEnabled(False)
            return
        if self.split_mode == "list":
            self.btn_export.setEnabled(bool(self.split_segments))
        else:
            self.btn_export.setEnabled(bool(self.split_analyzed))

    def export_split(self) -> None:
        if not self.split_ctx or not self.split_segments:
            return
        self._save_state()
        out = Path(self.split_out.text().strip() or DEFAULT_DOWNLOAD_DIR)
        local = bool(self.split_temp and Path(self.split_temp).exists())
        source = self.split_temp if local else self.split_ctx["track"].urls[0]
        titles = self.split_mode == "list" and self.cb_use_titles.isChecked()
        self.btn_export.setEnabled(False)
        self.split_progress.setValue(0)
        self.split_progress.setVisible(True)
        self.split_progress_label.setText("导出中…")
        self.split_log.appendPlainText("—— 开始导出 ——")
        self.worker = SplitExportWorker(
            self.split_ctx, self.split_segments, out, source, local, titles,
            self.cb_keep_temp.isChecked(),
            self.split_fmt.currentData() or "source",
            self.split_preset.currentData() or "medium",
            self.split_album.text().strip(),
            self.split_artist.text().strip(),
            self.split_cover.text().strip(),
        )
        self.worker.log.connect(self.split_log.appendPlainText)
        self.worker.progress.connect(self._export_progress)
        self.worker.done.connect(self._export_done)
        self.worker.failed.connect(self._export_failed)
        self.worker.start()

    def _export_progress(self, i: int, n: int) -> None:
        if n:
            self.split_progress.setValue(int(i / n * 1000))
        self.split_progress_label.setText(f"正在导出 {i}/{n}")

    def _export_done(self, n: int, folder: str) -> None:
        self.split_progress.setVisible(False)
        self.split_progress_label.setText("")
        self.btn_export.setEnabled(True)
        self.split_log.appendPlainText(f"—— 完成，共 {n} 个文件 ——")
        self._done_dialog("导出完成", f"已导出 {n} 个文件", folder)

    def _export_failed(self, msg: str) -> None:
        self.split_progress.setVisible(False)
        self.split_progress_label.setText("")
        self.btn_export.setEnabled(True)
        self.split_log.appendPlainText(f"✗ {msg}")
        QMessageBox.critical(self, "导出失败", msg)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    w = MainWindow()
    w.show()
    return app.exec()
