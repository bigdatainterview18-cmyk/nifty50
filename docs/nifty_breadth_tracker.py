"""
NIFTY 50 Live Market Breadth Tracker - H1 (Ceiling) vs L1 (Floor)
------------------------------------------------------------------
Strategy
========
For every NIFTY 50 constituent (the aggregate "NIFTY 50" index row itself
is excluded):

    Range = dayHigh - dayLow
    R20   = 0.20 * Range

    H1 (Ceiling / Bullish)  if lastPrice >= dayHigh - R20
    L1 (Floor   / Bearish)  if lastPrice <= dayLow  + R20

Any stock that satisfies neither condition is discarded entirely - there
is deliberately NO neutral bucket, no neutral slice, and no neutral label
anywhere in this program.

Edge case (documented assumption): when dayHigh == dayLow (zero intraday
range - e.g. a stock frozen at its open), both conditions become
mathematically true at once. To avoid double-counting a single stock in
both buckets, H1 is checked first and wins that tie. This only affects
stocks with literally zero traded range for the day.

    Total_Active = H1_count + L1_count
    H1_pct = H1_count / Total_Active * 100
    L1_pct = L1_count / Total_Active * 100

Bias / decision engine (thresholds as specified):
    L1_pct >= 70                      -> STRONG BEARISH
    H1_pct >= 70                      -> STRONG BULLISH
    40 <= H1_pct <= 60 and
    40 <= L1_pct <= 60                -> TUG-OF-WAR / BALANCED
    anything else (the 60-70% and
    <40% gaps the three bands above
    don't cover)                      -> MODERATE BULLISH / MODERATE BEARISH
                                          (whichever side leads) - added so
                                          every possible split always maps
                                          to a card; not in the original
                                          three named tiers.

Run:
    python nifty_breadth_tracker.py
(or just double-click run.bat on Windows)

Stops with Ctrl+C. Re-fetches and rewrites the dashboard every 5 minutes
(the HTML auto-refreshes itself in the browser on the same interval).

H1% history is appended to a fresh CSV each calendar day inside the H1_avg/
folder (nifty_h1_percentage_log_<YYYY-MM-DD>.csv), and that same file drives
the H1 vs AVG_H1 crossover line chart at the bottom of the dashboard - so the
chart only ever shows today's data.

Dependencies are deliberately kept to requests + pandas + matplotlib only
(no plotly) - matplotlib ships with most scientific-Python setups already,
which avoids the "No module named 'plotly'" install friction.
"""

import base64
import csv
import io
import math
import os
import sys
import time
import webbrowser
from datetime import datetime
from urllib.parse import quote

import requests

try:
    import pandas as pd  # noqa: F401  (kept for parity with requirements / easy future use)
except ImportError:
    sys.exit("pandas is required. Run: pip install requests pandas matplotlib")

try:
    import matplotlib
    matplotlib.use("Agg")  # no display needed, we only save PNGs
    import matplotlib.pyplot as plt
except ImportError:
    sys.exit("matplotlib is required. Run: pip install requests pandas matplotlib")

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
NSE_BASE = "https://www.nseindia.com"
NSE_LIVE_PAGE = f"{NSE_BASE}/market-data/live-equity-market"
INDEX_NAME = "NIFTY 50"

# NSE has changed the endpoint behind this page's table/download button at
# least once. Try the current one first, then fall back to the older one -
# whichever responds wins, so the tracker survives another silent switch.
API_NEW = f"{NSE_BASE}/api/NextApi/apiClient/marketWatchApi?functionName=getIndicesData&symbol="
API_OLD = f"{NSE_BASE}/api/equity-stockIndices?index="

FETCH_RETRIES = 3
REFRESH_SECONDS = 300  # 5 minutes
R20_FRACTION = 0.20

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_HTML = os.path.join(SCRIPT_DIR, "nifty_breadth_dashboard.html")
H1_AVG_DIR = os.path.join(SCRIPT_DIR, "H1_avg")


def get_h1_pct_csv_path(for_date: datetime = None) -> str:
    """One CSV per calendar day, e.g. H1_avg/nifty_h1_percentage_log_2026-09-09.csv.
    A new day automatically starts a fresh file, so the line chart never mixes
    in an older day's H1/AVG_H1 values."""
    d = (for_date or datetime.now()).strftime("%Y-%m-%d")
    return os.path.join(H1_AVG_DIR, f"nifty_h1_percentage_log_{d}.csv")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_LIVE_PAGE,
    "Connection": "keep-alive",
}

