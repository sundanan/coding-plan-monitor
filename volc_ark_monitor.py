#!/usr/bin/env python3
"""套餐用量 — tray widget entry point.

Volcengine Ark / Kimi Code / Qwen Token Plan quota monitor. The logic lives
in monitor_data (fetchers) and monitor_ui (PyQt5 widgets); this module only
wires them together. It keeps the historical filename because the .desktop
launchers point at it.
"""

import sys

from PyQt5.QtWidgets import QApplication, QSystemTrayIcon
from PyQt5.QtNetwork import QLocalSocket, QLocalServer

from monitor_data import log
from monitor_ui import ArkMonitorTray

# Backward-compatible re-exports for older scripts/notes importing from here.
from monitor_data import (  # noqa: F401
    load_config, fetch_all_usage, calc_alert, fmt_remaining,
    _fetch_via_sdk, _kimi_ensure_token, _kimi_call_usages,
    _fetch_qwen_usage_from_cache,
)
from monitor_ui import UsagePanel, make_icon_pixmap  # noqa: F401

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
