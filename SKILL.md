---
name: cc-switch-hermes-usage
description: 在 CC Switch 使用统计页面持续维护 Hermes Agent 的 token 用量统计。封装健康检查、Hermes state.db 数据获取、增量（delta）同步、成本兜底计算与验证逻辑，CC Switch 侧零改动。当用户提到"Hermes 用量统计"、"Hermes token 同步"、"CC Switch 使用统计 Hermes"、"hermes_session 来源"时使用。
license: MIT
compatibility: opencode
---

# CC Switch × Hermes 用量统计同步

> 目标：让本机 Hermes Agent 的模型用量出现在 CC Switch「设置 → 使用统计」页面，
> 且可持续增量同步。CC Switch 侧零改动，全部逻辑封装在本 skill 脚本中。

## 适用范围

- CC Switch（Tauri 桌面版，实测 3.20.4）使用统计页 `全部来源` 下拉中的 **Hermes Agent** 项
- Hero 总量 / 使用趋势图 / 请求日志 / Provider 统计 / 模型统计 五个区域均按来源筛选生效
- 数据源：Hermes `%LOCALAPPDATA%\hermes\state.db`（含 `profiles\*\state.db` 多 profile）

## 前置确认结论（已实测，2026-09-28 / 2026-09-29 更新）

| 问题 | 结论 |
|---|---|
| 有无现成项目可复用？ | 上游 PR [#6120](https://github.com/farion1231/cc-switch/pull/6120)（未合并）是完整 Rust+前端方案。本 skill 采用"外部同步 + 原生展示"路线；**Hermes App 筛选按钮已通过本地源码构建补齐**（见下节） |
| 本地是否有查看模型用量的工具？ | 有。数据源即 Hermes state.db（`sessions`/`session_model_usage` 表）；目标展示即 CC Switch 使用统计页；Hermes 桌面版另有 `settings?tab=billing` 可交叉核对 |
| Hermes 出现在页面哪个位置？ | **使用统计页 App 筛选按钮行有独立 "Hermes" 按钮**（本地构建补齐），叠加 `全部来源` 下拉的 "Hermes Agent" 项。日期范围需覆盖数据日期（默认"当天"不显示历史数据） |

## 本地构建记录（Hermes App 按钮）

CC Switch 3.20.4 发布版前端不含 Hermes 按钮（`KNOWN_APP_TYPES` 无 hermes）。已从源码构建补齐：

| 项 | 值 |
|---|---|
| 源码 | `D:\security-research\projects\cc-switch`（main = 3.20.4） |
| 改动 | `src/types/usage.ts`（AppType + KNOWN_APP_TYPES 加 hermes）、`UsageDashboard.tsx`（APP_FILTER_ICON）、`UsageHero.tsx`（TITLE_THEMES 配色）、4 个 i18n locale 加 `usage.appFilter.hermes` |
| 未改动 | 后端 Rust（3.20.4 原生已支持 `app_type='hermes'` 聚合查询，数据仍由本 skill 同步写入） |
| 产物 | `src-tauri\target\release\cc-switch.exe` → 安装到 `D:\ai\cc-switch.exe` |
| 备份 | `D:\ai\cc-switch.exe.bak-3.20.4-original` |
| 构建环境 | Rust **1.95**（仓库 `rust-toolchain.toml` 固定，需 USTC 镜像 `RUSTUP_DIST_SERVER`）、MSVC 2022 BuildTools、pnpm |

复现构建（注意国内网络）：

```powershell
$env:PATH = "$env:USERPROFILE\.cargo\bin;$env:PATH"
$env:RUSTUP_DIST_SERVER = "https://mirrors.ustc.edu.cn/rust-static"   # 关键：否则拉 1.95 工具链会挂起
$env:RUSTUP_UPDATE_ROOT = "https://mirrors.ustc.edu.cn/rust-static/rustup"
cd D:\security-research\projects\cc-switch
pnpm install
pnpm tauri build --no-bundle     # 产物 src-tauri\target\release\cc-switch.exe
```

> 未移植 PR #6120 的 `session_usage_hermes.rs`（原生 Rust 同步 + 残差对账）。原因：涉及 6+ 文件、上千 hunks，
> 而数据链路已由本 skill 打通且验证通过。合入后可将本 skill 降级为"仅验证"。

## v2（2026-10-03）

七项改动，全部由真机实测驱动：内容寻址幂等键、只读加固（query_only）、
`created_at` 改用真实消息时刻、不伪造 status_code、可选消费插件账本喂真实耗时、
`--repair-config` 清理合成 provider 污染、辅助调用行不套用主循环耗时。
新增 `--report` / `--repair-config`，退出码 41 = config 污染。
测试 22 + 4 例全绿；性能 v1 151.2ms → v2 147.3ms。
**细节与 A/B 数据见 `references/v2-bench-ab.md`。**
## 快速使用

```powershell
$sk = "C:\Users\zyq20\.config\opencode\skills\reverse-skill\skills\cc-switch-hermes-usage"

# 1. 健康检查（不写入）
python $sk\scripts\hermes_usage_sync.py --check

# 2. 试运行（计算增量但不写入）
python $sk\scripts\hermes_usage_sync.py --dry-run --verbose

# 3. 正式同步（幂等，可反复跑）
python $sk\scripts\hermes_usage_sync.py --verbose

# 4. 验证 CC Switch 中的结果
python $sk\scripts\verify_hermes_usage.py
```

**首次从旧版全量脚本迁移**（CC Switch 已有 hermes_session 记录但无同步状态时，脚本会报 ERROR[40] 拒绝双计）：

```powershell
python $sk\scripts\hermes_usage_sync.py --reset-baseline
```

## 用量面板（不想翻 CC Switch 时用它）

CC Switch 4.0.12 前端已 Rust 重写、本机无 cargo 无法改源码重编，所以另做了一个
独立面板：同样的库、同样的口径，点 app 图标即看该 app 用量。

```powershell
node "$sk\scripts\hermes-usage-panel.js" 8799
# 浏览器开 http://127.0.0.1:8799
```

- 顶部一排 app 图标（Claude/Codex/Gemini/Grok/OpenCode/**Hermes**/Pi/MCode），
  每个图标下面直接标请求数和成本，点一下即切换
- 卡片区：总请求数 / 真实消耗 Tokens / 缓存命中 / 总成本 / 活跃天数 / 模型数
- 每日趋势柱状图、按模型表、最近 200 条明细
- 只读 `~/.cc-switch/cc-switch.db`，15 秒自动刷新

口径注意（与 CC Switch 对齐，实测踩过）：
- **汇总**（图标栏/卡片）必须排除 `session_log / codex_session /
  gemini_session / opencode_session` 这些 data_source，因为它们与 CC Switch
  自己的 proxy 日志重复，不排除会把 opencode 从 121 次算成 242 次
- **明细**照实显示全部行，不过滤——CC Switch 的请求日志也是这样
  （早先版本两边都过滤，结果 opencode 详情直接变 0 条）

## 已知失真与对策（2026-10-17 实测）

| 项 | 状态 | 说明 |
|---|---|---|
| token 数 / 请求数 | **准** | 账本与 CC Switch 逐维相等；账本 ≥ Hermes 真值是因为上游升级删过 10 个老会话，历史已安全保留 |
| 成本 | **准** | 按 `model_pricing` 重算逐项一致；免费模型（space-bunny-free / upstage solar / nemotron）显式补 0 定价，语义从"未定价"变"已定价且免费" |
| HTTP 状态 | **已修** | 聚合源证明不了状态，曾统一写 200 导致成功率恒 100%。现在每次 sync 用插件归档回填真实 `status_code`（实测揪出 1 条 401） |
| 延迟 | **部分修** | 归档有 `duration_ms` 的按段回填中位数（实测 29 → 44 行有真值）；归档没覆盖的写 0 而不编造 |
| 首次基线前的老数据 | 不可恢复 | 插件归档只覆盖 10-03，更早的调用无逐次记录 |

### 回填怎么配对

CC 行 `request_id` 里的 `@c<N>` 是同步那一刻的累计 `api_call_count`；归档
`main` 事件 `event_id` 里的 `api:<n>` 是第 n 次调用。同一
`(session, model, task)` 序列下按调用序号切段：
第 i 个 CC 行覆盖 `(prev_c, c_i]` 这段调用。`status_code` 取段内首个错误码，
`latency` 取段内 `duration_ms` 中位数。

## 计划任务（持续同步）

```powershell
$action  = New-ScheduledTaskAction -Execute python -Argument "$sk\scripts\hermes_usage_sync.py"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "HermesUsageSync" -Action $action -Trigger $trigger -Settings $settings -Force
# 卸载：Unregister-ScheduledTask -TaskName "HermesUsageSync" -Confirm:$false
```

## 核心设计（为什么这样做）

### 1. cumulative 快照 ≠ 请求日志，必须转 delta

Hermes `session_model_usage` 是**累计计数器**（每次 API 调用后 UPDATE 同一行）。
若把每份快照原样写入，重复同步会指数级双计。本 skill 在
`%LOCALAPPDATA%\cc-switch-hermes-usage\sync-state.db` 记录上次快照，仅写**正增量**：

```
prev 不存在            → 全量为基线（baseline）
current >= prev        → 写差值（delta）
current <  prev        → 计数器被重置/重建 → 以当前值为新基线并告警
```

### 2. 成本三级兜底

`actual_cost_usd`（cost_status=actual/final/settled）→ `estimated_cost_usd`
→ 查 CC Switch `model_pricing` 表按百万 token 单价计算。免费模型（nemotron/upstage）
显示 $0，Hermes 侧未知价模型在 CC Switch 中不再"未定价"。

### 3. 幂等与安全

- `request_id = hermes:{profile}:{session}:{model}:{task}@c{api_calls}:{est}:{act}`
  （v2 改为内容寻址：不再依赖会抖动的 `last_seen_ms`，崩溃重跑不会双计）+ `INSERT OR IGNORE`
- Hermes DB 只读方式打开（`mode=ro`），避免与 Hermes 运行时的 WAL 冲突
- `--reset-baseline` 是唯一会删除数据的操作，需显式确认

### 4. 兼容性探测（继承上游 PR #6120 经验）

`sessions`/`session_model_usage` 缺列（老版本无 `task`/`cost_status` 等）时逐列降级，
不因 schema 演进而整体失败。

## 退出码

| 码 | 含义 | 处理 |
|---|---|---|
| 0 | 成功（含 0 增量） | - |
| 10 | 未发现 Hermes state.db | 确认 Hermes 已安装/运行过一次 |
| 11 | Hermes schema 不兼容 | 更新 Hermes 或人工检查表结构 |
| 20/21 | CC Switch DB 缺失/缺表 | 确认 CC Switch 安装路径 |
| 30 | 写入失败 | 看 stderr，常见为 DB 被锁（CC Switch 占用） |
| 40 | 历史记录无同步状态 | 加 `--reset-baseline` 迁移 |

## 验证与排障

完整验证：`python $sk\scripts\verify_hermes_usage.py`，应看到三方对账表
（实时真值 vs 同步账本 vs CC Switch）。基准关系（v2.2）：
**CC Switch 必须等于同步账本**（这是同步器自己的账，必须分毫不差）；
**账本 ≥ 实时真值 是常态**——Hermes 会删老会话聚合行（升级实测一次删 10 个、
input 差 882,626），历史已安全存于账本/CC，此时只 WARN「上游丢历史」，不是 FAIL。
绝不允许用 `--reset-baseline` 删真实历史来把检查做绿。
其余检查：provider 已注册、记录数 > 0、latency 归属、config 污染、只读保护。
测试隔离可用 `--hermes-home <目录>` 把真值/插件/config 检查全指向沙箱。

常见问题速查：

| 现象 | 原因 | 处理 |
|---|---|---|
| usage 页下拉没有 Hermes Agent | 日期范围不含数据（默认"当天"） | 日期切到"近30天"或自定义覆盖数据日期 |
| Hero 总数不含 hermes | App 筛选被设为具体 app 且不是全部 | App 筛选保持"全部" |
| ERROR[30] 写入失败 | CC Switch 正在写 DB（SQLite 锁） | 脚本已用 timeout=10 + BEGIN IMMEDIATE；仍失败则退出 CC Switch 重试 |
| 数字比 Hermes 侧大一点（verify 判 WARN 上游丢历史） | Hermes 清理/升级删了老会话聚合行，账本/CC 保留历史 | **正常，不动**。这是有意取舍：历史不因源端清档消失 |
| 数字比 Hermes 侧大约 2 倍或按轮翻倍 | 旧版全量脚本与新 delta 双计 | 仅此时才用 `--reset-baseline`（会删 CC 的 hermes 历史，先备份） |
| 数字比 Hermes 小 | 计数器重置被当基线，中间量丢失 | 已知限制，需 Hermes 侧时间戳事件才能精确归因（同 PR #6120 设计结论） |

## 迭代路线（后续可持续演进）

1. **上游 PR #6120 合入后**：卸掉本 skill 的同步职责，改为"仅验证"模式
2. **精确归因**：若 Hermes 未来提供 timestamped usage events，替换 delta 近似为精确事件导入
3. **成本源增强**：接入 models.dev 定价缓存，减少"未定价"
4. **多 profile UI**：当前所有 profile 数据汇入同一 Hermes Agent 项；如需按 profile 拆分，
   在 `compute_deltas` 的 key 中已含 profile，扩展 provider 维度即可

## 参考

- `references/schema.md` — Hermes 与 CC Switch 两侧 schema 实测细节 + 已验证数据流
- `references/troubleshooting.md` — 完整排障手册（含 CDP 验证方法）
- `tests/test_hermes_sync.py` — delta/重置/成本兜底/容错单元测试（4 用例）
