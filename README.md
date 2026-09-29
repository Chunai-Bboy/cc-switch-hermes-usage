# cc-switch-hermes-usage

把 **Hermes Agent** 的 token 用量同步到 **CC Switch** 的「使用统计」页面。

[English below](#english)

---

## 这是什么

[CC Switch](https://github.com/farion1231/cc-switch) 是一个 AI 编程工具的供应商切换器，
它的「使用统计」页面能汇总 Claude Code、Codex、OpenCode 等多个工具的 token 消耗。
但它的官方版本**没有统计 Hermes Agent**（Nous Research 的 agent）。

本项目解决这件事：读取 Hermes 本地数据库里的用量计数器，写入 CC Switch 的统计库，
让 Hermes 的用量和其他工具一起显示在同一张看板上。

**它是一个 opencode skill**（也可用作独立脚本），不修改 CC Switch 的后端代码。

## 为什么官方没有这个功能

上游曾实现过又主动下架，维护者解释是：

> "Hermes 会把同一进程内的所有 API 调用聚合进单条 session 记录、且模型字段锁定为
> 初始模型，导致仪表盘无法准确展示按次计费明细……所以暂时下线了这部分集成。"

官方 PR [#6120](https://github.com/farion1231/cc-switch/pull/6120) 尝试重新实现，
至今（2026-09-29）仍处于 open 状态。

**本项目部分绕开了这个卡点**：它读的是 `session_model_usage` 表——这张表按
`(会话, 模型, 任务类型)` 分行存储累计值，所以**模型归属是准确的**（维护者遇到的
"模型字段锁定" 问题在这里不存在）。实测同一会话里 3 个不同模型各自独立成行。

但**逐次请求的粒度问题依然存在**，见下方「已知局限」。

## 数据从哪来到哪去

```
Hermes Agent
  %LOCALAPPDATA%\hermes\state.db
  表 session_model_usage（累计计数器：input/output/cache_read/cache_write/api_call_count）
        │
        │  本脚本：每 30 分钟读一次，用「这次 − 上次」算出增量
        │  状态存在 %LOCALAPPDATA%\cc-switch-hermes-usage\sync-state.db
        ▼
CC Switch
  %USERPROFILE%\.cc-switch\cc-switch.db
  表 proxy_request_logs（app_type='hermes', data_source='hermes_session'）
        │
        ▼
CC Switch UI → 设置 → 使用统计 → Hermes 按钮
```

## 安装

前置条件：

- Windows（计划任务部分用了 PowerShell；数据同步脚本本身跨平台）
- [CC Switch](https://github.com/farion1231/cc-switch) 3.20.4 已安装并运行过
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) 至少运行过一次
  （需已生成 `state.db`）
- Python 3.8+（仅用标准库，无需 pip 安装任何东西）

作为 opencode skill 使用（推荐）：

```powershell
git clone https://github.com/Chunai-Bboy/cc-switch-hermes-usage.git `
  "$env:USERPROFILE\.config\opencode\skills\cc-switch-hermes-usage"
```

作为独立脚本使用：直接下载 `scripts/` 目录即可。

## 使用

```powershell
$sk = "$env:USERPROFILE\.config\opencode\skills\cc-switch-hermes-usage"

python $sk\scripts\hermes_usage_sync.py --check     # 健康检查，不写入
python $sk\scripts\hermes_usage_sync.py --dry-run -v # 试运行，看会写什么
python $sk\scripts\hermes_usage_sync.py --verbose    # 正式同步（幂等，可反复跑）
python $sk\scripts\verify_hermes_usage.py            # 验证结果
```

退出码：`0` 成功｜`10` Hermes 数据库不存在｜`11` schema 不兼容｜
`20/21` CC Switch 数据库异常｜`30` 写入失败（通常是 CC Switch 占用锁）｜
`40` 存在历史记录但无同步状态（需 `--reset-baseline`）

开启每 30 分钟自动同步：

```powershell
$action  = New-ScheduledTaskAction -Execute python -Argument "$sk\scripts\hermes_usage_sync.py"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30)
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "HermesUsageSync" -Action $action -Trigger $trigger -Settings $set -Force
```

## ⚠️ 已知局限（请务必读完）

这些是**设计上无法消除**的限制，不是待修的 bug。token 总量是准确的，但以下几项不是。

1. **「总请求数」不是真实的 API 调用次数。**
   CC Switch 的 `proxy_request_logs` 表共 27 列，**没有任何字段能记录"调用次数"**，
   所以它的"总请求数"必然等于日志行数。本脚本每次同步、每个 `(会话,模型,任务)` 组合
   最多写 1 行，因此该数字实际含义是"有过增量的同步窗口数"。
   *这也正是官方 PR 另建带 `api_call_count` 字段的专用表的原因。*

2. **趋势图的时间点是近似的。**
   每行的 `created_at` 取自 Hermes 计数器的"最后更新时间"（`last_seen`），
   而非真实请求发生的时刻。跨小时的会话，其用量会集中落在最后一次更新的那个时间点上。

3. **`latency` / `status` / `is_streaming` 是填充值。**
   Hermes 不提供这些信息，脚本写入 `0` / `200` / `0` 以满足 CC Switch 的表结构。
   请勿把请求日志里的这些数字当作真实测量值。官方 PR 的做法是**拒绝伪造**，
   改为显示一段说明文字告知"这是聚合数据、无逐次明细"——这个取舍更诚实。

4. **计数器被重置时，中间的增量会丢失。**
   Hermes 某些操作（如归档、数据库替换）会把计数清零。脚本检测到数值变小，
   会以当前值作为新基线继续，但被清掉的那段用量无法恢复。

5. **与 Hermes 侧总额可能不一致（本项目选择保留历史）。**
   Hermes 会清理过期会话（实测有 `startup_orphan_reap` 机制），但本项目**不会**
   同步删除 CC Switch 里的历史记录。因此 CC Switch 的 Hermes 累计值通常**大于**
   Hermes 当前仍保留的值。实测样本：CC Switch 侧 85,398 input tokens，
   Hermes 侧剩余 13,201（差额是被 Hermes 清理掉的会话）。
   这是**有意为之**：用量历史不应该因为源端清档而凭空消失。
   若你确实需要两边严格对齐，可手动执行
   `DELETE FROM proxy_request_logs WHERE data_source='hermes_session';`
   配合 `--reset-baseline` 重建基线——但请先理解代价。

## 已知局限 · 补充线索

如果需要**真正的逐次请求**数据，Hermes 数据库里可能存在这条路（截至 2026-09-29
尚未验证）：

- `messages` 表带 `timestamp`、`token_count`、`finish_reason` 字段，是**请求级**的
- 但实测样本中 `token_count` 为空，需要先确认当前 Hermes 版本是否稳定填充该字段
- 若可用，即可按真实请求逐条写入，彻底解决局限 1 和 2

这条线索也适合反馈给 CC Switch 官方（见 `references/` 下的讨论要点）。

## 为什么不直接用官方 PR #6120

官方方案把累计值存进**独立的快照/delta 表**（`api_call_count` 等字段齐全），
比本项目严谨；代价是需要改动 6+ 个 Rust 文件、上千行变更。

本项目走 `proxy_request_logs` 这条已被验证的现成通路，实现简单、可独立部署、
不侵入 CC Switch。取舍是上面 5 条局限。

两者不冲突：官方若合入，切换过去即可，本项目降级为「仅验证」。

## 开发

```powershell
python tests\test_hermes_sync.py     # 4 个用例：增量语义 / 计数器重置 / 成本兜底 / 缺库容错
```

| 目录 | 说明 |
|---|---|
| `scripts/hermes_usage_sync.py` | 核心同步脚本（健康检查 → 取数 → 增量 → 写入） |
| `scripts/verify_hermes_usage.py` | 结果验证 |
| `references/schema.md` | Hermes 与 CC Switch 两侧数据库结构实测细节 |
| `references/troubleshooting.md` | 排障手册 |
| `references/cc-switch-frontend-patch.md` | 配套的 CC Switch 前端改动清单（4 个文件） |

## 致谢与免责

- [CC Switch](https://github.com/farion1231/cc-switch) —— MIT，本项目不包含其源码或二进制
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) —— 数据来源
- 官方 PR [#6120](https://github.com/farion1231/cc-switch/pull/6120) 的作者
  `xrbs00` 完成了更完整的实现，本项目的数据链路设计受其文档启发

本项目与 CC Switch 官方无隶属关系。

---

<a name="english"></a>

## English

Sync **Hermes Agent** token usage into the **CC Switch** usage dashboard.

CC Switch aggregates token usage for Claude Code, Codex, OpenCode and others, but its
official build has no Hermes Agent support. This project reads Hermes's local
`state.db` counters and writes them into CC Switch's statistics database so Hermes
shows up on the same dashboard.

**What it is:** an [opencode](https://opencode.ai) skill (usable as a standalone script).
It does **not** modify CC Switch's Rust backend.

### How it works

```
Hermes state.db  (session_model_usage — cumulative counters, one row per
                  session × model × task)
      │  every 30 min: read, compute delta vs. last snapshot
      ▼
CC Switch cc-switch.db  (proxy_request_logs, app_type='hermes')
      ▼
CC Switch UI → Settings → Usage → Hermes button
```

Because it reads `session_model_usage` (per model, not the session's initial model),
model attribution is accurate — this is the specific problem the maintainer cited when
they previously removed Hermes support.

### Known limitations (please read)

The token **totals are accurate**. These are not, and are structural:

1. **"Total requests" is not the real API call count.** `proxy_request_logs` has 27
   columns and none can hold a call count, so CC Switch counts log rows. This is
   exactly why the official PR introduces dedicated tables with `api_call_count`.
2. **Trend timestamps are approximate** — taken from the counter's `last_seen`.
3. **`latency` / `status` / `is_streaming` are placeholders.** Hermes does not
   expose them. The official PR instead refuses to fabricate a request log.
4. **Counter resets lose the intermediate delta.**
5. **Totals may exceed Hermes' current view** — this project keeps history when
   Hermes garbage-collects expired sessions. Intentional: usage history shouldn't
   vanish because the source cleaned up.

A possible route to real per-request data: Hermes' `messages` table has `timestamp`,
`token_count` and `finish_reason` (request-level). Not yet verified whether the
current Hermes version populates `token_count`.

### Install & run

```powershell
git clone https://github.com/Chunai-Bboy/cc-switch-hermes-usage.git `
  "$env:USERPROFILE\.config\opencode\skills\cc-switch-hermes-usage"

$sk = "$env:USERPROFILE\.config\opencode\skills\cc-switch-hermes-usage"
python $sk\scripts\hermes_usage_sync.py --check    # health check
python $sk\scripts\hermes_usage_sync.py --dry-run -v
python $sk\scripts\hermes_usage_sync.py --verbose # idempotent sync
python $sk\scripts\verify_hermes_usage.py
```

Requires Windows (for the scheduled task), CC Switch 3.20.4, Hermes Agent run at
least once, and Python 3.8+ (standard library only).

### Tests

```powershell
python tests\test_hermes_sync.py
```

### Credits

[CC Switch](https://github.com/farion1231/cc-switch) (MIT) · [Hermes Agent](https://github.com/NousResearch/hermes-agent) ·
the official PR [#6120](https://github.com/farion1231/cc-switch/pull/6120) by `xrbs00`,
whose documentation informed this design.

Not affiliated with the CC Switch project.

## License

MIT — see [LICENSE](LICENSE).
