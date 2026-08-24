"""UI layer for the 套餐用量 tray monitor (PyQt5).

Tray icon, drawer-style popup panel, period cards, config dialog and the
background fetch worker. All numbers come from monitor_data; this module
never talks to the network itself (except via the FetchWorker thread).
"""

import json
import os
import time
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QProgressBar, QAction, QFrame, QPushButton,
    QDialog, QLineEdit, QFormLayout
)
from PyQt5.QtCore import (
    Qt, QTimer, QSize, QRect, QRectF, pyqtSignal, pyqtProperty, QPropertyAnimation, QEasingCurve,
    QThread
)
from PyQt5.QtNetwork import QLocalSocket, QLocalServer
from PyQt5.QtGui import (
    QIcon, QPixmap, QPainter, QPainterPath, QColor, QFont, QCursor, QPen,
    QLinearGradient, QRadialGradient, QBrush
)

from monitor_data import (
    log, CONFIG_PATH, KIMI_DEFAULT_CRED_PATH, QWEN_USAGE_CACHE_PATH,
    QWEN_CACHE_STALE_S, load_config, _qwen_cache_status, fetch_all_usage,
    calc_alert, fmt_remaining,
)

# --- Widget timing / geometry ---

REFRESH_INTERVAL_MS = 5 * 60 * 1000
AUTO_HIDE_DELAY_MS = 2500
EDGE_CHECK_MS = 300
EDGE_ZONE_HEIGHT = 6
EDGE_ZONE_WIDTH = 80

# --- Color Palette ---

C_BG          = "#0f1117"
C_BG_LIGHT    = "#181a22"
C_CARD        = "#1a1d26"
C_CARD_ALERT  = "#2a1418"
C_CARD_FAST   = "#26200e"
C_BORDER      = "#282b36"
C_BORDER_LITE = "#1e2028"
C_TEXT        = "#e2e4ec"
C_TEXT_DIM    = "#7a7e8c"
C_TEXT_MUTED  = "#4a4e5c"
C_NEON_GREEN  = "#00ff88"
C_NEON_BLUE   = "#00d4ff"
C_ALERT_RED   = "#ff3b3b"
C_GREEN_DIM   = "#0a2818"
C_RED_DIM     = "#2e0a0a"
C_ORANGE      = "#ffb347"
C_GRAD_S      = "#00d4ff"
C_GRAD_E      = "#7c3aed"

# --- Icon Generator ---

# Background gradient stops per icon level (based on available plan count):
#   red    - 0 providers have plan quota left
#   orange - 1 provider has plan quota left
#   yellow - 2 providers have plan quota left
#   blue   - all 3 providers have plan quota left
ICON_GRADIENTS = {
    "red":    [("#cc1133"), ("#ff3355"), ("#ff6688")],
    "orange": [("#ff6b35"), ("#f7931e"), ("#ffc107")],
    "yellow": [("#f5d000"), ("#ffd84d"), ("#fff099")],
    "blue":   [("#0080ff"), ("#00b4ff"), ("#4dd2ff")],
}


