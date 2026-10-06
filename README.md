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
   升级 Hermes 时这类清理尤其集中——0.21.2→0.21.5 一次删掉 10 个老会话，
   CC Switch 与本地账本各自比 Hermes 实时真值多 882,626 input。
   这是**有意为之**：用量历史不应该因为源端清档而凭空消失。
   `verify_hermes_usage.py` 对这种情况判 **WARN「上游丢历史」并点名丢失会话**，
   不判 FAIL——对账基准是「CC Switch == 本地账本（snapshots）」，两者必须分毫不差；
   实时真值只是参考量，允许账本领先它。
   若你确实需要两边严格对齐，可手动执行
   `DELETE FROM proxy_request_logs WHERE data_source='hermes_session';`
   配合 `--reset-baseline` 重建基线——但这会**永久删掉已同步的真实历史**，
   除非确凿是双计（见 troubleshooting B2），否则别用它换绿勾。

## 之前留着疑问的，现在都有定论（2026-10-03 实测）

### 1. `messages.token_count` 永远是 NULL —— 路径已死，别再留期待

实测 `messages` 共 199 行，assistant / tool / user 全类别，含所有 `finish_reason`
与 `tool_name`（terminal 18、vision_analyze 4、browser_exec 3、patch 2、skill_view 2…），
`token_count` 非 NULL 的行数 = **0**。schema_version = 30。

### 2. issue #5088 说的 `messages._usage` 字段不存在 —— 那个说法本身有误

`messages` 共 26 列，**没有任何一列叫 `_usage`**。
`vision_analyze` 的实际 content 是"Image loaded into your context — you can see it natively now"，
说明它早就不再单独发 API 请求，图片直接进原生上下文，所以压根没有可上报的独立 token 记录。

### 3. `messages.timestamp` 不是请求发出时刻 —— 但仍然有用作时间轴

7 对逐条配对实测：`messages.timestamp[i] - 插件 started_at[i]` ≈ 插件 `duration_ms[i]`

| # | 插件 started | 插件 duration | messages.timestamp | 差值 |
|---|---|---|---|---|
| 1 | 11:52:24 | 8708ms | 11:52:33 | 8720ms |
| 2 | 11:52:34 | 5378ms | 11:52:40 | 5392ms |
| 3 | 11:52:41 | 5751ms | 11:52:47 | 5765ms |
| 4 | 11:52:48 | 5106ms | 11:52:53 | 5125ms |
| 5 | 11:52:57 | 4815ms | 11:53:02 | 4828ms |
| 6 | 11:56:04 | 6240ms | 11:56:10 | 6255ms |
| 7 | 11:56:13 | 4862ms | 11:56:18 | 4873ms |

差值逐条等于耗时 ⇒ 它是**响应到达时刻**。刻度真实且唯一（7/7 不重复、0 NULL），
可作时间轴，但**拿不到耗时**。

### 4. `background_review` 这类行会把多次调用压成一个时刻

实测 `first_seen == last_seen == 1790969381.7378309`（7 位小数完全相同），而 `api_call_count = 8`。
子 agent 循环跑完一次性写库。**任何导入器都无法还原成 8 个独立请求。**

### 5. `reasoning_tokens` CC Switch 存不下

`proxy_request_logs` 27 列，**全库 0 个 `reasoning` 列**（逐表扫过）。
v2 只写入自己的 sidecar `dimension_totals`，并在 `--report` 的 `sidecar_unmapped` 里明确报告。
要进仪表盘必须上游加列，那会破坏"CC Switch 侧零改动"。

### 6. 辅助任务占 28.6%，聚合源全覆盖而 hook 覆盖不了

实测 133 次调用：main 95 / approval 28 / background_review 8 / title_generation 2。
`messages` 里正好 95 行 assistant（= main 调用数），**38 次辅助调用在 `messages` 里完全不存在**。
这是 `post_api_request` hook 结构性够不到的部分。

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