H1_COLOR = "#2ecc71"
L1_COLOR = "#e74c3c"


# ----------------------------------------------------------------------------
# Data fetching
# ----------------------------------------------------------------------------
def _extract_rows(payload: dict):
    """Both endpoint shapes end in a list of stock dicts - dig it out.
    Old endpoint:  {"data": [ {...}, {...} ]}
    New endpoint:  {"data": {"aduCount": {...}, "data": [ {...}, {...} ]}}"""
    d = payload["data"]
    if isinstance(d, dict):
        return d.get("data", [])
    return d


def fetch_nifty50_live(retries: int = FETCH_RETRIES) -> tuple:
    """Calls whichever JSON endpoint currently powers the live table + its
    CSV download button, trying the newer endpoint first and falling back
    to the legacy one, with a few retries against transient NSE blocks.
    Returns (rows, raw_payload)."""
    session = requests.Session()
    session.headers.update(HEADERS)
    quoted = quote(INDEX_NAME)

    last_err = None
    for attempt in range(1, retries + 1):
        for url in (API_NEW + quoted, API_OLD + quoted):
            try:
                # NSE only serves the API once these cookies exist on the session
                session.get(NSE_BASE, timeout=15)
                session.get(NSE_LIVE_PAGE, timeout=15)
                resp = session.get(url, timeout=20)
                resp.raise_for_status()
                payload = resp.json()
                rows = _extract_rows(payload)
                if rows:
                    return rows, payload
                last_err = RuntimeError(f"{url.split('?')[0]} returned no rows")
            except Exception as exc:
                last_err = exc
                print(f"  attempt {attempt} {url.split('?')[0]} failed: {exc}", file=sys.stderr)
        time.sleep(2 * attempt)

    raise RuntimeError(f"Could not fetch live NSE data after {retries} attempts: {last_err}")


def _ci_get(row: dict, *names):
    """Case/format-insensitive field lookup - NSE's two endpoints don't use
    the same key casing or naming (dayHigh vs DAYHIGH vs DAY_HIGH vs HIGH)."""
    norm_map = {str(k).upper().replace(" ", "").replace("_", ""): row[k] for k in row}
    for n in names:
        key = n.upper().replace(" ", "").replace("_", "")
        if key in norm_map:
            return norm_map[key]
    return None


