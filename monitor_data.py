"""Data layer for the 套餐用量 tray monitor.

Provider usage fetchers (Volcengine Ark OpenAPI, Kimi Code usages API +
web-gateway monthly cache) plus shared config, alert and formatting
helpers. Deliberately Qt-free so it stays testable headless.
"""

import json
import os
import logging
import logging.handlers
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
KIMI_ME_URL = "https://api.kimi.com/coding/v1/me"
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

# Monthly (subscription-cycle) usage source: ~/.kimi_monthly_cache.json,
# rewritten every 5 minutes by kimi_monthly_refresher.py (cron). The monthly
# 总使用量 is only served by the www.kimi.com web gateway, which rejects the
# CLI's OAuth token, so the refresher borrows the browser's web access token
# from localStorage (read-only; never rotates the refresh token).
KIMI_MONTHLY_CACHE_PATH = os.path.expanduser("~/.kimi_monthly_cache.json")
KIMI_MONTHLY_STALE_S = 30 * 60

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


def _kimi_call_me(access_token):
    """GET /coding/v1/me — account info, incl. user_level_name (plan tier).

    The /usages payload carries no user/membership object, so the plan tier
    shown in the section header (e.g. "Moderato") comes from here. Returns
    None on any failure; caller degrades to an empty badge.
    """
    req = urllib.request.Request(KIMI_ME_URL, method="GET", headers={
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("kimi me fetch failed: %s", e)
        return None


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


def _kimi_monthly_from_cache():
    """Read the monthly subscription usage from the refresher's cache.

    The web GetSubscriptionStats response carries subscriptionBalance
    (amountUsedRatio = 本月总用量 incl. Kimi chat + Code, expireTime =
    subscription renewal/reset) plus ratelimitCode5h/7d — the same numbers
    the subscription page renders. Returns the unified "monthly" period dict
    or None when the cache is missing/unusable. extra.stale marks a cache
    older than KIMI_MONTHLY_STALE_S (browser hasn't refreshed its web token).
    """
    try:
        with open(KIMI_MONTHLY_CACHE_PATH) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("kimi monthly cache read failed: %s", e)
        return None

    ratio = raw.get("monthlyUsedRatio")
    if ratio is None:
        return None
    try:
        usage_pct = float(ratio) * 100.0
    except (TypeError, ValueError):
        return None
    usage_pct = max(0.0, min(usage_pct, 100.0))

    reset_time = None
    if raw.get("monthlyExpireTime"):
        reset_time = _parse_kimi_time(raw["monthlyExpireTime"])
    now = datetime.now(timezone.utc)
    rem_sec = (reset_time - now).total_seconds() if reset_time else 0

    age_s = None
    extra = {"source": "web-cache"}
    fetched_at = raw.get("fetchedAt")
    if fetched_at:
        try:
            age_s = time.time() - float(fetched_at) / 1000
            if age_s > KIMI_MONTHLY_STALE_S:
                extra["stale"] = True
        except Exception:
            pass

    if raw.get("monthlyCodeRatio") is not None:
        extra["code_used_pct"] = round(float(raw["monthlyCodeRatio"]) * 100.0, 2)

    period = {
        "name": "本月", "level": "monthly",
        "usage_pct": usage_pct, "remaining_pct": max(100.0 - usage_pct, 0.0),
        "reset_time": reset_time,
        "remaining_seconds": max(rem_sec, 0),
        "total_seconds": 31 * 24 * 3600,
        "extra": extra,
    }
    log.info("kimi monthly (cache): %.2f%%, age=%s",
             usage_pct, f"{age_s:.0f}s" if age_s is not None else "unknown")
    return period


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

    # Monthly (subscription-cycle) usage from the web gateway cache — the
    # CLI usages endpoint only exposes 5h/weekly windows.
    monthly = _kimi_monthly_from_cache()
    if monthly:
        periods["monthly"] = monthly

    me = _kimi_call_me(access_token)
    membership = ""
    if me:
        membership = (me.get("user_level_name")
                      or KIMI_PLAN_NAMES.get(me.get("user_level"), ""))
    extra = {"membership": membership,
             "parallel": (data.get("parallel", {}) or {}).get("limit", "")}

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


def fetch_all_usage():
    """Fetch Volcengine Ark and Kimi Code usage.

    Volc failures fall back to mock (existing behaviour); Kimi failure
    degrades to a monthly-only dict from the web cache (or None) so the
    panel can show a placeholder without breaking the other section.
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
        # The CLI OAuth may be dead (e.g. refresh token revoked after days
        # offline) while the web-cache monthly number is still good — keep
        # the monthly card alive instead of blanking the whole section.
        monthly = _kimi_monthly_from_cache()
        if monthly:
            kimi = {"status": "Kimi Code", "update_time": datetime.now(),
                    "periods": {"monthly": monthly}, "source": "kimi",
                    "extra": {}}

    return {"volc": volc, "kimi": kimi}


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


