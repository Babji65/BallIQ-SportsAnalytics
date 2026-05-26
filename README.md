# balliq — Player Props Research Tool

Free NFL & NCAAF player props research. Powered by ESPN's unofficial API and Open-Meteo weather. No paid APIs needed.

---

## How it works

```
GitHub Actions (every 6 hours)
  └─ runs fetch_data.py
       └─ pulls from ESPN API (stats, injuries, rosters, schedules)
       └─ pulls from Open-Meteo (weather for outdoor stadiums)
       └─ writes data.json
            └─ committed back to repo
                 └─ index.html reads data.json on page load
```

---

## Repo structure

```
balliq/
├── index.html          ← the frontend (goes on GitHub Pages)
├── fetch_data.py       ← Python data fetcher
├── requirements.txt    ← just "requests"
├── data.json           ← auto-generated, do not edit by hand
└── .github/
    └── workflows/
        └── fetch.yml   ← GitHub Actions schedule
```

---



## Using the site

1. **Pick a game** from the top grid
2. **Filter by position** (QB / WR / RB / TE) on the left
3. **Click a player** to open their prop cards
4. **Enter the odds** you see on your book (e.g. `-115`, `+105`) into the Over/Under boxes
   - The tool will calculate **Expected Value** for you automatically
5. **Click the line** (e.g. `285.5`) to adjust it if your book has a different number
6. **Click OVER or UNDER** to mark your pick

### Reading the cards

| Indicator | Meaning |
|-----------|---------|
| Green bars in trend chart | Player hit the over in that game |
| Hit rate % | How often they've cleared this line in last 10 games |
| Matchup dots | 1–5 rating vs opponent's defense (green = favorable) |
| Model edge % | How far above/below average vs the line |
| Expected value | Your actual $ edge per $100 wagered (needs odds entered) |
| 🔥 VALUE BET | Edge > 4% AND hit rate ≥ 60% |

---

## Adjusting the schedule

Edit `.github/workflows/fetch.yml` to change how often data refreshes.

```yaml
# Every 6 hours (default)
- cron: '0 */6 * * *'

# Every 2 hours (game days)
- cron: '0 */2 * * *'

# Every hour
- cron: '0 * * * *'
```

Note: GitHub Actions free tier gives you 2,000 minutes/month. Running every 6 hours = ~120 runs/month = well within free limits. Every hour = ~720 runs/month, still free.

---

## Customising default prop lines

In `fetch_data.py`, find the `DEFAULT_PROPS` dict and adjust the lines:

```python
DEFAULT_PROPS = {
    "QB": [
        ("pass_yds", "Passing Yards",  250.5),   ← change this number
        ("pass_td",  "Passing TDs",      1.5),
        ...
    ],
    ...
}
```

You can also always tap the line number on any prop card in the UI to override it per player.

---

## Data sources

| Data | Source | Cost |
|------|--------|------|
| Schedules, rosters, scores | ESPN unofficial API | Free |
| Player game logs (last 10) | ESPN unofficial API | Free |
| Injury reports | ESPN unofficial API | Free |
| Game weather | Open-Meteo | Free forever |

---

## Troubleshooting

**Site shows "Could not load data.json"**
→ Run the GitHub Action manually first (step 4 above). `data.json` needs to exist in the repo before the site can read it.

**GitHub Action fails with permission error**
→ Check you enabled Read and write permissions in Settings → Actions → General.

**Players showing no props**
→ ESPN game logs may not be available for very new players or early in the preseason. This resolves as the season progresses.

**data.json isn't updating**
→ Check the Actions tab for errors. Most common cause: ESPN rate limiting if you run too frequently. The 0.4s delay in `fetch_data.py` handles this but if you fork and remove it, you may get blocked.
