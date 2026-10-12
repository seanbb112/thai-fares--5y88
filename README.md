# Thailand flight tracker

One-way, 1 adult, economy, Ireland → Thailand, leaving 27/12/26 or 28/12/26, direct or one stop.
Target: around €500. Personal project, not deal material.

## What it searches (every hour)

- **Through-tickets:** Dublin, Cork, Shannon → Bangkok (BKK, DMK), Phuket, Chiang Mai, Krabi, max one stop.
  Dublin on 26/12 as well, for comparison with the earlier `asia-trip` plan date.
- **Stopovers (two separate bookings):** direct Dublin → hub, then direct hub → Bangkok or Phuket,
  same day (3h+ gap) up to 4 nights later. Hubs are listed in `tracker/config.json`.

Prices come from Google Flights through the `fast-flights` library. There is no official
Google Flights API, so if Google changes its page the tracker can break. The dashboard says
when the last update failed or is stale.

## Where it runs

GitHub Actions runs `tracker/tracker.py` hourly (`.github/workflows/track.yml`), commits
`data/` and `dashboard/data.js`, and publishes `dashboard/` to GitHub Pages. The page opens on
any phone. To update immediately: GitHub → Actions → Hourly flight sweep → Run workflow.

Local run: `python tracker/tracker.py`, then open `dashboard/index.html`.
`tracker/schedule-hourly.ps1` is a fallback that runs it hourly on this PC instead.

## Files

| Path | What |
|---|---|
| `tracker/config.json` | Dates, airports, hubs, target. Edit to change the search |
| `tracker/tracker.py` | The sweep |
| `dashboard/index.html` | The dashboard; reads `dashboard/data.js` |
| `data/latest.json` | Every itinerary from the last good run |
| `data/history.csv` | Cheapest price per route, every run |
| `data/run.log` | One line per run, plus errors |
| `outputs/` | Research write-up (not published to GitHub) |
