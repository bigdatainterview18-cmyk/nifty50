#!/usr/bin/env python3
"""
Combined CI entry point -- runs both trackers in one process, then stacks
their two dashboards into a single page.

IMPORTANT: this file does NOT change either tracker's logic. It imports
nifty_breadth_tracker.py and nifty_option_score.py exactly as they already
are, and calls each one's existing run_once() the same way run_ci.py and
`nifty_option_score.py --once` already call them:
    - breadth.run_once(browser_opened=[True])   (same call run_ci.py makes)
    - option_score.run_once(args, browser_opened=[True])  (same call --once makes)

Why not literally paste both scripts into one file instead? Because they
both define several same-named things at the top level -- NSE_BASE,
HEADERS, _ci_get, _safe_float, run_once, main, and more. Concatenating the
files would make the second script's definitions silently overwrite the
first's, changing behaviour without anyone touching either script's actual
logic on purpose. Importing them as two separate modules keeps each one's
names in its own namespace (breadth.NSE_BASE vs option_score.NSE_BASE),
so nothing collides and nothing is altered -- while still giving you the
one thing you actually wanted: both fetches happening back-to-back in the
same run, so their data is from the same moment, plus one combined page.

What this file adds on top (new, not a change to either tracker):
    1. Run breadth's cycle, then option score's cycle, in the same process.
    2. Stack their two already-written HTML files into one wrapper page,
       dashboard.html, via iframes -- breadth on top, option score below.
       Using iframes (rather than merging the HTML/CSS by hand) means each
       dashboard keeps rendering exactly as it always has; there's no
       chance of their CSS classes or scripts colliding with each other.

CI use: the workflow calls `python run_combined.py` once per scheduled
cycle -- GitHub Actions' own cron handles the "every 5 minutes" timing,
same pattern as the two individual trackers already used.
"""
import argparse
import os

import nifty_breadth_tracker as breadth
import nifty_option_score as option_score

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COMBINED_HTML = os.path.join(SCRIPT_DIR, "dashboard.html")
REFRESH_SECONDS = 300  # 5 minutes -- matches the workflow's cron cadence


def build_combined_page():
    """Wrap the two already-written dashboard files into one page: breadth
    on top, option score on the bottom, each in its own iframe so neither
    dashboard's CSS/JS can affect the other."""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>NIFTY Dashboards</title>
<style>
  html, body {{ margin:0; padding:0; height:100%; background:#0f172a; }}
  .panel {{ width:100%; height:50vh; border:0; display:block; }}
  #option-score-frame {{ border-top:4px solid #1e293b; }}
</style>
</head>
<body>
  <iframe id="breadth-frame" class="panel" src="nifty_breadth_dashboard.html" title="NIFTY Breadth Dashboard"></iframe>
  <iframe id="option-score-frame" class="panel" src="option_score.html" title="NIFTY Option Score Dashboard"></iframe>
</body>
</html>
"""
    with open(COMBINED_HTML, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    # 1) Breadth tracker -- unchanged call, identical to what run_ci.py does.
    breadth.run_once(browser_opened=[True])

    # 2) Option score tracker -- unchanged call, identical to what
    #    `python nifty_option_score.py --once` does.
    args = argparse.Namespace(
        symbol="NIFTY",
        index_name="NIFTY 50",
        band=1000,
        interval=REFRESH_SECONDS,
        retries=3,
    )
    option_score.run_once(args, browser_opened=[True])

    # 3) Stack both dashboards' output into one page. This only wraps the
    #    two files that were just written above -- it doesn't parse or
    #    alter their content.
    build_combined_page()
    print("Combined dashboard written to", COMBINED_HTML)


if __name__ == "__main__":
    main()
