# WO Tracking Tool — Handover / Onboarding

> Read this first if you're picking the project up in Cursor (or any editor).
> It's the single source of truth for what the tool is, how it's built, how it
> deploys, its data sources, and what's left to do. No secrets are in this file —
> real tokens/keys/IDs live in Streamlit Cloud secrets and GitHub Actions secrets.

---

## 1. What it is

A **Streamlit** dashboard that gives UK/EU merchandise planners one live view of a
purchase order's journey — placement → work-order (WO) creation → warehouse
processing — for **Northampton (wh 138)** and **Wroclaw (wh 146)**, so no-WO / blocked
gaps get caught early. Data is read **live from Snowflake** (cached ~30 min).

- **Repo:** `ashwinkaushik-web/wo-tracking-tool` (GitHub, **public**)
- **Live app:** Streamlit Community Cloud — `https://wo-tracking-tool.streamlit.app`
- **Owner:** Ashwin Kaushik (ashwin.kaushik@pattern.com)
- **Language/stack:** Python 3.12, Streamlit 1.59.2, snowflake-connector-python, pandas, plotly

---

## 2. Deployment model & the #1 gotcha

- Streamlit Community Cloud **auto-deploys from `main`** on every commit.
- **⚠️ Auto-redeploy is flaky.** After merging a PR, the app often keeps serving the
  **old build** until you **manually reboot**: *Manage app → ⋮ → Reboot app* → wait ~60 s →
  hard-refresh (Ctrl+F5). If a change "isn't showing," this is almost always why —
  it's not a missing push. Verify what's on `main` with git before assuming a bug.
