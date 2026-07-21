#!/usr/bin/env python3
"""
Volcengine Ark Coding Plan Usage Monitor - System Tray Widget

Displays coding plan usage ratios for 5h/1w/1m periods with time-based
comparison. Alerts in red when quota remaining < time remaining.

Data source: Volcengine OpenAPI GetCodingPlanUsage (AK/SK auth)
Fallback: Mock data when AK/SK not configured
"""

import sys
import json
import os
import logging
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

from PyQt5.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QProgressBar, QAction, QFrame, QPushButton,
    QDialog, QLineEdit, QFormLayout
)
from PyQt5.QtCore import (
    Qt, QTimer, QSize, QRect, pyqtSignal, pyqtProperty, QPropertyAnimation, QEasingCurve
)
from PyQt5.QtNetwork import QLocalSocket, QLocalServer
from PyQt5.QtGui import (
    QIcon, QPixmap, QPainter, QPainterPath, QColor, QFont, QCursor, QPen,
    QLinearGradient, QRadialGradient, QBrush
)

# --- Config ---

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
REFRESH_INTERVAL_MS = 5 * 60 * 1000
AUTO_HIDE_DELAY_MS = 1500
EDGE_CHECK_MS = 300
EDGE_ZONE_HEIGHT = 6
EDGE_ZONE_WIDTH = 80

# --- Kimi Code OAuth / Usage endpoints (reuses local kimi CLI credentials) ---

KIMI_DEFAULT_CRED_PATH = os.path.expanduser("~/.kimi-code/credentials/kimi-code.json")
KIMI_OAUTH_TOKEN_URL = "https://auth.kimi.com/api/oauth/token"
KIMI_OAUTH_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
KIMI_USAGE_URL = "https://api.kimi.com/coding/v1/usages"
KIMI_TOKEN_SKEW_S = 60  # refresh if access_token expires within this many seconds

