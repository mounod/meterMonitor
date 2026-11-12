# meter-monitor

校园预付费电表余量监控，PC 端部署，定时查询剩余电量并通过飞书机器人推送简报、阈值告警、失败告警和周期报告。

零第三方依赖，只用 Python 标准库；不需要浏览器、不需要 Cookie、不需要登录。

> 适用前提：电费查询系统提供 `POST /SJ/sydl.shtml` 这类免登录表单接口，返回纯文本 `剩余电量:xx.xx`。本项目按某高校自助购电系统实现，**其他系统需要修改 `post_form` 和 `parse_kwh` 两个函数**。

## 功能

- 按分组查询多个房间，每组独立 Webhook，组数和组内目标数不设上限
- 运行窗口与推送窗口分离：窗口外只采集入库，不打扰
- 三种独立通知通道，各自开关：
  - 常规简报：每天 / 两天 / 三天一推，可指定推送小时
  - 阈值告警：余量从正常跌入低于阈值时推一次，持续低不重复轰炸
  - 失败告警：连续 N 次采集失败后推送
- 中断补告警：距上次成功采集超过 `stale_hours` 小时，**无视推送窗口**立即告警
- 周报 / 月报：逐日列出用电量，自动识别充值并按电价反推当日真实用电
- 数据落地 SQLite，方便后续画趋势图或二次分析

## 快速开始

```bash
git clone https://github.com/<you>/meter-monitor.git
cd meter-monitor
cp config.example.json config.json
```

编辑 `config.json`：

1. 把 `base_url` 改成实际电费系统地址
2. 填 `groups[].webhook`，飞书群 → 设置 → 群机器人 → 添加自定义机器人，复制 Webhook
3. 按实际房间填 `targets[].lh` / `fj`，`fj` 通常带“照明 / 空调”后缀
4. 按需调 `threshold_kwh`、`digest_interval_days`、`reports[].enabled`

验证：

```bash
python -m json.tool config.json          # 语法检查
python meter_monitor.py --test-feishu    # 每组发一条测试消息
python meter_monitor.py --dry-run        # 采集不推送，看日志
python meter_monitor.py                  # 正式跑一轮
```

## 定时运行（Windows）

每 2 小时触发，08:00 起，后台运行不弹窗：

```bat
schtasks /Create /TN "MeterMonitor" /TR "C:\Meter\run.bat" /SC HOURLY /MO 2 /ST 08:00 /F
```

```powershell
$t = Get-ScheduledTask -TaskName "MeterMonitor"
$t.Settings.StartWhenAvailable = $true
$t.Settings.RestartCount = 3
$t.Settings.RestartInterval = "PT10M"
$t.Principal.LogonType = "S4U"
Set-ScheduledTask -TaskName "MeterMonitor" -Settings $t.Settings -Principal $t.Principal
```

| 设置 | 作用 |
|---|---|
| `StartWhenAvailable` | 关机错过的触发，开机后补跑 |
| `RestartCount` + `RestartInterval` | 启动失败每 10 分钟重试，最多 3 次 |
| `LogonType = S4U` | 后台会话运行，不弹 cmd 窗口 |

手动验证：

```bat
schtasks /Run /TN "MeterMonitor"
```

如果任务计划里找不到 `python`，把 `run.bat` 改成绝对路径：

```bat
@echo off
cd /d %~dp0
"C:\Users\<you>\AppData\Local\Programs\Python\Python315\python.exe" meter_monitor.py
```

Linux / macOS 用 cron：

```cron
0 8-22/2 * * * cd /path/to/meter-monitor && python3 meter_monitor.py
```

## 配置说明

| 字段 | 说明 |
|---|---|
| `base_url` | 电费系统根地址，脚本会 POST 到 `/SJ/sydl.shtml` |
| `run_window` | 采集窗口，`[7,23)` 表示 7:00–22:59 才查网 |
| `notify.window` | 推送窗口，简报/阈值/失败/报告只在此区间发出 |
| `digest_enabled` | 常规简报总开关 |
| `digest_interval_days` | `1` 每天、`2` 两天一推、`3` 三天一推 |
| `digest_push_hour` | 当天几点后允许推简报，需与任务计划触发点对齐 |
| `threshold_alert_enabled` | 低于阈值告警开关，状态变化触发 |
| `fail_alert_after` | 连续失败几次告警 |
| `stale_hours` | 超过多少小时无成功采集立即告警，`0` 关闭 |
| `reports[].enabled` | 周报/月报各自独立开关 |
| `charge_price_per_kwh` | 电价，用于充值反推 |
| `groups[].webhook` | 每组独立飞书 Webhook |

**注意**：`digest_push_hour` 必须小于任务计划最后一次有效触发的小时，否则简报永远不会发出。例如 `/ST 08:00 /MO 2` 的最后一次有效触发是 22:00，`digest_push_hour` 设 20 或 21 都可以，设 23 就会静默失效。

## 报告样例

```
【周报 2026-11-05 ~ 2026-11-12】示例分组

51415照明
  期初 20.00 → 期末 15.44
  期间用电 4.56 度，期间充值 20.00 度，日均 0.65 度，预计可用 23.8 天
  每日用电：
    11-06  0.52 度
    11-07  0.61 度
    11-08  0.43 度
    11-09  0.48 度（充值 20.00 度）
    11-10  0.55 度
    11-11  0.50 度
    11-12  0.47 度
```

充值当天按“最小合法充值量”反推：充值度数只能是整数或 `整数 / 电价`，取满足余额增加量的最小值，剩余部分即当日真实用电量。

## 数据表

```sql
CREATE TABLE meter (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, group_name TEXT, name TEXT, lh TEXT, fj TEXT,
  kwh REAL, ok INTEGER, err TEXT
);
CREATE TABLE state (
  key TEXT, group_name TEXT, value TEXT, ts TEXT,
  PRIMARY KEY (key, group_name)
);
```

`meter` 存每次采集结果，`state` 存简报/阈值/中断/报告的去重状态。

## 常见问题

**Q：飞书机器人开了签名校验怎么办？**
当前脚本只支持 Webhook 直连，先不要开启签名校验。需要签名时在 `send_feishu` 里补 `timestamp + sign` 字段。

**Q：为什么阈值不出现在推送文案里？**
按需求设计，文案只写“余量偏低”，不暴露具体阈值。想改文案改 `fmt_reading`。

**Q：任务计划跑了但没推送？**
依次检查：当前小时是否在 `notify.window` 内；`digest_interval_days` 是否到期；`state` 表里 `__digest__` 的 `ts` 是否已更新；Webhook 是否填在对应 `group` 下。

**Q：怎么加更多房间？**
在对应组的 `targets` 数组里追加对象，`name` 唯一即可。新增组就复制一个 `groups` 元素，换 `name` 和 `webhook`。

## 免责声明

本项目仅用于个人合法查询自己的电费账户，请遵守所在学校/单位的系统使用规定，控制请求频率，不要用于批量扫描或压力测试。因滥用导致的账号封禁或服务异常，作者不承担责任。

## License

MIT