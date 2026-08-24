# 套餐用量

火山引擎方舟（Volcengine Ark）、Kimi Code 与千问 Token Plan 套餐用量监控系统托盘小部件。

## 功能特性

- 📊 **实时用量监控** - 火山方舟显示近5小时、本周、本月三个周期；Kimi Code 显示近5小时、本周套餐额度；千问显示 Token Plan 各时间窗口用量与套餐等级（lite/standard/pro）
- 💜 **Kimi Code 套餐监控** - 复用本地 `kimi` CLI 的 OAuth 凭证，无需 API Key
- 🔮 **千问 Token Plan 监控** - 后台无头浏览器每 5 分钟抓取实时用量写入缓存，面板优先读缓存；`qianwen` CLI 仅作降级数据源
- 🔴 **智能告警** - 额度耗尽的卡片标红「耗尽」，消耗速度超过时间进度的标橙「消耗偏快」；托盘图标颜色按尚有额度的厂商数量变化（蓝3/黄2/橙1/红0）
- 🖥️ **系统托盘集成** - 常驻系统托盘，鼠标悬停即见三家关键数据
- 🎯 **边缘触发** - 鼠标移到屏幕顶部边缘自动弹出详情面板
- 📌 **固定模式** - 支持将面板固定到桌面
- 🎨 **精美UI** - 深色主题，渐变图标，平滑动画

## 截图

![应用图标](icon.png)

## 安装依赖

```bash
pip install PyQt5 volcengine
```

## 配置

### 火山方舟

首次运行点击面板右下角「火山」按钮配置 AK/SK：

```json
{
  "ak": "your-access-key",
  "sk": "your-secret-key",
  "region": "cn-beijing"
}
```

未配置时将使用模拟数据展示效果。

### Kimi Code

Kimi 通过 OAuth 登录，复用本地 `kimi` CLI 的凭证（`~/.kimi-code/credentials/kimi-code.json`），监控应用会自动读取并在 token 过期时用 refresh_token 续期写回。

首次使用请在终端完成一次登录授权：

```bash
kimi login
```

按提示在浏览器中输入验证码完成授权即可。之后监控应用会自动读取并刷新 token，无需重复登录。

如需指定自定义凭证路径，可在「Kimi」配置项中填写 `kimi_credential_path`（留空用默认路径）。

凭证缺失或失效时，Kimi 区块显示「未授权」占位，不影响火山方舟正常监控。

### 千问 Token Plan

千问用量由后台无头浏览器（360 浏览器内核 + Playwright）每 5 分钟自动抓取（cron），把实时用量与套餐等级（`specCode`）写入 `~/.qwen_usage_cache.json`，面板优先读缓存。之所以走浏览器，是因为阿里 WAF 会在 TLS 层拦截所有非浏览器客户端，只有真实浏览器引擎能取到数据。

首次使用（会话过期、面板提示「缓存较旧」时同样处理）：

1. 用 360 浏览器登录 `platform.qianwenai.com`；
2. 终端执行 `python3 qwen_cookie_import.py`，把登录态导入自动化 profile。

`qianwen` CLI（[QianWen CLI](https://www.npmjs.com/package/@qianwenai/qianwen-cli)）仅作降级数据源：缓存缺失时执行 `qianwen usage summary --format json`。CLI 不在默认 PATH 时可在「千问」配置项填写 `qianwen_cli_path`（留空自动探测）。

缓存与 CLI 都不可用时千问区块显示占位，不影响其他厂商监控。

## 运行

```bash
python3 volc_ark_monitor.py
```

代码结构：`monitor_data.py`（纯数据层：三家用量抓取/告警计算，无 Qt 依赖）+ `monitor_ui.py`（PyQt5 界面）+ `volc_ark_monitor.py`（入口，.desktop 指向它）。

## 桌面快捷方式与开机自启

```bash
cp coding-plan-monitor.desktop ~/.local/share/applications/
cp coding-plan-monitor.desktop ~/.config/autostart/   # 开机自启
```

## 许可证

MIT License