logging.basicConfig(
    filename=os.path.join(BASE_DIR, "monitor.log"),
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# --- Color Palette ---

C_BG          = "#0f1117"
C_BG_LIGHT    = "#181a22"
C_CARD        = "#1a1d26"
C_CARD_ALERT  = "#2a1418"
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

# --- Data Layer ---

def load_config():
    default = {
        "ak": "", "sk": "", "region": "cn-beijing",
        "kimi_credential_path": "",  # empty -> default ~/.kimi-code/...
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            default.update(cfg)
        except Exception:
            pass
    return default


def fetch_usage_data():
    cfg = load_config()
    ak, sk = cfg.get("ak", ""), cfg.get("sk", "")
    if ak and sk:
        try:
            return _fetch_via_sdk(ak, sk, cfg.get("region", "cn-beijing"))
        except Exception as e:
            log.error("SDK fetch failed: %s", e)
    log.info("Using mock data")
    return _mock_data()


def _fetch_via_sdk(ak, sk, region):
    from volcengine.base.Service import Service
    from volcengine.ApiInfo import ApiInfo
    from volcengine.Credentials import Credentials
    from volcengine.ServiceInfo import ServiceInfo

    svc = Service(
        ServiceInfo("open.volcengineapi.com", {"Accept": "application/json"},
                    Credentials(ak, sk, "ark", region), 10, 10),
        {"GetCodingPlanUsage": ApiInfo("POST", "/",
            {"Action": "GetCodingPlanUsage", "Version": "2024-01-01"}, {}, {})},
    )
    svc.set_ak(ak)
    svc.set_sk(sk)
    res = svc.post("GetCodingPlanUsage", {}, {})
    data = json.loads(res)
    err = data.get("ResponseMetadata", {}).get("Error")
    if err:
        raise RuntimeError(f"API error: {err.get('Code')} - {err.get('Message')}")

    result = data.get("Result", {})
    status = result.get("Status", "Unknown")
    update_ts = result.get("UpdateTimestamp", 0)
    quota_usage = result.get("QuotaUsage", [])
    now = datetime.now()
    total_map = {"session": 5 * 3600, "weekly": 7 * 24 * 3600, "monthly": 31 * 24 * 3600}
    name_map = {"session": "近5小时", "weekly": "本周", "monthly": "本月"}
    periods = {}
    for item in quota_usage:
        level = item.get("Level", "")
        if level not in total_map:
            continue
        percent = item.get("Percent", 0)
        reset_ts = item.get("ResetTimestamp", 0)
        reset_dt = datetime.fromtimestamp(reset_ts) if reset_ts else None
        rem = (reset_dt - now).total_seconds() if reset_dt else 0
        periods[level] = {
            "name": name_map[level], "level": level,
            "usage_pct": percent, "remaining_pct": 100 - percent,
            "reset_time": reset_dt, "remaining_seconds": max(rem, 0),
            "total_seconds": total_map[level],
        }
    log.info("API data: status=%s, periods=%s", status,
             {k: f"{v['usage_pct']:.1f}%" for k, v in periods.items()})
    return {"status": status,
            "update_time": datetime.fromtimestamp(update_ts) if update_ts else now,
            "periods": periods, "source": "api"}


def _mock_data():
    from datetime import timedelta
    now = datetime.now()
    periods = {
        "session": {"name": "近5小时", "level": "session", "usage_pct": 12.0,
                    "remaining_pct": 88.0, "reset_time": now + timedelta(hours=3, minutes=20),
                    "remaining_seconds": 3.33 * 3600, "total_seconds": 5 * 3600},
        "weekly":  {"name": "本周",  "level": "weekly",  "usage_pct": 5.0,
                    "remaining_pct": 95.0, "reset_time": now + timedelta(days=5),
                    "remaining_seconds": 5 * 24 * 3600, "total_seconds": 7 * 24 * 3600},
        "monthly": {"name": "本月",  "level": "monthly", "usage_pct": 15.0,
                    "remaining_pct": 85.0, "reset_time": now + timedelta(days=20),
                    "remaining_seconds": 20 * 24 * 3600, "total_seconds": 31 * 24 * 3600},
    }
    return {"status": "Mock", "update_time": now, "periods": periods, "source": "mock"}


# --- Kimi Code Data Layer ---
#
# Reuses the local kimi CLI OAuth credentials (~/.kimi-code/credentials/kimi-code.json).
# The CLI performs device-code login; we only refresh+read its stored token, so the
# monitor never needs its own OAuth flow. Token file is shared with the CLI: each
# refresh rotates the refresh_token, so we write new tokens back atomically and use
# a non-blocking flock to avoid stomping a concurrent CLI refresh.

def _kimi_cred_path():
    cfg = load_config()
    return cfg.get("kimi_credential_path") or KIMI_DEFAULT_CRED_PATH


def _kimi_load_cred():
    """Read kimi CLI credential file. Returns dict or None on any failure."""
    path = _kimi_cred_path()
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _kimi_atomic_write_cred(cred):
    """Write credential dict back atomically, preserving 0600 perms."""
    path = _kimi_cred_path()
    tmp = path + ".tmp"
    try:
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, mode=0o700, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(cred, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("kimi cred write-back failed: %s", e)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _kimi_refresh_token(refresh_token):
    """Exchange refresh_token for a new access/refresh token pair via OAuth.

    Returns dict: {access_token, refresh_token, expires_at(unix), expires_in}.
    Raises RuntimeError on non-200 or parse failure.
    """
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": KIMI_OAUTH_CLIENT_ID,
    }).encode("utf-8")
    req = urllib.request.Request(
        KIMI_OAUTH_TOKEN_URL, data=body, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise RuntimeError(f"OAuth refresh HTTP {e.code}: {detail}")
    try:
        data = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"OAuth refresh parse error: {e}")
    access = data.get("access_token")
    new_refresh = data.get("refresh_token", refresh_token)
    expires_in = int(data.get("expires_in", 900))
    if not access:
        raise RuntimeError(f"OAuth refresh missing access_token: {raw[:200]}")
    return {
        "access_token": access,
        "refresh_token": new_refresh,
        "expires_in": expires_in,
        "expires_at": int(time.time()) + expires_in,
        "token_type": data.get("token_type", "Bearer"),
        "scope": data.get("scope", "kimi-code"),
    }


def _kimi_ensure_token(force_refresh=False):
    """Return a usable access_token, refreshing and writing back if needed.

    On refresh failure raises; caller catches and degrades.
    """
    cred = _kimi_load_cred()
    if not cred or not cred.get("refresh_token"):
        raise RuntimeError("no kimi credential file or refresh_token; run `kimi` to login")

    now = int(time.time())
    access = cred.get("access_token", "")
    expires_at = cred.get("expires_at", 0)

    if access and not force_refresh and expires_at - now > KIMI_TOKEN_SKEW_S:
        return access

    # Try to acquire a non-blocking lock so we don't fight the kimi CLI over the
    # same refresh_token. If locked, fall back to whatever token is on disk.
    lock_path = _kimi_cred_path() + ".lock"
    lock_acquired = False
    lock_fh = None
    try:
        import fcntl
        lock_fh = open(lock_path, "w")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_acquired = True
        except (BlockingIOError, OSError):
            lock_acquired = False
    except Exception:
        lock_acquired = False

    try:
        if not lock_acquired:
            # Another process (likely kimi CLI) is refreshing; reuse on-disk token.
            log.info("kimi cred lock busy; reusing on-disk token")
            if access:
                return access
            raise RuntimeError("kimi cred locked and no usable access_token")

        refreshed = _kimi_refresh_token(cred["refresh_token"])
        # Merge onto existing cred to preserve any extra fields the CLI stores.
        cred.update(refreshed)
        _kimi_atomic_write_cred(cred)
        log.info("kimi token refreshed, expires_at=%s", refreshed["expires_at"])
        return refreshed["access_token"]
    finally:
        if lock_fh is not None:
            try:
                if lock_acquired:
                    import fcntl
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)
                lock_fh.close()
            except Exception:
                pass


