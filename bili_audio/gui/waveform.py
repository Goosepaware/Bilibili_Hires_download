"""波形控件：显示整轨波形、分割点，可拖动调整。"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

BG = QColor("#0f172a")
GRID = QColor("#1e293b")
WAVE = QColor("#22c55e")
WAVE_SOFT = QColor("#16a34a")
MARK = QColor("#f59e0b")
MARK_SEL = QColor("#f43f5e")
TEXT = QColor("#94a3b8")
TEXT_HI = QColor("#e2e8f0")

LEFT = 10
RIGHT = 10
TOP = 22
BOTTOM = 24


class WaveformView(QWidget):
    markersChanged = Signal()
    selectionChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(190)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.peaks: list[tuple[float, float]] = []
        self.duration = 0.0
        self.markers: list[float] = []
        self.selected = -1
        self._drag = False

    # ---------- 数据 ----------
    def set_data(self, peaks, duration: float, markers: list[float] | None = None) -> None:
        self.peaks = peaks
        self.duration = max(0.001, duration)
        self.markers = list(markers or [])
        self.selected = -1
        self.update()

    def set_markers(self, markers: list[float]) -> None:
        self.markers = list(markers)
        self.selected = -1
        self.update()
        self.markersChanged.emit()

    def add_marker(self) -> None:
        if self.duration <= 0:
            return
        pts = [0.0] + sorted(self.markers) + [self.duration]
        gaps = [(pts[i + 1] - pts[i], pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
        gap, a, b = max(gaps)
        if gap < 5:
            return
        self.markers.append((a + b) / 2)
        self.markers.sort()
        self.selected = self.markers.index((a + b) / 2)
        self.update()
        self.markersChanged.emit()
        self.selectionChanged.emit(self.selected)

    def remove_selected(self) -> None:
        if 0 <= self.selected < len(self.markers):
            self.markers.pop(self.selected)
            self.selected = -1
            self.update()
            self.markersChanged.emit()
            self.selectionChanged.emit(-1)

    # ---------- 坐标 ----------
    def _w(self) -> float:
        return max(1.0, self.width() - LEFT - RIGHT)

    def _x(self, t: float) -> float:
        return LEFT + min(1.0, max(0.0, t / self.duration)) * self._w()

    def _t(self, x: float) -> float:
        return min(1.0, max(0.0, (x - LEFT) / self._w())) * self.duration

    # ---------- 绘制 ----------
    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.fillRect(self.rect(), BG)
        h = self.height()
        mid = TOP + (h - TOP - BOTTOM) / 2
        amp = max(10.0, (h - TOP - BOTTOM) / 2 - 4)

        p.setPen(QPen(GRID, 1))
        p.drawLine(LEFT, int(mid), self.width() - RIGHT, int(mid))

        if not self.peaks:
            p.setPen(QPen(TEXT, 1, Qt.DashLine))
            p.drawLine(LEFT, int(mid), self.width() - RIGHT, int(mid))
            p.setPen(TEXT)
            p.setFont(QFont("Microsoft YaHei UI", 9))
            p.drawText(QRectF(0, mid - 18, self.width(), 20), Qt.AlignCenter,
                       "载入音轨并分析后显示波形；也可直接切换到「时间列表」模式")
        else:
            n = len(self.peaks)
            p.setPen(QPen(WAVE, 1))
            for i, (lo, hi) in enumerate(self.peaks):
                x = LEFT + i / max(1, n - 1) * self._w()
                y1 = mid - hi * amp
                y2 = mid - lo * amp
                p.drawLine(int(x), int(y1), int(x), int(y2))

        # 刻度
        p.setPen(TEXT)
        p.setFont(QFont("Consolas", 8))
        ticks = 8 if self.width() > 700 else 4
        for i in range(ticks + 1):
            t = self.duration * i / ticks
            x = self._x(t)
            p.setPen(QPen(GRID, 1))
            p.drawLine(int(x), int(mid + amp + 2), int(x), int(mid + amp + 6))
            p.setPen(TEXT)
            text = _fmt(t)
            p.drawText(int(x) - 22, int(mid + amp + 20), 46, 14, Qt.AlignCenter, text)

        # 分割点
        p.setFont(QFont("Microsoft YaHei UI", 8))
        for i, t in enumerate(self.markers):
            x = self._x(t)
            color = MARK_SEL if i == self.selected else MARK
            p.setPen(QPen(color, 2 if i == self.selected else 1))
            p.drawLine(int(x), TOP - 4, int(x), int(mid + amp))
            p.setBrush(color)
            p.setPen(Qt.NoPen)
            p.drawPolygon([
                _pt(int(x) - 5, TOP - 10),
                _pt(int(x) + 5, TOP - 10),
                _pt(int(x), TOP - 3),
            ])
            p.setPen(QColor("#0b1220") if i == self.selected else QColor("#111827"))
            p.drawText(int(x) - 10, 2, 20, 12, Qt.AlignCenter, f"{i + 1:02d}")

        # 导出后每段序号（顶部小字）
        if self.markers:
            p.setPen(TEXT_HI)
            starts = [0.0] + sorted(self.markers)
            for i in range(len(starts)):
                a = starts[i]
                b = starts[i + 1] if i + 1 < len(starts) else self.duration
                if (b - a) > self.duration / 40:
                    cx = (self._x(a) + self._x(b)) / 2
                    p.drawText(QRectF(cx - 24, TOP + 2, 48, 14), Qt.AlignCenter, f"{i + 1:02d}  {_fmt(b - a)}")

    # ---------- 交互 ----------
    def _hit(self, x: float) -> int:
        best, idx = 9.0, -1
        for i, t in enumerate(self.markers):
            d = abs(self._x(t) - x)
            if d < best:
                best, idx = d, i
        return idx

    def mousePressEvent(self, e) -> None:
        idx = self._hit(e.position().x())
        if idx >= 0:
            self.selected = idx
            self._drag = True
            self.update()
            self.selectionChanged.emit(idx)

    def mouseMoveEvent(self, e) -> None:
        if not self._drag or self.selected < 0:
            return
        t = self._t(e.position().x())
        lo = self.markers[self.selected - 1] + 1.0 if self.selected > 0 else 1.0
        hi = self.markers[self.selected + 1] - 1.0 if self.selected + 1 < len(self.markers) else self.duration - 1.0
        self.markers[self.selected] = min(hi, max(lo, t))
        self.update()

    def mouseReleaseEvent(self, e) -> None:
        if self._drag:
            self._drag = False
            self.markersChanged.emit()

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key_Delete:
            self.remove_selected()
        else:
            super().keyPressEvent(e)


def _pt(x, y):
    from PySide6.QtCore import QPoint

    return QPoint(x, y)


def _fmt(t: float) -> str:
    t = int(max(0, t))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
