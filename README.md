# dallas-intel

Daily property-intelligence pipeline for Dallas County, Texas. Scrapes the County Clerk's Official Public Records, foreclosure notices, and DCAD parcel data; enriches and scores records to surface motivated-seller signals; outputs a static dashboard and CSV exports.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design rationale, legal posture, instrument-code mapping, schema, risk register, and decision ledger.

## What it does

1. Scrapes 12 instrument-code categories from `dallas.tx.publicsearch.us` (County Clerk Official Records)
2. Walks `dallascounty.org` foreclosure-notice PDFs as a fresh-data cross-check
3. Pulls DCAD bulk parcel/owner/value data directly from `dallascad.org/DataProducts.aspx` (cached weekly)
4. Joins County Clerk records to DCAD parcels by normalized address
5. Scores each record using a heuristic tuned for distressed-property signals
6. Outputs `data/records.json` (full archive) and `exports/ghl_export_YYYYMMDD.csv` (daily delta)
7. Notifies a Discord channel on run completion or failure
8. Serves a static dashboard via GitHub Pages

Runs daily at 07:00 UTC ±30 min jitter via GitHub Actions.

## Repository layout

```
.
├── scraper/                # Python pipeline (see scraper/README within)
│   ├── config.py           # all tunable constants
│   ├── normalize.py        # address normalization, instrument codes, HOA detection
│   ├── dcad_bulk.py        # DCAD ZIP fetcher (direct download)
│   ├── foreclosure_pdfs.py # dallascounty.org PDF walker
│   ├── publicsearch.py     # publicsearch.us SPA scraper (Playwright)
│   ├── enrichment.py       # join Clerk records to DCAD parcels
│   ├── scorer.py           # heuristic scoring + HOA filter
│   ├── output.py           # records.json + CSV writers
│   ├── monitor.py          # Discord webhook notifier
│   ├── main.py             # daily pipeline entrypoint
│   └── requirements.txt
├── dashboard/              # Static dashboard (GitHub Pages root)
├── data/                   # records.json — committed daily by bot
├── exports/                # Daily CSV exports — committed daily by bot
├── docs/                   # Design docs (ARCHITECTURE.md is canonical)
├── tests/                  # pytest regression suite
└── .github/workflows/      # CI cron
```

## Environment variables

Set locally via `.env` (gitignored) or in production via GitHub Actions secrets.

| Variable | Purpose | Required |
|---|---|---|
| `CONTACT_EMAIL` | Surfaced in the polite-crawler User-Agent | Yes |
| `DISCORD_WEBHOOK_URL` | Destination for run notifications | Yes |
| `DCAD_TARGET_YEAR` | DCAD bulk-data year (default `2026`) | No |
| `DAYS_BACK_OVERRIDE` | Override default `DAYS_BACK=7` for ad-hoc runs | No |
| `DCAD_CACHE_DIR` | Override local cache path (default `~/.dcad_cache`) | No |

## Local setup (Windows / PowerShell)

```powershell
# 1. Clone
git clone git@github.com:16171290/dallas-intel.git
cd dallas-intel

# 2. Python env
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r scraper\requirements.txt

# 3. Playwright browsers (one-time, ~250 MB)
playwright install chromium

# 4. Configure
Copy-Item .env.example .env   # then edit in VS Code / Notepad
# Or set just for this session:
$env:CONTACT_EMAIL       = "your-email@example.com"
$env:DISCORD_WEBHOOK_URL = "..."

# 5. Run once
python -m scraper.main

# 6. Run tests
pytest tests\
```

> **First-time PowerShell users:** If `Activate.ps1` is blocked with a security error, run this once (per user, not per session) to allow signed local scripts:
>
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
> ```

### Local setup (macOS / Linux — for reference)

```bash
git clone git@github.com:16171290/dallas-intel.git
cd dallas-intel
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r scraper/requirements.txt
playwright install chromium
cp .env.example .env  # then edit
python -m scraper.main
pytest tests/
```

## Continuous run

The workflow at `.github/workflows/scrape.yml` runs the pipeline daily and commits new records. Two secrets must be set in repo Settings → Secrets and variables → Actions:

- `CONTACT_EMAIL`
- `DISCORD_WEBHOOK_URL`

## Legal posture

This pipeline operates on public records only. The full legal analysis is in [`docs/ARCHITECTURE.md` §B](docs/ARCHITECTURE.md#section-b--constraints--authorization).

Key points:

- All scraped data is public record under Texas Government Code Ch. 552 (Public Information Act)
- DCAD's bulk-data download is explicitly free and sanctioned per its own Open Records page
- Dallas County's main site is robots-permissive
- `dallas.tx.publicsearch.us` (third-party Neumo platform) has a restrictive robots.txt; the project's posture on that is documented and risk-acknowledged in §F.1

This is not legal advice; consult a Texas attorney before commercial use of derived data.

## License

TBD (§3.8.2.a deferred).

## Reference

Architecturally modeled on `github.com/xcerebroai/harris-intel` (Harris County equivalent). Bug fixes against that reference are documented in §G of `ARCHITECTURE.md`.
