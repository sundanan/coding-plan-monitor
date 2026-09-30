#!/usr/bin/env python3
"""Headless refresher for the Kimi monthly subscription usage cache.

Launched by cron every 5 minutes. Kimi's monthly 总使用量 (the number shown
on www.kimi.com/settings/subscription) is only served by the web gateway
(apiv2 ...MembershipService/GetSubscriptionStats), which rejects the kimi
CLI's OAuth token (ES256) — it requires the web session's HS512 access
token. That token lives in the 360 browser's localStorage and is rotated by
the page itself every ~15 min while kimi.com is open.

So: read-only scan of the browser's Local Storage leveldb for the newest
access token; if it is still fresh, call GetSubscriptionStats and atomically
rewrite ~/.kimi_monthly_cache.json. Never touches the refresh token — a
script-side refresh would rotate it and log the browser session out.

If the token is stale (browser hasn't had kimi.com open recently) the cache
is left untouched; the monitor panel marks the monthly card as stale.
"""

import base64
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.expanduser("~/.kimi_monthly_cache.json")

LS_DIR = os.path.expanduser(
    "~/.config/com.360.browser/Default/Local Storage/leveldb")
STATS_URL = ("https://www.kimi.com/apiv2/"
             "kimi.gateway.membership.v2.MembershipService/GetSubscriptionStats")
# DOMAIN_NEXUS (1) is rejected by the stats endpoint ("must not be in list
# [1]"); the subscription page queries DOMAIN_KIMI (2), whose
# subscriptionBalance spans both Kimi chat and Code usage (the balance's own
# domain field echoes DOMAIN_NEXUS).
REQUEST_BODY = {"domain": "DOMAIN_KIMI"}

MIN_TOKEN_TTL_S = 90
LOG_PATH = os.path.join(BASE_DIR, "kimi_monthly_refresh.log")


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)


def load_access_token():
    """Newest iss=account/typ=access JWT from the 360 browser localStorage."""
    if not os.path.isdir(LS_DIR):
        return None
    files = sorted(glob.glob(os.path.join(LS_DIR, "*.ldb"))) + \
        sorted(glob.glob(os.path.join(LS_DIR, "*.log")))
    if not files:
        return None

    # Snapshot: a live browser may be mid-write on the real files.
    tmpdir = tempfile.mkdtemp(prefix="kimi_ls_")
    try:
        local = []
        for i, path in enumerate(files):
            dst = os.path.join(tmpdir, f"f{i}")
            try:
                shutil.copy2(path, dst)
                local.append(dst)
            except Exception:
                continue

        jwt_re = re.compile(
            rb'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}')
        best = None  # (exp, token)
        for path in local:
            try:
                raw = open(path, "rb").read()
            except Exception:
                continue
            for m in jwt_re.finditer(raw):
                tok = m.group().decode("ascii", "replace")
                try:
                    payload_b64 = tok.split(".")[1]
                    payload = json.loads(base64.urlsafe_b64decode(
                        payload_b64 + "=" * (-len(payload_b64) % 4)))
                except Exception:
                    continue
                if payload.get("iss") != "account" or \
                        payload.get("typ") != "access":
                    continue
                exp = payload.get("exp", 0)
                if best is None or exp > best[0]:
                    best = (exp, tok)
        return best[1] if best else None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def fetch_stats(access_token):
    """Call GetSubscriptionStats. Returns parsed JSON dict; raises on error."""
    body = json.dumps(REQUEST_BODY).encode()
    req = urllib.request.Request(STATS_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "Accept": "application/json",
        "X-Msh-Platform": "web",
        "User-Agent": ("Mozilla/5.0 (X11; Linux aarch64) "
                       "AppleWebKit/537.36 Chrome/126 Safari/537.36"),
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _num(v):
    """proto doubles may arrive as number or string."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def extract(payload):
    """Pull the monthly balance + code window ratios out of the response."""
    out = {}
    bal = payload.get("subscriptionBalance") or {}
    ratio = _num(bal.get("amountUsedRatio"))
    if ratio is not None:
        out["monthlyUsedRatio"] = ratio
    code_ratio = _num(bal.get("kimiCodeUsedRatio"))
    if code_ratio is not None:
        out["monthlyCodeRatio"] = code_ratio
    if bal.get("expireTime"):
        out["monthlyExpireTime"] = bal["expireTime"]
    if bal.get("displayName"):
        out["planName"] = bal["displayName"]

    for key, field in (("code5hRatio", "ratelimitCode5h"),
                       ("code7dRatio", "ratelimitCode7d"),
                       ("kimi5hRatio", "ratelimit5h"),
                       ("kimi7dRatio", "ratelimit7d")):
        stat = payload.get(field) or {}
        r = _num(stat.get("ratio"))
        if r is not None:
            out[key] = r
        if stat.get("resetTime"):
            out[key.replace("Ratio", "Reset")] = stat["resetTime"]
    return out


def main():
    token = load_access_token()
    if not token:
        log("未找到 web 端 access token（浏览器未登录或 localStorage 为空）")
        return 1
    try:
        payload_b64 = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(
            payload_b64 + "=" * (-len(payload_b64) % 4)))
        ttl = payload.get("exp", 0) - time.time()
    except Exception:
        ttl = -1
    if ttl < MIN_TOKEN_TTL_S:
        log(f"access token 已过期或临期（剩余 {ttl:.0f}s），跳过本次抓取")
        return 0

    try:
        data = fetch_stats(token)
    except urllib.error.HTTPError as e:
        log(f"GetSubscriptionStats HTTP {e.code}: "
            f"{e.read().decode('utf-8', 'replace')[:200]}")
        return 1
    except Exception as e:
        log(f"GetSubscriptionStats 失败: {type(e).__name__}: {e}")
        return 1

    parsed = extract(data)
    if "monthlyUsedRatio" not in parsed:
        log(f"响应缺少 subscriptionBalance.amountUsedRatio: {str(data)[:300]}")
        return 1

    parsed["fetchedAt"] = int(time.time() * 1000)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(parsed, f, indent=2)
    os.replace(tmp, CACHE_PATH)
    log(f"刷新成功 monthly={parsed['monthlyUsedRatio'] * 100:.2f}% "
        f"code7d={parsed.get('code7dRatio', 0) * 100:.2f}% "
        f"expire={parsed.get('monthlyExpireTime', '?')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
