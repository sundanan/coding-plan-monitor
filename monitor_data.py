"""Data layer for the 套餐用量 tray monitor.

Provider usage fetchers (Volcengine Ark OpenAPI, Kimi Code usages API,
Qwen Token Plan browser cache / qianwen CLI) plus shared config, alert and
formatting helpers. Deliberately Qt-free so it stays testable headless.
"""

import json
import os
import glob
import logging
import logging.handlers
import shutil
import subprocess
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

# --- Config ---

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# --- Kimi Code OAuth / Usage endpoints (reuses local kimi CLI credentials) ---

KIMI_DEFAULT_CRED_PATH = os.path.expanduser("~/.kimi-code/credentials/kimi-code.json")
KIMI_OAUTH_TOKEN_URL = "https://auth.kimi.com/api/oauth/token"
KIMI_OAUTH_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
KIMI_USAGE_URL = "https://api.kimi.com/coding/v1/usages"
KIMI_TOKEN_SKEW_S = 60  # refresh if access_token expires within this many seconds

# membershipLevel enum -> plan display name (Kimi's plans are named after musical
# tempo terms; the /usages endpoint returns only the enum, so we map it locally).
KIMI_PLAN_NAMES = {
    "LEVEL_FREE": "Adagio",
    "LEVEL_TRIAL": "Andante",
    "LEVEL_BASIC": "Moderato",
    "LEVEL_INTERMEDIATE": "Allegretto",
    "LEVEL_ADVANCED": "Allegro",
}

# --- Qwen Token Plan usage sources ---
# Primary: ~/.qwen_usage_cache.json — real-time 5h/7d ratios rewritten every
# 5 minutes by qwen_usage_refresher.py (cron + headless browser capture;
# Alibaba's WAF blocks all plain non-browser clients, so a real browser
# engine is required).
# Fallback: qianwen CLI's own OAuth session (no AK/SK needed).

QIANWEN_CLI_TIMEOUT_S = 30
QWEN_USAGE_CACHE_PATH = os.path.expanduser("~/.qwen_usage_cache.json")
QWEN_CACHE_STALE_S = 30 * 60  # UI warns when the cache is older than this

# INFO keeps urllib3's DEBUG HTTP chatter out; rotation caps disk usage
# (the old DEBUG setup grew the log past 12 MB in a few weeks).
import logging.handlers

log = logging.getLogger("coding_plan_monitor")
if not log.handlers:
    log.setLevel(logging.INFO)
    _log_handler = logging.handlers.RotatingFileHandler(
        os.path.join(BASE_DIR, "monitor.log"),
        maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    _log_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_log_handler)

# --- Data Layer ---

