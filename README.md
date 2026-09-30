# 套餐用量

火山引擎方舟（Volcengine Ark）与 Kimi Code 套餐用量监控系统托盘小部件。

## 功能特性

- 📊 **实时用量监控** - 火山方舟显示近5小时、本周、本月三个周期；Kimi Code 显示近5小时、本周、本月（订阅周期）套餐额度
- 💜 **Kimi Code 套餐监控** - 复用本地 `kimi` CLI 的 OAuth 凭证，无需 API Key
- 📅 **Kimi 月度总用量** - 订阅页的"总使用量"（含 Kimi 对话 + Code），由后台脚本每 5 分钟从网页网关抓取（cron）
- 🔴 **智能告警** - 额度耗尽的卡片标红「耗尽」，消耗速度超过时间进度的标橙「消耗偏快」；托盘图标按订阅现状显示健康度：蓝=两家正常，黄=有厂商余量偏低（≤15%），橙=一家耗尽，红=全耗尽。判定看各家权益本体（火山=月度/周度取紧、Kimi=月度订阅余额），而非短期限速窗
- 🖥️ **系统托盘集成** - 常驻系统托盘，鼠标悬停即见两家关键数据
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

### Kimi 月度总用量

面板里的「本月」卡片对应订阅页（www.kimi.com/settings/subscription）的"总使用量"，含 Kimi 对话与 Code 两部分，按订阅周期（每月续费日）重置。CLI 的 `/coding/v1/usages` 接口只暴露 5h/7d 窗口，月度数在网页网关 `GetSubscriptionStats` 里，且只认网页版的 HS512 token（CLI 的 OAuth token 被拒）。

`kimi_monthly_refresher.py`（cron 每 5 分钟）从 360 浏览器的 localStorage（leveldb）只读地取最新网页 access token——页面开着时会每 15 分钟自新——调网关后把结果写入 `~/.kimi_monthly_cache.json`。脚本刻意**不刷新 refresh token**：脚本侧轮换会让浏览器保存的旧令牌失效，把网页登录态搞掉。因此数据新鲜度取决于最近是否打开过 kimi.com；缓存超过 30 分钟未更新时数字会保留旧值（月度用量变化缓慢，影响有限）。

## 运行

```bash
python3 volc_ark_monitor.py
```

代码结构：`monitor_data.py`（纯数据层：两家用量抓取/告警计算，无 Qt 依赖）+ `monitor_ui.py`（PyQt5 界面）+ `volc_ark_monitor.py`（入口，.desktop 指向它）+ `kimi_monthly_refresher.py`（Kimi 月度缓存定时刷新，cron）。

## 桌面快捷方式与开机自启

```bash
cp coding-plan-monitor.desktop ~/.local/share/applications/
cp coding-plan-monitor.desktop ~/.config/autostart/   # 开机自启
```

## 许可证

MIT License
