# BMW M2 Car Monitor

Scrapes AutoTrader UK, PistonHeads, and BMW UK Approved Used every 2 hours.
Publishes a live dashboard webpage — no email, no local install needed.

---

## How it works

1. GitHub Actions runs the scraper on a schedule (every 2 hours, free)
2. It commits the updated `dashboard.html` back to your repo
3. GitHub Pages serves it as a public URL you can bookmark

---

## One-time setup (~10 minutes)

### Step 1 — Create a free GitHub account

Go to [github.com](https://github.com) and sign up if you don't already have one.

### Step 2 — Create a new repository

1. Click the **+** icon (top right) → **New repository**
2. Name it something like `bmw-m2-monitor`
3. Set it to **Public** (required for free GitHub Pages)
4. Leave everything else as default → click **Create repository**

### Step 3 — Upload the files

1. On your new repo page, click **uploading an existing file**
2. Drag and drop ALL the files from this folder (including the hidden `.github` folder — you may need to enable "Show hidden items" in Windows Explorer)
3. Click **Commit changes**

> **Tip for the `.github` folder:** In Windows Explorer, go to View → Show → Hidden items, then drag the `.github` folder along with the rest.

### Step 4 — Enable GitHub Pages

1. Go to your repo → **Settings** → **Pages** (left sidebar)
2. Under **Source**, select **Deploy from a branch**
3. Branch: **main**, Folder: **/ (root)** → click **Save**
4. Wait ~1 minute, then your dashboard will be live at:
   ```
   https://YOUR-USERNAME.github.io/bmw-m2-monitor/dashboard.html
   ```

### Step 5 — Test it manually

1. Go to your repo → **Actions** tab
2. Click **Car Monitor** in the left sidebar
3. Click **Run workflow** → **Run workflow** (green button)
4. Wait ~60 seconds for it to complete
5. Open your Pages URL — you should see listings

> The first run will show everything as "NEW". On subsequent runs only genuinely new listings get the badge.

---

## Adjusting filters

Edit `config.json` directly on GitHub (click the file → pencil icon):

```json
{
  "filters": {
    "make": "BMW",
    "model": "M2",
    "min_year": 2023,
    "max_year": 2026,
    "max_price": 55000,
    "max_mileage": 20000,
    "postcode": "GU15 2BA",
    "radius_miles": 1000
  },
  "sites": {
    "autotrader": true,
    "pistonheads": true,
    "bmw_uk": true
  }
}
```

Commit the change and the next run will use the new filters.

---

## Changing the schedule

Edit `.github/workflows/car-monitor.yml`. The default runs every 2 hours:

```yaml
- cron: '0 */2 * * *'   # every 2 hours
```

Other examples:
```
0 * * * *      every hour
0 8,12,18 * * *   at 8am, noon, 6pm daily
```

Times are UTC. GitHub Actions may run up to 15 minutes late during busy periods.

---

## Files

| File | Purpose |
|------|---------|
| `car_monitor.py` | Main scraper script |
| `config.json` | Search filters and site toggles |
| `seen_listings.json` | Auto-maintained list of seen listing IDs (committed by the bot) |
| `dashboard.html` | Generated dashboard — this is what you view in your browser |
| `requirements.txt` | Python dependencies (installed automatically by Actions) |
| `.github/workflows/car-monitor.yml` | GitHub Actions schedule and steps |

---

## Notes

- **BMW UK** scraper is best-effort — their site is a JavaScript SPA and the API endpoint may change. Check the Actions run log if it returns 0 BMW results.
- The script pauses 1–2 seconds between sites to avoid rate limiting.
- To reset and re-receive all listings as NEW, delete `seen_listings.json` on GitHub (or clear its contents to `{}`).