def load_config():
    default = {
        "ak": "", "sk": "", "region": "cn-beijing",
        "kimi_credential_path": "",  # empty -> default ~/.kimi-code/...
        "volc_plan_name": "",  # Coding Plan tier badge, e.g. "Coding Plan Pro"
        "qianwen_cli_path": "",  # empty -> auto-detect `qianwen` binary
        "qwen_plan_name": "",  # Token Plan badge, e.g. "Token Plan"
        "qwen_usage_cache_path": "",  # empty -> ~/.qwen_usage_cache.json
        "qwen_cache_stale_min": "",  # empty -> 30 (minutes)
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
    if not (ak and sk):
        # No credentials yet: demo with mock data (badge says MOCK).
        log.info("AK/SK not configured; using mock data")
        return _mock_data()
    try:
        return _fetch_via_sdk(ak, sk, cfg.get("region", "cn-beijing"))
    except Exception as e:
        # Configured but failing: show a real error instead of fake numbers.
        log.error("volc fetch failed: %s", e)
        return {"status": "Error", "source": "error", "error": str(e),
                "periods": {}, "update_time": datetime.now()}


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
    now = datetime.now(timezone.utc)
    total_map = {"session": 5 * 3600, "weekly": 7 * 24 * 3600, "monthly": 31 * 24 * 3600}
    name_map = {"session": "近5小时", "weekly": "本周", "monthly": "本月"}
    periods = {}
    for item in quota_usage:
        level = item.get("Level", "")
        if level not in total_map:
            continue
        percent = item.get("Percent", 0)
        reset_ts = item.get("ResetTimestamp", 0)
        reset_dt = datetime.fromtimestamp(reset_ts, tz=timezone.utc) if reset_ts else None
        rem = (reset_dt - now).total_seconds() if reset_dt else 0
        periods[level] = {
            "name": name_map[level], "level": level,
            "usage_pct": percent, "remaining_pct": 100 - percent,
            "reset_time": reset_dt, "remaining_seconds": max(rem, 0),
            "total_seconds": total_map[level],
        }
    log.info("API data: status=%s, periods=%s", status,
             {k: f"{v['usage_pct']:.1f}%" for k, v in periods.items()})

    # Show each window's raw percentage, exactly like the Ark web console.
    # (An earlier version clamped 5h/weekly remaining to the monthly one,
    # which made the panel disagree with the console.) The monthly ceiling
    # is still honoured by the icon-level "has quota" check in _refresh().

    # update_time is display-only; keep it local wall time (never UTC).
    return {"status": status,
            "update_time": datetime.fromtimestamp(update_ts) if update_ts else datetime.now(),
            "periods": periods, "source": "api"}


def _mock_data():
    from datetime import timedelta
    now = datetime.now()
    now_utc = datetime.now(timezone.utc)  # reset_time values are tz-aware
    periods = {
        "session": {"name": "近5小时", "level": "session", "usage_pct": 12.0,
                    "remaining_pct": 88.0, "reset_time": now_utc + timedelta(hours=3, minutes=20),
                    "remaining_seconds": 3.33 * 3600, "total_seconds": 5 * 3600},
        "weekly":  {"name": "本周",  "level": "weekly",  "usage_pct": 5.0,
                    "remaining_pct": 95.0, "reset_time": now_utc + timedelta(days=5),
                    "remaining_seconds": 5 * 24 * 3600, "total_seconds": 7 * 24 * 3600},
        "monthly": {"name": "本月",  "level": "monthly", "usage_pct": 15.0,
                    "remaining_pct": 85.0, "reset_time": now_utc + timedelta(days=20),
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

        # Cap 5h remaining by 5× the weekly plan's remaining quota.
        # A week contains roughly 5 independent 5h windows, so the effective
        # 5h ceiling is weekly_remaining × 5. When the weekly plan is nearly
        # exhausted the 5h window may still report a full bucket, which is
        # misleading -- effective 5h remaining can't exceed that ceiling.
        raw_remaining_int = remaining_int
        capped_by = None
        weekly = periods.get("weekly")
        if weekly:
            w_limit = weekly.get("limit", 0)
            w_used = weekly.get("used", 0)
            weekly_remaining_count = (w_limit - w_used) if w_limit else None
            weekly_5x = weekly_remaining_count * 5 if weekly_remaining_count is not None else None
            if weekly_5x is not None and weekly_5x < remaining_int:
                log.info("kimi 5h remaining capped %d -> %d by weekly×5 (%d×5)",
                         remaining_int, weekly_5x, weekly_remaining_count)
                remaining_int = weekly_5x
                capped_by = "weekly"

        effective_used = max(limit_int - remaining_int, 0)
        sess = _build_kimi_period(
            "近5小时", "session", str(effective_used), limit, reset_time, 5 * 3600)
        if capped_by:
            raw_usage_pct = ((limit_int - raw_remaining_int) / limit_int * 100) if limit_int > 0 else 0.0
            sess["capped_by"] = capped_by
            sess["raw_remaining"] = raw_remaining_int
            sess["raw_usage_pct"] = raw_usage_pct
            sess["raw_remaining_pct"] = max(100.0 - raw_usage_pct, 0.0)
        periods["session"] = sess
        break

    raw_level = (data.get("user", {}) or {}).get("membership", {}).get("level", "")
    extra = {
        "membership": KIMI_PLAN_NAMES.get(raw_level, raw_level),
        "parallel": (data.get("parallel", {}) or {}).get("limit", ""),
    }

    log.info("kimi usage: periods=%s, extra=%s, capped=%s",
             {k: f"{v['usage_pct']:.1f}%" for k, v in periods.items()}, extra,
             {k: f"raw={v.get('raw_remaining_pct', '?')}%" for k, v in periods.items() if v.get("capped_by")})

    return {
        "status": "Kimi Code",
        "update_time": now,
        "periods": periods,
        "source": "kimi",
        "extra": extra,
    }


# --- Qwen Token Plan Data Layer ---
#
# Primary source: ~/.qwen_usage_cache.json, a file the user refreshes from the
# browser (DevTools Network -> Copy response on the billing page). It carries
# the real-time 5h/7d usage ratios that no non-browser client can fetch
# directly (WAF blocks curl/Python at the TLS level).
# Fallback: QianWen CLI (`qianwen usage summary --format json`). The CLI holds
# its own OAuth device-flow credentials (first-time setup: `qianwen auth
# login`), but v1.3.0 falsely reports not_subscribed on Gray accounts, so the
# cache wins whenever it holds usable data.
# All failures raise; the caller degrades to None and the panel shows a hint
# without breaking the other sections.

def _qianwen_cli_path():
    """Locate the qianwen CLI binary.

    Order: config `qianwen_cli_path` -> PATH -> well-known install locations
    (the panel is usually launched from a .desktop file whose PATH lacks the
    user's npm-global / nvm bin dirs).
    """
    cfg = load_config()
    p = cfg.get("qianwen_cli_path", "")
    if p and os.path.exists(p):
        return p
    w = shutil.which("qianwen")
    if w:
        return w
    candidates = [os.path.expanduser("~/.npm-global/bin/qianwen")]
    candidates += sorted(glob.glob(
        os.path.expanduser("~/.nvm/versions/node/*/bin/qianwen")))
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    return None


def _build_qwen_period(name, level, used, limit, reset_time, total_seconds):
    """Convert a Qwen usage window into the unified period dict shape."""
    used = float(used) if used not in (None, "") else 0
    limit = float(limit) if limit not in (None, "") else 0
    usage_pct = (used / limit * 100) if limit > 0 else 0.0
    remaining_pct = max(100.0 - usage_pct, 0.0)
    now = datetime.now(timezone.utc)
    if reset_time and reset_time.tzinfo is None:
        reset_time = reset_time.replace(tzinfo=timezone.utc)
    rem_sec = (reset_time - now).total_seconds() if reset_time else 0
    return {
        "name": name, "level": level,
        "usage_pct": usage_pct, "remaining_pct": remaining_pct,
        "reset_time": reset_time,
        "remaining_seconds": max(rem_sec, 0),
        "total_seconds": total_seconds,
        "limit": limit, "used": used,
    }


def _qwen_cache_path():
    """Cache file location, overridable via config `qwen_usage_cache_path`."""
    p = load_config().get("qwen_usage_cache_path", "")
    return os.path.expanduser(p) if p else QWEN_USAGE_CACHE_PATH


def _qwen_stale_s():
    """Staleness threshold in seconds, overridable via `qwen_cache_stale_min`."""
    try:
        mins = float(load_config().get("qwen_cache_stale_min") or 0)
    except (TypeError, ValueError):
        mins = 0
    return mins * 60 if mins > 0 else QWEN_CACHE_STALE_S


def _qwen_cache_status():
    """Human-readable cache freshness line for the config dialog."""
    try:
        with open(_qwen_cache_path()) as f:
            raw = json.load(f)
        fetched = float(raw.get("fetchedAt") or 0)
    except Exception:
        return "缓存文件不存在 · 等待定时任务生成"
    if not fetched:
        return "缓存缺少 fetchedAt 字段"
    age = max(time.time() - fetched / 1000, 0)
    if age < 90:
        return f"缓存更新于 {int(age)} 秒前"
    if age < 5400:
        return f"缓存更新于 {int(age / 60)} 分钟前"
    return f"缓存更新于 {age / 3600:.1f} 小时前"


def _fetch_qwen_usage_from_cache():
    """Read real-time 5h/7d usage ratios from the browser-generated cache.

    The cache is rewritten every 5 minutes by qwen_usage_refresher.py (cron),
    which captures the usage API response with a headless browser — the only
    way past Alibaba's WAF. Expected fields: per5HourPercentage /
    per1WeekPercentage (0-1 fractions), per5HourResetTime / per1WeekResetTime
    (epoch ms), fetchedAt (epoch ms). Any subset is fine — the page sometimes
    returns only the weekly window. Returns the unified usage dict, or None
    when the file is missing or has no usable fields (caller then falls back
    to the CLI).
    """
    cache_path = _qwen_cache_path()
    try:
        with open(cache_path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("qwen cache read failed: %s", e)
        return None

    def _epoch_ms_dt(v):
        try:
            return datetime.fromtimestamp(float(v) / 1000, tz=timezone.utc)
        except Exception:
            return None

    periods = {}
    win_specs = [
        ("per5HourPercentage", "per5HourResetTime", "session", "近5小时", 5 * 3600),
        ("per1WeekPercentage", "per1WeekResetTime", "weekly", "本周", 7 * 24 * 3600),
    ]
    for pct_key, reset_key, level, name, total_s in win_specs:
        pct = raw.get(pct_key)
        if pct is None:
            continue
        periods[level] = _build_qwen_period(
            name, level, float(pct) * 100, 100,
            _epoch_ms_dt(raw.get(reset_key)), total_s)
    if not periods:
        return None

    extra = {"source": "browser-cache"}
    # Plan tier (e.g. "standard") captured from the subscription endpoint.
    spec = raw.get("specCode")
    if spec:
        extra["plan"] = str(spec)
    age_s = None
    fetched_at = raw.get("fetchedAt")
    if fetched_at:
        try:
            age_s = time.time() - float(fetched_at) / 1000
            if age_s > _qwen_stale_s():
                extra["stale"] = True
        except Exception:
            pass

    log.info("qwen usage (cache): periods=%s, age=%s",
             {k: f"{v['usage_pct']:.1f}%" for k, v in periods.items()},
             f"{age_s:.0f}s" if age_s is not None else "unknown")

    return {
        "status": "Qwen Token Plan",
        "update_time": datetime.now(),
        "periods": periods,
        "source": "browser-cache",
        "extra": extra,
    }


def _fetch_qwen_usage():
    """Fetch Qwen Token Plan usage: browser cache first, CLI as fallback.

    The cache path is the only way to get real-time 5h/7d ratios (WAF blocks
    non-browser clients); the CLI fallback parses the `token_plan` section of
    `qianwen usage summary --format json`. When the account has no Token Plan
    subscription the CLI result carries an empty `periods` map plus
    extra.not_subscribed, and the panel shows a placeholder instead of period
    cards.
    """
    cached = _fetch_qwen_usage_from_cache()
    if cached:
        return cached

    cli = _qianwen_cli_path()
    if not cli:
        raise RuntimeError(
            "qianwen CLI not found; install with "
            "`npm install -g @qianwenai/qianwen-cli`")

    try:
        proc = subprocess.run(
            [cli, "usage", "summary", "--format", "json"],
            capture_output=True, text=True, timeout=QIANWEN_CLI_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise RuntimeError("qianwen CLI timed out")
    if proc.returncode == 2:
        raise RuntimeError("qianwen CLI not authenticated; run `qianwen auth login`")
    if proc.returncode != 0:
        raise RuntimeError(
            f"qianwen CLI exit {proc.returncode}: {proc.stderr.strip()[:200]}")

    # The CLI may print non-JSON notices before the document; skip to '{'.
    out = proc.stdout
    start = out.find("{")
    if start < 0:
        raise RuntimeError(f"qianwen CLI returned no JSON: {out[:200]}")
    data = json.loads(out[start:])

    now = datetime.now()
    periods = {}
    extra = {}

    tp = data.get("token_plan") or data.get("coding_plan") or {}
    if not tp.get("subscribed"):
        extra["not_subscribed"] = True
    else:
        if tp.get("plan"):
            extra["plan"] = tp["plan"]
        windows = tp.get("windows") or {}
        win_specs = [
            ("per_5h", "session", "近5小时", 5 * 3600),
            ("weekly", "weekly", "本周", 7 * 24 * 3600),
            ("monthly", "monthly", "本月", 31 * 24 * 3600),
        ]
        for wkey, level, name, total_s in win_specs:
            w = windows.get(wkey)
            if not w:
                continue
            used_pct = w.get("used_pct")
            total = w.get("total")
            remaining = w.get("remaining")
            if used_pct is None and total:
                used_pct = (total - (remaining or 0)) / total * 100
            reset_time = _parse_kimi_time(
                w.get("resetTime") or w.get("resetDate") or w.get("reset_time"))
            period = _build_qwen_period(
                name, level, str(used_pct or 0), "100", reset_time, total_s)
            if total:
                period["limit"] = total
                period["used"] = total - (remaining or 0)
            periods[level] = period

    log.info("qwen usage: periods=%s, extra=%s",
             {k: f"{v['usage_pct']:.1f}%" for k, v in periods.items()}, extra)

    return {
        "status": "Qwen Token Plan",
        "update_time": now,
        "periods": periods,
        "source": "qianwen-cli",
        "extra": extra,
    }


def fetch_all_usage():
    """Fetch Volcengine Ark, Kimi Code, and Qwen Token Plan usage.

    Volc failures fall back to mock (existing behaviour); Kimi/Qwen failures
    degrade to None so the panel can show a placeholder without breaking
    the other sections.
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

    qwen = None
    try:
        qwen = _fetch_qwen_usage()
    except Exception as e:
        log.error("qwen fetch failed: %s", e)

    return {"volc": volc, "kimi": kimi, "qwen": qwen}


def calc_alert(usage_pct, remaining_seconds, total_seconds):
    """Classify one period window.

    Returns (state, time_rem_pct) where state is:
      "exhausted" - quota gone (< 0.01% left, same epsilon as the icon logic)
      "fast"      - spending faster than the linear pace (quota remaining %
                    below time remaining %) — a warning, not exhaustion
      "ok"        - everything fine
    """
    time_rem = remaining_seconds / total_seconds * 100 if total_seconds > 0 else 0
    quota_rem = 100 - usage_pct
    if quota_rem < 0.01:
        return "exhausted", time_rem
    if quota_rem < time_rem:
        return "fast", time_rem
    return "ok", time_rem


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


