#!/usr/bin/env python3
"""Headless refresher for the Qwen Token Plan usage cache.

Launched by cron every 5 minutes. Opens the billing page in a headless
Chromium that reuses the persistent profile created by qwen_usage_setup.py,
captures the usage API response the page itself requests (the only way past
Alibaba's WAF), and atomically rewrites ~/.qwen_usage_cache.json.

If nothing is captured the cache is left untouched — the monitor panel then
shows its "缓存较旧" warning, which usually means the login session expired
and qwen_usage_setup.py needs to be run again.
"""

import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "qwen_refresh_config.json")
CACHE_PATH = os.path.expanduser("~/.qwen_usage_cache.json")
WAIT_S = 25
NAV_TIMEOUT_MS = 40000

CHROME_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
]


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def extract_usage(payload, sink):
    try:
        inner = payload["data"]["DataV2"]["data"]["data"]
    except (KeyError, TypeError):
        return False
    if not isinstance(inner, dict):
        return False
    found = False
    # usage endpoint: real-time window ratios.
    for key in ("per5HourPercentage", "per1WeekPercentage",
                "per5HourResetTime", "per1WeekResetTime"):
        if key in inner:
            sink[key] = inner[key]
            found = True
    # subscription endpoint: plan tier (specCode, e.g. lite/standard/pro)
    # shown as the section badge in the monitor panel.
    if "specCode" in inner:
        sink["specCode"] = inner["specCode"]
        found = True
    return found


def main():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except Exception:
        log("未找到配置，请先运行 qwen_usage_setup.py")
        return 2

    from playwright.sync_api import sync_playwright

    captured = {}

    def on_response(resp):
        url = resp.url
        if "api.json" not in url or "tokenplan" not in url or resp.status != 200:
            return
        try:
            payload = resp.json()
        except Exception:
            return
        extract_usage(payload, captured)

    chromium = cfg.get("chromium", "/usr/bin/chromium")
    rc = 0
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            cfg["profile_dir"],
            executable_path=chromium,
            headless=True,
            args=CHROME_ARGS,
        )
        ctx.on("response", on_response)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(cfg["billing_url"], timeout=NAV_TIMEOUT_MS,
                      wait_until="domcontentloaded")
            # wait_for_timeout pumps the Playwright event loop (a bare
            # time.sleep would never deliver on_response callbacks).
            for _ in range(WAIT_S):
                if "per1WeekPercentage" in captured:
                    break
                page.wait_for_timeout(1000)
            page.wait_for_timeout(2000)  # let a 2nd response (5h window) land
            # the subscription response (plan tier) usually lands with the
            # usage one; give it a few extra seconds when it hasn't yet.
            for _ in range(5):
                if "specCode" in captured:
                    break
                page.wait_for_timeout(1000)
        except Exception as e:
            log(f"页面加载异常: {e}")
        finally:
            ctx.close()

    if "per1WeekPercentage" not in captured and "per5HourPercentage" not in captured:
        log("未捕获用量数据：登录会话可能已过期，请重新运行 qwen_cookie_import.py")
        return 1

    captured["fetchedAt"] = int(time.time() * 1000)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(captured, f, indent=2)
    os.replace(tmp, CACHE_PATH)
    pcts = {k: f"{v * 100:.2f}%" for k, v in captured.items()
            if k.endswith("Percentage")}
    log(f"刷新成功 {pcts}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
