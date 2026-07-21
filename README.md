# 套餐用量

火山引擎方舟（Volcengine Ark）与 Kimi Code 套餐用量监控系统托盘小部件。

## 功能特性

- 📊 **实时用量监控** - 火山方舟显示近5小时、本周、本月三个周期；Kimi Code 显示近5小时、本周套餐额度
- 💜 **Kimi Code 套餐监控** - 复用本地 `kimi` CLI 的 OAuth 凭证，无需 API Key
- 🔴 **智能告警** - 任一厂商额度剩余百分比 < 时间剩余百分比时托盘图标变红
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

## 运行

```bash
python3 volc_ark_monitor.py
```

## 创建桌面快捷方式

```bash
cp volc-ark-monitor.desktop ~/.local/share/applications/
```

## 许可证

MIT License
