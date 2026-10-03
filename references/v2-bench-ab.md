# CC Switch × Hermes 用量统计同步

> 让本机 Hermes Agent 的模型用量出现在 CC Switch「设置 → 使用统计」页面，且可持续增量同步。
> CC Switch 侧零改动。核心逻辑全部在本 skill 的 Python 脚本里。

## v2 变更摘要（2026-10-03）

| # | 变更 | 动机（均为真机实测发现） |
|---|---|---|
| 1 | **内容寻址幂等键**：`hermes:{profile}:{session}:{model}:{task}@c{api_calls}:{est}:{act}` | v1 用 `last_seen_ms` 收尾。`last_seen` 会抖动，且「log 写成功但快照丢失」时重跑会得到不同 key → 重复计一行。新键由累计值派生，崩溃可重跑、抖动不造垃圾行 |
| 2 | **只读加固**：`mode=ro` + `PRAGMA query_only=ON` + `busy_timeout` | 让 SQLite 自己拒绝写，而不是靠脚本自觉。单测断言 `state.db` 字节级不变 |
| 3 | **`created_at` 改用真实 assistant 消息时刻** | 原来用 `int(last_seen)`，整段同步窗口挤在一个时间点上；`background_review` 那种「8 次调用压进同一毫秒」更是失真 |
| 4 | **不伪造 status_code**：`--report` 显式声明这是已知失真 | CC Switch 用 `200<=status_code<300` 判成功，写 200 → 成功率恒 100%；写 0 → 恒 0%。表结构 NOT NULL，没有「未知」哨兵值。根治要靠上游加 `statusAvailable` 标志位 |
| 5 | **可选消费插件账本**：读 `ccswitch-usage.sqlite`，把真实耗时写进 `latency_ms`，逐请求事件落到**本地 sidecar** | 主循环耗时只有 hook 能拿到。事件绝不进 `proxy_request_logs`（会双计） |
| 6 | **`--repair-config`**：清理 `config.yaml` 的 `custom_providers` 合成空壳条目 | skill 往 CC Switch `providers` 插 `_hermes_session`，CC Switch 的 Hermes 同步会把它当 provider 回写成只有 name 的空壳。实测 10 份备份里 4 份被污染 |
| 7 | **辅助调用行不套用主循环耗时** | 回归测试锁定。`latency_index` 只收 `kind=='main'` 的事件，且只在 `task==''` 的行上应用 |

### 性能

同数据 12 轮稳态中位：**v1 151.2ms → v2 147.3ms**（p95 174.3 → 159.9ms）。
初版曾因 `import yaml` 冷启动慢 50%，已把 config 污染检查移出热路径。

## 新增命令

```powershell
$sk = "C:\Users\zyq20\.config\opencode\skills\reverse-skill\skills\cc-switch-hermes-usage"
$py = "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe"

& $py "$sk\scripts\hermes_usage_sync.py" --check          # 含插件账本 + config 污染诊断
& $py "$sk\scripts\hermes_usage_sync.py" --report         # 聚合 vs 逐请求 JSON 报告
& $py "$sk\scripts\hermes_usage_sync.py" --repair-config  # 清理合成 provider 空壳
& $py "$sk\scripts\hermes_usage_sync.py" -v               # 逐条打印写入明细
```

退出码新增：**41** = `config.yaml` 被合成 provider 污染（仅 `--check` 报告，用 `--repair-config` 修）

## 职责边界（防双计的关键）

| 位置 | 放什么 | 绝不放什么 |
|---|---|---|
| `proxy_request_logs` | 聚合总量，**覆盖全部 task 含辅助任务** | 逐请求事件 |
| `sync-state.db` 的 `request_events` | 插件来的逐请求事实 | 总量 |

两者**永不相加**。这一点与上游 PR #6120 的设计结论一致。

## 与上游 PR #6120 / ccswitch-usage 插件的关系

实测交叉验证（2026-10-03，Hermes v0.21.2，Windows）：

| 能力 | 本 skill | ccswitch-usage 插件 |
|---|---|---|
| 辅助任务调用（标题/审批/后台审查） | ✅ 全覆盖 | ❌ 0（`post_auxiliary_call` 不在官方 37 个 VALID_HOOKS 里） |
| 主循环 Token 四维 | ✅ 与真值全等 | ✅ 与真值全等（两套独立实现互为验证） |
| 逐请求真实耗时 | 仅中位数（依赖插件数据） | ✅ 全量 |
| 真实 status_code | ❌ 拿不到 | ✅ 错误时可得 |
| 需装插件+重启 | ❌ 不需要 | ✅ 需要 |
| CC Switch 侧零改动 | ✅ | ❌ |

**结论：互补，不替代。** 插件账本作为可选增强喂 `latency_ms`，聚合仍由本 skill 全覆盖。

### 长任务实测修正（2026-10-03，32 分钟 / 43 次调用）

| 项 | 之前 | 实测 |
|---|---|---|
| 插件漏掉的调用 | 笼统说「辅助任务」 | 精确为 `title_generation` + `approval`（走 `auxiliary_client`）；`background_review` **被覆盖**（子 agent 走同一条 `conversation_loop`） |
| `latency_ms` 语义 | 全会话耗时中位数 | 只统计落在 `(prev_last_seen, cur_last_seen]` 窗口内的事件；首次基线留 0 而非编造 |
| 快照键 | `(profile, session, model, task)` | 必须等于源表 6 列主键，否则同 session 内重复计数（实测 +2 次调用 / +1994 input） |
| 聚合口径 | 插件 `input_tokens` = 净值 | 三方恒等式成立：`agent.log` 的 `in - cache_read` == 插件 == 聚合 |

详见 `references/v2-bench-ab.md`。

## 测试

```powershell
& $py "$sk\tests\test_hermes_sync_v2.py"   # 29 例：幂等/崩溃重放/只读/时间戳/延迟归属/config 修复
& $py "$sk\tests\test_hermes_sync.py"      # 4 例：v1 回归（基线/delta/重置/定价兜底/缺库）
```

## 参考

- `references/schema.md` — Hermes 与 CC Switch 两侧 schema 实测细节
- `references/v2-bench-ab.md` — **v2 优化记录 + v1/v2 性能基准 + 与插件的真机 A/B 数据**
- `references/troubleshooting.md` — 排障手册