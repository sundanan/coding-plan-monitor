// 千问 Token Plan 实时用量查询（浏览器控制台脚本）
// 用法：登录 https://platform.qianwenai.com 任意页面 → F12 Console → 粘贴运行
// 结果会自动复制到剪贴板，然后在终端执行：xclip -o > ~/.qwen_usage_cache.json
(async () => {
  const url =
    "https://cs-data.qianwenai.com/data/api.json?product=sfm_bailian" +
    "&action=BroadScopeAspnGateway" +
    "&api=zeldaHttp.apikeyMgr.%2Ftokenplan%2Fpersonal%2Fapi%2Fv2%2Fusage";
  try {
    const resp = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: "",
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const json = await resp.json();
    const inner = json?.data?.DataV2?.data?.data;
    if (!inner || inner.per1WeekPercentage === undefined) {
      throw new Error("响应结构异常: " + JSON.stringify(json).slice(0, 400));
    }
    const cache = {
      per5HourPercentage: inner.per5HourPercentage,
      per1WeekPercentage: inner.per1WeekPercentage,
      per5HourResetTime: inner.per5HourResetTime,
      per1WeekResetTime: inner.per1WeekResetTime,
      fetchedAt: Date.now(),
    };
    const text = JSON.stringify(cache, null, 2);
    console.log("Token Plan usage:\n" + text);
    try {
      await navigator.clipboard.writeText(text);
      console.log(
        "✅ 已复制到剪贴板。请在终端执行: xclip -o > ~/.qwen_usage_cache.json"
      );
    } catch (e) {
      window.prompt("剪贴板写入失败，请手动复制:", text);
    }
  } catch (err) {
    console.error("❌ 查询失败:", err.message);
    console.error("请确认当前页面是 platform.qianwenai.com 且已登录。");
  }
})();