Resolved 2026-10-03: `messages.token_count` is NULL in 199/199 rows - dead end.
`messages.timestamp` is the response-arrival moment (verified by 7 paired comparisons
against plugin hook timings): usable as a time axis, not as latency. Real per-request
data requires the `post_api_request` hook. See `references/v2-bench-ab.md`.

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
python tests\test_bugfix_regressions.py   # 17 例
python tests\test_hermes_sync_v2.py       # 29 例
python tests\test_hermes_sync.py          # 4 例 v1 回归
```

### Credits

[CC Switch](https://github.com/farion1231/cc-switch) (MIT) · [Hermes Agent](https://github.com/NousResearch/hermes-agent) ·
the official PR [#6120](https://github.com/farion1231/cc-switch/pull/6120) by `xrbs00`,
whose documentation informed this design.

Not affiliated with the CC Switch project.

## License

MIT — see [LICENSE](LICENSE).

---

## v2 changelog（2026-10-03）

七项实测驱动的改动：内容寻址幂等键 / 只读加固 / 真实消息时刻 / 不伪造 status_code /
可选消费插件账本 / `--repair-config` 修 config 污染 / 辅助行不套主循环耗时。

新增：`--report`（聚合 vs 逐请求 JSON 报告）、`--repair-config`，退出码 41。
测试：`test_hermes_sync_v2.py` 22 例 + `test_hermes_sync.py` 4 例回归。
性能：v1 151.2ms → v2 147.3ms（12 轮稳态中位）。
详见 `references/v2-bench-ab.md`。


---

## v2.1 changelog（2026-10-03，32 分钟长任务实测后）

一次 32 分钟、43 次调用的真实 Hermes 会话（session `20261003_144834_1d260f`）挖出三个 bug：两个在同步脚本里，一个是我自己引入后又当场揪出来的。全部有回归测试锁定。

### BUG1 快照键漏了 3 列，会重复计数

`session_model_usage` 的主键是 **6 列**：

```sql
PRIMARY KEY (session_id, model, billing_provider,
                billing_base_url, billing_mode, task)
```

v2 的快照键只用了 4 列 `(profile, session, model, task)`。那个会话里有**两条**`task='approval'`，靠 `billing_base_url` 区分（`''` vs `https://opencode.ai/zen/v1/`），于是被并成一条序列。真实回放三轮：

```第1轮基线      5 条  calls=43   in=61276
第2轮前进      3 条  calls=21   in=20582   <- 3 条而不是 2 条
第3轮应为空    1 条  calls=1    in=997     <- 本该是空的，却冒出重复行
合计               calls=65   in=82855   差 +2 次调用 / +1994 input```

修法：快照键与 `request_id` 都改用表本身的完整主键。修完同一份数据：

```第3轮应为空    0 条
合计               calls=63   in=80861   与真值完全一致```

**注意 SQLite 在普通表上会为 PRIMARY KEY 建隐式唯一索引**，所以 6 列本身就够防重复，不需要额外字段。

### 自己引入又揪出来的坑：first_seen 不能进键

第一版修法在键尾加了 `first_seen` 兜底。v1 回归测试立刻报 `360/40 != 260/30`——fixture 把 `first_seen` 从 1000 改成 2000（模拟长会话），老序列就变成新序列，整行重新基线，多写 100 input / 10 output。

**主键之外的任何可变列进键都会重复计数。** 已移除，并加测试锁死。

### BUG2 耗时用全会话中位数，语义是错的

v2 把「全会话所有事件的耗时中位数」填进**每一个**同步窗口行。那个会话窗口行显示`latency=59805ms`，而该会话真实耗时区间是 `14.2s ~ 198.2s`。行代表一个时间窗口，填的却是整段会话——数字看着合理，含义完全对不上。

修法：快照存住上一次的 `last_seen`，用 `(prev_last_seen, cur_last_seen]` 这个真实时间窗去归属插件事件，只有落在窗里的才参与中位数。首次基线没有「上一次」，返回 `None` -> 写 0，**宁可留空也不编**。

副作用（正确的）：基线行的 `latency_ms` 现在恒为 0，耗时从第二次同步开始才出现。

### 顺带修正的两个认知

```| 之前以为 | 实测 |
|---|---|
| 插件只覆盖主循环 | 也覆盖 `background_review`（子 agent 走同一条 conversation_loop） |
| 插件漏掉 31% 调用 | 漏的是 `title_generation` + `approval`（走 auxiliary_client，无此 hook） |```

### 迁移

键格式变了，老 `request_id` 不会被去重。升级必须重建：

```python scripts\hermes_usage_sync.py --reset-baseline```

删掉 CC Switch 里的 `hermes_session` 行和本地快照，再从 `state.db` 全量重扫。源库是累计完整的，重建结果与真值逐项一致。重建前务必备份两个 db。

### 测试

```test_bugfix_regressions.py   17 例  <- 新增，锁住上面三个 bug
test_hermes_sync_v2.py       29 例
test_hermes_sync.py           4 例  v1 回归```