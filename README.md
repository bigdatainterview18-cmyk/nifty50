# Deploying the NIFTY Breadth Dashboard for free (GitHub Actions + GitHub Pages)

This makes the dashboard run fully unattended — GitHub fetches live data every
5 minutes, updates the page, and hosts it at a public link you can send to
anyone. No server, no manual refresh, no cost.

## 0. Test NSE access from GitHub's servers FIRST

This is the one thing that can break the whole plan: `nseindia.com` sometimes
blocks requests from datacenter IPs (which is what GitHub Actions runners
use), even though it works fine from your home/office PC.

Do steps 1-6 below, then go to your repo's **Actions** tab and manually run
the workflow once (`Run workflow` button — this is what `workflow_dispatch`
in the yaml is for). Check the logs:

- **It fetched data successfully** → great, you're done, skip to "Share the
  link" at the bottom.
- **It failed / got a 403 / empty response** → see "Fallback: self-hosted
  runner" at the bottom. Don't build further on this path until you know
  which case you're in.

## 1. Create the GitHub repo

```
git init
git add .
git commit -m "Initial commit: NIFTY breadth dashboard"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

(This folder is already laid out correctly — `docs/` holds the script and
will hold the live dashboard + CSVs; `.github/workflows/update.yml` is the
automation.)

## 2. Give the workflow permission to push

Repo → **Settings → Actions → General → Workflow permissions** → select
**"Read and write permissions"** → Save.

(Without this, the workflow can fetch data but can't commit the updated
dashboard back to the repo.)

## 3. Turn on GitHub Pages

Repo → **Settings → Pages** → under "Build and deployment", **Source: Deploy
from a branch** → **Branch: main**, folder **/docs** → Save.

GitHub will give you a URL like:
```
https://<your-username>.github.io/<your-repo>/nifty_breadth_dashboard.html
```
That's the link you send your friend.

## 4. Let it run

The workflow (`.github/workflows/update.yml`) is already scheduled to run
every 5 minutes, 08:30–16:30 IST, Monday–Friday. Nothing else to do — it
fetches data, rewrites `docs/nifty_breadth_dashboard.html`, appends to
today's file in `docs/H1_avg/`, and pushes both back to the repo. GitHub
Pages picks up the new commit automatically (usually within a minute or so).

The page itself already auto-refreshes every 5 minutes in the browser (the
`setTimeout`/meta-refresh already in the HTML), so your friend never has to
touch it either.

## Fallback: self-hosted runner (if NSE blocks GitHub's IPs)

If step 0 shows NSE is blocked, you can keep everything else (the workflow,
the Pages hosting, the "no manual intervention" part) and just run the
*fetch* step from your own PC instead of GitHub's cloud:

1. Repo → **Settings → Actions → Runners → New self-hosted runner** → follow
   the instructions to install it on your PC (it's a small background
   service).
2. In `.github/workflows/update.yml`, change:
   ```yaml
   runs-on: ubuntu-latest
   ```
   to:
   ```yaml
   runs-on: self-hosted
   ```
3. Install it as a Windows service (the setup script GitHub gives you has a
   `svc install` / `svc start` option) so it starts automatically on boot —
   no manual intervention needed, same as before.

Now GitHub still handles scheduling, committing, and Pages hosting for free —
only the actual NSE fetch happens from your PC's IP, which you already know
works.

## Notes

- GitHub disables a repo's scheduled workflows after 60 days with **zero**
  commits. Since this workflow commits on every successful run during market
  hours, that won't happen as long as it's working.
- Cron timing on GitHub is best-effort — a run can occasionally slip by a
  few minutes under GitHub-wide load. That's on top of the normal "up to 5
  min old" data lag discussed earlier, not a separate bug.
- `docs/H1_avg/*.csv` gets committed to the repo, so your daily H1%/AVG_H1%
  history is preserved permanently in git history too, not just on disk.
