# 排障手册：cc-switch-hermes-usage

## A. 验证 CC Switch 集成是否正常（不依赖 UI）

CC Switch 是 Tauri 应用，可用 WebView2 远程调试直调后端命令验证。
这是本 skill 开发时确认数据链路的实测方法，排障时同样适用：

```powershell
# 1. 带调试端口启动（用完务必重启干净实例）
#    把 <安装路径> 换成你的 cc-switch.exe 实际位置
Get-Process cc-switch -ErrorAction SilentlyContinue | Stop-Process -Force
$env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = "--remote-debugging-port=9222"
Start-Process "<安装路径>\cc-switch.exe"
Start-Sleep 8
Invoke-RestMethod http://127.0.0.1:9222/json   # 取 webSocketDebuggerUrl

# 2. Node 脚本连接页面并调用 Tauri 命令（Node >= 22 内置 WebSocket）
#    window.__TAURI_INTERNALS__.invoke('get_provider_stats', { startDate, endDate })
#    startDate/endDate 为 i64 Unix 秒；filters 结构见 references/schema.md
```

> ⚠️ 用完记得关掉调试端口：停止进程后**不带**该环境变量重新启动，避免 9222 端口长期开放。

预期返回值（宽日期范围）应含：
```json
{"providerId":"_hermes_session","providerName":"Hermes Agent","requestCount":N,...}
```

## B. 症状 → 原因 → 处理

### B1. usage 页"全部来源"下拉没有 "Hermes Agent"

| 排查 | 命令 / 方法 |
|---|---|
| 是否根本没同步 | `python scripts\hermes_usage_sync.py --check` + `verify_hermes_usage.py` |
| 日期范围太窄（最常见） | usage 页日期默认"当天"；hermes 数据若是历史的，切"近30天"/自定义 |
| App 筛选不是"全部" | App 筛选是快捷按钮行，不含 hermes；保持"全部"，用来源下拉精确筛 |
| 前端未刷新 | 页面 60s 自动刷新；或退出设置页重进 |

### B2. 数字不对

| 现象 | 原因 | 处理 |
|---|---|---|
| 偏大（约 2 倍或倍数增长） | 旧版全量脚本 + 新脚本 delta 双计 | 仅此情形用 `--reset-baseline`（先备份两个 db） |
| verify 报 WARN「上游丢历史」 | Hermes 升级/清理删了老会话聚合行，属常态 | **不用处理**。CC==账本分毫不差即健康；别 reset |
| 偏小 | 计数器重置把中间量当基线丢了 | 已知限制（cumulative 源无逐次事件）；可对比 Hermes 桌面版 billing 页人工核对 |
| 成本全 0 或"未定价" | model_pricing 无该模型 | 在 usage 页"成本定价"里补价，下次同步兜底计算生效 |
| cost_status=unknown | Hermes 未结算 | 脚本回退 estimated → pricing 计算 |

### B3. 同步脚本失败

| 退出码 | 场景 | 处理 |
|---|---|---|
| 10 | `%LOCALAPPDATA%\hermes\state.db` 不存在 | 装/跑一次 Hermes 桌面版产生 state.db |
| 11 | 缺 sessions/session_model_usage | Hermes 版本过旧，升级 |
| 20/21 | CC Switch DB 路径不对 | `--cc-db` 显式指定（`~\.cc-switch\cc-switch.db`） |
| 30 | 写入被锁 / IO 错误 | 退出 CC Switch（释放 SQLite 锁）后重试；或停掉占用进程 |
| 40 | 历史记录无状态 | `--reset-baseline`（唯一危险操作，仅删 hermes_session 记录） |

### B4. SQLite 锁（ERROR[30] database is locked）

- CC Switch 高频写（60s 轮询 usage）。脚本已用 `timeout=10` + `BEGIN IMMEDIATE`。
- 仍失败：临时退出 CC Switch → 同步 → 重开。
- 长期方案：把计划任务安排在低活跃时段，或改用 WAL（CC Switch 侧决定，脚本只读 Hermes 不影响）。

## C. Windows 计划任务维护

```powershell
# 状态
Get-ScheduledTask -TaskName HermesUsageSync | Get-ScheduledTaskInfo

# 手动触发
Start-ScheduledTask -TaskName HermesUsageSync

# 修改频率（示例 15 分钟）
$t = Get-ScheduledTask -TaskName HermesUsageSync
$t.Triggers[0].Repetition.Interval = "PT15M"
Set-ScheduledTask -TaskName HermesUsageSync -Trigger $t.Triggers

# 删除
Unregister-ScheduledTask -TaskName HermesUsageSync -Confirm:$false
```