def _parse_kimi_time(s):
    """Parse Kimi ISO8601 timestamps like '2026-07-21T08:18:53.857509Z'.

    Returns a tz-aware UTC datetime, or None on failure.
    """
    if not s:
        return None
    try:
        t = s.strip()
        # fromisoformat gained 'Z' support in 3.11; normalise for older Pythons.
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        # Truncate sub-microsecond digits (nanoseconds) to 6 places.
        if "." in t:
            head, frac = t.split(".", 1)
            # keep only the fractional part up to the timezone offset marker
            for sep in ("+", "-"):
                if sep in frac:
                    frac, tz = frac.split(sep, 1)
                    frac = (frac + "000000")[:6]
                    t = head + "." + frac + sep + tz
                    break
            else:
                frac = (frac + "000000")[:6]
                t = head + "." + frac
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception as e:
        log.warning("kimi time parse failed for %r: %s", s, e)
        return None


def _kimi_call_usages(access_token):
    """GET /coding/v1/usages. Returns parsed JSON dict. Raises on non-2xx."""
    req = urllib.request.Request(
        KIMI_USAGE_URL, method="GET",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        err = RuntimeError(f"usages HTTP {e.code}: {body}")
        err.kimi_http_code = e.code
        raise err


def _build_kimi_period(name, level, used, limit, reset_time, total_seconds):
    """Convert a Kimi usage window into the unified period dict shape."""
    used = int(used) if used not in (None, "") else 0
    limit = int(limit) if limit not in (None, "") else 0
    usage_pct = (used / limit * 100) if limit > 0 else 100.0
    remaining_pct = max(100.0 - usage_pct, 0.0)
    now = datetime.now(timezone.utc)
    rem_sec = (reset_time - now).total_seconds() if reset_time else 0
    return {
        "name": name, "level": level,
        "usage_pct": usage_pct, "remaining_pct": remaining_pct,
        "reset_time": reset_time,
        "remaining_seconds": max(rem_sec, 0),
        "total_seconds": total_seconds,
        "limit": limit, "used": used,
    }


def _fetch_kimi_usage():
    """Fetch Kimi Code plan usage via the CLI's OAuth token.

    Returns dict shaped like the volc data (status/update_time/periods/source)
    plus an 'extra' dict with membership + parallel info for UI badges.
    Raises on auth/transport failure; caller degrades to None.
    """
    access_token = _kimi_ensure_token()
    try:
        data = _kimi_call_usages(access_token)
    except RuntimeError as e:
        # 401 -> token went stale between ensure and call; force one refresh+retry.
        if getattr(e, "kimi_http_code", None) == 401:
            log.info("kimi usages 401, forcing refresh and retry")
            access_token = _kimi_ensure_token(force_refresh=True)
            data = _kimi_call_usages(access_token)
        else:
            raise

    now = datetime.now()
    periods = {}

    # Main plan quota (weekly cycle per observed resetTime ~7d): top-level usage.
    # Built first so its remaining can cap the 5h window (a 5h window can never
    # have more quota left than the weekly plan it draws from).
    usage = data.get("usage", {}) or {}
    if usage:
        reset_time = _parse_kimi_time(usage.get("resetTime"))
        periods["weekly"] = _build_kimi_period(
            "本周", "weekly", usage.get("used", "0"), usage.get("limit", "0"),
            reset_time, 7 * 24 * 3600)

    # 5h rolling window: limits[].window with duration in minutes.
    for item in data.get("limits", []) or []:
        window = item.get("window", {}) or {}
        duration_min = int(window.get("duration", 0) or 0)
        # Match the 5h (300min) window; skip anything else.
        if duration_min != 300:
            continue
        detail = item.get("detail", {}) or {}
        limit = detail.get("limit", "0")
        remaining = detail.get("remaining", "0")
        limit_int = int(limit) if limit not in (None, "") else 0
        remaining_int = int(remaining) if remaining not in (None, "") else 0
        reset_time = _parse_kimi_time(detail.get("resetTime"))

        # Cap 5h remaining by the weekly plan's remaining quota. usage/limits
        # share the same quota unit, so compare counts directly. When the weekly
        # plan is nearly exhausted the 5h window may still report a full bucket,
        # which is misleading -- effective 5h remaining can't exceed weekly left.
        weekly = periods.get("weekly")
        if weekly:
            w_limit = weekly.get("limit", 0)
            w_used = weekly.get("used", 0)
            weekly_remaining_count = (w_limit - w_used) if w_limit else None
            if weekly_remaining_count is not None and weekly_remaining_count < remaining_int:
                log.info("kimi 5h remaining capped %d -> %d by weekly remaining",
                         remaining_int, weekly_remaining_count)
                remaining_int = weekly_remaining_count

        effective_used = max(limit_int - remaining_int, 0)
        periods["session"] = _build_kimi_period(
            "近5小时", "session", str(effective_used), limit, reset_time, 5 * 3600)
        break

    extra = {
        "membership": (data.get("user", {}) or {}).get("membership", {}).get("level", ""),
        "parallel": (data.get("parallel", {}) or {}).get("limit", ""),
    }

    log.info("kimi usage: periods=%s, extra=%s",
             {k: f"{v['usage_pct']:.1f}%" for k, v in periods.items()}, extra)

    return {
        "status": "Kimi Code",
        "update_time": now,
        "periods": periods,
        "source": "kimi",
        "extra": extra,
    }


def fetch_all_usage():
    """Fetch both Volcengine Ark and Kimi Code usage.

    Volc failures fall back to mock (existing behaviour); Kimi failures degrade
    to None so the panel can show an 'unauthorized' placeholder without breaking
    the Volc section.
    """
    try:
        volc = fetch_usage_data()
    except Exception as e:
        log.error("volc fetch_usage_data failed: %s", e)
        volc = _mock_data()

    kimi = None
    try:
        kimi = _fetch_kimi_usage()
    except Exception as e:
        log.error("kimi fetch failed: %s", e)

    return {"volc": volc, "kimi": kimi}


def calc_alert(usage_pct, remaining_seconds, total_seconds):
    time_rem = remaining_seconds / total_seconds * 100 if total_seconds > 0 else 0
    return (100 - usage_pct) < time_rem, time_rem


def fmt_remaining(seconds):
    if seconds > 86400:
        d = int(seconds // 86400)
        h = int((seconds % 86400) // 3600)
        return f"{d}天{h}时"
    if seconds > 3600:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}时{m}分"
    if seconds > 60:
        return f"{int(seconds // 60)}分"
    return "即将重置"


# --- Icon Generator ---

def make_icon_pixmap(size=64, alert=False):
    """Volcano Ark themed icon: fiery gradient with ark silhouette + data bars."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.Antialiasing)

    s = size
    pad = s * 0.10
    r = s * 0.24

    if alert:
        bg_grad = QLinearGradient(0, 0, s, s)
        bg_grad.setColorAt(0, QColor("#cc1133"))
        bg_grad.setColorAt(0.5, QColor("#ff3355"))
        bg_grad.setColorAt(1, QColor("#ff6688"))
    else:
        bg_grad = QLinearGradient(0, 0, s, s)
        bg_grad.setColorAt(0, QColor("#ff6b35"))
        bg_grad.setColorAt(0.5, QColor("#f7931e"))
        bg_grad.setColorAt(1, QColor("#ffc107"))
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

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(int(min(value_pct, 100)))
        bar.setTextVisible(False)
        bar.setFixedHeight(16)
        bar.setStyleSheet(f"""
            QProgressBar {{
                background: {C_BG_LIGHT}; border: none; border-radius: 8px;
            }}
            QProgressBar::chunk {{
                background: {color}; border-radius: 8px;
            }}
        """)

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
        is_alert, time_rem_pct = calc_alert(usage_pct, remaining_seconds, total_seconds)

        bg = C_CARD_ALERT if is_alert else C_CARD
        border = "#4a1820" if is_alert else C_BORDER_LITE
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

        if is_alert:
            badge = QLabel("超额")
            badge.setAlignment(Qt.AlignCenter)
            badge.setFixedHeight(16)
            badge.setStyleSheet(f"""
                background: {C_RED_DIM}; color: {C_ALERT_RED};
                border-radius: 8px; padding: 0 6px;
                font-size: 9px; font-weight: bold; border: none;
            """)
            top.addWidget(badge)
        else:
            badge = QLabel("正常")
            badge.setAlignment(Qt.AlignCenter)
            badge.setFixedHeight(16)
            badge.setStyleSheet(f"""
                background: {C_GREEN_DIM}; color: {C_NEON_GREEN};
                border-radius: 8px; padding: 0 6px;
                font-size: 9px; font-weight: bold; border: none;
            """)
            top.addWidget(badge)
        lay.addLayout(top)

        # Quota bar: green when OK, red when alert
        quota_color = C_ALERT_RED if is_alert else C_NEON_GREEN
        lay.addWidget(BarRow("额度余", remaining_pct, quota_color))

        # Time bar: always neon blue
        lay.addWidget(BarRow("时间余", time_rem_pct, C_NEON_BLUE))


# --- Config Dialog ---

class ConfigDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("用量监控配置")
        self.setFixedSize(320, 360)
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
        cfg = {
            "ak": self.ak_input.text().strip(),
            "sk": self.sk_input.text().strip(),
            "region": self.region_input.text().strip() or "cn-beijing",
            "kimi_credential_path": self.kimi_path_input.text().strip(),
        }
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

    # --- Slide animation property ---

    def _get_slide_y(self):
        return self.y()

    def _set_slide_y(self, y):
        x = self.x()
        self.move(x, int(y))

    slide_y = pyqtProperty(int, _get_slide_y, _set_slide_y)

    def _animate_show(self):
        self._reposition()
        screen = QApplication.primaryScreen()
        if not screen:
            self.show()
            return
        geo = screen.availableGeometry()
        start_y = geo.top() - self.height()
        end_y = geo.top()
        self.move(self.x(), start_y)
        self.show()
        self._anim = QPropertyAnimation(self, b"slide_y")
        self._anim.setDuration(300)
        self._anim.setStartValue(start_y)
        self._anim.setEndValue(end_y)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.start()

    def _animate_hide(self):
        screen = QApplication.primaryScreen()
        if not screen:
            self.hide()
            self._edge_timer.start()
            return
        geo = screen.availableGeometry()
        start_y = self.y()
        end_y = geo.top() - self.height()
        self._anim = QPropertyAnimation(self, b"slide_y")
        self._anim.setDuration(300)
        self._anim.setStartValue(start_y)
        self._anim.setEndValue(end_y)
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
        layout.addLayout(self._section_header("🔥", "火山方舟"))
        layout.addLayout(self._period_cards(volc, ["session", "weekly", "monthly"]))

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

        # Footer
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 10, 0)
        footer.setSpacing(6)
        update_time = volc.get("update_time", datetime.now())
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

        layout.addLayout(footer)

        frame_lay.addWidget(body)
        outer.addWidget(self._frame)

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

class ArkMonitorTray(QSystemTrayIcon):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.data = None
        self.panel = None
        self._is_alert = False

        self._update_icon()
        self.setToolTip("套餐用量")
        self._setup_menu()
        self._refresh()

        self.activated.connect(self._on_activated)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(REFRESH_INTERVAL_MS)

    def _update_icon(self):
        self.setIcon(QIcon(make_icon_pixmap(32, alert=self._is_alert)))

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
        refresh.triggered.connect(self._refresh)
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

    def _refresh(self):
        try:
            self.data = fetch_all_usage()
        except Exception as e:
            log.error("fetch_all_usage failed: %s", e)
            self.setToolTip("套餐用量监控 - 获取失败")
            return

        volc = self.data.get("volc") or {}
        kimi = self.data.get("kimi")

        volc_sess = (volc.get("periods") or {}).get("session", {})
        kimi_sess = (kimi.get("periods") or {}).get("session", {}) if kimi else {}

        volc_alert = self._session_alert(volc_sess)
        kimi_alert = self._session_alert(kimi_sess)
        self._is_alert = volc_alert or kimi_alert

        # Tooltip: both providers' 5h snapshot.
        parts = []
        v_pct = volc_sess.get("remaining_pct", 0)
        v_str = fmt_remaining(volc_sess.get("remaining_seconds", 0))
        parts.append(f"火5h余:{v_pct:.0f}%|{v_str}")
        if kimi:
            k_pct = kimi_sess.get("remaining_pct", 0)
            k_str = fmt_remaining(kimi_sess.get("remaining_seconds", 0))
            parts.append(f"K5h余:{k_pct:.0f}%|{k_str}")
        else:
            parts.append("Kimi:未授权")
        self.setToolTip("  ".join(parts))
        self._update_icon()

    @staticmethod
    def _session_alert(period):
        """True when quota remaining% < time remaining% for a 5h window."""
        if not period:
            return False
        rem_pct = period.get("remaining_pct", 0)
        total = period.get("total_seconds", 0)
        rem_sec = period.get("remaining_seconds", 0)
        time_rem_pct = rem_sec / total * 100 if total > 0 else 0
        return rem_pct < time_rem_pct

    def _show_panel(self):
        if self.data is None:
            self._refresh()

        if self.panel is not None:
            self.panel.close()
            self.panel = None

        self.panel = UsagePanel(self.data)
        self.panel.configChanged.connect(self._refresh)
        self.panel.showRequested.connect(self._show_panel)
        self.panel._animate_show()

        # In auto-hide mode, start the hide timer since tray click
        # puts mouse at tray (not over panel)
        if not self.panel._pinned:
            self.panel._hide_timer.start()


# --- Main ---

SINGLE_INSTANCE_SERVER = "volc_ark_monitor_single_instance"


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # --- Single Instance Check ---
    socket = QLocalSocket()
    socket.connectToServer(SINGLE_INSTANCE_SERVER)
    if socket.waitForConnected(500):
        # Another instance is already running, just exit
        socket.close()
        log.info("Another instance is already running, exiting")
        return
    socket.close()

    # Create local server for new instance
    local_server = QLocalServer()
    local_server.removeServer(SINGLE_INSTANCE_SERVER)  # Clean up any leftover
    if not local_server.listen(SINGLE_INSTANCE_SERVER):
        log.error("Failed to start single instance server")

    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.warning("System tray not available")

    tray = ArkMonitorTray(app)
    tray.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
