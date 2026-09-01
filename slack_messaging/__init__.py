"""
Slack messaging — in-app "send a message to the team"
=====================================================
Self-contained feature module for the WO Tracking Tool. Import
``slack_messenger_button`` in app.py and place it in the header:

    from slack_messaging import slack_messenger_button
    ...
    slack_messenger_button()   # renders a small 💬 Slack button + dialog

Config lives entirely in ``st.secrets`` — nothing sensitive in code. Shape:

    [slack]
    bot_token    = "xoxb-..."          # bot token: chat:write (+ im:write for DMs)
    channel_id   = "C0123456789"        # target channel ID (not the #name)
    channel_name = "#wo-tracker"        # display label only

    [slack.people]                      # name shown in dropdown -> Slack user ID
    "Ashwin Kaushik" = "U0123456789"
    "Owen ..."       = "U0987654321"

If the [slack] section is missing/empty the messenger degrades to a short
"not configured yet" note instead of erroring. See SETUP.md in this folder.
"""

import json
import urllib.request
import urllib.error

import streamlit as st

__all__ = ["slack_messenger_button"]


def _slack_cfg():
    """Return the Slack config dict, or None if not configured."""
    try:
        s = st.secrets["slack"]
    except Exception:
        return None
    token = s.get("bot_token")
    if not token:
        return None
    people = {}
    try:
        people = {str(k): str(v) for k, v in dict(s.get("people", {})).items()}
    except Exception:
        people = {}
    return {
        "token": token,
        "channel_id": s.get("channel_id"),
        "channel_name": s.get("channel_name", "the team channel"),
        "people": people,
    }


def _slack_api(token, method, payload):
    """POST to a Slack Web API method. Returns (body_dict, error_str)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, f"network error: {e.reason}"
    except Exception as e:
        return None, f"unexpected error: {e}"
    if not body.get("ok"):
        return None, body.get("error", "unknown Slack error")
    return body, None


def _slack_send(cfg, sender, target, note):
    """Compose and post the message. target = {'kind': 'channel'} or
    {'kind': 'dm', 'user_id': 'U...', 'name': '...'}. Returns (ok, message)."""
    note = (note or "").strip()
    if not note:
        return False, "Message is empty — nothing to send."

    text = f":clipboard: *WO Tracking Tool* — message from *{sender or 'someone'}*\n\n{note}"

    if target["kind"] == "channel":
        channel = cfg.get("channel_id")
        if not channel:
            return False, "No channel is configured (slack.channel_id)."
    else:
        user_id = target.get("user_id")
        if not user_id:
            return False, "That person has no Slack user ID in the people list."
        # Open (or reuse) the DM channel first — most reliable path for DMs.
        opened, err = _slack_api(cfg["token"], "conversations.open", {"users": user_id})
        if err:
            # Fall back to posting to the user ID directly.
            channel = user_id
        else:
            channel = opened["channel"]["id"]

    _, err = _slack_api(cfg["token"], "chat.postMessage", {"channel": channel, "text": text})
    if err:
        return False, err
    where = cfg["channel_name"] if target["kind"] == "channel" else f"@{target.get('name', 'user')}"
    return True, f"Sent to {where}."


@st.dialog("💬 Send a Slack message")
def _slack_dialog():
    cfg = _slack_cfg()
    if cfg is None:
        st.info(
            "Slack messaging isn't configured yet. Add a `[slack]` section to the app "
            "secrets (bot token, channel ID, and a `[slack.people]` name → user-ID map). "
            "See **slack_messaging/SETUP.md** in the repo for the exact steps."
        )
        return

    people = cfg["people"]
    names = list(people.keys())

    with st.form("slack_msg_form", clear_on_submit=False):
        if names:
            sender = st.selectbox("From", names, index=None, placeholder="Pick your name")
        else:
            sender = st.text_input("From (your name)")

        targets = [f"📢 Channel · {cfg['channel_name']}"] + [f"📩 DM · {n}" for n in names]
        target_label = st.selectbox("Send to", targets, index=0)

        note = st.text_area(
            "Message", height=130,
            placeholder="e.g. Please raise a WO for PO 12345 (SKU ABC-123) — arriving Friday, no WO yet.",
        )
        submitted = st.form_submit_button("Send to Slack", type="primary", use_container_width=True)

    if submitted:
        if not sender:
            st.error("Pick who the message is from first.")
            return
        if target_label.startswith("📢"):
            target = {"kind": "channel"}
        else:
            nm = target_label.split("·", 1)[1].strip()
            target = {"kind": "dm", "user_id": people.get(nm), "name": nm}
        with st.spinner("Sending…"):
            ok, msg = _slack_send(cfg, sender, target, note)
        if ok:
            st.success(f"✅ {msg}")
            st.caption("You can close this dialog.")
        else:
            st.error(f"❌ Not sent — {msg}")


def slack_messenger_button():
    """Small header launcher for the Slack messenger dialog."""
    st.markdown("##### 💬 Message")
    if st.button("Slack", use_container_width=True, help="Send a message to the team on Slack"):
        _slack_dialog()
