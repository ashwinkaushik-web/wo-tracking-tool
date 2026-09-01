# In-App Slack Messenger — Setup

> Feature module: `slack_messaging/` · entry point: `slack_messenger_button()` imported in `app.py`.

The WO Tracking Tool has a **💬 Slack** button in the header. It opens a popup where a
user picks who they are, chooses a **channel** or a **person (DM)**, types a message,
optionally **attaches a file**, and sends it to Slack.

Nothing works until the secrets below are added. No token or ID lives in the code.

---

## 1. Create a Slack app + bot token

1. <https://api.slack.com/apps> → **Create New App** → *From scratch* → name it, pick the workspace.
2. **OAuth & Permissions** → **Bot Token Scopes**, add:
   - `chat:write` — post messages
   - `im:write` — open/DM a person
   - `files:write` — **attach files** (required for the file picker)
   - `users:read` *(optional)*
3. **Install to Workspace** → **Allow** → copy the **Bot User OAuth Token** (`xoxb-...`).
   - If you add `files:write` later, you must **reinstall** the app for it to take effect,
     then re-copy the token.
4. **Invite the bot to every channel it should post to:** in Slack, in that channel, run
   `/invite @<your bot name>`. A bot can only post/upload where it is a member.

## 2. Find the IDs

- **Channel ID** — open the channel → click its name → *About* → bottom shows `C0123456789`.
- **User IDs** — click a person → *View full profile* → *⋮ More* → *Copy member ID* → `U0123456789`.
  (The `U…` is the person. A `D…` id is a DM thread — don't use that here.)

## 3. Add the secrets

Streamlit Cloud → app → **Settings → Secrets**, add **below** the `[snowflake]` block:

```toml
[slack]
bot_token = "xoxb-your-token-here"

# One line per channel the bot can post to (label -> channel ID).
# Invite the bot to each of these channels.
[slack.channels]
"#wo-trial"     = "C0BU1CU0885"
"#eu-warehouse" = "C0XXXXXXXXX"

# One line per person for the From dropdown / DMs (name -> user ID).
[slack.people]
"Ashwin Kaushik" = "U083SSCU5ML"
"Owen Davies"    = "U0987654321"
```

> **Adding more later:** just add another line under `[slack.channels]` (with an
> `/invite`) or `[slack.people]`, then Save. No code change needed.

Back-compat: the old single `channel_id` / `channel_name` pair still works if present.

## 4. Test

- Reload the app → **💬 Slack** → pick From, a channel or DM, type a note, optionally
  attach a file, **Send to Slack**.
- Success/failure shows inline. Failures quote Slack's error.

---

## Errors → fixes

- `not_in_channel` → invite the bot to that channel.
- `channel_not_found` → wrong channel ID.
- `missing_scope` → add the scope in step 1, then **reinstall** the app.
- file upload `missing_scope` / `access_denied` → add **`files:write`** and reinstall.

## Notes / limits

- **No login layer** — the sender is self-selected, not authenticated.
- Anyone with the app URL can send. Gate the app if that's a concern.
- Files are uploaded straight to Slack; keep attachments reasonably sized.
