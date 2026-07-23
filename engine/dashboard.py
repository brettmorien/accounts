"""Generate a self-contained dashboard.html from the CSV store.

Charts use the vendored uPlot inlined into the page, so the generated file opens
fully offline (nothing is fetched at view time). If the vendored assets are ever
missing, we degrade to an inline-SVG total-net-worth chart + table so the
dashboard still renders.

Balances are observed on irregular dates (statements = month-end; aggregator =
run day), so every account is expanded to a last-observation-carried-forward
(LOCF) series over the union of dates before summing into net worth.
"""
import datetime as dt
import html
import json
import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from . import config, store

_PALETTE = [
    "#2563eb", "#16a34a", "#db2777", "#d97706", "#7c3aed", "#0891b2",
    "#dc2626", "#4d7c0f", "#9333ea", "#0d9488", "#ea580c", "#4f46e5",
]

# Map an institution name to a canonical key so a "(history)" series and the live
# aggregator accounts for the same brokerage can be matched despite formatting.
_INST_KEYS = [
    ("e-trade", "etrade"), ("e*trade", "etrade"), ("etrade", "etrade"),
    ("fidelity", "fidelity"), ("vanguard", "vanguard"), ("schwab", "schwab"),
    ("edward", "edwardjones"), ("merrill", "merrill"),
    ("robinhood", "robinhood"), ("morgan", "morganstanley"),
]


def _inst_key(org: Optional[str]) -> Optional[str]:
    s = (org or "").lower()
    for k, v in _INST_KEYS:
        if k in s:
            return v
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or None


# ---------------------------------------------------------------------------
# Data preparation
# ---------------------------------------------------------------------------

def _load() -> Tuple[List[dict], Dict[str, dict]]:
    balances = store.load_balances()
    accounts = {a["account_id"]: a for a in store.load_accounts()}
    return balances, accounts


def _account_label(aid: str, accounts: Dict[str, dict]) -> str:
    meta = accounts.get(aid, {})
    name = meta.get("name") or aid
    org = meta.get("org")
    return f"{org} · {name}" if org else name


