#!/usr/bin/env python3
"""Import the qianwenai/aliyun login cookies from the 360 browser profile
into the automation browser profile used by qwen_usage_refresher.py.

Both browsers encrypt cookies with the Linux "basic" scheme (v10:
AES-128-CBC, PBKDF2 key from the fixed password "peanuts"), so values are
decrypted and re-encrypted with the identical parameters. Session cookies
get a 30-day client-side expiry so they survive browser restarts.

The destination table schema is introspected, so any Chromium version
(90 or 126) works as the target.

Re-run this whenever the refresher reports an expired session — it is
enough that the daily browser (360) is logged in; no extra window needed.

Usage: python3 qwen_cookie_import.py [target Cookies DB path]
"""

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import time

from Crypto.Cipher import AES

SRC_PROFILE = os.path.expanduser("~/.config/com.360.browser/Default")
DEFAULT_DST = os.path.expanduser(
    "~/.config/qwen-usage-refresh/profile126/Default/Cookies")
DOMAINS = ("%qianwenai%", "%aliyun%", "%alibaba%")
EXPIRE_DELTA_US = 30 * 24 * 3600 * 1000 * 1000  # 30 days in Chromium µs

SALT = b"saltysalt"
IV = b" " * 16


def derive_key():
    return hashlib.pbkdf2_hmac("sha1", b"peanuts", SALT, 1, 16)


def unpad(b):
    if not b:
        return b
    pad = b[-1]
    if isinstance(pad, int) and 1 <= pad <= 16 and b.endswith(bytes([pad]) * pad):
        return b[:-pad]
    return b


def decrypt_v10(blob, key):
    if blob[:3] == b"v10":
        cipher = AES.new(key, AES.MODE_CBC, IV)
        return unpad(cipher.decrypt(blob[3:]))
    return blob  # plaintext


def encrypt_v10(value, key):
    raw = value.encode("utf-8") if isinstance(value, str) else value
    pad = 16 - (len(raw) % 16)
    cipher = AES.new(key, AES.MODE_CBC, IV)
    return b"v10" + cipher.encrypt(raw + bytes([pad]) * pad)


def chrome_now_us():
    # Chromium timestamps are µs since 1601-01-01
    return int((time.time() + 11644473600) * 1000000)


def main():
    dst_cookies = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DST
    key = derive_key()

    src_db = os.path.join(SRC_PROFILE, "Cookies")
    if not os.path.exists(src_db):
        print(f"未找到 360 浏览器 Cookies: {src_db}")
        return 1

    # Snapshot the source DB so a running browser can't interfere.
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copy(src_db, tmp.name)
    for suffix in ("-journal", "-wal", "-shm"):
        extra = src_db + suffix
        if os.path.exists(extra):
            shutil.copy(extra, tmp.name + suffix)

    src = sqlite3.connect(f"file:{tmp.name}?mode=ro", uri=True)
    cur = src.cursor()
    where = " OR ".join("host_key LIKE ?" for _ in DOMAINS)
    cur.execute(
        f"""SELECT host_key, name, value, encrypted_value, path, expires_utc,
                   is_secure, is_httponly, has_expires, is_persistent,
                   priority, samesite, source_scheme, source_port
            FROM cookies WHERE {where}""",
        DOMAINS)
    rows = cur.fetchall()
    src.close()
    os.unlink(tmp.name)
    if not rows:
        print("360 浏览器里没有相关域名的 Cookie —— 请先在 360 浏览器里登录平台")
        return 1

    imported = []
    for (host, name, value, enc, path, expires, secure, httponly,
         has_exp, persistent, prio, samesite, scheme, port) in rows:
        plain = decrypt_v10(enc, key) if enc else (value or "")
        if not plain:
            continue
        imported.append((host, name, plain.decode("utf-8", "replace"), path,
                         expires, secure, httponly, has_exp,
                         prio, samesite, scheme, port))
    print(f"从 360 浏览器解出 {len(imported)} 条相关 Cookie")

    if not os.path.exists(dst_cookies):
        print(f"目标 Cookies 不存在: {dst_cookies}")
        print("请先用目标浏览器启动一次生成配置目录")
        return 1

    dst = sqlite3.connect(dst_cookies)
    dcur = dst.cursor()
    dcur.execute("PRAGMA table_info(cookies)")
    dcols = [r[1] for r in dcur.fetchall()]
    now_us = chrome_now_us()
    future_us = now_us + EXPIRE_DELTA_US

    n = 0
    for (host, name, plain, path, expires, secure, httponly,
         has_exp, prio, samesite, scheme, port) in imported:
        vals = {
            "creation_utc": now_us + n,
            "host_key": host, "name": name, "value": "", "path": path,
            # Session cookies would be dropped on browser restart; force a
            # 30-day client-side expiry (server only ever sees the value).
            "expires_utc": expires if has_exp else future_us,
            "is_secure": secure, "is_httponly": httponly,
            "last_access_utc": now_us, "has_expires": 1, "is_persistent": 1,
            "priority": prio, "encrypted_value": encrypt_v10(plain, key),
            "samesite": samesite, "source_scheme": scheme,
            "source_port": port if port is not None else -1,
            # newer-schema extras
            "top_frame_site_key": "", "last_update_utc": now_us,
            "source_type": 0, "has_cross_site_ancestor": 0,
            "is_same_party": 0,
        }
        dcur.execute(
            "DELETE FROM cookies WHERE host_key=? AND name=? AND path=?",
            (host, name, path))
        cols = [c for c in dcols if c in vals]
        ph = ",".join("?" for _ in cols)
        dcur.execute(
            f"INSERT INTO cookies ({','.join(cols)}) VALUES ({ph})",
            [vals[c] for c in cols])
        n += 1
    dst.commit()
    dst.close()

    names = sorted({r[1] for r in imported})
    print(f"已写入 {n} 条到: {dst_cookies}")
    print("关键 Cookie:", ", ".join(
        x for x in names if x.startswith("login_")) or "(无 login_* 项)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