def _safe_float(val):
    if val is None:
        return None
    s = str(val).replace(",", "").strip()
    if s in ("", "-", "N/A", "nan", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def get_data_timestamp(payload: dict) -> str:
    """NSE's own 'as on' timestamp for the data itself - checked in every
    location either endpoint shape is known to put it."""
    candidates = [payload, payload.get("data") if isinstance(payload.get("data"), dict) else None]
    for c in candidates:
        if not c:
            continue
        for key in ("timestamp", "time", "lastUpdateTime"):
            if c.get(key):
                return str(c[key])
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------------
def classify_h1_l1(rows: list):
    """Returns (h1_stocks, l1_stocks, total_fetched) where each stock is a
    dict: {symbol, ltp, high, low}. The aggregate index row itself (symbol
    starting with 'NIFTY') is skipped. A stock that satisfies neither H1 nor
    L1 is simply not counted toward Total_Active."""
    h1_stocks, l1_stocks = [], []
    total_fetched = 0

    for row in rows:
        symbol = _ci_get(row, "SYMBOL")
        if not symbol or str(symbol).strip().upper().startswith("NIFTY"):
            continue
        symbol = str(symbol).strip()
        total_fetched += 1

        day_high = _safe_float(_ci_get(row, "DAYHIGH", "HIGH"))
        day_low = _safe_float(_ci_get(row, "DAYLOW", "LOW"))
        ltp = _safe_float(_ci_get(row, "LASTPRICE", "LTP", "LAST"))
        if day_high is None or day_low is None or ltp is None:
            continue  # skip rows with missing/unparseable prices

        rng = day_high - day_low
        r20 = R20_FRACTION * rng

        stock = {"symbol": symbol, "ltp": ltp, "high": day_high, "low": day_low}

        if ltp >= day_high - r20:
            h1_stocks.append(stock)
        elif ltp <= day_low + r20:
            l1_stocks.append(stock)
        # else: not H1, not L1 - doesn't count toward Total_Active/H1_pct/L1_pct.

    return h1_stocks, l1_stocks, total_fetched


def compute_bias(h1_pct: float, l1_pct: float):
    """Returns (label, css_class, message) for the decision-engine card."""
    if l1_pct >= 70:
        return ("STRONG BEARISH", "bearish",
                "Sellers in Complete Control &mdash; Favor PE Buying / CE Selling")
    if h1_pct >= 70:
        return ("STRONG BULLISH", "bullish",
                "Buyers in Complete Control &mdash; Favor CE Buying / PE Selling")
    if 40 <= h1_pct <= 60 and 40 <= l1_pct <= 60:
        return ("TUG-OF-WAR / BALANCED", "balanced",
                "Neither side in control &mdash; wait for a clearer breakout before committing")
    if h1_pct > l1_pct:
        return ("MODERATE BULLISH", "bullish-mod",
                "Buyers lead but not decisively &mdash; favor CE Buying with tighter risk")
    return ("MODERATE BEARISH", "bearish-mod",
            "Sellers lead but not decisively &mdash; favor PE Buying with tighter risk")


def short_bias_word(h1_pct: float, l1_pct: float) -> str:
    """Single-word bias for the terminal log line."""
    if l1_pct > h1_pct:
        return "BEARISH"
    if h1_pct > l1_pct:
        return "BULLISH"
    return "BALANCED"


# ----------------------------------------------------------------------------
# Matplotlib pie chart, embedded as a base64 PNG - no plotly/JS dependency,
# renders identically offline since nothing is fetched from a CDN.
# ----------------------------------------------------------------------------
def build_pie_html(h1_count: int, h1_pct: int, l1_count: int, l1_pct: int) -> str:
    labels = [f"H1 (Ceiling)\n{h1_count} ({h1_pct}%)",
              f"L1 (Floor)\n{l1_count} ({l1_pct}%)"]
    sizes = [h1_count, l1_count]
    colors = [H1_COLOR, L1_COLOR]
    explode = [0.04 if h1_count >= l1_count else 0, 0.04 if l1_count > h1_count else 0]

    fig, ax = plt.subplots(figsize=(6, 6))
    wedges, texts = ax.pie(
        sizes,
        colors=colors,
        startangle=90,
        counterclock=False,
        explode=explode,
        wedgeprops={"edgecolor": "white", "linewidth": 2, "width": 0.65},  # donut look
    )
    for wedge, label in zip(wedges, labels):
        angle = (wedge.theta2 + wedge.theta1) / 2.0
        x = 0.68 * math.cos(math.radians(angle))
        y = 0.68 * math.sin(math.radians(angle))
        ax.text(x, y, label, ha="center", va="center", fontsize=12,
                fontweight="bold", color="white")
    ax.set_title("H1 vs L1", fontsize=13, fontweight="bold")
    ax.set_xlim(-1.3, 1.3)
    ax.set_ylim(-1.3, 1.3)
    ax.axis("equal")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", pad_inches=0.25, transparent=True)
    plt.close(fig)
    buf.seek(0)
    img_b64 = base64.b64encode(buf.read()).decode("utf-8")
    return f'<img src="data:image/png;base64,{img_b64}" alt="H1 vs L1 pie chart" style="max-width:100%;">'


# ----------------------------------------------------------------------------
# Matplotlib line chart of today's H1% vs AVG_H1% history, embedded as a
# base64 PNG - shows the crossover between the current H1 reading and its
# running average through the day. Reads only TODAY's H1_avg CSV file.
# ----------------------------------------------------------------------------
def build_line_chart_html(csv_path: str) -> str:
    if not os.path.isfile(csv_path):
        return '<div class="empty-pie">No H1% history logged yet today.</div>'

    times, h1_vals, avg_vals = [], [], []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                times.append(row["time"].split(" ")[-1])  # HH:MM:SS portion
                h1_vals.append(float(row["H1"]))
                avg_vals.append(float(row["AVG_H1"]))
            except (KeyError, ValueError, TypeError):
                continue

    if len(times) < 1:
        return '<div class="empty-pie">No H1% history logged yet today.</div>'

    fig, ax = plt.subplots(figsize=(9.5, 3.4))
    x = range(len(times))
    ax.plot(x, h1_vals, color=H1_COLOR, linewidth=2, marker="o", markersize=3, label="H1 %")
    ax.plot(x, avg_vals, color="#3498db", linewidth=2, marker="o", markersize=3, label="AVG H1 %")

    # Mark crossover points where H1 flips from above to below AVG_H1 (or vice versa)
    for i in range(1, len(x)):
        prev_diff = h1_vals[i - 1] - avg_vals[i - 1]
        curr_diff = h1_vals[i] - avg_vals[i]
        if prev_diff == 0:
            continue
        if (prev_diff > 0) != (curr_diff > 0):
            ax.scatter([x[i]], [h1_vals[i]], color="#f39c12", s=55, zorder=5, edgecolor="white")

    step = max(1, len(times) // 8)
    ax.set_xticks(list(x)[::step])
    ax.set_xticklabels([times[i] for i in x][::step], rotation=0, fontsize=8)
    ax.set_ylabel("Percent (%)", fontsize=9)
    ax.set_title("Today's H1% vs AVG H1% (Crossover Tracker)", fontsize=11, fontweight="bold")
    ax.legend(loc="upper right", fontsize=8, frameon=False)
    ax.grid(True, axis="y", linestyle="--", alpha=0.35)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", pad_inches=0.25, transparent=True)
    plt.close(fig)
    buf.seek(0)
    img_b64 = base64.b64encode(buf.read()).decode("utf-8")
    return (f'<img src="data:image/png;base64,{img_b64}" alt="H1 vs AVG H1 line chart" '
            f'style="max-width:100%; width:100%; height:auto; display:block;">')


# ----------------------------------------------------------------------------
# H1% history CSV - one file per day inside H1_avg/, e.g.
# H1_avg/nifty_h1_percentage_log_2026-09-09.csv
# ----------------------------------------------------------------------------
H1_PCT_CSV_FIELDS = ["time", "H1", "AVG_H1"]


def write_h1_percentage_csv(h1_pct: float, checked_at: str) -> tuple:
    """Appends one row per run to today's H1_avg CSV: time, this run's H1%
    (rounded to a whole number, no decimals), and the average of every H1%
    value logged so far today INCLUDING this one - e.g. after 2 runs with
    H1 = 60 and 40, AVG_H1 on that 2nd row is (60 + 40) / 2 = 50, rounded to
    a whole number. Because a new file starts automatically each calendar
    day, the average - and the line chart built from this file - only ever
    reflects today's values, never older days'. Returns (h1_rounded, avg_h1_rounded)."""
    os.makedirs(H1_AVG_DIR, exist_ok=True)
    csv_path = get_h1_pct_csv_path()

    h1_rounded = round(h1_pct)

    previous_values = []
    if os.path.isfile(csv_path):
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    previous_values.append(float(row["H1"]))
                except (KeyError, ValueError, TypeError):
                    pass  # skip any malformed row rather than crash

    all_values = previous_values + [h1_rounded]
    avg_h1_rounded = round(sum(all_values) / len(all_values))

    file_exists = os.path.isfile(csv_path)
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=H1_PCT_CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({"time": checked_at, "H1": h1_rounded, "AVG_H1": avg_h1_rounded})

    return h1_rounded, avg_h1_rounded


# ----------------------------------------------------------------------------
# HTML assembly
# ----------------------------------------------------------------------------
def _stock_rows(stocks: list) -> str:
    if not stocks:
        return '<tr><td colspan="4" class="empty">No stocks currently active in this bucket</td></tr>'
    rows = []
    for s in sorted(stocks, key=lambda x: x["symbol"]):
        rows.append(
            f'<tr><td>{s["symbol"]}</td><td>{s["ltp"]:.2f}</td>'
            f'<td>{s["high"]:.2f}</td><td>{s["low"]:.2f}</td></tr>'
        )
    return "\n".join(rows)


def write_dashboard_html(h1_stocks: list, l1_stocks: list, total_fetched: int,
                          data_as_on: str, checked_at: str) -> tuple:
    h1_count, l1_count = len(h1_stocks), len(l1_stocks)
    total_active = h1_count + l1_count

    if total_active == 0:
        pie_html = '<div class="empty-pie">No H1 or L1 stocks currently active.</div>'
        h1_pct = l1_pct = 0
        bias_label, bias_class, bias_msg = "NO ACTIVE SIGNAL", "balanced", \
            "No stock currently qualifies as H1 or L1 - check back next cycle"
    else:
        h1_pct = round(h1_count / total_active * 100)
        l1_pct = round(l1_count / total_active * 100)
        pie_html = build_pie_html(h1_count, h1_pct, l1_count, l1_pct)
        bias_label, bias_class, bias_msg = compute_bias(h1_pct, l1_pct)

    line_chart_html = build_line_chart_html(get_h1_pct_csv_path())

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>
  // JS backup for the meta-refresh above, so the page auto-reloads every
  // 5 minutes even in browsers/contexts that ignore <meta http-equiv=refresh>.
  setTimeout(function () {{ window.location.reload(); }}, {REFRESH_SECONDS} * 1000);
</script>
<title>NIFTY 50 Live Market Breadth (H1 vs L1)</title>
<style>
  :root {{
    --h1: {H1_COLOR}; --l1: {L1_COLOR};
    --bearish: #e74c3c; --bearish-bg: #fdecea;
    --bullish: #2ecc71; --bullish-bg: #eafaf1;
    --balanced: #f39c12; --balanced-bg: #fef5e7;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: 'Segoe UI', Arial, sans-serif; background: #eef1f5; margin: 0;
    padding: 32px 16px; color: #222;
  }}
  .wrap {{ max-width: 1080px; margin: 0 auto; }}
  header {{ text-align: center; margin-bottom: 24px; }}
  h1 {{ font-size: 26px; margin: 0 0 10px; color: #1a1a2e; }}
  .badge {{
    display: inline-block; background: #1a1a2e; color: #fff; font-size: 13px;
    padding: 8px 18px; border-radius: 999px; font-weight: 600; letter-spacing: .2px;
  }}
  .checked {{ color: #888; font-size: 12px; margin-top: 8px; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }}
  @media (max-width: 800px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  .card {{
    background: #fff; border-radius: 16px; padding: 24px;
    box-shadow: 0 2px 14px rgba(0,0,0,.06);
  }}
  .card h2 {{ font-size: 15px; text-transform: uppercase; letter-spacing: .5px;
              color: #888; margin: 0 0 14px; }}
  .decision {{
    grid-column: 1 / -1; border-radius: 16px; padding: 26px 30px; text-align: center;
  }}
  .decision.bearish, .decision.bearish-mod {{ background: var(--bearish-bg); border: 2px solid var(--bearish); }}
  .decision.bullish, .decision.bullish-mod {{ background: var(--bullish-bg); border: 2px solid var(--bullish); }}
  .decision.balanced {{ background: var(--balanced-bg); border: 2px solid var(--balanced); }}
  .decision .label {{ font-size: 22px; font-weight: 800; margin-bottom: 6px; }}
  .decision.bearish .label, .decision.bearish-mod .label {{ color: var(--bearish); }}
  .decision.bullish .label, .decision.bullish-mod .label {{ color: var(--bullish); }}
  .decision.balanced .label {{ color: var(--balanced); }}
  .decision .msg {{ font-size: 14px; color: #444; }}
  .stats {{ display: flex; justify-content: space-around; margin-top: 18px; }}
  .stat {{ text-align: center; }}
  .stat .n {{ font-size: 24px; font-weight: 800; }}
  .stat.h1 .n {{ color: var(--h1); }}
  .stat.l1 .n {{ color: var(--l1); }}
  .stat .k {{ font-size: 12px; color: #888; text-transform: uppercase; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid #eee; }}
  th {{ color: #888; font-size: 11px; text-transform: uppercase; }}
  td.empty {{ color: #aaa; text-align: center; padding: 18px 0; }}
  .list-card.h1 h2 {{ color: var(--h1); }}
  .list-card.l1 h2 {{ color: var(--l1); }}
  .scroll {{ max-height: 320px; overflow-y: auto; }}
  .empty-pie {{ text-align: center; color: #aaa; padding: 80px 0; font-size: 15px; }}
  .pie-card {{ display: flex; flex-direction: column; align-items: center; }}
  .line-card {{ grid-column: 1 / -1; }}
  .line-card img {{ max-width: 100%; height: auto; }}
  footer {{ text-align: center; color: #aaa; font-size: 12px; margin-top: 24px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>NIFTY 50 Live Market Breadth (H1 vs L1)</h1>
    <div class="badge">Last Updated: {data_as_on} &nbsp;(Updated every 5 mins)</div>
    <div class="checked">Dashboard last checked: {checked_at} &middot; Stocks fetched: {total_fetched} &middot; Active (H1+L1): {total_active}</div>
  </header>

  <div class="grid">
    <div class="decision {bias_class}">
      <div class="label">{bias_label}</div>
      <div class="msg">{bias_msg}</div>
      <div class="stats">
        <div class="stat h1"><div class="n">{h1_count} ({h1_pct}%)</div><div class="k">H1 &middot; Ceiling</div></div>
        <div class="stat l1"><div class="n">{l1_count} ({l1_pct}%)</div><div class="k">L1 &middot; Floor</div></div>
      </div>
    </div>
  </div>

  <div class="grid">
    <div class="card pie-card">
      <h2>H1 vs L1 Distribution</h2>
      {pie_html}
    </div>
    <div class="grid" style="grid-template-columns: 1fr; gap: 20px; margin-bottom:0;">
      <div class="card list-card h1">
        <h2>H1 &mdash; Ceiling Stocks ({h1_count})</h2>
        <div class="scroll">
        <table>
          <tr><th>Symbol</th><th>LTP</th><th>Day High</th><th>Day Low</th></tr>
          {_stock_rows(h1_stocks)}
        </table>
        </div>
      </div>
      <div class="card list-card l1">
        <h2>L1 &mdash; Floor Stocks ({l1_count})</h2>
        <div class="scroll">
        <table>
          <tr><th>Symbol</th><th>LTP</th><th>Day High</th><th>Day Low</th></tr>
          {_stock_rows(l1_stocks)}
        </table>
        </div>
      </div>
    </div>
  </div>

  <div class="grid">
    <div class="card line-card">
      <h2>Today's H1% vs AVG H1% &mdash; Crossover</h2>
      {line_chart_html}
    </div>
  </div>

  <footer>Auto-refreshes every 5 minutes &middot; generated by nifty_breadth_tracker.py</footer>
</div>
</body>
</html>"""

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    return h1_count, h1_pct, l1_count, l1_pct


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------
def run_once(browser_opened: list) -> None:
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_time = datetime.now().strftime("%H:%M:%S")

    try:
        rows, payload = fetch_nifty50_live()
        h1_stocks, l1_stocks, total_fetched = classify_h1_l1(rows)
        data_as_on = get_data_timestamp(payload)
    except Exception as exc:
        print(f"[{log_time}] [ERROR] Live NSE fetch failed: {exc} - will retry next cycle.")
        return

    # Compute H1% now and log it to the CSV FIRST, so when write_dashboard_html()
    # below builds the line chart from that same CSV, this cycle's own point is
    # already in the file - otherwise the chart always lags one cycle behind
    # (it would only ever show up through the PREVIOUS run's point).
    h1_count_raw, l1_count_raw = len(h1_stocks), len(l1_stocks)
    total_active_raw = h1_count_raw + l1_count_raw
    h1_pct_raw = round(h1_count_raw / total_active_raw * 100) if total_active_raw else 0
    h1_rounded, avg_h1_rounded = write_h1_percentage_csv(h1_pct_raw, checked_at)

    h1_count, h1_pct, l1_count, l1_pct = write_dashboard_html(
        h1_stocks, l1_stocks, total_fetched, data_as_on, checked_at
    )
    total_active = h1_count + l1_count
    bias_word = short_bias_word(h1_pct, l1_pct) if total_active else "NONE"

    print(f"[{log_time}] Fetched {total_fetched} stocks | Active: {total_active} | "
          f"H1: {h1_count} ({h1_pct}%) | L1: {l1_count} ({l1_pct}%) | Bias: {bias_word}")
    print(f"[{log_time}] H1% log -> {get_h1_pct_csv_path()} | H1: {h1_rounded} | AVG_H1 so far: {avg_h1_rounded}")

    if not browser_opened[0]:
        try:
            webbrowser.open("file://" + OUTPUT_HTML)
        except Exception:
            pass
        browser_opened[0] = True


def main() -> None:
    print("NIFTY 50 Live Market Breadth Tracker (H1 vs L1)")
    print(f"Writing dashboard to: {OUTPUT_HTML}")
    print(f"Appending H1% history to (new file each day): {H1_AVG_DIR}{os.sep}nifty_h1_percentage_log_<date>.csv")
    print(f"Refresh interval: {REFRESH_SECONDS} seconds")
    print("Press Ctrl+C to stop.\n")

    browser_opened = [False]
    while True:
        cycle_start = time.monotonic()
        try:
            run_once(browser_opened)
        except Exception as exc:
            print(f"[ERROR] Unexpected failure in update cycle: {exc}")

        # Sleep only the time REMAINING in this 5-minute slot, not a flat
        # 300s on top of whatever run_once() (fetch + retries) already took.
        # Without this, slow fetches/retries silently stack cycle after
        # cycle and the dashboard's data creeps further behind wall-clock
        # time the longer the script runs.
        elapsed = time.monotonic() - cycle_start
        remaining = REFRESH_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)
        else:
            print(f"[WARN] Cycle took {elapsed:.1f}s, longer than the "
                  f"{REFRESH_SECONDS}s refresh interval - running next cycle immediately.")


if __name__ == "__main__":
    main()