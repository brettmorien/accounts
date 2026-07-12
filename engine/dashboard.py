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
       <div id="chart-comp"></div></div>
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

function mk(el, series, data, extra) {{
  const opts = Object.assign({{
    width: el.clientWidth || 900, height: 340,
    scales: {{ x: {{ time: true }} }},
    axes: [ {{}}, {{ values: (u,vals)=>vals.map(usd) }} ],
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

// Composition (stacked areas via opaque cumulative fills, tallest drawn first)
const C = DATA.comp;
const compSeries = [ {{}} ];
const compData = [ DATA.x ];
for (let i=0; i<C.labels.length; i++) {{
  compSeries.push({{
    label: C.labels[i], stroke: C.colors[i], width: 1,
    fill: C.colors[i] + 'd9',
    value: (u, _v, sIdx, dIdx) => usd(C.contrib[sIdx-1][dIdx]),
  }});
  compData.push(C.cumulative[i]);
}}
mk(document.getElementById('chart-comp'), compSeries, compData);

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