def make_icon_pixmap(size=64, level="blue"):
    """Coding-plan monitor icon: rounded tile with data bars + ark silhouette.

    level selects the gradient: red (0 plans left) / orange (1 plan left) /
    yellow (2 plans left) / blue (3 plans left).
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.Antialiasing)

    s = size
    pad = s * 0.10
    r = s * 0.24

    stops = ICON_GRADIENTS.get(level, ICON_GRADIENTS["blue"])
    bg_grad = QLinearGradient(0, 0, s, s)
    bg_grad.setColorAt(0, QColor(stops[0]))
    bg_grad.setColorAt(0.5, QColor(stops[1]))
    bg_grad.setColorAt(1, QColor(stops[2]))
    p.setBrush(QBrush(bg_grad))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRect(int(pad), int(pad), int(s - 2 * pad), int(s - 2 * pad)), int(r), int(r))

    cx = s / 2
    cy = s / 2

    inner_glow = QRadialGradient(cx, cy, s * 0.35)
    inner_glow.setColorAt(0, QColor(255, 255, 255, 40))
    inner_glow.setColorAt(1, QColor(255, 255, 255, 0))
    p.setBrush(QBrush(inner_glow))
    p.drawEllipse(QRect(int(pad), int(pad), int(s - 2 * pad), int(s - 2 * pad)))

    bar_area_x = pad + s * 0.16
    bar_area_w = s - 2 * pad - s * 0.32
    bar_area_y = pad + s * 0.26
    bar_area_h = s - 2 * pad - s * 0.44
    bar_w = bar_area_w / 3 - s * 0.05
    gap = s * 0.075

    white = QColor(255, 255, 255, 240)
    p.setBrush(white)
    p.setPen(Qt.NoPen)

    heights = [0.45, 0.7, 0.95]
    for i, h_frac in enumerate(heights):
        x = bar_area_x + i * (bar_w + gap)
        bh = bar_area_h * h_frac
        y = bar_area_y + bar_area_h - bh
        p.drawRoundedRect(QRect(int(x), int(y), int(bar_w), int(bh)), int(s * 0.035), int(s * 0.035))

    ark_y = pad + s * 0.70
    ark_h = s * 0.08
    ark_w = s - 2 * pad - s * 0.20
    ark_x = pad + s * 0.10

    ark_path = QPainterPath()
    ark_path.moveTo(ark_x, ark_y + ark_h)
    ark_path.quadTo(ark_x + ark_w * 0.05, ark_y, ark_x + ark_w * 0.15, ark_y)
    ark_path.lineTo(ark_x + ark_w * 0.85, ark_y)
    ark_path.quadTo(ark_x + ark_w * 0.95, ark_y, ark_x + ark_w, ark_y + ark_h)
    ark_path.closeSubpath()

    p.setBrush(QColor(255, 255, 255, 160))
    p.setPen(Qt.NoPen)
    p.drawPath(ark_path)

    p.end()
    return pixmap


# --- Custom Widgets ---

class CloseButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(22, 22)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("关闭")
        self._hovered = False

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        bg = QColor(255, 255, 255, 30) if self._hovered else QColor(255, 255, 255, 0)
        p.setBrush(bg)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(self.rect(), 5, 5)
        c = QColor(C_TEXT) if self._hovered else QColor(C_TEXT_DIM)
        p.setPen(QPen(c, 1.5))
        r = self.rect().adjusted(7, 7, -7, -7)
        p.drawLine(r.topLeft(), r.bottomRight())
        p.drawLine(r.topRight(), r.bottomLeft())
        p.end()

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def sizeHint(self):
        return QSize(22, 22)


class PinButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(22, 22)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("固定到桌面")
        self._hovered = False
        self._pinned = False

    @property
    def pinned(self):
        return self._pinned

    @pinned.setter
    def pinned(self, val):
        self._pinned = val
        self.setToolTip("取消固定" if val else "固定到桌面")
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        bg = QColor(255, 255, 255, 30) if self._hovered else QColor(255, 255, 255, 0)
        p.setBrush(bg)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(self.rect(), 5, 5)
        c = QColor(C_NEON_BLUE) if self._pinned else (QColor(C_TEXT) if self._hovered else QColor(C_TEXT_DIM))
        p.setPen(QPen(c, 1.5))
        cx, cy = self.rect().center().x(), self.rect().center().y()
        p.setBrush(c if self._pinned else Qt.NoBrush)
        p.drawEllipse(cx - 2, cy - 5, 4, 4)
        p.drawLine(cx, cy - 1, cx, cy + 5)
        p.end()

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def sizeHint(self):
        return QSize(22, 22)


class ProgressChunk(QWidget):
    """Custom-painted progress bar with reliable rounded corners.

    QProgressBar::chunk border-radius renders inconsistently on narrow chunks
    (a chunk at ~11% fill is only ~17px wide, so a 7px corner eats most of it
    and looks rectangular). Painting directly lets the chunk keep a proper
    rounded-cap shape at any fill level.
    """
    def __init__(self, value_pct, color, parent=None):
        super().__init__(parent)
        self._value = max(0.0, min(float(value_pct), 100.0))
        self._color = QColor(color)
        self.setFixedHeight(16)

    def setValue(self, value_pct):
        self._value = max(0.0, min(float(value_pct), 100.0))
        self.update()

    def sizeHint(self):
        return QSize(120, 16)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        radius = r.height() / 2.0  # pill shape: corner = half height

        # Track (background).
        p.setBrush(QColor(C_BG_LIGHT))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(r, radius, radius)

        # Filled chunk, clamped so it never exceeds the track width.
        fill_w = r.width() * (self._value / 100.0)
        if fill_w < 1:
            return
        # Draw the fill at its true width so the bar length always tracks the
        # value. When fill_w is narrower than the diameter, Qt clamps the corner
        # radius to half the width, so it renders as a small rounded cap rather
        # than a rectangle -- no need to floor the width (which froze the bar
        # below ~13%).
        chunk_r = QRectF(r.left(), r.top(), fill_w, r.height())
        p.setBrush(self._color)
        p.drawRoundedRect(chunk_r, radius, radius)


class BarRow(QWidget):
    """A labeled progress bar with percentage text right-aligned."""
    def __init__(self, label, value_pct, color, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)

        lbl = QLabel(label)
        lbl.setFixedWidth(30)
        lbl.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 10px; border: none;")

        bar = ProgressChunk(value_pct, color)

        pct = QLabel(f"{value_pct:.1f}%")
        pct.setFixedWidth(38)
        pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        pct.setStyleSheet(f"color: {C_TEXT}; font-size: 10px; border: none;")

        h.addWidget(lbl)
        h.addWidget(bar, 1)
        h.addWidget(pct)


class PeriodCard(QFrame):
    """Card for one time period: title + two stacked bars (quota vs time)."""
    def __init__(self, period, parent=None):
        super().__init__(parent)
        self.setObjectName("PeriodCard")
        self._build(period)

    def _build(self, period):
        name = period["name"]
        usage_pct = period["usage_pct"]
        remaining_pct = period["remaining_pct"]
        remaining_seconds = period["remaining_seconds"]
        total_seconds = period["total_seconds"]
        reset_time = period.get("reset_time")
        capped_by = period.get("capped_by")
        state, time_rem_pct = calc_alert(usage_pct, remaining_seconds, total_seconds)

        if state == "exhausted":
            bg, border = C_CARD_ALERT, "#4a1820"
        elif state == "fast":
            bg, border = C_CARD_FAST, "#4a3a12"
        else:
            bg, border = C_CARD, C_BORDER_LITE
        self.setStyleSheet(f"""
            QFrame#PeriodCard {{
                background: {bg}; border: 1px solid {border}; border-radius: 8px;
            }}
        """)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(3)

        # Title row: name + reset countdown + status badge
        top = QHBoxLayout()
        top.setSpacing(4)
        n = QLabel(name)
        n.setFont(QFont("", 11, QFont.Bold))
        n.setStyleSheet(f"color: {C_TEXT}; border: none;")
        top.addWidget(n)

        if reset_time:
            reset_lbl = QLabel(f"{fmt_remaining(remaining_seconds)}后重置")
            reset_lbl.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 9px; border: none;")
            top.addWidget(reset_lbl)

        top.addStretch()

        # Badge priority: exhausted > fast burn > capped by a bigger window > ok.
        if state == "exhausted":
            badge = QLabel("耗尽")
            badge_bg, badge_fg = C_RED_DIM, C_ALERT_RED
        elif state == "fast":
            badge = QLabel("消耗偏快")
            badge_bg, badge_fg = "#2e1a0a", C_ORANGE
        elif capped_by:
            badge = QLabel("受限")
            badge_bg, badge_fg = "#2e1a0a", C_ORANGE
        else:
            badge = QLabel("正常")
            badge_bg, badge_fg = C_GREEN_DIM, C_NEON_GREEN
        badge.setAlignment(Qt.AlignCenter)
        badge.setFixedHeight(16)
        badge.setStyleSheet(f"""
            background: {badge_bg}; color: {badge_fg};
            border-radius: 8px; padding: 0 6px;
            font-size: 9px; font-weight: bold; border: none;
        """)
        top.addWidget(badge)
        lay.addLayout(top)

        # Quota bar: red when exhausted, orange when fast/capped, green otherwise
        if state == "exhausted":
            quota_color = C_ALERT_RED
        elif state == "fast" or capped_by:
            quota_color = C_ORANGE
        else:
            quota_color = C_NEON_GREEN
        lay.addWidget(BarRow("额度余", remaining_pct, quota_color))

        # Time bar: always neon blue
        lay.addWidget(BarRow("时间余", time_rem_pct, C_NEON_BLUE))


# --- Config Dialog ---

class ConfigDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("用量监控配置")
        self.setFixedSize(320, 650)
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        self.setStyleSheet(f"""
            QDialog {{ background: {C_BG}; color: {C_TEXT}; }}
            QLineEdit {{
                background: {C_BG_LIGHT}; color: {C_TEXT}; border: 1px solid {C_BORDER};
                border-radius: 6px; padding: 5px 8px; font-size: 12px;
            }}
            QLabel {{ color: {C_TEXT_DIM}; font-size: 11px; border: none; }}
        """)

        layout = QFormLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 16)

        cfg = load_config()

        self.ak_input = QLineEdit(cfg.get("ak", ""))
        self.ak_input.setPlaceholderText("Access Key")
        layout.addRow("AK:", self.ak_input)

        self.sk_input = QLineEdit(cfg.get("sk", ""))
        self.sk_input.setPlaceholderText("Secret Key")
        self.sk_input.setEchoMode(QLineEdit.Password)
        layout.addRow("SK:", self.sk_input)

        self.region_input = QLineEdit(cfg.get("region", "cn-beijing"))
        self.region_input.setPlaceholderText("cn-beijing")
        layout.addRow("Region:", self.region_input)

        self.volc_plan_input = QLineEdit(cfg.get("volc_plan_name", ""))
        self.volc_plan_input.setPlaceholderText("如 Coding Plan Pro")
        layout.addRow("火山套餐:", self.volc_plan_input)

        # --- Kimi section ---
        kimi_sep = QFrame()
        kimi_sep.setFixedHeight(1)
        kimi_sep.setStyleSheet(f"background: {C_BORDER_LITE}; border: none;")
        layout.addRow(kimi_sep)

        kimi_hint = QLabel("Kimi 复用本地 kimi CLI 的 OAuth 凭证，无需 API Key。\n首次使用请在终端运行: kimi login")
        kimi_hint.setWordWrap(True)
        kimi_hint.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 10px; border: none;")
        layout.addRow(kimi_hint)

        self.kimi_path_input = QLineEdit(cfg.get("kimi_credential_path", ""))
        self.kimi_path_input.setPlaceholderText(f"留空用默认 {KIMI_DEFAULT_CRED_PATH}")
        layout.addRow("Kimi凭证:", self.kimi_path_input)

        # --- Qwen Token Plan section (cron headless refresh + cache) ---
        qwen_sep = QFrame()
        qwen_sep.setFixedHeight(1)
        qwen_sep.setStyleSheet(f"background: {C_BORDER_LITE}; border: none;")
        layout.addRow(qwen_sep)

        qwen_hint = QLabel(
            "千问用量由后台无头浏览器每 5 分钟自动抓取并写入缓存文件（cron）。\n"
            "若面板提示缓存过旧（会话过期），在终端运行:\n"
            "python3 ~/coding-plan-monitor/qwen_cookie_import.py")
        qwen_hint.setWordWrap(True)
        qwen_hint.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 10px; border: none;")
        layout.addRow(qwen_hint)

        cache_status = QLabel("状态: " + _qwen_cache_status())
        cache_status.setWordWrap(True)
        cache_status.setStyleSheet(f"color: {C_NEON_GREEN}; font-size: 10px; border: none;")
        layout.addRow(cache_status)

        self.qwen_cache_input = QLineEdit(cfg.get("qwen_usage_cache_path", ""))
        self.qwen_cache_input.setPlaceholderText(f"留空用默认 {QWEN_USAGE_CACHE_PATH}")
        layout.addRow("缓存文件:", self.qwen_cache_input)

        stale_cfg = cfg.get("qwen_cache_stale_min", "")
        self.qwen_stale_input = QLineEdit("" if stale_cfg in ("", None) else str(stale_cfg))
        self.qwen_stale_input.setPlaceholderText(f"留空用默认 {QWEN_CACHE_STALE_S // 60} 分钟")
        layout.addRow("过旧阈值:", self.qwen_stale_input)

        self.qianwen_cli_input = QLineEdit(cfg.get("qianwen_cli_path", ""))
        self.qianwen_cli_input.setPlaceholderText("留空自动探测（CLI 为降级数据源）")
        layout.addRow("CLI路径:", self.qianwen_cli_input)

        self.qwen_plan_input = QLineEdit(cfg.get("qwen_plan_name", ""))
        self.qwen_plan_input.setPlaceholderText("如 Token Plan")
        layout.addRow("千问套餐:", self.qwen_plan_input)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("保存")
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C_NEON_BLUE}; color: {C_BG}; border: none;
                border-radius: 6px; padding: 6px 20px; font-weight: bold; font-size: 11px;
            }}
            QPushButton:hover {{ background: {C_NEON_GREEN}; }}
        """)
        save_btn.clicked.connect(self._save)

        cancel_btn = QPushButton("取消")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C_BG_LIGHT}; color: {C_TEXT_DIM}; border: 1px solid {C_BORDER};
                border-radius: 6px; padding: 6px 20px; font-size: 11px;
            }}
            QPushButton:hover {{ background: {C_CARD}; }}
        """)
        cancel_btn.clicked.connect(self.reject)

        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        layout.addRow(btn_row)

    def _save(self):
        stale_min = self.qwen_stale_input.text().strip()
        try:
            stale_min = int(float(stale_min)) if stale_min else ""
        except ValueError:
            stale_min = ""  # invalid input falls back to default
        # Merge onto the existing file so fields this dialog doesn't know
        # about survive a save.
        cfg = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH) as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
        cfg.update({
            "ak": self.ak_input.text().strip(),
            "sk": self.sk_input.text().strip(),
            "region": self.region_input.text().strip() or "cn-beijing",
            "kimi_credential_path": self.kimi_path_input.text().strip(),
            "volc_plan_name": self.volc_plan_input.text().strip(),
            "qianwen_cli_path": self.qianwen_cli_input.text().strip(),
            "qwen_plan_name": self.qwen_plan_input.text().strip(),
            "qwen_usage_cache_path": self.qwen_cache_input.text().strip(),
            "qwen_cache_stale_min": stale_min,
        })
        with open(CONFIG_PATH, 'w') as f:
            json.dump(cfg, f, indent=2)
        self.accept()


# --- Panel Frame (custom painted border with gradient fade) ---

class PanelFrame(QWidget):
    """Custom-painted container for top-edge panel.

    Top corners are sharp (flush with screen top edge),
    bottom corners are symmetric rounded.
    Left, bottom, and right edges get gradient-fade borders.
    """
    def __init__(self, parent=None):
        super().__init__(parent)

    def _build_path(self, r, radius_bl, radius_br):
        """Build shape path: sharp top corners, rounded bottom corners."""
        path = QPainterPath()
        path.moveTo(r.left(), r.top())
        path.lineTo(r.right(), r.top())
        path.lineTo(r.right(), r.bottom() - radius_br)
        path.quadTo(r.right(), r.bottom(), r.right() - radius_br, r.bottom())
        path.lineTo(r.left() + radius_bl, r.bottom())
        path.quadTo(r.left(), r.bottom(), r.left(), r.bottom() - radius_bl)
        path.closeSubpath()
        return path

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        radius = 10
        path = self._build_path(r, radius, radius)

        # Background fill
        p.setBrush(QColor(C_BG))
        p.setPen(Qt.NoPen)
        p.drawPath(path)

        # Gradient-fade border — left, bottom, and right edges
        grad_color = QColor(C_BORDER)
        bw = 1
        a = grad_color.alpha()

        def make_edge_grad(c1, c2):
            g = QLinearGradient(c1, c2)
            g.setColorAt(0.0, QColor(grad_color.red(), grad_color.green(), grad_color.blue(), 0))
            g.setColorAt(0.15, QColor(grad_color.red(), grad_color.green(), grad_color.blue(), int(a * 0.7)))
            g.setColorAt(0.5, QColor(grad_color.red(), grad_color.green(), grad_color.blue(), a))
            g.setColorAt(0.85, QColor(grad_color.red(), grad_color.green(), grad_color.blue(), int(a * 0.7)))
            g.setColorAt(1.0, QColor(grad_color.red(), grad_color.green(), grad_color.blue(), 0))
            return g

        # Left edge (vertical gradient)
        p.setPen(QPen(QBrush(make_edge_grad(r.topLeft(), r.bottomLeft())), bw))
        p.drawLine(r.left(), r.top(), r.left(), r.bottom() - radius)

        # Bottom edge (horizontal gradient)
        p.setPen(QPen(QBrush(make_edge_grad(r.bottomLeft(), r.bottomRight())), bw))
        p.drawLine(r.left() + radius, r.bottom(), r.right() - radius, r.bottom())

        # Right edge (vertical gradient)
        p.setPen(QPen(QBrush(make_edge_grad(r.topRight(), r.bottomRight())), bw))
        p.drawLine(r.right(), r.top(), r.right(), r.bottom() - radius)

        p.end()


# --- Popup Panel ---

class UsagePanel(QWidget):
    configChanged = pyqtSignal()
    showRequested = pyqtSignal()

    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.setObjectName("UsagePanel")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedWidth(240)
        self._drag_pos = None
        self._data = data
        self._pinned = False

        # Auto-hide timer
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(AUTO_HIDE_DELAY_MS)
        self._hide_timer.timeout.connect(self._do_auto_hide)

        # Edge detection timer
        self._edge_timer = QTimer(self)
        self._edge_timer.setInterval(EDGE_CHECK_MS)
        self._edge_timer.timeout.connect(self._check_edge)

        self._build(data)
        self._apply_window_flags()

    # --- Drawer animation property ---
    #
    # The window stays parked at its final position while the frame slides
    # one-way downward out of the screen's top edge (upward to hide) — like
    # a drawer being pulled down. Moving the frame inside the fixed window
    # works under Wayland, where top-level move() is compositor-managed and
    # window masks are unreliable; plain child clipping does the job.

    def _get_drawer_y(self):
        return self._frame.y()

    def _set_drawer_y(self, y):
        self._frame.move(0, int(y))

    drawer_y = pyqtProperty(int, _get_drawer_y, _set_drawer_y)

    def _animate_show(self):
        self._reposition()
        h = self.height()
        # Park the frame fully above the clip before show() (same event-loop
        # tick, so nothing flashes), then pull it down into view.
        self._frame.move(0, -h)
        self.show()
        self._anim = QPropertyAnimation(self, b"drawer_y")
        self._anim.setDuration(320)
        self._anim.setStartValue(-h)
        self._anim.setEndValue(0)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.start()

    def _animate_hide(self):
        self._anim = QPropertyAnimation(self, b"drawer_y")
        self._anim.setDuration(320)
        self._anim.setStartValue(self._frame.y())
        self._anim.setEndValue(-self.height())
        self._anim.setEasingCurve(QEasingCurve.InCubic)
        self._anim.finished.connect(self._on_hide_finished)
        self._anim.start()

    def _on_hide_finished(self):
        self.hide()
        if not self._pinned:
            self._edge_timer.start()

    def _build(self, data):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._frame = PanelFrame()
        frame_lay = QVBoxLayout(self._frame)
        frame_lay.setContentsMargins(1, 1, 1, 1)
        frame_lay.setSpacing(0)

        self._build_body(data)

        # Drawer clip: the frame slides vertically inside this container
        # (see the drawer animation above); child widgets are clipped to
        # their parent's rect, so the frame is invisible while parked
        # above the container's top edge.
        self._clip = QWidget(self)
        outer.addWidget(self._clip)
        self._frame.setParent(self._clip)
        self._frame.move(0, 0)
        frame_h = self._frame.sizeHint().height()
        self._frame.setFixedSize(self.width(), frame_h)
        self.setFixedHeight(frame_h)

    def update_data(self, data):
        """Rebuild the panel body in place while the panel stays visible.

        Called when a background refresh lands new data on a shown (possibly
        pinned) panel, so the numbers never go stale until re-opened.
        """
        self._data = data
        old = self._body
        self._build_body(data)
        old.setParent(None)
        old.deleteLater()

    def _build_body(self, data):
        body = QWidget()
        body.setObjectName("PanelBody")
        body.setStyleSheet(f"""
            QWidget#PanelBody {{
                background: transparent; border: none;
            }}
        """)

        layout = QVBoxLayout(body)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        # Title bar
        title_bar = QHBoxLayout()
        title_bar.setSpacing(4)

        icon_lbl = QLabel()
        icon_lbl.setPixmap(QIcon(make_icon_pixmap(16)).pixmap(16, 16))
        title_bar.addWidget(icon_lbl)

        title = QLabel("套餐用量")
        title.setFont(QFont("", 10, QFont.Bold))
        title.setStyleSheet(f"color: {C_TEXT}; border: none;")
        title_bar.addWidget(title)
        title_bar.addStretch()

        self.pin_btn = PinButton()
        self.pin_btn.clicked.connect(self._toggle_pin)
        title_bar.addWidget(self.pin_btn)

        close_btn = CloseButton()
        close_btn.clicked.connect(self.close)
        title_bar.addWidget(close_btn)
        layout.addLayout(title_bar)

        self._divider(layout)

        # data is {"volc": <volc data>, "kimi": <kimi data or None>}.
        # Volcengine Ark section (5h / weekly / monthly).
        volc = data.get("volc") or {}
        volc_plan_name = load_config().get("volc_plan_name", "")
        layout.addLayout(self._section_header("🔥", "火山方舟",
                                              badge=volc_plan_name or None))
        if volc.get("source") == "error":
            hint = QLabel(f"获取失败 · 点「火山」检查 AK/SK\n{str(volc.get('error', ''))[:80]}")
            hint.setStyleSheet(f"color: {C_ALERT_RED}; font-size: 9px; border: none; padding: 2px 0;")
            hint.setWordWrap(True)
            layout.addWidget(hint)
        elif volc.get("periods"):
            layout.addLayout(self._period_cards(volc, ["session", "weekly", "monthly"]))
        else:
            hint = QLabel("加载中…")
            hint.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 9px; border: none; padding: 2px 0;")
            layout.addWidget(hint)

        # Divider between providers.
        self._divider(layout)

        # Kimi Code section (5h / weekly; no monthly field exposed by the API).
        kimi = data.get("kimi")
        kimi_extra = (kimi or {}).get("extra", {}) or {}
        badge_parts = []
        if kimi_extra.get("membership"):
            badge_parts.append(kimi_extra["membership"])
        if kimi_extra.get("parallel"):
            badge_parts.append(f"并发{kimi_extra['parallel']}")
        layout.addLayout(self._section_header("💜", "Kimi Code",
                                              badge=" · ".join(badge_parts) or None))
        if kimi:
            layout.addLayout(self._period_cards(kimi, ["session", "weekly"]))
        else:
            hint = QLabel("未授权 · 终端运行 kimi login 或点「Kimi」配置")
            hint.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 9px; border: none; padding: 2px 0;")
            hint.setWordWrap(True)
            layout.addWidget(hint)

        # Divider between providers.
        self._divider(layout)

        # Qwen Token Plan section (browser cache first, QianWen CLI fallback).
        qwen = data.get("qwen")
        qwen_extra = (qwen or {}).get("extra", {}) or {}
        qwen_plan_name = load_config().get("qwen_plan_name", "")
        qwen_badge_parts = []
        if qwen_plan_name:
            qwen_badge_parts.append(qwen_plan_name)
        if qwen_extra.get("plan"):
            qwen_badge_parts.append(qwen_extra["plan"])
        layout.addLayout(self._section_header("🔮", "千问 Token Plan",
                                              badge=" · ".join(qwen_badge_parts) or None))
        if qwen:
            qwen_periods = qwen.get("periods", {})
            if qwen_periods:
                layout.addLayout(self._period_cards(qwen, ["session", "weekly", "monthly"]))
                # Cache older than the staleness threshold -> the cron-driven
                # headless refresher is failing (usually an expired session).
                if qwen_extra.get("stale"):
                    stale_lbl = QLabel("⚠ 缓存较旧 · 自动刷新异常，查看 qwen_refresh.log")
                    stale_lbl.setStyleSheet(
                        f"color: {C_ORANGE}; font-size: 8px; border: none; padding: 1px 0;")
                    stale_lbl.setWordWrap(True)
                    layout.addWidget(stale_lbl)
            elif qwen_extra.get("not_subscribed"):
                hint2 = QLabel("当前账号未订阅 Token Plan")
                hint2.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 8px; border: none; padding: 1px 0;")
                hint2.setWordWrap(True)
                layout.addWidget(hint2)
        else:
            hint = QLabel("无数据 · 终端运行 qwen_cookie_import.py 导入会话")
            hint.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 9px; border: none; padding: 2px 0;")
            hint.setWordWrap(True)
            layout.addWidget(hint)

        # Footer
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 10, 0)
        footer.setSpacing(6)
        update_time = volc.get("update_time") or datetime.now()
        ts = QLabel(f"更新于 {update_time.strftime('%H:%M:%S')}")
        ts.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 9px; border: none;")
        footer.addWidget(ts)

        if volc.get("source") == "mock":
            mock_lbl = QLabel("MOCK")
            mock_lbl.setAlignment(Qt.AlignCenter)
            mock_lbl.setFixedSize(32, 14)
            mock_lbl.setStyleSheet(f"""
                background: #1e1a0e; color: {C_ORANGE};
                border-radius: 7px; font-size: 8px; border: none;
            """)
            footer.addWidget(mock_lbl)

        if not kimi:
            na_lbl = QLabel("KIMI未授权")
            na_lbl.setAlignment(Qt.AlignCenter)
            na_lbl.setFixedSize(54, 14)
            na_lbl.setStyleSheet(f"""
                background: {C_RED_DIM}; color: {C_ALERT_RED};
                border-radius: 7px; font-size: 8px; border: none;
            """)
            footer.addWidget(na_lbl)

        if not qwen:
            qw_lbl = QLabel("千问无数据")
            qw_lbl.setAlignment(Qt.AlignCenter)
            qw_lbl.setFixedSize(54, 14)
            qw_lbl.setStyleSheet(f"""
                background: {C_RED_DIM}; color: {C_ALERT_RED};
                border-radius: 7px; font-size: 8px; border: none;
            """)
            footer.addWidget(qw_lbl)

        footer.addStretch()

        # 火山 config button
        volc_btn = QPushButton("火山")
        volc_btn.setCursor(Qt.PointingHandCursor)
        volc_btn.setFixedSize(32, 18)
        volc_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C_BG_LIGHT}; color: {C_NEON_BLUE};
                border: 1px solid {C_BORDER}; border-radius: 9px;
                font-size: 9px; border: none; padding: 0;
            }}
            QPushButton:hover {{
                background: {C_CARD}; color: {C_NEON_GREEN};
            }}
        """)
        volc_btn.clicked.connect(self._open_config)
        footer.addWidget(volc_btn)

        # Kimi config button (opens same config dialog; path field configures cred file)
        kimi_btn = QPushButton("Kimi")
        kimi_btn.setCursor(Qt.PointingHandCursor)
        kimi_btn.setFixedSize(32, 18)
        kimi_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C_BG_LIGHT}; color: {C_NEON_GREEN};
                border: 1px solid {C_BORDER}; border-radius: 9px;
                font-size: 9px; border: none; padding: 0;
            }}
            QPushButton:hover {{
                background: {C_CARD}; color: {C_NEON_BLUE};
            }}
        """)
        kimi_btn.clicked.connect(self._open_config)
        footer.addWidget(kimi_btn)

        # Qwen config button (opens same config dialog; cache path/staleness/CLI fields)
        qwen_btn = QPushButton("千问")
        qwen_btn.setCursor(Qt.PointingHandCursor)
        qwen_btn.setFixedSize(32, 18)
        qwen_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C_BG_LIGHT}; color: {C_GRAD_E};
                border: 1px solid {C_BORDER}; border-radius: 9px;
                font-size: 9px; border: none; padding: 0;
            }}
            QPushButton:hover {{
                background: {C_CARD}; color: {C_NEON_BLUE};
            }}
        """)
        qwen_btn.clicked.connect(self._open_config)
        footer.addWidget(qwen_btn)

        layout.addLayout(footer)

        self._frame.layout().addWidget(body)
        self._body = body

    def _section_header(self, icon_text, title, badge=None):
        """A provider section header row: icon + title + optional badge."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 1, 0, 1)
        row.setSpacing(5)

        icon = QLabel(icon_text)
        icon.setStyleSheet(f"color: {C_TEXT}; font-size: 11px; border: none;")
        row.addWidget(icon)

        name = QLabel(title)
        name.setFont(QFont("", 10, QFont.Bold))
        name.setStyleSheet(f"color: {C_TEXT}; border: none;")
        row.addWidget(name)

        if badge:
            b = QLabel(badge)
            b.setStyleSheet(f"""
                color: {C_TEXT_DIM}; font-size: 8px; border: none;
                background: {C_BG_LIGHT}; border-radius: 7px; padding: 1px 6px;
            """)
            row.addWidget(b)

        row.addStretch()
        return row

    def _period_cards(self, section_data, keys):
        """Build a vertical layout of PeriodCards for the given period keys."""
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        periods = section_data.get("periods", {}) or {}
        for key in keys:
            if key in periods:
                col.addWidget(PeriodCard(periods[key]))
        return col

    def _divider(self, layout):
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {C_BORDER_LITE}; border: none; margin: 1px 0;")
        layout.addWidget(line)

    def _apply_window_flags(self):
        if self._pinned:
            self.setWindowFlags(
                Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnBottomHint | Qt.NoDropShadowWindowHint
            )
        else:
            self.setWindowFlags(
                Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint
            )
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def _toggle_pin(self):
        self._pinned = not self._pinned
        self.pin_btn.pinned = self._pinned
        self._apply_window_flags()
        if self._pinned:
            self._hide_timer.stop()
            self._edge_timer.stop()
        else:
            # Back to auto-hide: if mouse not over panel, start hide timer
            if not self.rect().contains(self.mapFromGlobal(QCursor.pos())):
                self._hide_timer.start()
        self.show()

    def _do_auto_hide(self):
        if self._pinned:
            return
        self._animate_hide()

    def _check_edge(self):
        if self._pinned or self.isVisible():
            self._edge_timer.stop()
            return
        pos = QCursor.pos()
        screen = QApplication.primaryScreen()
        if not screen:
            return
        geo = screen.availableGeometry()
        # Top edge zone matching panel position
        panel_x = int(geo.left() + geo.width() * 0.618)
        if (pos.y() <= geo.top() + EDGE_ZONE_HEIGHT and
                panel_x <= pos.x() <= panel_x + self.width()):
            self._edge_timer.stop()
            self.showRequested.emit()

    def _reposition(self):
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            x = int(geo.left() + geo.width() * 0.618)
            y = geo.top()
            self.move(x, y)

    def _open_config(self):
        dlg = ConfigDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            self.configChanged.emit()

    # --- Mouse events ---

    def enterEvent(self, event):
        self._hide_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if not self._pinned:
            self._hide_timer.start()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.LeftButton:
            self.move(event.globalPos() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def closeEvent(self, event):
        self._hide_timer.stop()
        self._edge_timer.stop()
        super().closeEvent(event)


# --- Tray Icon ---

class FetchWorker(QThread):
    """Fetches all providers off the UI thread.

    A refresh can stall on the volc SDK, kimi OAuth round-trips, or (when
    the cache is missing) the qianwen CLI's 30s subprocess timeout — none
    of which may freeze the tray icon, tooltip, or panel animations.
    """
    done = pyqtSignal(dict)

    def run(self):
        try:
            data = fetch_all_usage()
        except Exception as e:
            log.error("fetch_all_usage failed: %s", e)
            data = {}
        self.done.emit(data)


class ArkMonitorTray(QSystemTrayIcon):
    REFRESH_MIN_GAP_S = 30  # skip auto-refreshes when data is fresher

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.data = None
        self.panel = None
        self._icon_level = "blue"
        self._worker = None
        self._last_fetch = 0

        self._update_icon()
        self.setToolTip("套餐用量")
        self._setup_menu()
        self._refresh(force=True)

        self.activated.connect(self._on_activated)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(REFRESH_INTERVAL_MS)

    def _update_icon(self):
        self.setIcon(QIcon(make_icon_pixmap(32, level=self._icon_level)))

    def _setup_menu(self):
        menu = QMenu()
        menu.setStyleSheet(f"""
            QMenu {{
                background: {C_BG}; color: {C_TEXT};
                border: 1px solid {C_BORDER}; border-radius: 8px; padding: 4px;
            }}
            QMenu::item {{ padding: 6px 20px; border-radius: 4px; }}
            QMenu::item:selected {{ background: {C_CARD}; }}
            QMenu::separator {{ height: 1px; background: {C_BORDER_LITE}; margin: 4px 8px; }}
        """)
        refresh = QAction("刷新", self)
        refresh.triggered.connect(lambda: self._refresh(force=True))
        menu.addAction(refresh)

        show = QAction("显示详情", self)
        show.triggered.connect(self._show_panel)
        menu.addAction(show)

        menu.addSeparator()

        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.app.quit)
        menu.addAction(quit_action)

        self.setContextMenu(menu)

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self._show_panel()

    def _refresh(self, force=False):
        """Schedule a background fetch (single-flight, min-gap guarded)."""
        if self._worker is not None and self._worker.isRunning():
            return  # a fetch is already in flight
        if (not force and self.data is not None
                and time.time() - self._last_fetch < self.REFRESH_MIN_GAP_S):
            return  # data still fresh
        self._worker = FetchWorker(self)
        self._worker.done.connect(self._on_data)
        self._worker.start()

    def _on_data(self, data):
        self.data = data
        self._last_fetch = time.time()
        self._worker = None
        self._apply_status()
        # Push fresh numbers into a visible panel (pinned panels especially
        # used to go stale until re-opened).
        if self.panel is not None and self.panel.isVisible():
            self.panel.update_data(data)

    def _apply_status(self):
        volc = self.data.get("volc") or {}
        kimi = self.data.get("kimi")
        qwen = self.data.get("qwen")

        volc_periods = volc.get("periods") or {}
        kimi_periods = (kimi.get("periods") or {}) if kimi else {}
        qwen_periods = (qwen.get("periods") or {}) if qwen else {}

        # A provider "has plan quota" if its weekly remaining is above a
        # small threshold (0.01%). Floating-point residue from API rounding
        # can leave values like 0.00015% which should count as exhausted.
        _QUOTA_EPS = 0.01

        def has_quota(periods):
            wk = periods.get("weekly", {})
            return wk.get("remaining_pct", 0) > _QUOTA_EPS

        def volc_has_quota(periods):
            # Cards show raw per-window percentages (matching the web
            # console), so an exhausted monthly window must be checked here
            # explicitly: it blocks the 5h/weekly windows regardless of
            # their own headroom.
            rems = [p.get("remaining_pct", 0)
                    for p in (periods.get("weekly"), periods.get("monthly")) if p]
            return bool(rems) and min(rems) > _QUOTA_EPS

        volc_plan = volc_has_quota(volc_periods)
        kimi_plan = has_quota(kimi_periods)
        qwen_plan = has_quota(qwen_periods) if qwen else False

        plans_left = int(volc_plan) + int(kimi_plan) + int(qwen_plan)
        log.info("icon: volc_plan=%s kimi_plan=%s qwen_plan=%s plans_left=%d | volc_wk_rem=%s",
                 volc_plan, kimi_plan, qwen_plan, plans_left,
                 volc_periods.get("weekly", {}).get("remaining_pct"))

        if plans_left == 0:
            self._icon_level = "red"
        elif plans_left == 1:
            self._icon_level = "orange"
        elif plans_left == 2:
            self._icon_level = "yellow"
        else:
            self._icon_level = "blue"

        # Tooltip: all providers' snapshot.
        volc_sess = volc_periods.get("session", {})
        kimi_sess = kimi_periods.get("session", {})
        parts = []
        if volc.get("source") == "error":
            parts.append("火山:获取失败")
        elif volc_sess:
            v_pct = volc_sess.get("remaining_pct", 0)
            v_str = fmt_remaining(volc_sess.get("remaining_seconds", 0))
            parts.append(f"火5h余:{v_pct:.0f}%|{v_str}")
        else:
            parts.append("火山:加载中")
        if kimi:
            k_pct = kimi_sess.get("remaining_pct", 0)
            k_str = fmt_remaining(kimi_sess.get("remaining_seconds", 0))
            if kimi_sess.get("capped_by"):
                k_raw = kimi_sess.get("raw_remaining_pct", 0)
                parts.append(f"K5h余:{k_pct:.0f}%(窗口{k_raw:.0f}%)|{k_str}")
            else:
                parts.append(f"K5h余:{k_pct:.0f}%|{k_str}")
        else:
            parts.append("Kimi:未授权")
        if qwen:
            qwen_sess = qwen_periods.get("session", {})
            if qwen_sess:
                q_pct = qwen_sess.get("remaining_pct", 0)
                q_str = fmt_remaining(qwen_sess.get("remaining_seconds", 0))
                parts.append(f"千5h余:{q_pct:.0f}%|{q_str}")
            else:
                parts.append("千问:未订阅")
        else:
            parts.append("千问:未登录")
        self.setToolTip("  ".join(parts))
        self._update_icon()

    def _show_panel(self):
        # Refresh in the background while the panel shows; _on_data pushes
        # the new numbers into it. The min-gap guard keeps this cheap.
        self._refresh()

        if self.panel is not None:
            self.panel.close()
            self.panel = None

        self.panel = UsagePanel(self.data)
        self.panel.configChanged.connect(lambda: self._refresh(force=True))
        self.panel.showRequested.connect(self._show_panel)
        self.panel._animate_show()

        # In auto-hide mode, start the hide timer since tray click
        # puts mouse at tray (not over panel)
        if not self.panel._pinned:
            self.panel._hide_timer.start()


