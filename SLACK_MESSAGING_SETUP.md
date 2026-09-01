# In-App Slack Messenger — Setup

The WO Tracking Tool has a **💬 Slack** button in the top-right header. It opens a
small popup where a user picks who they are, chooses to post to the **team channel**
or **DM a person**, types a message, and sends it to Slack.

Nothing works until the secrets below are added. No token or ID lives in the code —
everything is read from `st.secrets`.

---

## 1. Create a Slack app + bot token

1. Go to <https://api.slack.com/apps> → **Create New App** → *From scratch*.
2. Name it (e.g. `WO Tracker Bot`) and pick the workspace.
3. **OAuth & Permissions** → *Scopes* → *Bot Token Scopes*, add:
   - `chat:write` — post messages
   - `im:write` — open/DM a person
   - `users:read` *(optional)* — nice-to-have for future name lookups
4. **Install to Workspace** → approve. Copy the **Bot User OAuth Token** (starts with `xoxb-`).
5. **Invite the bot to the target channel**: in Slack, open the channel and type
   `/invite @WO Tracker Bot`. A bot can only post to channels it's a member of.

## 2. Find the IDs you need

- **Channel ID** — open the channel in Slack → *channel name* → *About* → bottom shows
  an ID like `C0123456789`. (Or right-click the channel → *Copy link*; the last path
  segment is the ID.)
- **User IDs** (for the people dropdown / DMs) — click a person's profile →
  *⋮ (More)* → *Copy member ID* → `U0123456789`.

## 3. Add the secrets

In **Streamlit Community Cloud** → your app → *⋮* → **Settings → Secrets**, add:

```toml
[slack]
bot_token    = "xoxb-your-token-here"
channel_id   = "C0123456789"
channel_name = "#wo-tracker"          # display label only

[slack.people]
"Ashwin Kaushik" = "U0123456789"
"Owen Smith"     = "U0987654321"
# add one line per person who should appear in the "From" / "DM" dropdowns
```

For **local dev**, put the same block in `.streamlit/secrets.toml` (already gitignored).

## 4. Test

- Redeploy / rerun the app. Click **💬 Slack** in the header.
- If secrets are missing, the popup shows a "not configured yet" note instead of the form.
- Pick your name, choose **📢 Channel** or **📩 DM · <name>**, type a note, **Send to Slack**.
- Success/failure is shown inline in the popup. A failure quotes Slack's error
  (e.g. `not_in_channel` → invite the bot; `channel_not_found` → wrong channel_id).

---

## Notes / limits

- **No login layer**, so the sender is self-selected from the dropdown — it's an
  honesty field, not authenticated identity.
- The button is visible to anyone with the app URL. If that becomes a concern, gate
  the app behind Streamlit's auth or a shared password.
- Messages are **fire-and-forget** (no logging yet). A Google-Sheet or Snowflake
  request log can be added later if the team wants an audit trail.
