"""
CI entry point for GitHub Actions.

The main script's main() runs an infinite loop with time.sleep(300) - that's
fine for a PC left running, but wrong for GitHub Actions, where each workflow
run is a fresh, short-lived container. Instead, GitHub Actions itself does the
"every 5 minutes" scheduling (via cron), and each run just needs to:
  1. Fetch live data once
  2. Update the dashboard HTML + append to today's H1_avg CSV
  3. Exit

That's exactly what run_once() does - we just call it a single time here,
skipping the local browser-open attempt (nothing to open on a CI runner).
"""
from nifty_breadth_tracker import run_once

if __name__ == "__main__":
    run_once(browser_opened=[True])  # [True] = never try to open a local browser
