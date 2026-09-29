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
| 上游源码 | <https://github.com/farion1231/cc-switch>（tag/版本 3.20.4） |
| 改动 | `src/types/usage.ts`（AppType + KNOWN_APP_TYPES 加 hermes）、`UsageDashboard.tsx`（APP_FILTER_ICON）、`UsageHero.tsx`（TITLE_THEMES 配色）、4 个 i18n locale 加 `usage.appFilter.hermes` |
| 未改动 | 后端 Rust（3.20.4 原生已支持 `app_type='hermes'` 聚合查询，数据仍由本 skill 同步写入） |
| 产物 | `src-tauri/target/release/cc-switch.exe`（需你自行替换安装目录中的 `cc-switch.exe`，并自行备份原件） |
| 构建环境 | Rust **1.95**（仓库 `rust-toolchain.toml` 固定）、MSVC 2022 Build Tools、pnpm |

> ⚠️ 这些改动**只在你本地的 CC Switch 副本里**，本仓库不包含、也不分发 CC Switch 的源码或二进制。
> 四处改动的精确内容见 `references/cc-switch-frontend-patch.md`（含验证命令）。

复现构建（注意国内网络）：

```powershell
$env:PATH = "$env:USERPROFILE\.cargo\bin;$env:PATH"
# 国内网络必设，否则拉取 Rust 1.95 工具链会长时间挂起（卡在官方源）
$env:RUSTUP_DIST_SERVER   = "https://mirrors.ustc.edu.cn/rust-static"
$env:RUSTUP_UPDATE_ROOT  = "https://mirrors.ustc.edu.cn/rust-static/rustup"

git clone --depth 1 --branch v3.20.4 https://github.com/farion1231/cc-switch.git
cd cc-switch
pnpm install
pnpm tauri build --no-bundle     # 产物 src-tauri/target/release/cc-switch.exe
```

其中 4 处前端改动的精确内容见 `references/cc-switch-frontend-patch.md`（含 `pnpm typecheck` 验证命令）。

> 未移植 PR #6120 的 `session_usage_hermes.rs`（原生 Rust 同步 + 残差对账）。原因：涉及 6+ 文件、上千 hunks，
> 而数据链路已由本 skill 打通且验证通过。合入后可将本 skill 降级为"仅验证"。

## 快速使用

```powershell
# 本 skill 的安装位置（opencode 从这里读取）
$sk = "$env:USERPROFILE\.config\opencode\skills\cc-switch-hermes-usage"

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

- `request_id = hermes:{profile}:{session}:{model}:{task}:{last_seen_ms}` + `INSERT OR IGNORE`
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

完整验证：`python $sk\scripts\verify_hermes_usage.py`，应看到：
provider 已注册、记录数 > 0、模型统计表、UI 查看路径提示。

常见问题速查：

| 现象 | 原因 | 处理 |
|---|---|---|
| usage 页下拉没有 Hermes Agent | 日期范围不含数据（默认"当天"） | 日期切到"近30天"或自定义覆盖数据日期 |
| Hero 总数不含 hermes | App 筛选被设为具体 app 且不是全部 | App 筛选保持"全部" |
| ERROR[30] 写入失败 | CC Switch 正在写 DB（SQLite 锁） | 脚本已用 timeout=10 + BEGIN IMMEDIATE；仍失败则退出 CC Switch 重试 |
| 数字比 Hermes 侧大 | 旧版全量脚本双计 | `--reset-baseline` 重建 |
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
