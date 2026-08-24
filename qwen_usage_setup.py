#!/usr/bin/env python3
"""LEGACY one-time setup for the Qwen Token Plan usage refresher.

NOTE (2026-08-17): the window opened by this script does not become visible
on this DDE-Wayland machine, so this flow is deprecated. The supported way
to (re-)authorize is qwen_cookie_import.py, which copies the login cookies
from the daily 360 browser — no window needed.

Original purpose: open a real Chromium window (persistent profile), let the
user log in and open the Token Plan page, watch the network for the usage
API response, then record the billing page URL and write the first cache.

Usage: python3 qwen_usage_setup.py
"""

import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "qwen_refresh_config.json")
CACHE_PATH = os.path.expanduser("~/.qwen_usage_cache.json")
PROFILE_DIR = os.path.expanduser("~/.config/qwen-usage-refresh/profile")
START_URL = "https://platform.qianwenai.com/"
USAGE_API_MARK = "tokenplan"
TIMEOUT_S = 600

CHROMIUM = "/usr/bin/chromium"
CHROME_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
]


def extract_usage(payload, sink):
    """Merge the nested usage fields from one API response into sink."""
    try:
        inner = payload["data"]["DataV2"]["data"]["data"]
    except (KeyError, TypeError):
        return False
    if not isinstance(inner, dict):
        return False
    found = False
    for key in ("per5HourPercentage", "per1WeekPercentage",
                "per5HourResetTime", "per1WeekResetTime"):
        if key in inner:
            sink[key] = inner[key]
            found = True
    return found


def main():
    print("=" * 62)
    print("千问 Token Plan 自动刷新 · 一次性设置")
    print("=" * 62)
    print("即将打开一个 Chromium 浏览器窗口，请按顺序操作：")
    print()
    print("  第 1 步：在窗口里登录你的千问/阿里云账号")
    print("  第 2 步：登录后，打开显示 Token Plan 额度的页面")
    print("          （就是能看到「5小时/周」用量百分比的那个页面）")
    print()
    print("脚本会自动识别，成功后窗口会自动关闭。最多等待 10 分钟。")
    print("=" * 62)

    captured = {}
    captured_url = {"url": None}

    def on_response(resp):
        url = resp.url
        if "api.json" not in url or USAGE_API_MARK not in url:
            return
        if resp.status != 200:
            return
        try:
            payload = resp.json()
        except Exception:
            return
        if extract_usage(payload, captured):
            if not captured_url["url"]:
                try:
                    captured_url["url"] = resp.request.page.url
                except Exception:
                    pass
            print(f"  ✓ 捕获到用量数据: {sorted(captured)}")

    os.makedirs(PROFILE_DIR, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            executable_path=CHROMIUM,
            headless=False,
            args=CHROME_ARGS,
            viewport=None,
        )
        ctx.on("response", on_response)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(START_URL, timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"打开页面失败: {e}")
            ctx.close()
            return 1

        deadline = time.time() + TIMEOUT_S
        need = ("per1WeekPercentage",)
        try:
            while time.time() < deadline:
                if all(k in captured for k in need):
                    # give the page a moment to deliver the 5h window too
                    time.sleep(3)
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            ctx.close()

    if not all(k in captured for k in need):
        print("\n✗ 超时：未捕获到用量数据。")
        print("  请确认已登录，并打开了显示 Token Plan 额度的页面，然后重试。")
        return 1

    billing_url = captured_url["url"] or START_URL
    config = {
        "billing_url": billing_url,
        "profile_dir": PROFILE_DIR,
        "chromium": CHROMIUM,
        "configured_at": int(time.time()),
    }
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    captured["fetchedAt"] = int(time.time() * 1000)
    with open(CACHE_PATH, "w") as f:
        json.dump(captured, f, indent=2)

    print()
    print("✅ 设置完成！")
    print(f"   账单页: {billing_url}")
    print(f"   已捕获: {sorted(k for k in captured if k != 'fetchedAt')}")
    print(f"   登录状态保存在: {PROFILE_DIR}")
    print("   接下来会安装每 5 分钟一次的后台刷新任务。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
