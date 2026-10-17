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

## 本地构建记录（用量页的 Hermes 图标）

发布版把 hermes 排除在用量页筛选行之外：`KNOWN_APP_TYPES` 不含 hermes，
且上游注释写明是**有意设计**（"hermes/openclaw 只作为被管理的应用出现在别处"）。
后端其实早就支持——`get_usage_summary_by_app` 直接返回 `appType=hermes`，
所以缺的只有前端白名单里那一行。

**⚠ 两个反复踩的坑：**
1. **CC Switch 会自动更新**（本会话内 4.0.10 → 4.0.12 无人干预）。官方更新会
   直接覆盖 `cc-switch.exe`，**补丁随之失效、图标消失**。用户报"图标又没了"时
   先查版本号，别先查数据。彻底避免需关掉应用内自动更新，否则每次更新重打一次。
2. **别用"页面里搜不到字样"判断**：侧栏本来就有 Hermes（被管理的应用），
   搜字会误判成"已支持"。必须精确定位到**用量页那条筛选行**再数按钮。
   验证方法（唯一可靠，已沉淀）：
   ```powershell
   # 带调试端口启动，再用 CDP 读真实 DOM
   $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = "--remote-debugging-port=9225"
   Start-Process "D:\ai\cc-switch.exe"
   # node + WebSocket 连 http://127.0.0.1:9225/json 的 page，
   # Runtime.evaluate 取 button[aria-label] 列表；
   # 还能 fetch 自己页面加载的 bundle，确认 exe 内嵌的到底是哪份前端。
   ```

### 4.0.12 构建（2026-10-17，当前生效）

| 项 | 值 |
|---|---|
| 源码 | `D:\GitHub\cc-switch-fork` = 官方 tag `v4.0.12`，**tar.gz 下载**（git 协议在本机网络会挂死，codeload 可用） |
| 改动 | **只有 `src/types/usage.ts` 两处**：`AppType` 联合加 `\| "hermes"`、`KNOWN_APP_TYPES` 在 `"opencode"` 后加 `"hermes"` |
| 比 3.20.4 少改哪些 | 4.0.12 图标行改用 `AppGlyph` + `APP_DISPLAY_NAME`（品牌名不翻译，已含 hermes/图标资产），且 `UsageHero` 已无 `TITLE_THEMES`、chips 不再走 `usage.appFilter.*` 翻译 —— **i18n 与两个组件都不用改** |
| 刻意不改 | `CACHE_INCLUSIVE_APP_TYPES` / `PARTIAL_CACHE_WRITE_APP_TYPES`。Hermes 的 `input_tokens` 本就是非缓存口径（实测 input 8.1M < cache_read 135M），误入白名单会被减成负数 |
| 后端 | 未动（Rust 原生按 app_type 聚合） |
| 产物/安装 | `src-tauri\target\release\cc-switch.exe`(41.5MB) → `D:\ai\cc-switch.exe`，SHA256 一致 |
| 回退 | `D:\ai\cc-switch.exe.bak-v4.0.12`（官方版）；更早还有 `.bak-3.20.4-original` |
| 环境 | rustup/cargo **1.98.1 stable**（`rustup-init -y --profile minimal`，装完须把 `%USERPROFILE%\.cargo\bin` 塞进 PATH，否则后台 `pnpm tauri build` 报 `cargo program not found`）；pnpm **10.12.3**（corepack 会挂死，用 `npm i -g pnpm@10.12.3`）；MSVC BuildTools 2022 + Windows SDK 26100 本机已有（`vswhere -latest` 返回空是假阴性，直接找 `link.exe`） |
| 网络 | crates 用 rsproxy（`~\.cargo\config.toml`）、npm 用 npmmirror；`pnpm tauri build --no-bundle` 全量 ~8 分钟 |

复现：

```powershell
$env:PATH = "$env:USERPROFILE\.cargo\bin;$env:PATH"
cd D:\GitHub\cc-switch-fork
pnpm install --registry https://registry.npmmirror.com
pnpm typecheck            # AppType 是穷举类型，漏改会被 tsc 拦下
pnpm tauri build --no-bundle
```

> 3.20.4 时代的构建在 `D:\security-research\projects\cc-switch`（改过
> `APP_FILTER_ICON`/`TITLE_THEMES`/4 个 locale），已被 4.0.12 的 2 行方案取代。
> 仍未移植 PR #6120 的 `session_usage_hermes.rs`；若上游合入，本 skill 可降级为"仅验证"。

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
