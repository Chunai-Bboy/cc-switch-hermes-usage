#!/usr/bin/env node
/**
 * Hermes 用量面板前端（单文件 HTML，内嵌 CSS/JS）。
 * 由 hermes-usage-panel.js 的 renderHTML() 返回。
 * 设计目标：一眼看到所有 app，点图标即看该 app 用量。
 */
"use strict";

const APP_META = {
  claude:    { label: "Claude",   icon: "C", color: "#d97757" },
  codex:     { label: "Codex",    icon: "O", color: "#10a37f" },
  gemini:    { label: "Gemini",   icon: "G", color: "#4285f4" },
  grokbuild: { label: "Grok",     icon: "X", color: "#1d9bf0" },
  opencode:  { label: "OpenCode", icon: "◆", color: "#a855f7" },
  hermes:    { label: "Hermes",   icon: "H", color: "#22c55e" },
  pi:        { label: "Pi",       icon: "π", color: "#f59e0b" },
  mcode:     { label: "MCode",    icon: "M", color: "#ef4444" },
};

function renderHTML() {
  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes 用量面板</title>
<style>
  :root{
    --bg:#0b0d10; --panel:#14171c; --line:#24282f; --fg:#e6e8eb;
    --dim:#8b9199; --accent:#22c55e;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;}
  .wrap{max-width:1180px;margin:0 auto;padding:20px}
  header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:18px}
  h1{font-size:18px;margin:0;letter-spacing:.3px}
  .muted{color:var(--dim);font-size:12px}
  .ranges{display:flex;gap:6px;margin-left:auto}
  .ranges button{background:var(--panel);border:1px solid var(--line);color:var(--dim);
    padding:5px 11px;border-radius:7px;cursor:pointer;font-size:12px}
  .ranges button.on{background:var(--accent);border-color:var(--accent);color:#04240f;font-weight:600}

  /* ---- app 图标栏：核心入口 ---- */
  .apps{display:grid;grid-template-columns:repeat(auto-fill,minmax(128px,1fr));
    gap:10px;margin-bottom:20px}
  .app{background:var(--panel);border:1px solid var(--line);border-radius:12px;
    padding:14px 12px;cursor:pointer;text-align:center;transition:.15s;position:relative}
  .app:hover{border-color:#3a4048;transform:translateY(-1px)}
  .app.on{border-color:var(--accent);background:#16211a}
  .app .ic{width:34px;height:34px;border-radius:9px;display:flex;align-items:center;
    justify-content:center;font-weight:700;font-size:16px;margin:0 auto 8px;color:#fff}
  .app .nm{font-size:13px;font-weight:600}
  .app .rq{font-size:11px;color:var(--dim);margin-top:3px}
  .app.on .rq{color:var(--accent)}

  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));
    gap:12px;margin-bottom:20px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
  .card .k{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.6px}
  .card .v{font-size:22px;font-weight:650;margin-top:5px}
  .card .s{font-size:11px;color:var(--dim);margin-top:3px}

  section{background:var(--panel);border:1px solid var(--line);border-radius:12px;
    padding:16px;margin-bottom:16px}
  h2{font-size:13px;margin:0 0 12px;color:var(--dim);font-weight:600;
    text-transform:uppercase;letter-spacing:.6px}

  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th,td{text-align:right;padding:7px 9px;border-bottom:1px solid var(--line);white-space:nowrap}
  th:first-child,td:first-child{text-align:left}
  th{color:var(--dim);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.4px}
  tr:last-child td{border-bottom:none}
  td.num{font-variant-numeric:tabular-nums}

  .bars{display:flex;align-items:flex-end;gap:4px;height:120px;padding-top:8px}
  .bars .b{flex:1;background:linear-gradient(180deg,#2f7d4f,#1d4a30);border-radius:3px 3px 0 0;
    min-height:2px;position:relative;cursor:default}
  .bars .b:hover{background:var(--accent)}
  .bars .b span{position:absolute;bottom:-18px;left:50%;transform:translateX(-50%);
    font-size:9px;color:var(--dim)}
  .err{color:#ef4444;font-size:12px;padding:10px;background:#2a1518;border-radius:8px}
  .empty{color:var(--dim);text-align:center;padding:24px;font-size:13px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Hermes 用量面板</h1>
    <span class="muted" id="meta"></span>
    <div class="ranges" id="ranges"></div>
  </header>

  <div class="apps" id="apps"></div>

  <div class="cards" id="cards"></div>

  <section>
    <h2>每日趋势（按请求数）</h2>
    <div class="bars" id="bars"></div>
    <div style="height:22px"></div>
  </section>

  <section>
    <h2>按模型</h2>
    <table id="models"></table>
  </section>

  <section>
    <h2>请求明细（最近 200 条）</h2>
    <table id="logs"></table>
  </section>
</div>

<script>
const META = ${JSON.stringify(APP_META)};
const PRESETS = ["today","1d","7d","14d","30d","all"];
const PRESET_LABEL = {today:"当天",all:"全部"};
let cur = { range:"7d", app:"hermes" };

const fmtN = n => (n||0).toLocaleString("en-US");
const fmtT = n => {
  n = n||0;
  if (n >= 1e6) return (n/1e6).toFixed(1)+"M";
  if (n >= 1e3) return (n/1e3).toFixed(1)+"K";
  return String(n);
};
const fmtU = n => "$"+(n||0).toFixed(4);

function el(tag, cls, html){
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}

function renderRanges(){
  const box = document.getElementById("ranges");
  box.innerHTML = "";
  PRESETS.forEach(p => {
    const b = el("button", p === cur.range ? "on" : "",
                 PRESET_LABEL[p] || p);
    b.onclick = () => { cur.range = p; cur.app = "hermes"; loadAll(); };
    box.appendChild(b);
  });
}

function renderApps(apps){
  const box = document.getElementById("apps");
  box.innerHTML = "";
  if (!apps.length){ box.appendChild(el("div","empty","该时间范围内没有任何用量")); return; }
  apps.forEach(a => {
    const m = META[a.app_type] || {label:a.app_type, icon:"?", color:"#666"};
    const d = el("div", "app" + (a.app_type === cur.app ? " on" : ""));
    d.appendChild(el("div","ic",m.icon));
    d.style.setProperty("--accent", m.color);
    d.querySelector(".ic").style.background = m.color;
    d.appendChild(el("div","nm",m.label));
    d.appendChild(el("div","rq",fmtN(a.reqs)+" 次 · "+fmtU(a.cost)));
    d.title = m.label + "：" + fmtN(a.reqs) + " 次请求";
    d.onclick = () => { cur.app = a.app_type; loadAll(); };
    box.appendChild(d);
  });
}

function renderCards(d, models){
  const totalReq = d.logs.length;
  const inp = models.reduce((s,m)=>s+(m.inp||0),0);
  const oup = models.reduce((s,m)=>s+(m.oup||0),0);
  const cr  = models.reduce((s,m)=>s+(m.cr||0),0);
  const cost= models.reduce((s,m)=>s+(m.cost||0),0);
  const hit = (cr+inp) > 0 ? (100*cr/(cr+inp)).toFixed(1)+"%" : "0%";
  const data = [
    ["总请求数", fmtN(totalReq), "明细行数（窗口内）"],
    ["真实消耗 Tokens", fmtT(inp+oup), "input "+fmtT(inp)+" + output "+fmtT(oup)],
    ["缓存命中", fmtT(cr), "命中率 "+hit],
    ["总成本", fmtU(cost), "按 model_pricing 计"],
    ["活跃天数", fmtN(d.days.length), "窗口内有调用的天数"],
    ["模型数", fmtN(models.length), "用过的模型个数"],
  ];
  const box = document.getElementById("cards");
  box.innerHTML = "";
  data.forEach(([k,v,s]) => {
    const c = el("div","card");
    c.appendChild(el("div","k",k));
    c.appendChild(el("div","v",v));
    c.appendChild(el("div","s",s));
    box.appendChild(c);
  });
}

function renderBars(days){
  const box = document.getElementById("bars");
  box.innerHTML = "";
  if (!days.length){ box.appendChild(el("div","empty","无数据")); return; }
  const max = Math.max(...days.map(x=>x.reqs||0), 1);
  days.slice().reverse().forEach(x => {
    const b = el("div","b");
    b.style.height = Math.max(2, 100*(x.reqs||0)/max) + "%";
    b.title = x.d + "：" + fmtN(x.reqs) + " 次 · " + fmtT(x.inp) + " in · " + fmtU(x.cost);
    b.appendChild(el("span", "", x.d.slice(5)));
    box.appendChild(b);
  });
}

function renderTable(id, head, rows){
  const t = document.getElementById(id);
  t.innerHTML = "";
  const tr = el("tr");
  head.forEach(h => tr.appendChild(el("th", "", h)));
  t.appendChild(tr);
  if (!rows.length){
    const r = el("tr"); const td = el("td","","（无数据）");
    td.colSpan = head.length; td.style.textAlign="center"; td.style.color="var(--dim)";
    r.appendChild(td); t.appendChild(r); return;
  }
  rows.forEach(row => {
    const r = el("tr");
    head.forEach((h,i) => {
      const td = el("td", typeof row[i] === "number" ? "num" : "", row[i]);
      r.appendChild(td);
    });
    t.appendChild(r);
  });
}

async function loadAll(){
  renderRanges();
  try {
    const meta = document.getElementById("meta");
    meta.textContent = "加载中…";
    const [appsRes, appRes] = await Promise.all([
      fetch("/api/apps?range=" + cur.range).then(r=>r.json()),
      fetch("/api/app?app=" + cur.app + "&range=" + cur.range).then(r=>r.json()),
    ]);
    if (appsRes.error) throw new Error(appsRes.error);
    if (appRes.error)   throw new Error(appRes.error);

    // 选中的 app 若在列表里不存在（该范围无数据），回退到第一个
    if (!appsRes.apps.some(a => a.app_type === cur.app) && appsRes.apps.length){
      cur.app = appsRes.apps[0].app_type;
      return loadAll();
    }

    renderApps(appsRes.apps);
    renderCards(appRes, appRes.models);
    renderBars(appRes.days);

    const m = META[cur.app] || {label:cur.app};
    document.querySelector("h1").textContent = m.label + " 用量";

    renderTable("models",
      ["模型","请求数","input","output","缓存读","成本"],
      appRes.models.map(x => [x.model, fmtN(x.reqs), fmtT(x.inp),
                              fmtT(x.oup), fmtT(x.cr), fmtU(x.cost)]));

    renderTable("logs",
      ["时间","模型","input","output","缓存","耗时ms","成本"],
      appRes.logs.map(x => [x.t, x.model, fmtN(x.inp), fmtN(x.oup),
                            fmtN(x.cr), fmtN(x.lat), fmtU(x.cost)]));

    meta.textContent = "范围 " + cur.range + " · 更新于 " +
      new Date(appsRes.generated).toLocaleTimeString();
  } catch (e) {
    document.getElementById("meta").innerHTML =
      '<span class="err">加载失败：' + (e.message || e) + "</span>";
  }
}

loadAll();
setInterval(loadAll, 15000);
</script>
</body>
</html>`;
}

module.exports = { renderHTML, APP_META };
