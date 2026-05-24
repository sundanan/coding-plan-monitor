# 火山方舟用量监控

火山引擎方舟（Volcengine Ark）Coding Plan 用量监控系统托盘小部件。

## 功能特性

- 📊 **实时用量监控** - 显示近5小时、本周、本月三个周期的额度使用情况
- 🔴 **智能告警** - 额度剩余百分比 < 时间剩余百分比时自动变红告警
- 🖥️ **系统托盘集成** - 常驻系统托盘，鼠标悬停即见关键数据
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

首次运行点击面板右下角「火山」按钮配置 AK/SK：

```json
{
  "ak": "your-access-key",
  "sk": "your-secret-key",
  "region": "cn-beijing"
}
```

未配置时将使用模拟数据展示效果。

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
