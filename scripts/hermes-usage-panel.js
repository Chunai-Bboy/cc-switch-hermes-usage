#!/usr/bin/env node
/**
 * Hermes 用量面板 —— 独立本地服务，只读 CC Switch 的库。
 *
 * 为什么单独做：CC Switch 4.0.12 前端已 Rust 重写，本机无 cargo 无法改源码重编；
 * 而 Hermes 数据一直都在库里，只缺一个入口。
 *
 * 用法： node hermes-usage-panel.js [端口]   默认 8799
 * 然后浏览器开 http://127.0.0.1:8799
 *
 * 数据源： ~/.cc-switch/cc-switch.db  (proxy_request_logs)
 * 口径：   与 CC Switch 的 by_app 一致 —— 排除会被 proxy 日志双算的 data_source，
 *          claude-desktop 折叠进 claude。
 */
"use strict";

const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync } = require("child_process");

const PORT = Number(process.argv[2] || 8799);
const CC_DB = path.join(os.homedir(), ".cc-switch", "cc-switch.db");

// 复用 hermes venv 的 python 读 sqlite（本机 node 没有 sqlite 绑定，别为此装依赖）
const PY = path.join(process.env.LOCALAPPDATA || "", "hermes", "hermes-agent",
                     "venv", "Scripts", "python.exe");
const HAS_PY = fs.existsSync(PY);

// UI 单独一个文件，避免本文件过长难维护
const { renderHTML } = require("./hermes-usage-panel-ui.js");

/** 与 usage_stats.rs 的 by_app 同口径 */
const SQL_BY_APP = `
SELECT app_type,
       SUM(reqs)  AS reqs,
       SUM(cost)  AS cost,
       SUM(inp)   AS input,
       SUM(oup)   AS output,
       SUM(cr)    AS cache_read,
       SUM(cc)    AS cache_creation
FROM (
  SELECT CASE WHEN l.app_type='claude-desktop' THEN 'claude' ELSE l.app_type END AS app_type,
         COUNT(*) AS reqs,
         COALESCE(SUM(CAST(l.total_cost_usd AS REAL)),0) AS cost,
         COALESCE(SUM(l.input_tokens),0)  AS inp,
         COALESCE(SUM(l.output_tokens),0) AS oup,
         COALESCE(SUM(l.cache_read_tokens),0) AS cr,
         COALESCE(SUM(l.cache_creation_tokens),0) AS cc
  FROM proxy_request_logs l
  WHERE l.created_at >= :s AND l.created_at <= :e
    AND NOT (COALESCE(l.data_source,'proxy') IN
             ('session_log','codex_session','gemini_session','opencode_session'))
  GROUP BY 1
  UNION ALL
  SELECT CASE WHEN r.app_type='claude-desktop' THEN 'claude' ELSE r.app_type END,
         COALESCE(SUM(r.request_count),0),
         COALESCE(SUM(CAST(r.total_cost_usd AS REAL)),0),
         COALESCE(SUM(r.input_tokens),0),
         COALESCE(SUM(r.output_tokens),0),
         COALESCE(SUM(r.cache_read_tokens),0),
         COALESCE(SUM(r.cache_creation_tokens),0)
  FROM usage_daily_rollups r
  WHERE r.date >= :ds AND r.date <= :de
  GROUP BY 1
) GROUP BY app_type ORDER BY reqs DESC`;

/**
 * 防双算规则只用于【汇总】（/api/apps）：CC Switch 自己的 proxy 日志
 * (data_source='proxy') 与会话明细会重复，Hero 总数必须排除其中一份，
 * 否则 opencode 这类会被算两遍（实测 121 次翻成 242 次）。
 *
 * 但【明细/日志表】照实显示全部行 —— CC Switch 的请求日志就是这么做的
 * （用户截图里 opencode 明细正常列出）。所以详情接口不带这个过滤。
 */
const SQL_APP_DAYS = `
SELECT date(created_at,'unixepoch','localtime') AS d,
       COUNT(*) AS reqs,
       COALESCE(SUM(CAST(total_cost_usd AS REAL)),0) AS cost,
       COALESCE(SUM(input_tokens),0)  AS inp,
       COALESCE(SUM(output_tokens),0) AS oup,
       COALESCE(SUM(cache_read_tokens),0) AS cr,
       COALESCE(SUM(cache_creation_tokens),0) AS cc
FROM proxy_request_logs
WHERE app_type=:app AND created_at>=:s AND created_at<=:e
GROUP BY d ORDER BY d`;

