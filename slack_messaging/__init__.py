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
    bot_token = "xoxb-..."               # scopes: chat:write, im:write, files:write

    [slack.channels]                     # label shown in dropdown -> channel ID
    "#wo-trial"     = "C0BU1CU0885"
    "#eu-warehouse" = "C0XXXXXXXXX"

    [slack.people]                       # name shown in dropdown -> Slack user ID
    "Ashwin Kaushik" = "U0123456789"
    "Owen Davies"    = "U0987654321"

Back-compat: a single ``channel_id`` / ``channel_name`` pair (the original
shape) is still accepted and treated as one channel.

If the [slack] section is missing/empty the messenger degrades to a short
"not configured yet" note instead of erroring. See SETUP.md in this folder.
"""

import json
import uuid
import urllib.parse
import urllib.request
import urllib.error

import streamlit as st

__all__ = ["slack_messenger_button", "slack_send_panel_button", "slack_table_sender"]


def _slack_cfg():
    """Return the Slack config dict, or None if not configured."""
    try:
        s = st.secrets["slack"]
    except Exception:
        return None
    token = s.get("bot_token")
    if not token:
        return None

    def _as_map(v):
        try:
            return {str(k): str(v2) for k, v2 in dict(v).items()}
        except Exception:
            return {}

    channels = _as_map(s.get("channels", {}))
    # Back-compat: fold a legacy single channel into the channels map.
    if not channels and s.get("channel_id"):
        channels = {s.get("channel_name", "#channel"): s.get("channel_id")}

    return {
        "token": token,
        "channels": channels,
        "people": _as_map(s.get("people", {})),
    }


def _slack_post(token, method, payload):
    """POST JSON to a Slack Web API method. Returns (body_dict, error_str)."""
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
    return _read(req)


def _slack_get(token, method, params):
    """GET a Slack Web API method with query params. Returns (body_dict, error)."""
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"https://slack.com/api/{method}?{qs}",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    return _read(req)


def _read(req):
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
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


def _resolve_channel(cfg, target):
    """Return (channel_id, error). For a DM, open the bot↔user IM first."""
    if target["kind"] == "channel":
        cid = target.get("channel_id")
        return (cid, None) if cid else (None, "No channel ID configured.")
    user_id = target.get("user_id")
    if not user_id:
        return None, "That person has no Slack user ID in the people list."
    opened, err = _slack_post(cfg["token"], "conversations.open", {"users": user_id})
    if err:
        # Fall back to posting to the user ID directly.
        return user_id, None
    return opened["channel"]["id"], None


def _upload_file(token, channel_id, filename, data_bytes, comment):
    """Upload + share a file via the modern external-upload flow. Returns (ok, err)."""
    # 1) Reserve an upload URL.
    info, err = _slack_get(token, "files.getUploadURLExternal",
                           {"filename": filename, "length": len(data_bytes)})
    if err:
        return False, f"getUploadURL: {err}"
    upload_url, file_id = info["upload_url"], info["file_id"]

    # 2) POST the bytes to the reserved URL as multipart/form-data.
    boundary = uuid.uuid4().hex
    pre = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    post = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = pre + data_bytes + post
    up_req = urllib.request.Request(
        upload_url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(up_req, timeout=60) as resp:
            if resp.status != 200:
                return False, f"upload HTTP {resp.status}"
    except Exception as e:
        return False, f"upload failed: {e}"

    # 3) Complete + share to the channel/DM, with the message as the comment.
    payload = {"files": [{"id": file_id, "title": filename}], "channel_id": channel_id}
    if comment:
        payload["initial_comment"] = comment
    _, err = _slack_post(token, "files.completeUploadExternal", payload)
    if err:
        return False, f"completeUpload: {err}"
    return True, None


def _slack_send(cfg, sender, target, note, upload=None):
    """Send a message (and optional file). upload = {'name','bytes'} or None.
    Returns (ok, message)."""
    note = (note or "").strip()
    if not note and upload is None:
        return False, "Add a message or attach a file."

    if note:
        text = f":clipboard: *WO Tracking Tool* — message from *{sender or 'someone'}*\n\n{note}"
    else:
        text = f":clipboard: *WO Tracking Tool* — file from *{sender or 'someone'}*"

    channel, err = _resolve_channel(cfg, target)
    if err:
        return False, err

    where = target.get("name", "the channel") if target["kind"] == "channel" else f"@{target.get('name', 'user')}"

    if upload is not None:
        ok, err = _upload_file(cfg["token"], channel, upload["name"], upload["bytes"], text)
        if not ok:
            return False, err
        return True, f"Sent to {where} with {upload['name']}."

    _, err = _slack_post(cfg["token"], "chat.postMessage", {"channel": channel, "text": text})
    if err:
        return False, err
    return True, f"Sent to {where}."


@st.dialog("💬 Send a Slack message")
def _slack_dialog():
    cfg = _slack_cfg()
    if cfg is None:
        st.info(
            "Slack messaging isn't configured yet. Add a `[slack]` section to the app "
            "secrets (bot token, `[slack.channels]`, and a `[slack.people]` name → user-ID "
            "map). See **slack_messaging/SETUP.md** in the repo for the exact steps."
        )
        return

    channels = cfg["channels"]          # {label: channel_id}
    people = cfg["people"]              # {name: user_id}
    chan_labels = list(channels.keys())
    names = list(people.keys())

    if not chan_labels and not names:
        st.warning("No channels or people configured. Add `[slack.channels]` and/or "
                   "`[slack.people]` entries in the app secrets.")
        return

    # Optional context set by a panel's "Send to Slack" button: a prefilled note
    # and/or a ready-made attachment (a CSV built in-memory — no download needed).
    ctx = st.session_state.get("_slack_ctx") or {}
    default_note = ctx.get("note", "")
    tool_file = ctx.get("attachment")   # {"name": str, "bytes": bytes} or None

    with st.form("slack_msg_form", clear_on_submit=False):
        if names:
            sender = st.selectbox("From", names, index=None, placeholder="Pick your name")
        else:
            sender = st.text_input("From (your name)")

        targets = [f"📢 {c}" for c in chan_labels] + [f"📩 DM · {n}" for n in names]
        target_label = st.selectbox("Send to", targets, index=0 if targets else None)

        note = st.text_area(
            "Message", value=default_note, height=120,
            placeholder="e.g. Please raise a WO for PO 12345 (SKU ABC-123) — arriving Friday, no WO yet.",
        )

        include_tool_file = False
        if tool_file:
            include_tool_file = st.checkbox(
                f"📎 Attach **{tool_file['name']}** (from this view)", value=True)
            uploaded = st.file_uploader("…or attach a different file instead")
        else:
            uploaded = st.file_uploader(
                "Attach a file (optional)",
                help="e.g. the 'Download this list (CSV)' export from a panel.",
            )
        submitted = st.form_submit_button("Send to Slack", type="primary", use_container_width=True)

    if submitted:
        if not sender:
            st.error("Pick who the message is from first.")
            return
        if not target_label:
            st.error("Pick a channel or person to send to.")
            return
        if target_label.startswith("📢 "):
            label = target_label[2:].strip()
            target = {"kind": "channel", "channel_id": channels.get(label), "name": label}
        else:
            nm = target_label.split("·", 1)[1].strip()
            target = {"kind": "dm", "user_id": people.get(nm), "name": nm}

        # A freshly uploaded file wins; otherwise use the tool-provided attachment.
        upload = None
        if uploaded is not None:
            upload = {"name": uploaded.name, "bytes": uploaded.getvalue()}
        elif tool_file and include_tool_file:
            upload = tool_file

        with st.spinner("Sending…"):
            ok, msg = _slack_send(cfg, sender, target, note, upload=upload)
        if ok:
            st.success(f"✅ {msg}")
            st.caption("You can close this dialog.")
        else:
            st.error(f"❌ Not sent — {msg}")


def slack_messenger_button():
    """Small header launcher for the Slack messenger dialog (blank compose)."""
    st.markdown("##### 💬 Message")
    if st.button("Slack", use_container_width=True, help="Send a message to the team on Slack"):
        st.session_state.pop("_slack_ctx", None)   # blank compose, no prefill
        _slack_dialog()


def slack_send_panel_button(key, *, df, filename, note="", label="📤 Send to Slack",
                            use_container_width=False):
    """Render a button that opens the Slack dialog pre-loaded with ``df`` as an
    in-memory CSV attachment and a prefilled ``note`` — no manual download needed.

    Call it right next to a panel's download button:

        slack_send_panel_button("slack_nowo", df=d,
            filename="pos_without_wo.csv",
            note="PO-level no-WO list attached from WO Tracker.")
    """
    if st.button(label, key=key, use_container_width=use_container_width,
                 help="Send this list to Slack with the table attached as a CSV"):
        st.session_state["_slack_ctx"] = {
            "note": note,
            "attachment": {
                "name": filename,
                "bytes": df.to_csv(index=False).encode("utf-8"),
            },
        }
        _slack_dialog()


def _row_note(row, columns):
    """Compact 'field: value' summary of one row for the message body."""
    lines = []
    for c in columns:
        if c == "Open":
            continue
        v = row[c]
        s = "" if v is None else str(v)
        if s.strip() and s.lower() != "nan":
            lines.append(f"• *{c}*: {s}")
    return "\n".join(lines)


def slack_table_sender(key, df, *, label_cols=None, filename="wo_tracker_table.csv",
                       allow_whole=True):
    """Compact '📤 Send to Slack' control shown under any table. Lets the user send
    the whole (visible) table as a CSV, or pick a single line and send just that row
    (as a 1-row CSV + a prefilled details message). Works on every table via
    render_table; the dialog itself handles the not-configured case."""
    if df is None or len(df) == 0:
        return
    cols = [c for c in (label_cols or list(df.columns)[:3]) if c in df.columns] or list(df.columns[:1])

    with st.expander("📤 Send to Slack"):
        modes = (["A single line", "Whole table"] if allow_whole else ["A single line"])
        mode = st.radio("What to send", modes, horizontal=True,
                        key=f"{key}_slk_mode", label_visibility="collapsed")

        if mode == "Whole table":
            if st.button("Compose in Slack →", key=f"{key}_slk_all", use_container_width=True):
                st.session_state["_slack_ctx"] = {
                    "note": f"{len(df):,} rows from the WO Tracking Tool (attached).",
                    "attachment": {"name": filename, "bytes": df.to_csv(index=False).encode("utf-8")},
                }
                _slack_dialog()
        else:
            # Cap the dropdown so a huge table doesn't build a giant widget every run.
            pick_cap = 250
            n_opts = min(len(df), pick_cap)
            if len(df) > pick_cap:
                st.caption(f"Showing the first {pick_cap:,} of {len(df):,} rows — "
                           "filter/search the table to reach the rest.")

            def _fmt(i):
                r = df.iloc[i]
                parts = [str(r[c]) for c in cols
                         if str(r[c]).strip() and str(r[c]).lower() != "nan"]
                return " · ".join(parts) if parts else f"Row {i + 1}"

            idx = st.selectbox("Pick a line", options=list(range(n_opts)), format_func=_fmt,
                               index=None, placeholder="Choose a line…", key=f"{key}_slk_pick")
            if st.button("Compose in Slack →", key=f"{key}_slk_row",
                         disabled=(idx is None), use_container_width=True) and idx is not None:
                row = df.iloc[[idx]]
                note = "Single line flagged from the WO Tracking Tool —\n" + _row_note(row.iloc[0], df.columns)
                st.session_state["_slack_ctx"] = {
                    "note": note,
                    "attachment": {"name": filename.replace(".csv", "_line.csv"),
                                   "bytes": row.to_csv(index=False).encode("utf-8")},
                }
                _slack_dialog()
