# Hermes / CC Switch Schema 与数据流（实测 2026-09-28）

## 1. Hermes `state.db`（`%LOCALAPPDATA%\hermes\state.db`）

### sessions 表（关键列）

| 列 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | 如 `20260911_215548_ff024d` |
| model | TEXT | 会话主模型 |
| billing_provider | TEXT | 如 `llamacpp`/`custom`/`nous` |
| billing_base_url | TEXT | |
| billing_mode | TEXT | 如 `chat_completions` |
| input_tokens / output_tokens / cache_read_tokens / cache_write_tokens | INTEGER | 会话级累计 |
| estimated_cost_usd / actual_cost_usd | REAL | cost_status=actual 时为真实成本 |
| started_at / ended_at / last_activity_at | REAL | Unix 秒（float） |
| api_call_count | INTEGER | 会话主循环调用累计（PR #6120 用于残差对账） |
| title | TEXT | LLM 生成标题 |

### session_model_usage 表（**本 skill 数据源**）

| 列 | 类型 | 说明 |
|---|---|---|
| session_id | TEXT | FK → sessions.id |
| model | TEXT | 实际计费模型，如 `step-3.7-flash` |
| task | TEXT | `''`=主循环；`title_generation` 等为辅助任务 |
| api_call_count | INTEGER | **累计** |
| input_tokens / output_tokens / cache_read_tokens / cache_write_tokens | INTEGER | **累计** |
| estimated_cost_usd / actual_cost_usd | REAL | **累计** |
| cost_status | TEXT | `actual`/`estimated`/`unknown` |
| first_seen / last_seen | REAL | 首次/最后更新时间（Unix 秒 float） |

特性：一行 = (session, model, task) 的**累计快照**，UPDATE 语义因此必须转 delta。
多 profile：`%LOCALAPPDATA%\hermes\profiles\<name>\state.db` 结构相同。

## 2. CC Switch `cc-switch.db`（`~\.cc-switch\cc-switch.db`）

### proxy_request_logs 表（写入目标，24 列）

```
request_id(PK), provider_id, app_type, model, request_model,
input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
input_cost_usd, output_cost_usd, cache_read_cost_usd, cache_creation_cost_usd,
total_cost_usd, latency_ms, first_token_ms, status_code, error_message,
session_id, provider_type, is_streaming, cost_multiplier, created_at, data_source
```

### providers 表

- 本 skill 注册 `_hermes_session` / `app_type=hermes` / `name='Hermes Agent'` / `settings_config='{}'`

### model_pricing 表

- `model_id`, `input_cost_per_million`, `output_cost_per_million`,
  `cache_read_cost_per_million`, `cache_creation_cost_per_million`（美元/百万 token）
- 成本兜底计算来源

### 原生 session provider 命名规则（Rust usage_stats.rs 内 CASE）

```
'_session'            -> 'Claude (Session)'
'_codex_session'      -> 'Codex (Session)'
'_gemini_session'     -> 'Gemini (Session)'
'_opencode_session'   -> 'OpenCode (Session)'
'_grok_session'       -> 'Grok Build (Session)'
'_mcode_session'      -> 'MiniMax Code (Session)'
'_pi_session'         -> 'Pi (Session)'
ELSE provider_id
```

`_hermes_session` 无 CASE 分支，但 `COALESCE(providers.name, ...)` 优先取
providers 表 name → 显示 **Hermes Agent**（实测 get_provider_stats 已验证）。

## 3. 已验证数据流（CDP 实测）

```
hermes_usage_sync.py
  → INSERT OR IGNORE proxy_request_logs (data_source='hermes_session', app_type='hermes')
      → CC Switch Tauri 命令（CDP 直调验证）:
          get_usage_data_sources     → {dataSource:'hermes_session', requestCount, totalCostUsd}
          get_usage_summary_by_app   → {appType:'hermes', summary:{...}}
          get_provider_stats         → {providerId:'_hermes_session', providerName:'Hermes Agent'}
          get_model_stats(appType=hermes) → 按模型聚合
          get_request_logs           → 请求日志行
      → 前端使用统计页:
          Hero 总量 / 趋势图 / 请求日志 / Provider 统计 / 模型统计
          "全部来源"下拉 = get_provider_stats.providerName → 出现 "Hermes Agent"
```

前端源码要点（farion1231/cc-switch main 分支 `src/types/usage.ts`）：
- `AppType` = claude|codex|gemini|grokbuild|opencode|pi|mcode，**不含 hermes**
  （注释：hermes 仅作为 managed app 出现）→ 无独立 App 按钮，数据走"全部"App
- 下拉选项池来自 `get_provider_stats`（按当前 App + 时间范围过滤）

## 4. 已知边界

1. **日期范围**：下拉/统计默认"当天"，hermes 历史数据需切宽范围才出现
2. **双计防护**：CC Switch 有 hermes_session 记录但 sync-state.db 为空时脚本拒绝运行（ERROR[40]）
3. **缓存语义**：CC Switch 使用 `usage_daily_rollups` 汇总，由后端自动维护，无需手动写