- **GitHub API token used by tooling is read-only** (can't open PRs/branches). Pushes
  are done with **local git** (`git push`); PRs are opened/merged in the **GitHub web UI**.
- **Working convention:** branch → push → open PR in web UI → merge → **reboot** → verify.
  Keep the diff clean (LF line endings; `app.py` is large — prefer surgical edits).

---

## 3. Local dev setup (for Cursor)

```bash
git clone https://github.com/ashwinkaushik-web/wo-tracking-tool.git
cd wo-tracking-tool
python -m venv .venv && . .venv/Scripts/activate   # Windows; use bin/activate on mac/linux
pip install -r requirements.txt
# create .streamlit/secrets.toml from the example (gitignored — never commit real secrets)
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then fill in real values
streamlit run app.py
```

`requirements.txt` is deliberately pinned (`streamlit==1.59.2`, `starlette<1.4`,
pandas/numpy/pyarrow ranges) — see §9 for why. Don't loosen these casually.

---

## 4. File map

```
app.py                       Main Streamlit app (~2.2k lines). Nav, Snowflake fetch,
                             all tabs/panels, table renderer, drill-downs.
flag_guide.py                Curated flag → meaning/root-cause/action reference
                             (Information Station-grounded). render_flag_guide,
                             render_flag_guide_inline, flag_action.
slack_messaging/             In-app Slack messenger feature package
  __init__.py                messenger dialog, multi-channel + DM, file upload,
                             per-table / per-row / per-panel senders.
  SETUP.md                   How to create the Slack app + secrets.
queries/
  wo_tracker.sql             MAIN WO-items feed (base of the app).
  po_tracker.sql             PO Details source (report table).
  po_wo_agg.sql              Per-PO rollup of linked WOs (no-WO detection).
  po_item_wo_gap.sql         Item-level (PO × Master ID) WO coverage gaps.
  wo_unpickable_detail.sql   Per-WOI active block reasons + resolvable qty (root-cause).
scripts/
  post_no_wo_to_slack.py     Standalone no-WO Slack auto-post (runs in GitHub Actions).
.github/workflows/
  no-wo-slack.yml            Cron (Mon & Wed) + manual dispatch for the auto-post.
.streamlit/
  secrets.toml.example       Secrets template (Snowflake + Slack). Real file is gitignored.
  config.toml                Streamlit config.
SLACK_AUTOPOST_SETUP.md      GitHub-secrets + test steps for the scheduled post.
README.md                    User-facing overview.
```

---

## 5. Data sources (Snowflake)

Account/role/warehouse (non-secret) are in `secrets.toml.example`:
`account WEDSQVX-PATTERN`, `role DATA_FANATICS_READ_ROLE`, `warehouse PREDICT_BG_WH_LARGE`,
user `SVC_PC_LOOKUP_TOOL__PROD__USER` (**key-pair auth**; private key is a secret).

### WO feed — `queries/wo_tracker.sql`
Base: `ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDER_ITEMS` (woi), joined to:
- `STG_AMACZAR__WORK_ORDERS` (wo), `__WORK_ORDER_ITEM_TYPES`, `__WORK_ORDER_ITEM_RESULTS`
- **Block status (PFS):** `PATTERN_DB.OPERATIONS.PICK_FROM_STOW_WORK_ORDER_ITEMS`
  (drives `processing_status` / block reasons; different schema — note `PATTERN_DB`).
- Dims: `__LISTINGS`, `__ITEMS`, `__PARTNERS`, `__CATALOG_MARKETPLACES`, `__WAREHOUSES`,
  `__USERS`, `__INVENTORY_LOCATIONS`, `__TRANSIENT_BOXES`, `__INVENTORY_REQUESTS`, `__PURCHASES`.

### PO Details — `queries/po_tracker.sql`
Base: `ANALYTICS_DB.REPORTING.REPORT__BRAND_MANAGEMENT_V7__PURCHASE_ORDERS` (transient,
daily-rebuilt; one row per PO × line item). Scope: Northampton + Wroclaw, placed ≥ 2025-07-01.

### PO→WO linkage (used everywhere)
WOs link to POs via `WORK_ORDERS.RECEIVABLE_TYPE='Purchase'` / `RECEIVABLE_ID = PURCHASES.ID`
— **NOT** `PURCHASE_ID` (which is NULL in practice). WOIs have no PO-item FK; they resolve
to `master_id` via `LISTINGS` (workable_type='Listing') → `ITEMS.master_id`, with a
`COALESCE(l.item_id, woi.workable_id)` fallback that also handles item-workable (FBM) WOIs.

### Root-cause detail — `queries/wo_unpickable_detail.sql`
`STG_AMACZAR__WORK_ORDER_ITEM_UNPICKABLE_REASONS` (join `work_order_item_id`) +
`STG_AMACZAR__UNPICKABLE_REASONS` (id→name). **Always `WHERE deleted_at IS NULL`** —
only ~1,670 of ~233K rows are active (the worker soft-deletes/re-emits each run).
`RESOLVABLE_QUANTITY` is **blank for reason 1 (No Available Inventory) and 4 (Inventory Expired)** —
blank ≠ "0 units."

### Column gotchas
- Report "Original Ordered" = `ORDERED_UNITS`; "Ordered/Current" = `CURRENT_UNITS`. Easy to swap.
- Fill rates are intentionally **uncapped** (>100% = real over-receipt, not a bug).

---

## 6. Features (all live on `main`)

**Core views:** Overview (KPIs + Needs-Attention panels A–G) · SKU Journey · POs
(PO Details / PO WOs) · Manual/Storage WOs. Single-page `st.radio` nav (not `st.tabs` —
a deliberate memory fix). Native `st.dataframe` tables via `render_table()`.

**Slack messenger** (`slack_messaging/`):
- Header **💬 Slack** button (every tab) → dialog: pick sender (people dropdown),
  **multi-select recipients** (channels + DMs), message, **file attachment**.
- **📤 Send to Slack** under **every table** (whole table or a single line, as CSV) via
  `slack_table_sender` wired into `render_table`. Panel-level one-click senders on the
  no-WO / item-gap panels.
- Uses stdlib `urllib` only. Slack Web API: `chat.postMessage`, `conversations.open`,
  `files.getUploadURLExternal` + `completeUploadExternal`.

**Flag guide** (`flag_guide.py`) — Information-Station-grounded:
- Overview **ℹ️ Flag guide** reference (all reasons) + **inline** "what these mean"
  expanders inside Blocked / no-WO / item-gap / Storage panels + a per-row **What to do**
  column. Covers all 8 PFS block reasons (Listing Failed, Listing Not Shippable, Replen
  Needed, No Inventory, Inventory Expired, Pending Sellable, Missing Exp Date, Missing Lot)
  plus the PO/WO coverage reasons.

**Root-cause drill-down:** Open a WO (Storage or PO WOs) → **🔎 Root-cause detail**:
per-item Marketplace, **Active Reasons** (exact stacked cause), **Resolvable Qty**, raw
block status, pick type, listing/master IDs, and What to do. Backed by
`wo_unpickable_detail.sql` (validated to return correct rows).

**Clickable PO links:** every `PO #` links to Shelf
`https://www.useshelf.com/order-management/po/preview/details/<PO#>`. Rendered on a
throwaway copy so Ctrl+C / CSV exports keep the plain number. *(No per-WO deep link
exists in Shelf — the work-orders view has a static URL, so WO links were dropped.)*

**Scheduled Slack auto-post** (`scripts/` + workflow) — see §8. Code is live; needs
GitHub secrets + a test run to switch on.

---

## 7. Config / secrets (shapes only — real values live in Streamlit/GitHub)

Streamlit Cloud → app → **Settings → Secrets** (also `.streamlit/secrets.toml` locally):

```toml
[snowflake]
user = "SVC_PC_LOOKUP_TOOL__PROD__USER"
account = "WEDSQVX-PATTERN"
role = "DATA_FANATICS_READ_ROLE"
warehouse = "PREDICT_BG_WH_LARGE"
database = "ANALYTICS_DB"
schema = "STG_AMACZAR"
private_key = """-----BEGIN PRIVATE KEY----- ... -----END PRIVATE KEY-----"""   # SECRET

[slack]
bot_token = "xoxb-..."               # SECRET. Scopes: chat:write, im:write, files:write

[slack.channels]                     # label -> channel ID (bot must be /invited to each)
"#merchandise-planning-europe" = "C0..."
"#wo-trial"                    = "C0..."

[slack.people]                       # name -> Slack user ID (13 planners configured in prod)
"Ashwin Kaushik" = "U0..."
# ...
```

- The **live app's Slack config (channel IDs + 13 people's user IDs)** is already set in
  Streamlit Secrets. It is intentionally **not reproduced here** (public repo). Read it
  from the app's Secrets UI if you need the actual values.
