#!/usr/bin/env python3
"""
Nifty Option Score Tracker -- live NSE fetch version
=======================================================

This version fetches directly from the two NSE pages instead of reading
CSV files you download by hand:
    1) https://www.nseindia.com/option-chain
    2) https://www.nseindia.com/market-data/live-equity-market

No browser, no clicking Download, no manual re-downloading every cycle --
the script calls NSE's own JSON APIs behind those pages itself, on a
timer, and keeps everything else (the scoring formula, the daily Strike x
Time log, the HTML page) exactly as before.

Why this needed a real fix, not just "call the API": NSE retired the old
`/api/option-chain-indices?symbol=NIFTY` endpoint (that's why it was
returning 404 Not Found) and replaced it with `/api/option-chain-v3`,
which also requires a specific expiry date rather than defaulting to the
nearest one. So each cycle now does three calls in sequence, all on the
same warmed-up session (matching how the site itself behaves):
    a) visit the option-chain page to pick up cookies
    b) ask /api/option-chain-contract-info for the list of valid expiry
       dates, and take the nearest one
    c) ask /api/option-chain-v3 for that specific expiry's data

The NIFTY-50 OPEN price fetch (from the live-equity-market page's API)
is unchanged from before and was already working correctly for you.

If NSE fails or blocks a request on any given cycle (this can happen even
from a home connection, just less often than from a datacenter), the
script does NOT crash -- it prints the error, keeps showing the last
successful data with a "stale" warning once too much time has passed,
and simply tries again next cycle.

Formula, rounding, and file formats are UNCHANGED from the version you
were running -- verified against your Opions_score.xlsx:
    Call Previous OI = Call OI - Call Change in OI
    Put  Previous OI = Put OI  - Put  Change in OI
    Call % change    = Call Change in OI / Call Previous OI * 100
    Put  % change     = Put  Change in OI / Put  Previous OI * 100
    Option Score      = (Put % change - Call % change) / 10
    -> rounded to the nearest whole number, ties away from zero (Excel-style)

USAGE
-----
    pip install requests
    python nifty_option_score.py

Useful flags:
    --symbol NAME        Option-chain symbol (default NIFTY)
    --index-name NAME     Row label to match for the open price (default "NIFTY 50")
    --band POINTS         +/- points around today's open (default 1000)
    --interval SECONDS    How often to refresh (default 600 = 10 min)
    --retries N            Fetch attempts per cycle before giving up (default 3)

Files produced (next to this script):
    option_score.html                 <- open this / it opens itself automatically
    option_score/option_score_<date>.csv
                                        <- one clean pivot file per trading day: Strike
                                           price, then one column per time (HH:MM), each
                                           cell the rounded score. Nothing else stored.
"""

import argparse
import csv
import os
import sys
import time
import webbrowser
from datetime import datetime
from urllib.parse import quote

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(SCRIPT_DIR, "option_score.html")
LOG_DIR = os.path.join(SCRIPT_DIR, "option_score")

NSE_BASE = "https://www.nseindia.com"
NSE_OPTION_CHAIN_PAGE = f"{NSE_BASE}/option-chain"
NSE_LIVE_PAGE = f"{NSE_BASE}/market-data/live-equity-market"

OPTION_CHAIN_CONTRACT_INFO_API = f"{NSE_BASE}/api/option-chain-contract-info?symbol="
OPTION_CHAIN_V3_API = f"{NSE_BASE}/api/option-chain-v3?type=Indices&symbol={{}}&expiry={{}}"

# NSE has changed the endpoint behind the live-equity-market table/download
# button at least once. Try the newer one first, fall back to the older one.
INDEX_API_NEW = f"{NSE_BASE}/api/NextApi/apiClient/marketWatchApi?functionName=getIndicesData&symbol="
INDEX_API_OLD = f"{NSE_BASE}/api/equity-stockIndices?index="

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}


# --------------------------------------------------------------------------
# Live NSE fetching -- session warm-up + retries + graceful failure.
# --------------------------------------------------------------------------
def new_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _ci_get(row: dict, *names):
    """Case/format-insensitive field lookup -- NSE's endpoints don't always
    use the same key casing (open vs OPEN vs Open)."""
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


