# 📊 WO Tracking Tool

Live Streamlit dashboard for tracking Storage and PO Work Orders across Pattern's UK/EU warehouses (Northampton + Wroclaw).

Pulls all year-to-date WOIs from Snowflake, classifies blocked / partial / on-track items, and presents two views: WO-level summary and item-level drill-down.

## What it does

- **Storage WOs tab**: blocked detection via the PFS table (Listing Failed, Replen Needed, etc.)
- **PO WOs tab**: block flag based on later of WO ship-by and PO requested-ship date
  - 🔴 Blocked / Issue — 21+ days past ship-by, 0% processed
  - 🟠 Partially Processed — 14+ days past ship-by, partial progress
  - 🟡 Approaching ship-by — 0–13 days past
  - 🟢 On Track — before ship-by
  - ✅ Complete — fully processed

Data refreshes every 30 minutes automatically. Manual refresh button in the sidebar.

## Setup (local)

### 1. Clone and install

```bash
git clone https://github.com/ashwinkaushik-web/wo-tracking-tool.git
cd wo-tracking-tool

python -m venv .venv
source .venv/bin/activate         # Mac/Linux
.venv\Scripts\activate            # Windows

pip install -r requirements.txt
```

### 2. Configure secrets

Copy the template:

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

Edit `.streamlit/secrets.toml` and replace `PASTE_REAL_PASSWORD_HERE` with the actual service account password.

> ⚠️ Never commit `secrets.toml` — `.gitignore` excludes it.

### 3. Run

```bash
streamlit run app.py
```

Opens at `http://localhost:8501`.

## Deploy to Streamlit Community Cloud

1. Push this repo to GitHub (public, secrets stay out via `.gitignore`)
2. Sign in to [share.streamlit.io](https://share.streamlit.io) with your GitHub account
3. Click **New app** → select `ashwinkaushik-web/wo-tracking-tool`, branch `main`, main file `app.py`
4. Click **Advanced settings** → **Secrets** → paste the contents of your local `secrets.toml`
5. Click **Deploy**

App will be live at `https://ashwinkaushik-web-wo-tracking-tool.streamlit.app` (or similar).

## File structure

```
wo-tracking-tool/
├── app.py                          # Main Streamlit app
├── requirements.txt                # Python dependencies
├── queries/
│   └── wo_tracker.sql              # Production Snowflake query
├── .streamlit/
│   ├── config.toml                 # Theme & app settings
│   ├── secrets.toml                # Real credentials (gitignored)
│   └── secrets.toml.example        # Template
├── .gitignore
└── README.md
```

## How the data flow works

```
Streamlit (page load / refresh)
   ↓
@st.cache_data (30 min TTL)
   ↓ cache miss?
queries/wo_tracker.sql → Snowflake (PREDICT_BG_WH_LARGE)
   ↓
pandas DataFrame (~33K WOIs YTD)
   ↓
WO-level aggregation in memory
   ↓
Render Storage + PO tabs
```

A typical refresh runs the Snowflake query in ~1–2 seconds. Cached results are served instantly to all users hitting the app within the 30-min TTL.

## Editing the query

The full production SQL lives in `queries/wo_tracker.sql`. To change thresholds (e.g., switch the partial-block window from 14 to 10 days), edit the `po_block_flag` `CASE` statement and redeploy. Streamlit Cloud auto-redeploys on every push to `main`.

## Maintenance notes

- **Service account password rotation**: update via Streamlit Cloud secrets manager. No code changes needed.
- **Adding a new warehouse**: edit the `WHERE wh.id IN (...)` clause at the bottom of `queries/wo_tracker.sql`.
- **Adjusting block thresholds**: edit `po_block_flag` CASE in `queries/wo_tracker.sql`.
- **Cache TTL**: change `CACHE_TTL_SECONDS` constant at the top of `app.py`.

## Troubleshooting

| Issue | Fix |
|---|---|
| `KeyError: 'snowflake'` on startup | `.streamlit/secrets.toml` missing or malformed — check the `[snowflake]` section exists |
| `Authentication failed` | Service account password expired; rotate and update secrets |
| `OPERATION_NOT_PERMITTED` | Role `DATA_FANATICS_READ_ROLE` doesn't have access — check with data team |
| `Object 'X' does not exist` | One of the source tables was renamed — check query against current Snowflake schema |
| Loading is slow | First load runs the query (~1–2s). After that it's cached for 30 min. Click Refresh in sidebar to force-rerun. |

## Background

Built to replace manual CSV exports + Tableau snapshots that were already 8 days stale by the time the team reviewed them. Verified against Tableau as of 17 June 2026:
- 247/247 Storage WOI quantities matched
- 52/52 PO WOI quantities matched
- 32,959 total YTD WOIs tracked

Block detection rules verified with Owen Davies (UK/EU Merchandise Planning) and ground-truthed against Snowflake `in_progress_at` audit data — the timestamp field is unreliable, so we use WOI-level processing percentages instead.