const SQL_APP_MODELS = `
SELECT COALESCE(model,'?') AS model, COUNT(*) AS reqs,
       COALESCE(SUM(CAST(total_cost_usd AS REAL)),0) AS cost,
       COALESCE(SUM(input_tokens),0) AS inp,
       COALESCE(SUM(output_tokens),0) AS oup,
       COALESCE(SUM(cache_read_tokens),0) AS cr
FROM proxy_request_logs
WHERE app_type=:app AND created_at>=:s AND created_at<=:e
GROUP BY model ORDER BY reqs DESC`;

const SQL_APP_LOGS = `
SELECT datetime(created_at,'unixepoch','localtime') AS t,
       COALESCE(model,'?') AS model,
       COALESCE(input_tokens,0) AS inp,
       COALESCE(output_tokens,0) AS oup,
       COALESCE(cache_read_tokens,0) AS cr,
       COALESCE(latency_ms,0) AS lat,
       COALESCE(CAST(total_cost_usd AS REAL),0) AS cost
FROM proxy_request_logs
WHERE app_type=:app AND created_at>=:s AND created_at<=:e
ORDER BY created_at DESC LIMIT 200`;

const APP_META = {
  claude:   { label: "Claude",   icon: "C", color: "#d97757" },
  codex:    { label: "Codex",    icon: "O", color: "#10a37f" },
  gemini:   { label: "Gemini",   icon: "G", color: "#4285f4" },
  grokbuild:{ label: "Grok",     icon: "X", color: "#1d9bf0" },
  opencode: { label: "OpenCode", icon: "◈", color: "#a855f7" },
  hermes:   { label: "Hermes",   icon: "H", color: "#22c55e" },
  pi:       { label: "Pi",       icon: "π", color: "#f59e0b" },
  mcode:    { label: "MCode",    icon: "M", color: "#ef4444" },
};

const PY_HELPER = `
import json, os, sqlite3, sys, time
db, sql, params = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
c = sqlite3.connect("file:%s?mode=ro" % db.replace(chr(92), "/"), uri=True)
c.execute("PRAGMA query_only=ON")
cols = [d[0] for d in c.execute(sql, params).description]
print(json.dumps([dict(zip(cols, r)) for r in c.execute(sql, params)]))
`;

function query(sql, params) {
  if (!HAS_PY) throw new Error("找不到 hermes venv 的 python： " + PY);
  const helper = path.join(os.tmpdir(), "hus_panel_q.py");
  fs.writeFileSync(helper, PY_HELPER, "utf8");
  const out = execFileSync(PY, ["-u", "-X", "utf8", helper, CC_DB, sql,
                               JSON.stringify(params)],
                          { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
  return JSON.parse(out);
}

function rangeBounds(preset) {
  const now = Math.floor(Date.now() / 1000);
  const d = new Date();
  const today0 = Math.floor(new Date(d.getFullYear(), d.getMonth(),
                                     d.getDate()).getTime() / 1000);
  const table = {
    today: today0,
    "1d": now - 86400,
    "7d": now - 7 * 86400,
    "14d": now - 14 * 86400,
    "30d": now - 30 * 86400,
    all: 0,
  };
  const start = table[preset] !== undefined ? table[preset] : table["7d"];
  return { s: start, e: now + 60,
           ds: new Date(start * 1000).toISOString().slice(0, 10),
           de: new Date(now * 1000).toISOString().slice(0, 10) };
}

function json(res, obj, code) {
  const body = JSON.stringify(obj);
  res.writeHead(code || 200, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  res.end(body);
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://127.0.0.1");
  const preset = url.searchParams.get("range") || "7d";
  const b = rangeBounds(preset);

  try {
    if (url.pathname === "/api/apps") {
      return json(res, { preset, generated: Date.now(),
                         apps: query(SQL_BY_APP, b) });
    }
    if (url.pathname === "/api/app") {
      const app = url.searchParams.get("app") || "hermes";
      return json(res, {
        app, preset,
        days: query(SQL_APP_DAYS, Object.assign({ app }, b)),
        models: query(SQL_APP_MODELS, Object.assign({ app }, b)),
        logs: query(SQL_APP_LOGS, Object.assign({ app }, b)),
      });
    }
    if (url.pathname === "/" || url.pathname === "/index.html") {
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      return res.end(renderHTML());
    }
    json(res, { error: "not found" }, 404);
  } catch (e) {
    json(res, { error: String(e && e.message || e) }, 500);
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log("Hermes 用量面板: http://127.0.0.1:%d" % PORT);
  console.log("数据源(只读): %s" % CC_DB);
});