- Adding a channel/person = one line in the relevant table + Save (+ `/invite` the bot for
  a channel). No code change, no redeploy needed.

---

## 8. Scheduled Slack auto-post (to switch on)

- `scripts/post_no_wo_to_slack.py` reproduces the Overview no-WO logic (2026-placed POs)
  and posts a fixed-width table to Slack. `.github/workflows/no-wo-slack.yml` runs it
  **Mon & Wed** (`cron: 0 6 * * 1,3`, ~06–07:00 UK) + manual dispatch.
- **Not yet activated.** To turn on (see `SLACK_AUTOPOST_SETUP.md`):
  1. Add GitHub repo secrets (Settings → Secrets → Actions): the Snowflake ones +
     `SNOWFLAKE_PRIVATE_KEY`, `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID` (= the
     #merchandise-planning-europe channel id).
  2. `/invite` the bot to that channel.
  3. Actions tab → "No-WO Slack post" → **Run workflow** to test immediately.

---

## 9. Known issues, accuracy notes, and past fixes

- **`po_item_wo_gap.sql` is verified accurate** (2026-09). A live Snowflake check found
  the item-level WO-coverage query uses the correct `STG_AMACZAR__LISTINGS` table and the
  `RECEIVABLE_TYPE='Purchase'` linkage, item-workable (FBM) WOIs resolve fully (0 dropped),
  there is **no bundle workable type** on the Purchase path, and only 17 of ~126M coverage
  units drop to a null master_id (~0.00001%). No fix needed; the "genuine gap" count is trustworthy.
- **Starlette pin:** `starlette<1.4` — 1.4.0 changed `GZipResponder.__init__` and crashed
  Streamlit 1.59.2 on boot. Don't unpin. pandas 3 / numpy 2.5 / pyarrow 25 segfault Arrow rendering.
- **Silent Overview fetch:** Overview swallows PO-fetch exceptions (`except: po_df=None`),
  so a real query error looks like "no data." Consider surfacing the error (a known nicety).
- **"Which eligibility check failed" is not available** at the unpickable-reasons table grain
  (lives only in upstream Postgres per Information Station) — root-cause shows the reason
  name + resolvable qty, not the specific failed check.
- **Deploy drift:** a class of bug where `app.py` expects a column a `queries/*.sql` doesn't
  return has bitten this project before — when you add a column to app.py's expectations,
  redeploy the SQL in the **same** commit.

---

## 10. Open items / next steps

- [ ] **Switch on the Mon/Wed auto-post** (§8) — GitHub secrets + a test run.
- [ ] (Optional) Footer **commit-stamp** in the app so "did my change deploy?" is a glance,
      not a guess — recommended given the flaky auto-redeploy.
- [ ] (Optional) Surface the Overview silent-exception error message.
- [ ] (Optional) Extend the auto-post to include item-level gaps, or @-mention owners.

---

## 11. Information Station (context source)

The flag guide's root-cause content is distilled from Pattern's **Information Station**
(private repo `patterninc/information-station`, cloned locally at `~/Desktop/information-station`).
Key docs used: `work_orders.md`, `unpickable_reasons.md`, `work_order_item_unpickable_reasons.md`,
`purchase_order_state_manager.md`, `po_wo_flow.md`, and the warehouse/ordering flow graphs.
It's baked into `flag_guide.py` (not fetched live — the app can't read a private repo from
Streamlit Cloud). Update `flag_guide.py` when those docs change.
