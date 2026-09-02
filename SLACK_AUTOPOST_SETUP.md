# Scheduled Slack auto-post — Setup

A GitHub Action posts the PO-level **"POs placed/arriving with no work order"** list
to **#merchandise-planning-europe** on **Mondays and Wednesdays** (~06:00–07:00 UK).
It runs outside the app, so it reads config from **GitHub repo secrets** (never in code).

- Script: [`scripts/post_no_wo_to_slack.py`](scripts/post_no_wo_to_slack.py)
- Workflow: [`.github/workflows/no-wo-slack.yml`](.github/workflows/no-wo-slack.yml)

---

## 1. Add the GitHub repo secrets

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.
Add each of these (values are the same ones already in your Streamlit secrets):

| Secret name | Value |
|---|---|
| `SNOWFLAKE_USER` | `SVC_PC_LOOKUP_TOOL__PROD__USER` |
| `SNOWFLAKE_ACCOUNT` | `WEDSQVX-PATTERN` |
| `SNOWFLAKE_ROLE` | `DATA_FANATICS_READ_ROLE` |
| `SNOWFLAKE_WAREHOUSE` | `PREDICT_BG_WH_LARGE` |
| `SNOWFLAKE_DATABASE` | `ANALYTICS_DB` |
| `SNOWFLAKE_SCHEMA` | `STG_AMACZAR` |
| `SNOWFLAKE_PRIVATE_KEY` | the **full** `.p8` PEM text, incl. the BEGIN/END lines |
| `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` | only if your key is encrypted (else skip) |
| `SLACK_BOT_TOKEN` | `xoxb-…` (the WO Tracker Bot token) |
| `SLACK_CHANNEL_ID` | `C052JR5LBNZ`  (#merchandise-planning-europe) |

Notes:
- GitHub secrets are **encrypted** and safe even on a public repo — they are never
  printed in logs.
- The bot must be a **member** of the channel: `/invite @WO Tracker Bot` in
  #merchandise-planning-europe (needs `chat:write`).

## 2. Test it now (don't wait for Monday)

Repo → **Actions** tab → **"No-WO Slack post"** → **Run workflow** → **Run**.
Watch the run; it should print `Fetched N no-WO PO(s).` then `Posted to C052…`, and the
message appears in the channel. If it fails, the log shows the exact error.

## 3. Schedule

Defined in the workflow as `cron: "0 6 * * 1,3"` → **Mon & Wed, 06:00 UTC**
(≈ 07:00 UK in summer / 06:00 UK in winter). To change the time, edit the hour in the
cron; to change days, edit `1,3` (0=Sun … 6=Sat).

---

## Errors → fixes

- `not_in_channel` → invite the bot to #merchandise-planning-europe.
- `invalid_auth` → check `SLACK_BOT_TOKEN`.
- Snowflake auth errors → check the private-key secret was pasted **whole** (BEGIN/END
  lines included) and the user/account/role are correct.
- GitHub cron can lag a few minutes and is skipped if the default branch has no recent
  activity for 60+ days — not a concern for an active repo.
