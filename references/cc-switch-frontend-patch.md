# CC Switch 前端改动清单（4 个文件）

CC Switch **3.20.4 发布版**的使用统计页没有 Hermes 入口。本文档记录本 skill 配套的
本地前端改动，让你可以自行复现。

> **重要**：这些改动只在你本地的 CC Switch 副本里生效。本仓库不包含、不分发
> CC Switch 的源码或二进制。改动本身是 MIT 许可的上游代码，请遵守其许可证。
>
> 若官方未来合入等价实现（见文末「与官方 PR 的关系」），请优先用官方版本。

## 前置：CC Switch 后端已原生支持，无需改 Rust

3.20.4 的后端聚合查询（`get_usage_summary` / `get_provider_stats` / `get_model_stats` /
`get_usage_data_sources`）**已能按 `app_type = 'hermes'` 正确聚合**。
实测：向 `proxy_request_logs` 写入 `app_type='hermes'` 的记录后，这些接口会正常返回
`providerName = "Hermes Agent"`。缺的只是前端筛选按钮的入口，所以**只改前端即可**。

## 改动 1：`src/types/usage.ts`

把 `hermes` 加入 App 类型与已知应用列表。

```diff
 export type AppType =
   | "claude"
   | "codex"
   | "gemini"
   | "grokbuild"
   | "opencode"
+  | "hermes"
   | "pi"
   | "mcode";

 export const KNOWN_APP_TYPES: ReadonlyArray<AppType> = [
   "claude",
   "codex",
   "gemini",
   "grokbuild",
   "opencode",
+  "hermes",
   "pi",
   "mcode",
 ];
```

建议同时更新上方那段注释——原文写着 Hermes「只在别处作为受管应用出现」，改动后已不再准确：

```diff
- * `opencode` and `pi` have no proxy handler; their usage reaches this
- * dashboard through session importers. `openclaw` / `hermes` appear only as
- * managed apps elsewhere.
+ * `opencode` and `pi` have no proxy handler; their usage reaches this
+ * dashboard through session importers. `hermes` is the same: usage is
+ * imported from its cumulative session/model counters and filtered by
+ * `app_type = 'hermes'` like any other app.
```

## 改动 2：`src/components/usage/UsageDashboard.tsx`

筛选按钮行的图标映射。该字段类型为 `Record<AppType, string>`，所以改动 1 之后
**必须**同步补上，否则 `pnpm typecheck` 会报错。

```diff
 const APP_FILTER_ICON: Record<AppType, string> = {
   claude: "claude",
   codex: "openai",
   gemini: "gemini",
   grokbuild: "grok",
   opencode: "opencode",
+  hermes: "hermes",
   pi: "pi",
   mcode: "minimax",
 };
```

图标资源 `hermes.png` 及其 `metadata.ts` 注册**上游已自带**，无需新增图片。

## 改动 3：`src/components/usage/UsageHero.tsx`

标题区主题映射，类型同为 `Record<AppType | "all", TitleTheme>`，缺失会编译失败。
配色沿用其他本地应用的中性色系。

```diff
   opencode: {
     accent: "text-purple-600 dark:text-purple-400",
     iconBg: "bg-purple-500/10",
   },
+  hermes: {
+    accent: "text-emerald-600 dark:text-emerald-400",
+    iconBg: "bg-emerald-500/10",
+  },
   mcode: {
```

## 改动 4：4 个语言包

`src/i18n/locales/{zh,zh-TW,en,ja}.json`，在 `usage.appFilter` 中、紧随 `opencode` 之后插入：

```json
    "appFilter": {
      "all": "全部",
      "claude": "Claude Code",
      "codex": "Codex",
      "gemini": "Gemini",
      "opencode": "OpenCode",
      "hermes": "Hermes",
      "grokbuild": "Grok Build",
      "pi": "Pi",
      "mcode": "MiniMax Code"
    },
```

> 建议用脚本改写以保持 4 个文件键序完全一致（本 skill 首次实现即因手工缩进差异
> 产生了 diff 噪音）。改完务必 `python -m json.tool` 校验 JSON 合法。

## 验证

```bash
pnpm typecheck        # 关键：能同时验证改动 1/2/3 的 Record 类型完整性
pnpm test:unit        # UsageDashboard 相关测试应通过
pnpm tauri build --no-bundle
```

产物：`src-tauri/target/release/cc-switch.exe`。替换安装前请自行备份原文件。

## 与官方 PR 的关系

上游 [PR #6120](https://github.com/farion1231/cc-switch/pull/6120)
「feat(usage): sync aggregate Hermes usage」是官方的完整实现（后端 `session_usage_hermes.rs`
+ 前端）。截至 2026-09-29 仍为 open 状态。

本 skill **有意不移植**该 PR 的后端，原因与取舍见 `../README.md` 的
「为什么不直接用官方 PR」一节：官方方案用独立的快照/delta 表表达累计计数器，
更严谨；而本 skill 走 `proxy_request_logs`，实现简单但**存在无法消除的固有限制**
（见 README「已知局限」）。

两条路线数据层互不冲突：官方若合入，切换到官方同步后本 skill 应降级为「仅验证」，
直接停用 `--reset-baseline` 之外的写入即可。