def _extract_index_rows(payload: dict):
    """Both known endpoint shapes end in a list of index/stock dicts.
    Old endpoint:  {"data": [ {...}, {...} ]}
    New endpoint:  {"data": {"aduCount": {...}, "data": [ {...}, {...} ]}}"""
    d = payload.get("data")
    if isinstance(d, dict):
        return d.get("data", [])
    return d or []


def _get_nearest_expiry(session, symbol):
    """Ask NSE for the list of valid expiry dates for `symbol` and return
    the nearest one -- option-chain-v3 needs an explicit expiry, it no
    longer defaults to the nearest one on its own."""
    url = OPTION_CHAIN_CONTRACT_INFO_API + quote(symbol)
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    dates = data.get("expiryDates") or data.get("records", {}).get("expiryDates")
    if not dates:
        raise RuntimeError("No expiry dates returned by option-chain-contract-info")
    return dates[0]


def fetch_option_chain_raw(symbol="NIFTY", retries=3):
    """Returns a list of (strike, call_oi, call_chng_oi, put_oi, put_chng_oi)
    for every strike NSE currently lists for `symbol`'s nearest expiry."""
    last_err = None

    for attempt in range(1, retries + 1):
        try:
            session = new_session()
            session.headers["Referer"] = NSE_OPTION_CHAIN_PAGE
            session.get(NSE_BASE, timeout=15)
            session.get(NSE_OPTION_CHAIN_PAGE, timeout=15)  # cookies required for the API calls below

            expiry = _get_nearest_expiry(session, symbol)

            url = OPTION_CHAIN_V3_API.format(quote(symbol), quote(expiry))
            resp = session.get(url, timeout=20)
            if resp.status_code == 401:
                # Cookies expired mid-flow -- refresh once and retry this same attempt.
                session = new_session()
                session.get(NSE_BASE, timeout=15)
                session.get(NSE_OPTION_CHAIN_PAGE, timeout=15)
                resp = session.get(url, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
            records = payload.get("records", {}).get("data", [])

            rows = []
            for rec in records:
                # v3 can return more than one expiry in a single payload --
                # keep only the strikes for the expiry we actually asked for.
                rec_expiry = rec.get("expiryDates") or rec.get("expiryDate")
                if rec_expiry and rec_expiry != expiry:
                    continue

                ce, pe = rec.get("CE"), rec.get("PE")
                strike = rec.get("strikePrice")
                if strike is None and ce:
                    strike = ce.get("strikePrice")
                if strike is None and pe:
                    strike = pe.get("strikePrice")
                if strike is None or not ce or not pe:
                    continue

                rows.append((
                    float(strike),
                    float(ce.get("openInterest", 0) or 0),
                    float(ce.get("changeinOpenInterest", 0) or 0),
                    float(pe.get("openInterest", 0) or 0),
                    float(pe.get("changeinOpenInterest", 0) or 0),
                ))

            if rows:
                return rows
            last_err = RuntimeError(f"option-chain-v3 returned no strikes for expiry {expiry}")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            print(f"  option-chain attempt {attempt} failed: {exc}", file=sys.stderr)
        time.sleep(2 * attempt)

    raise RuntimeError(f"Could not fetch option chain after {retries} attempts: {last_err}")


def fetch_index_open(index_name="NIFTY 50", retries=3):
    """Returns today's OPEN value for `index_name` (e.g. "NIFTY 50")."""
    last_err = None

    for attempt in range(1, retries + 1):
        session = new_session()
        for url in (INDEX_API_NEW + quote(index_name), INDEX_API_OLD + quote(index_name)):
            try:
                session.headers["Referer"] = NSE_LIVE_PAGE
                session.get(NSE_BASE, timeout=15)
                session.get(NSE_LIVE_PAGE, timeout=15)
                resp = session.get(url, timeout=20)
                resp.raise_for_status()
                payload = resp.json()
                rows = _extract_index_rows(payload)
                for row in rows:
                    sym = _ci_get(row, "SYMBOL", "INDEX")
                    if sym and str(sym).strip().upper() == index_name.upper():
                        open_v = _safe_float(_ci_get(row, "OPEN"))
                        if open_v is not None:
                            return open_v
                last_err = RuntimeError(f"'{index_name}' row not found / no OPEN field")
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                print(f"  index attempt {attempt} {url.split('?')[0]} failed: {exc}", file=sys.stderr)
        time.sleep(2 * attempt)

    raise RuntimeError(f"Could not fetch index open after {retries} attempts: {last_err}")


# --------------------------------------------------------------------------
# Calculations -- UNCHANGED (verified against Opions_score.xlsx)
# --------------------------------------------------------------------------
def round_half_up(x):
    """Round to the nearest whole number, ties away from zero -- e.g. 2.5
    -> 3, -2.5 -> -3. Matches Excel's ROUND(), unlike Python's built-in
    round() which rounds ties to even (2.5 -> 2)."""
    if x >= 0:
        return int(x + 0.5)
    return -int(-x + 0.5)


def compute_rows(raw_rows, open_price, band, verbose=True):
    low, high = open_price - band, open_price + band

    if verbose and raw_rows:
        all_strikes = sorted(r[0] for r in raw_rows)
        in_range = [s for s in all_strikes if low <= s <= high]
        below = [s for s in in_range if s < open_price]
        above = [s for s in in_range if s > open_price]
        print(f"    strikes fetched     : {len(all_strikes)} "
              f"({all_strikes[0]:,.0f} to {all_strikes[-1]:,.0f})")
        print(f"    open price used     : {open_price:,.2f}")
        print(f"    window              : {low:,.0f} to {high:,.0f}")
        print(f"    kept in window      : {len(in_range)} "
              f"({len(below)} below open, {len(above)} above open)")
        if not above:
            print("    NOTE: no strikes above the open were found in the fetched chain.")

    rows = []
    for strike, call_oi, call_chng, put_oi, put_chng in raw_rows:
        if not (low <= strike <= high):
            continue

        call_prev = call_oi - call_chng
        put_prev = put_oi - put_chng

        call_pct = (call_chng / call_prev * 100) if call_prev else 0.0
        put_pct = (put_chng / put_prev * 100) if put_prev else 0.0

        option_score = (put_pct - call_pct) / 10

        rows.append(
            {
                "strike": strike,
                "call_oi": call_oi,
                "call_chng": call_chng,
                "call_prev_oi": call_prev,
                "call_pct_chng": round(call_pct, 2),
                "put_oi": put_oi,
                "put_chng": put_chng,
                "put_prev_oi": put_prev,
                "put_pct_chng": round(put_pct, 2),
                "option_score": round_half_up(option_score),
            }
        )
    rows.sort(key=lambda r: r["strike"])
    return rows


# --------------------------------------------------------------------------
# Time-pivoted score log (Strike x Time grid) -- UNCHANGED
# --------------------------------------------------------------------------
def log_path_for_date(date_str):
    os.makedirs(LOG_DIR, exist_ok=True)
    return os.path.join(LOG_DIR, f"option_score_{date_str}.csv")


def load_score_grid(path):
    """Read an existing pivot CSV into {strike: {time_label: score}}.
    Returns an empty dict if the file doesn't exist yet.

    Expected layout:
        Strike price,09:30,09:40,...
        22500,-0.5,-1.08
        22550,-2.02,
    """
    grid = {}
    if not os.path.exists(path):
        return grid

    with open(path, newline="", encoding="utf-8") as f:
        reader = list(csv.reader(f))
    if not reader:
        return grid

    header = reader[0]
    times = header[1:]

    for row in reader[1:]:
        if not row or row[0].strip() == "":
            continue
        try:
            strike = float(row[0].strip())
        except ValueError:
            continue
        for j, tlabel in enumerate(times, start=1):
            if j >= len(row):
                continue
            v = row[j].strip()
            if v != "":
                try:
                    grid.setdefault(strike, {})[tlabel] = float(v)
                except ValueError:
                    pass

    return grid


def save_score_grid(path, grid):
    """Write {strike: {time_label: score}} out as a clean pivot CSV:
    Strike price in column 1, times across the header row, rounded scores
    in the body -- nothing else (no OI, no change, no volume)."""
    strikes = sorted(grid.keys())
    times = sorted({t for scores in grid.values() for t in scores.keys()})

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Strike price"] + times)
        for strike in strikes:
            row = [f"{strike:g}"]
            for t in times:
                v = grid[strike].get(t)
                row.append("" if v is None else round_half_up(v))
            writer.writerow(row)


def update_score_grid(rows, timestamp_dt):
    """Add this cycle's strike -> option_score values into today's pivot
    CSV (overwriting the same time-slot if this cycle re-runs), save it,
    and return (grid, log_path) as freshly re-read from that file.

    If the file can't be saved right now (e.g. it's open in Excel and
    Windows has it locked), the in-memory grid -- including this cycle's
    new values -- is still returned so the HTML page keeps updating; the
    save is simply retried next cycle.
    """
    date_str = timestamp_dt.strftime("%Y-%m-%d")
    time_label = timestamp_dt.strftime("%H:%M")
    log_path = log_path_for_date(date_str)

    grid = load_score_grid(log_path)
    for r in rows:
        grid.setdefault(r["strike"], {})[time_label] = round_half_up(r["option_score"])

    try:
        save_score_grid(log_path, grid)
        grid = load_score_grid(log_path)  # read back exactly what's on disk
    except PermissionError:
        print(f"    NOTE: could not save {log_path} right now (likely open in "
              f"Excel) -- will retry next cycle. Showing today's data anyway.")

    return grid, log_path


# --------------------------------------------------------------------------
# HTML output -- UNCHANGED (Strike x Time pivot grid, sticky headers)
# --------------------------------------------------------------------------
def render_html(grid, open_price, band, symbol, timestamp, interval,
                 error=None, source_note="", log_path=""):
    def score_class(v):
        if v > 0:
            return "pos"
        if v < 0:
            return "neg"
        return "zero"

    strikes = sorted(grid.keys())
    times = sorted({t for scores in grid.values() for t in scores.keys()})

    header_cells = "".join(f"<th>{t}</th>" for t in times)

    body_rows = []
    for strike in strikes:
        cells = [f'<td class="strike-col">{strike:,.0f}</td>']
        for t in times:
            v = grid[strike].get(t)
            if v is None:
                cells.append("<td>&ndash;</td>")
            else:
                v = round_half_up(v)
                cells.append(f'<td class="{score_class(v)}">{v}</td>')
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    body_html = "\n".join(body_rows)

    error_banner = (
        f'<div class="error">Warning: {error}. '
        f'Showing most recent successful data.</div>'
        if error
        else ""
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="{interval}">
<title>Nifty Option Score</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
          background:#0f172a; color:#e2e8f0; margin:0; padding:24px; }}
  h1 {{ margin:0 0 4px 0; font-size:22px; }}
  .meta {{ color:#94a3b8; font-size:13px; margin-bottom:16px; }}
  .error {{ background:#7c2d12; color:#fed7aa; padding:8px 12px; border-radius:6px;
            margin-bottom:12px; font-size:13px; }}
  .grid-wrap {{ overflow:auto; max-height:80vh; max-width:100%; border:1px solid #1e293b; }}
  table {{ border-collapse:collapse; }}
  th, td {{ padding:6px 12px; text-align:right; border-bottom:1px solid #1e293b;
            border-right:1px solid #1e293b; white-space:nowrap; }}
  th {{ background:#1e293b; color:#cbd5e1; font-size:13px; position:sticky; top:0; z-index:2; }}
  td.strike-col, th:first-child {{
      position:sticky; left:0; z-index:1; background:#0f172a;
      text-align:left; font-weight:600; color:#e2e8f0;
  }}
  th:first-child {{ z-index:3; background:#1e293b; }}
  tr:hover td {{ background:#1e293b; }}
  tr:hover td.strike-col {{ background:#1e293b; }}
  .pos {{ color:#4ade80; font-weight:600; }}
  .neg {{ color:#f87171; font-weight:600; }}
  .zero {{ color:#94a3b8; }}
  .badge {{ display:inline-block; background:#1e293b; padding:2px 8px; border-radius:4px;
            font-size:12px; color:#93c5fd; margin-left:8px; }}
</style>
</head>
<body>
  <h1>{symbol} Option Score <span class="badge">Open &plusmn; {band:g}</span></h1>
  <div class="meta">
    Open: {open_price:,.2f} &nbsp;|&nbsp; Range: {open_price - band:,.0f} &ndash; {open_price + band:,.0f}
    &nbsp;|&nbsp; Last updated: {timestamp} &nbsp;|&nbsp; Auto-refresh every {interval // 60} min
    {('<br>' + source_note) if source_note else ''}
    {('<br>Log: ' + log_path) if log_path else ''}
  </div>
  {error_banner}
  <div class="grid-wrap">
    <table>
      <thead>
        <tr><th>Strike</th>{header_cells}</tr>
      </thead>
      <tbody>
        {body_html if body_html else '<tr><td class="strike-col">No data yet</td></tr>'}
      </tbody>
    </table>
  </div>
</body>
</html>
"""
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)


# --------------------------------------------------------------------------
# Main loop -- fetches live instead of reading downloaded CSVs
# --------------------------------------------------------------------------
def run_cycle(symbol, index_name, band, retries):
    open_price = fetch_index_open(index_name, retries=retries)
    raw_rows = fetch_option_chain_raw(symbol, retries=retries)
    rows = compute_rows(raw_rows, open_price, band)
    return open_price, rows


def run_once(args, browser_opened):
    """One fetch + compute + write cycle. On failure, prints the error and
    does NOT touch option_score.html or today's log -- exactly like
    nifty_breadth_tracker.py's run_once(): leaving prior output untouched
    means the CI workflow's `git diff --quiet` check finds nothing changed
    and simply skips committing, so the last known-good dashboard stays
    live instead of being overwritten with a blank/stale page.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_time = datetime.now().strftime("%H:%M:%S")

    try:
        open_price, rows = run_cycle(args.symbol, args.index_name, args.band, args.retries)
    except Exception as exc:  # noqa: BLE001
        print(f"[{log_time}] [ERROR] Live NSE fetch failed: {exc} -- will retry next cycle.")
        return

    grid, log_path = update_score_grid(rows, datetime.now())
    render_html(
        grid, open_price, args.band, args.symbol, timestamp, args.interval,
        source_note=f"Live NSE fetch, checked {timestamp}",
        log_path=log_path,
    )
    print(f"[{log_time}] OK - open={open_price:.2f} strikes={len(rows)} -> {log_path}")

    if not browser_opened[0]:
        try:
            webbrowser.open(f"file://{HTML_PATH}")
        except Exception:  # noqa: BLE001
            pass
        browser_opened[0] = True


def main():
    parser = argparse.ArgumentParser(description="Nifty Option Score tracker (live NSE fetch version)")
    parser.add_argument("--symbol", default="NIFTY", help="Option-chain symbol (default NIFTY)")
    parser.add_argument("--index-name", default="NIFTY 50",
                         help="Row label to match for the open price (default 'NIFTY 50')")
    parser.add_argument("--band", type=float, default=1000, help="+/- points around open (default 1000)")
    parser.add_argument("--interval", type=int, default=600, help="Refresh interval in seconds (default 600 = 10 min)")
    parser.add_argument("--retries", type=int, default=3, help="Fetch attempts per cycle before giving up (default 3)")
    parser.add_argument("--once", action="store_true",
                         help="Run a single fetch/update cycle and exit -- used by GitHub Actions CI, "
                              "where the scheduler (not this script) handles the 'every 10 minutes' timing. "
                              "Omit this flag for normal local PC use, which loops forever like before.")
    args = parser.parse_args()

    if args.once:
        # CI mode: one cycle, no loop, no browser -- GitHub Actions' cron
        # calls this fresh every 10 minutes, so there's nothing to sleep for.
        run_once(args, browser_opened=[True])
        return

    print("Nifty Option Score tracker (live NSE fetch version)")
    print(f"  Symbol: {args.symbol}   Index: {args.index_name}   Band: +/-{args.band}")
    print(f"  Refresh interval: {args.interval}s   Fetch retries/cycle: {args.retries}")
    print(f"  Output HTML: {HTML_PATH}")
    print(f"  Daily Strike x Time log: {LOG_DIR}{os.sep}option_score_<date>.csv")
    print("  No manual downloads needed -- fetching directly from nseindia.com each cycle.")
    print("  Press Ctrl+C to stop.\n")

    browser_opened = [False]
    while True:
        cycle_start = time.monotonic()
        try:
            run_once(args, browser_opened)
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] Unexpected failure in update cycle: {exc}")

        elapsed = time.monotonic() - cycle_start
        remaining = args.interval - elapsed
        if remaining > 0:
            time.sleep(remaining)
        else:
            print(f"[WARN] Cycle took {elapsed:.1f}s, longer than the "
                  f"{args.interval}s refresh interval -- running next cycle immediately.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