def _build(balances: List[dict], accounts: Dict[str, dict]) -> dict:
    # A whole-institution history series must hand off to live per-account data
    # once that institution goes live, or the two would double-count. For each
    # import account, find the date its institution first appears in the
    # aggregator; from that date on the history series stops contributing.
    acct_sources: Dict[str, set] = defaultdict(set)
    for r in balances:
        acct_sources[r["account_id"]].add(r.get("source", ""))
    agg_first_inst: Dict[str, str] = {}
    for r in balances:
        if r.get("source") == "aggregator":
            key = _inst_key(accounts.get(r["account_id"], {}).get("org"))
            if key:
                cur = agg_first_inst.get(key)
                agg_first_inst[key] = min(cur, r["as_of"]) if cur else r["as_of"]
    retire: Dict[str, str] = {}
    for aid, srcs in acct_sources.items():
        # Only a pure-history account (no live rows of its own) whose institution
        # is now covered live gets retired. An account that carries both its own
        # history and live data (stitched via account_map) is kept intact.
        if "aggregator" not in srcs and "import" in srcs:
            key = _inst_key(accounts.get(aid, {}).get("org"))
            if key in agg_first_inst:
                retire[aid] = agg_first_inst[key]

    by_acct: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for r in balances:
        by_acct[r["account_id"]].append((r["as_of"], float(r["balance"])))
    for aid in by_acct:
        by_acct[aid].sort()

    dates = sorted({r["as_of"] for r in balances})
    if not dates:
        return {"empty": True}

    # LOCF value per account aligned to `dates` (None before first observation).
    locf: Dict[str, List[Optional[float]]] = {}
    for aid, series in by_acct.items():
        r_date = retire.get(aid)
        vals: List[Optional[float]] = []
        si, last = 0, None
        for d in dates:
            while si < len(series) and series[si][0] <= d:
                last = series[si][1]
                si += 1
            # A retired history series contributes nothing from its retire date on.
            vals.append(None if (r_date is not None and d >= r_date) else last)
        locf[aid] = vals

    total = [round(sum(v for v in (locf[a][i] for a in locf) if v is not None), 2)
             for i in range(len(dates))]

    # Order accounts by latest known value, descending (largest at stack bottom).
    latest_val = {aid: next((v for v in reversed(locf[aid]) if v is not None), 0.0)
                  for aid in locf}
    ordered = sorted(locf.keys(), key=lambda a: latest_val[a], reverse=True)

    # Cumulative (stacked) series, bottom-to-top: cum[i] = sum of first i+1 accts.
    cum: List[List[float]] = []
    running = [0.0] * len(dates)
    for aid in ordered:
        running = [running[i] + (locf[aid][i] or 0.0) for i in range(len(dates))]
        cum.append(list(running))

    # Emit in *draw order* (tallest band first so opaque fills stack correctly).
    comp_labels, comp_colors, comp_cumulative, comp_contrib = [], [], [], []
    for k in reversed(range(len(ordered))):
        aid = ordered[k]
        comp_labels.append(_account_label(aid, accounts))
        comp_colors.append(_PALETTE[k % len(_PALETTE)])
        comp_cumulative.append([round(x, 2) for x in cum[k]])
        comp_contrib.append([round(locf[aid][i] or 0.0, 2) for i in range(len(dates))])

    # Latest-balances table: current accounts only (retired history is superseded).
    table = []
    for aid in ordered:
        if aid in retire:
            continue
        series = by_acct[aid]
        latest = series[-1]
        prev = series[-2] if len(series) > 1 else None
        delta = latest[1] - prev[1] if prev else None
        pct = (delta / prev[1] * 100.0) if (prev and prev[1]) else None
        table.append({
            "label": _account_label(aid, accounts),
            "as_of": latest[0],
            "balance": round(latest[1], 2),
            "delta": round(delta, 2) if delta is not None else None,
            "pct": round(pct, 2) if pct is not None else None,
        })

    x = [int(dt.datetime.strptime(d, "%Y-%m-%d")
             .replace(tzinfo=dt.timezone.utc).timestamp()) for d in dates]

    # Reserve enough y-axis gutter for the widest "$1,234,567"-style tick label,
    # sized from the peak value so large balances are not clipped.
    peak = max(total) if total else 0.0
    ysize = max(56, int(len(f"${int(round(peak)):,}") * 7.5) + 22)

    return {
        "empty": False,
        "x": x,
        "datelabels": dates,
        "total": total,
        "comp": {
            "labels": comp_labels,
            "colors": comp_colors,
            "cumulative": comp_cumulative,
            "contrib": comp_contrib,
        },
        "table": table,
        "ysize": ysize,
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "grand_total": total[-1] if total else 0.0,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def generate() -> str:
    balances, accounts = _load()
    model = _build(balances, accounts)
    if model.get("empty"):
        html_doc = _empty_html()
    elif config.UPLOT_JS.exists() and config.UPLOT_CSS.exists():
        html_doc = _uplot_html(model)
    else:
        html_doc = _svg_html(model)
    config.DASHBOARD_HTML.write_text(html_doc, encoding="utf-8")
    return str(config.DASHBOARD_HTML)


def _empty_html() -> str:
    return _shell("Investment Trends", "<p class='muted'>No data yet. Run "
                  "<code>accounts-sync</code> (and drop history CSVs in "
                  "<code>import/</code>) to populate.</p>", head="", script="")


def _shell(title: str, body: str, head: str = "", script: str = "") -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 14px -apple-system, system-ui, sans-serif; margin: 0; padding: 24px;
         background: #fafafa; color: #18181b; }}
  h1 {{ font-size: 20px; margin: 0 0 2px; }}
  .muted {{ color: #71717a; }}
  .card {{ background: #fff; border: 1px solid #e4e4e7; border-radius: 12px;
          padding: 16px 18px; margin: 16px 0; box-shadow: 0 1px 2px rgba(0,0,0,.04); }}
  .big {{ font-size: 30px; font-weight: 650; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: right; padding: 7px 10px; border-bottom: 1px solid #f0f0f2; }}
  th:first-child, td:first-child {{ text-align: left; }}
  .up {{ color: #16a34a; }} .down {{ color: #dc2626; }}
  .hint {{ font-size: 12px; color: #71717a; margin: 2px 0 6px; }}
  .legend {{ margin-top: 10px; }}
  .legend .chip {{ display: inline-flex; align-items: center; gap: 5px;
        margin: 0 14px 6px 0; cursor: pointer; font-size: 12px; user-select: none; }}
  .legend .chip i {{ width: 11px; height: 11px; border-radius: 2px;
        display: inline-block; flex: none; }}
  .legend .chip .cv {{ color: #71717a; }}
  .legend .chip.dim {{ opacity: .32; }}
  .legend .chip.sel b {{ text-decoration: underline; }}
  .controls {{ display: flex; flex-wrap: wrap; gap: 8px 18px; align-items: center;
        margin: 6px 0 10px; font-size: 12px; }}
  .controls label {{ display: inline-flex; align-items: center; gap: 6px; }}
  .controls input[type=number] {{ width: 76px; padding: 2px 4px; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #0b0b0e; color: #e4e4e7; }}
    .card {{ background: #17171b; border-color: #27272a; }}
    th, td {{ border-color: #27272a; }} .muted {{ color: #a1a1aa; }}
  }}
  {head}
</style></head>
<body>{body}{script}</body></html>
"""


def _uplot_html(model: dict) -> str:
    uplot_css = config.UPLOT_CSS.read_text(encoding="utf-8")
    uplot_js = config.UPLOT_JS.read_text(encoding="utf-8")
    data_json = json.dumps(model)
    body = f"""
  <h1>Investment Trends</h1>
  <div class="muted">Net worth as of {html.escape(model['datelabels'][-1])} ·
       generated {html.escape(model['generated'])}</div>
  <div class="card"><div class="muted">Total net worth</div>
       <div class="big" id="grand"></div>
       <div id="chart-total"></div></div>
  <div class="card"><div class="muted">Composition over time</div>
       <div class="hint">Click an account to show only that one; click it again to restore all.</div>
       <div id="chart-comp"></div>
       <div id="comp-legend" class="legend"></div></div>
  <div class="card"><div class="muted">Projection</div>
       <div class="controls">
         <label>To year <input type="range" id="p-year" min="2027" max="2080" value="2060">
                <b id="p-year-val">2060</b></label>
         <label>Return %/yr <input type="number" id="p-return" value="7" step="0.5" min="0" max="25"></label>
         <label>Added $/yr <input type="number" id="p-contrib" value="0" step="1000" min="0">
                until <input type="number" id="p-contrib-until" value="2032" step="1" min="2027" max="2080"></label>
         <label>then withdraw $/yr (today's $) <input type="number" id="p-withdraw" value="0" step="1000" min="0"></label>
         <label>Inflation %/yr <input type="number" id="p-infl" value="3" step="0.25" min="0" max="20"></label>
         <label><input type="checkbox" id="p-ss" checked> Social Security (max)</label>
         <label>current age <input type="number" id="p-age" value="45" step="1" min="18" max="90"></label>
         <label>SS start age <input type="number" id="p-ss-age" value="65" step="1" min="62" max="70"></label>
       </div>
       <div id="chart-proj"></div>
       <div class="muted" id="proj-summary" style="margin-top:8px"></div></div>
  <div class="card"><div class="muted">Accounts</div>
       <table id="tbl"><thead><tr><th>Account</th><th>As of</th><th>Balance</th>
       <th>Change</th><th>%</th></tr></thead><tbody></tbody></table></div>
"""
    head = f"<style>{uplot_css}</style>"
    script = f"""
<script>{uplot_js}</script>
<script>
const DATA = {data_json};
const usd = v => v==null ? '' : '$' + Math.round(v).toLocaleString('en-US');
document.getElementById('grand').textContent = usd(DATA.grand_total);

// uPlot draws axis text on <canvas>, which CSS can't recolor, so pick readable
// axis/grid colors for the viewer's current color scheme.
const DARK = !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
const AXIS_STROKE = DARK ? '#a1a1aa' : '#52525b';
const GRID_STROKE = DARK ? 'rgba(255,255,255,0.10)' : 'rgba(0,0,0,0.07)';
const xAxis = () => ({{ stroke: AXIS_STROKE, grid: {{ stroke: GRID_STROKE, width: 1 }},
                        ticks: {{ stroke: GRID_STROKE, width: 1 }} }});
const yAxis = extra => Object.assign({{ stroke: AXIS_STROKE, grid: {{ stroke: GRID_STROKE, width: 1 }},
                        ticks: {{ stroke: GRID_STROKE, width: 1 }} }}, extra || {{}});

function mk(el, series, data, extra) {{
  const opts = Object.assign({{
    width: el.clientWidth || 900, height: 340,
    scales: {{ x: {{ time: true }} }},
    axes: [ xAxis(), yAxis({{ size: DATA.ysize, values: (u,vals)=>vals.map(usd) }}) ],
    legend: {{ live: true }},
    series: series,
  }}, extra || {{}});
  const u = new uPlot(opts, data, el);
  new ResizeObserver(() => u.setSize({{ width: el.clientWidth, height: 340 }}))
      .observe(el);
  return u;
}}

// Total net worth
mk(document.getElementById('chart-total'),
   [ {{}}, {{ label:'Total', stroke:'#2563eb', width:2,
             fill:'rgba(37,99,235,0.14)', value:(u,v)=>usd(v) }} ],
   [ DATA.x, DATA.total ]);

// Composition: stacked cumulative areas by default. A custom legend (below)
// lets you click an account to isolate it -- shown as its OWN value, not the
// cumulative band -- and click again to restore the full stack.
const C = DATA.comp;
const compSeries = [ {{}} ];
const compData = [ DATA.x ];
for (let i=0; i<C.labels.length; i++) {{
  compSeries.push({{ label: C.labels[i], stroke: C.colors[i], width: 1,
                     fill: C.colors[i] + 'd9' }});
  compData.push(C.cumulative[i]);
}}
const uComp = mk(document.getElementById('chart-comp'), compSeries, compData, {{
  legend: {{ show: false }},
  hooks: {{ setCursor: [ u => compHoverValues(u) ] }},
}});

let isolated = null;
const legendBox = document.getElementById('comp-legend');
C.labels.forEach((label, k) => {{
  const s = k + 1;                       // uPlot series index (0 = x)
  const chip = document.createElement('span');
  chip.className = 'chip';
  chip.dataset.s = s;
  chip.innerHTML = `<i style="background:${{C.colors[k]}}"></i>`
    + `<b></b> <span class="cv"></span>`;
  chip.querySelector('b').textContent = label;   // textContent avoids HTML injection
  chip.addEventListener('click', () => compToggle(s));
  legendBox.appendChild(chip);
}});

function compToggle(s) {{
  const restore = (isolated === s);
  isolated = restore ? null : s;
  uComp.batch(() => {{
    for (let i = 1; i < uComp.series.length; i++)
      uComp.setSeries(i, {{ show: restore ? true : i === s }});
    if (restore) {{
      uComp.setData(compData);
    }} else {{
      const nd = compData.map(a => a.slice());
      nd[s] = C.contrib[s - 1].slice();  // isolated series shows its own value
      uComp.setData(nd);
    }}
  }});
  legendBox.querySelectorAll('.chip').forEach(c => {{
    const sel = !restore && +c.dataset.s === s;
    c.classList.toggle('sel', sel);
    c.classList.toggle('dim', !restore && !sel);
  }});
}}

function compHoverValues(u) {{
  const idx = u.cursor.idx;
  legendBox.querySelectorAll('.chip').forEach(c => {{
    const k = (+c.dataset.s) - 1;
    c.querySelector('.cv').textContent = (idx == null) ? '' : usd(C.contrib[k][idx]);
  }});
}}

// Table
const tb = document.querySelector('#tbl tbody');
for (const r of DATA.table) {{
  const cls = r.delta==null ? '' : (r.delta>=0 ? 'up' : 'down');
  const sign = r.delta==null ? '' : (r.delta>=0 ? '+' : '');
  tb.insertAdjacentHTML('beforeend',
    `<tr><td>${{r.label}}</td><td class="muted">${{r.as_of}}</td>`
    + `<td>${{usd(r.balance)}}</td>`
    + `<td class="${{cls}}">${{r.delta==null?'—':sign+usd(r.delta)}}</td>`
    + `<td class="${{cls}}">${{r.pct==null?'—':sign+r.pct+'%'}}</td></tr>`);
}}

// ---- Projection: extend the net-worth line to a chosen year, with an optional
// inflation-adjusted ("today's dollars") overlay. All computed in the browser.
const projEl = document.getElementById('chart-proj');
const gid = id => document.getElementById(id);
const anchorTs = DATA.x[DATA.x.length - 1];   // latest history timestamp (sec)
const anchorVal = DATA.grand_total;           // latest total net worth
const anchorDate = new Date(anchorTs * 1000);
const thisYear = anchorDate.getUTCFullYear();
gid('p-year').min = thisYear + 1;
if (+gid('p-year').value <= thisYear) gid('p-year').value = Math.min(2080, thisYear + 30);

const projSeries = [ {{}},
  {{ label: 'History', stroke: '#2563eb', width: 2, fill: 'rgba(37,99,235,0.10)', value: (u,v)=>usd(v) }},
  {{ label: 'Projected (nominal)', stroke: '#7c3aed', width: 2, dash: [6,4], value: (u,v)=>usd(v) }},
  {{ label: "Projected (today's $)", stroke: '#0891b2', width: 2, dash: [2,4], value: (u,v)=>usd(v) }} ];
// uPlot passes already-formatted tick strings here (but tolerate raw numbers too).
const ysizeFn = (u, vals) => Math.max(70,
  (vals||[]).reduce((a,b)=>Math.max(a, (typeof b === 'number' ? usd(b) : String(b)).length), 0) * 8 + 22);
const uProj = new uPlot({{
  width: projEl.clientWidth || 900, height: 340, scales: {{ x: {{ time: true }} }},
  axes: [ xAxis(), yAxis({{ size: ysizeFn, values: (u,vals)=>vals.map(usd) }}) ],
  legend: {{ live: true }}, series: projSeries,
}}, [ DATA.x, DATA.total, DATA.x.map(()=>null), DATA.x.map(()=>null) ], projEl);
new ResizeObserver(() => uProj.setSize({{ width: projEl.clientWidth, height: 340 }})).observe(projEl);

// Social Security claiming-age factor vs full retirement age (67): claiming early
// reduces the benefit (5/9%/mo for the first 36 months, 5/12%/mo beyond); delaying
// adds 2/3%/mo (8%/yr) up to age 70.
function ssFactor(age) {{
  const FRA = 67;
  age = Math.max(62, Math.min(70, age));
  const months = (age - FRA) * 12;
  if (months < 0) {{
    const early = -months;
    const pct = Math.min(early, 36) * (5/9) + Math.max(0, early - 36) * (5/12);
    return 1 - pct/100;
  }}
  return 1 + months * (2/3)/100;
}}

function buildProjection() {{
  const endYear = Math.max(thisYear + 1, Math.min(2080, +gid('p-year').value || (thisYear + 30)));
  gid('p-year-val').textContent = endYear;
  const r = (+gid('p-return').value || 0) / 100;
  const c = +gid('p-contrib').value || 0;
  const W = +gid('p-withdraw').value || 0;   // yearly withdrawal in today's $, after contributions stop
  const infl = (+gid('p-infl').value || 0) / 100;
  const years = endYear - thisYear;
  // Contributions stop after this many years, then the balance keeps compounding.
  const tStop = Math.max(0, Math.min(years, (+gid('p-contrib-until').value || endYear) - thisYear));
  // Social Security (max earner): benefit from the SSA max-at-FRA, scaled by claiming age.
  const FRA_MAX_ANNUAL = 48216;            // ~SSA max of $4,018/mo at FRA (2025), today's $
  const ssOn = gid('p-ss').checked;
  const curAge = +gid('p-age').value || 0;
  const ssStartAge = +gid('p-ss-age').value || 65;
  const ssAnnual = ssOn ? FRA_MAX_ANNUAL * ssFactor(ssStartAge) : 0;      // today's $
  const ssStartYear = thisYear + Math.max(0, ssStartAge - curAge);

  const futTs = [], nominal = [], real = [];
  let bal = anchorVal, depletedYear = null;
  for (let t = 1; t <= years; t++) {{
    const ms = Date.UTC(thisYear + t, anchorDate.getUTCMonth(), anchorDate.getUTCDate());
    bal *= (1 + r);                          // one year of growth
    if (t <= tStop) {{
      bal += c;                              // accumulation: add contribution (end of year)
    }} else {{
      // Drawdown: spend W (today's $); Social Security covers part once it starts.
      const ssReal = (ssOn && (curAge + t) >= ssStartAge) ? ssAnnual : 0;
      const netReal = Math.max(0, W - ssReal);
      bal -= netReal * Math.pow(1 + infl, t); // remaining need, inflated to year t
      if (bal <= 0) {{ bal = 0; if (depletedYear === null) depletedYear = thisYear + t; }}
    }}
    futTs.push(Math.floor(ms / 1000));
    nominal.push(bal);
    real.push(bal / Math.pow(1 + infl, t));  // deflate to today's purchasing power
  }}
  const nHist = DATA.x.length;
  const nom = DATA.x.map(()=>null); nom[nHist - 1] = anchorVal;   // anchor at latest point
  const rl  = DATA.x.map(()=>null); rl[nHist - 1]  = anchorVal;
  uProj.setData([ DATA.x.concat(futTs),
                  DATA.total.concat(futTs.map(()=>null)),
                  nom.concat(nominal),
                  rl.concat(real) ]);

  const endNom = nominal.length ? nominal[nominal.length - 1] : anchorVal;
  const endReal = real.length ? real[real.length - 1] : anchorVal;
  let summary =
    'Projected ' + endYear + ': ' + usd(endNom) + ' nominal · ' + usd(endReal)
    + " in today's dollars — assumes " + (r * 100).toFixed(1) + '%/yr return'
    + (c ? ', ' + usd(c) + '/yr added until ' + (thisYear + tStop) : '')
    + (W ? ", then spending " + usd(W) + "/yr (today's $)" : '')
    + (ssAnnual > 0 ? ", Social Security " + usd(ssAnnual) + "/yr from age " + ssStartAge
        + ' (' + ssStartYear + ')' : '')
    + ', ' + (infl * 100).toFixed(1) + '%/yr inflation.';
  if (W > 0 && tStop < years) {{
    // Fixed real spending; its nominal amount grows with inflation each year.
    const nomFirst = W * Math.pow(1 + infl, tStop + 1);   // first drawdown year (tStop+1)
    const nomLast  = W * Math.pow(1 + infl, years);       // end year
    summary += ' Spending ' + usd(W) + "/yr today's $ = " + usd(nomFirst)
      + ' in ' + (thisYear + tStop + 1) + ' → ' + usd(nomLast) + ' by ' + endYear + ' nominal'
      + (ssAnnual > 0 ? "; Social Security covers " + usd(ssAnnual) + "/yr (today's $) from " + ssStartYear : '')
      + '.' + (depletedYear ? ' ⚠ Runs out ~' + depletedYear + '.' : '');
  }}
  gid('proj-summary').textContent = summary;
}}
['p-year','p-return','p-contrib','p-contrib-until','p-withdraw','p-infl','p-ss','p-age','p-ss-age'].forEach(
  id => gid(id).addEventListener('input', buildProjection));
buildProjection();
</script>
"""
    return _shell("Investment Trends", body, head=head, script=script)


def _svg_html(model: dict) -> str:
    """Static fallback: inline-SVG total line + table (no JS, no assets)."""
    total = model["total"]
    labels = model["datelabels"]
    w, h, pad = 900, 320, 44
    lo, hi = min(total), max(total)
    span = (hi - lo) or 1.0
    n = len(total)

    def pt(i, v):
        x = pad + (w - 2 * pad) * (i / max(n - 1, 1))
        y = h - pad - (h - 2 * pad) * ((v - lo) / span)
        return f"{x:.1f},{y:.1f}"

    poly = " ".join(pt(i, v) for i, v in enumerate(total))
    rows = ""
    for r in model["table"]:
        cls = "" if r["delta"] is None else ("up" if r["delta"] >= 0 else "down")
        delta = "—" if r["delta"] is None else f"{r['delta']:+,.0f}"
        pct = "—" if r["pct"] is None else f"{r['pct']:+.2f}%"
        rows += (f"<tr><td>{html.escape(r['label'])}</td>"
                 f"<td class='muted'>{r['as_of']}</td>"
                 f"<td>${r['balance']:,.0f}</td>"
                 f"<td class='{cls}'>{delta}</td><td class='{cls}'>{pct}</td></tr>")
    svg = f"""<svg viewBox="0 0 {w} {h}" width="100%" role="img">
  <polyline fill="none" stroke="#2563eb" stroke-width="2" points="{poly}"/>
  <text x="{pad}" y="20" fill="#71717a" font-size="12">${hi:,.0f}</text>
  <text x="{pad}" y="{h-pad+16}" fill="#71717a" font-size="12">{labels[0]}</text>
  <text x="{w-pad}" y="{h-pad+16}" fill="#71717a" font-size="12"
        text-anchor="end">{labels[-1]}</text>
</svg>"""
    body = f"""
  <h1>Investment Trends</h1>
  <div class="muted">Net worth as of {labels[-1]} · generated {model['generated']}
       · (static fallback — run accounts-setup to enable interactive charts)</div>
  <div class="card"><div class="muted">Total net worth</div>
       <div class="big">${model['grand_total']:,.0f}</div>{svg}</div>
  <div class="card"><div class="muted">Accounts</div>
       <table><thead><tr><th>Account</th><th>As of</th><th>Balance</th>
       <th>Change</th><th>%</th></tr></thead><tbody>{rows}</tbody></table></div>
"""
    return _shell("Investment Trends", body)


if __name__ == "__main__":
    print(generate())