## D. 上游 PR 追踪（合入后本 skill 定位变化）

| PR | 内容 | 状态动作 |
|---|---|---|
| #6120 feat(usage): sync aggregate Hermes usage | 官方 Rust+前端 Hermes 用量同步（snapshot+delta、unattributed_main 残差对账） | 合入且本地升级后：本 skill 停同步改验证；残差对账逻辑可参考其 spec |
| #6694 fix(sessions): Hermes sessions visible in Session Manager | 会话管理器可见性 | 影响会话管理页，不影响本 skill |
| #7702 feat(universal-provider): CodeBuddy and Hermes | universal provider 配置投影 | 影响配置写入，不影响本 skill |

检查合入状态：
```powershell
gh pr view 6120 --repo farion1231/cc-switch --json state,mergedAt
```

## E. 数据一致性人工核对

Hermes 桌面版自身数据：`%LOCALAPPDATA%\hermes\state.db`：

```powershell
sqlite3 "$env:LOCALAPPDATA\hermes\state.db" "SELECT model, SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens) FROM session_model_usage GROUP BY model;"
```

与 CC Switch 对比：
```powershell
sqlite3 "$HOME\.cc-switch\cc-switch.db" "SELECT model, SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens) FROM proxy_request_logs WHERE data_source='hermes_session' GROUP BY model;"
```

对账分两条线，别混：
1. **CC Switch vs 本地账本** `%LOCALAPPDATA%\cc-switch-hermes-usage\sync-state.db`
   的 `snapshots`（`SELECT SUM(input),SUM(output),SUM(cache_read) FROM snapshots`）。
   这两个必须**逐维相等**——不等才是同步故障（半写入/算错）。
2. **本地账本 vs Hermes 实时累计**。账本**允许大于**实时值：Hermes 升级/清理会删
   老会话聚合行（0.21.2→0.21.5 实测一次删 10 个会话、input 差 882,626）。
   这不是故障，历史已安全落在账本/CC，verify 只会 WARN，**不要**为此跑 --reset-baseline。
只有确认是「旧全量脚本+新 delta 双计」（数字约 2 倍/按轮翻倍）时，才备份后用 --reset-baseline。

## F. Windows 中文控制台下的编码问题

### F1. 脚本输出乱码

控制台默认 GBK/cp936，脚本输出 UTF-8 中文时显示为乱码。**不影响数据正确性**，
只影响显示。要正确显示：

```powershell
$env:PYTHONIOENCODING = "utf-8"   # 仅影响本次会话
python scripts\hermes_usage_sync.py --verbose
```

若要在脚本内部也保证，运行时带上 `python -X utf8 scripts\hermes_usage_sync.py`。

### F2. 自建测试时 `r.stdout` 变成 `None`

如果你的测试用 `subprocess.run(..., text=True)` 跑本脚本，且脚本带 `--verbose`，
在非 UTF-8 locale 上会抛 `UnicodeDecodeError`（发生在读取器线程里），
`r.stdout` 变 `None`，断言随之报出难以定位的 `TypeError`。
**两侧都要对齐编码**，参考 `tests/test_hermes_sync.py` 里的 `run_sync()`：

```python
env["PYTHONIOENCODING"] = "utf-8"
subprocess.run(cmd, capture_output=True, text=True,
               encoding="utf-8", errors="replace", env=env)
```

### F3. 访问 GitHub 超时（国内网络常见）

`github.com` 常连不上，但 `api.github.com` 往往正常。若 git/gh/gh-capi 类工具卡住：

```powershell
# 先探测
curl.exe -s -o NUL -w "%{http_code} %{time_total}s" --max-time 15 https://github.com
curl.exe -s -o NUL -w "%{http_code} %{time_total}s" --max-time 15 https://api.github.com

# 若前者超时、后者正常，给命令行走代理
$env:HTTPS_PROXY = "http://127.0.0.1:<你的代理端口>"
$env:HTTP_PROXY  = "http://127.0.0.1:<你的代理端口>"
```

常见本地代理端口：clash 系 7890 / 7897，v2ray 系 10809 / 1080。
**端口以你自己代理软件的实际配置为准**，先用
`Get-NetTCPConnection -State Listen` 确认。
注意：浏览器能用不代表命令行能用，代理环境变量只对命令行进程生效。
