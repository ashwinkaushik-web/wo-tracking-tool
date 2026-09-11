"""Session-only raise-WO pack and chase list.

These are working copies for Slack / Shelf CSV. They do not create or complete
work orders in Shelf, and they do not write to Snowflake. Streamlit Cloud
clears them on reboot or a new browser session.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import streamlit as st

try:
    from slack_messaging import slack_send_panel_button
except Exception:
    slack_send_panel_button = None

SHELF_WO_UPLOAD_COLS = [
    "Work Order Item Type",
    "Product (Listing ID or Master ID)",
    "Request Amount",
    "Ship By Date (MM/DD/YYYY)",
    "Prioritized (T/F)",
    "Receivable ID (Inventory Request ID or Purchase Order ID)",
    "Receivable type (InventoryRequest or Purchase)",
]

RAISE_COLS = SHELF_WO_UPLOAD_COLS + ["SKU", "Note"]
CHASE_COLS = ["Kind", "PO #", "WO", "SKU", "WH", "Status", "Note", "Added"]
CHASE_STATUSES = ["Not asked", "Asked", "Waiting", "Done chasing"]

RAISE_KEY = "_raise_wo_rows"
RAISE_NONCE = "_raise_nonce"
CHASE_KEY = "_chase_rows"
CHASE_NONCE = "_chase_nonce"

WO_COVERAGE_ALL = "All POs"
WO_COVERAGE_NO_ANY = "No WO (any)"
WO_COVERAGE_NO_ACTIVE = "No WO (active — Overview rule)"
WO_COVERAGE_HAS = "Has WO"
WO_COVERAGE_OPTIONS = [
    WO_COVERAGE_ALL,
    WO_COVERAGE_NO_ANY,
    WO_COVERAGE_NO_ACTIVE,
    WO_COVERAGE_HAS,
]


def _records(key, columns):
    raw = st.session_state.get(key) or []
    if not raw:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(raw)
    for c in columns:
        if c not in df.columns:
            df[c] = ""
    return df[columns]


def _set_records(key, nonce_key, df):
    if df is None or df.empty:
        st.session_state[key] = []
    else:
        out = df.copy()
        for c in out.columns:
            out[c] = out[c].fillna("").astype(str)
        st.session_state[key] = out.to_dict("records")
    st.session_state[nonce_key] = int(st.session_state.get(nonce_key) or 0) + 1


def raise_df():
    return _records(RAISE_KEY, RAISE_COLS)


def chase_df():
    return _records(CHASE_KEY, CHASE_COLS)


def raise_count():
    return len(st.session_state.get(RAISE_KEY) or [])


def chase_count():
    return len(st.session_state.get(CHASE_KEY) or [])


def add_raise_rows(rows):
    cur = list(st.session_state.get(RAISE_KEY) or [])
    for row in rows or []:
        rec = {c: "" for c in RAISE_COLS}
        rec.update({k: "" if v is None else str(v) for k, v in dict(row).items() if k in rec})
        if not rec.get("Product (Listing ID or Master ID)", "").strip():
            continue
        if not rec.get("Prioritized (T/F)"):
            rec["Prioritized (T/F)"] = "F"
        if rec.get("Receivable ID (Inventory Request ID or Purchase Order ID)") and not rec.get(
            "Receivable type (InventoryRequest or Purchase)"
        ):
            rec["Receivable type (InventoryRequest or Purchase)"] = "Purchase"
        cur.append(rec)
    st.session_state[RAISE_KEY] = cur
    st.session_state[RAISE_NONCE] = int(st.session_state.get(RAISE_NONCE) or 0) + 1
    return len(cur)


def add_chase_rows(rows):
    today = date.today().isoformat()
    cur = list(st.session_state.get(CHASE_KEY) or [])
    for row in rows or []:
        rec = {c: "" for c in CHASE_COLS}
        rec.update({k: "" if v is None else str(v) for k, v in dict(row).items() if k in rec})
        rec["Status"] = rec.get("Status") or "Not asked"
        rec["Added"] = rec.get("Added") or today
        cur.append(rec)
    st.session_state[CHASE_KEY] = cur
    st.session_state[CHASE_NONCE] = int(st.session_state.get(CHASE_NONCE) or 0) + 1
    return len(cur)


def shelf_send_df(df):
    """Rows ready for Shelf upload: qty > 0, Shelf columns only."""
    empty = pd.DataFrame(columns=SHELF_WO_UPLOAD_COLS)
    if df is None or df.empty:
        return empty
    out = df.copy()
    qty = pd.to_numeric(out.get("Request Amount"), errors="coerce").fillna(0)
    out = out.loc[qty > 0].copy()
    if out.empty:
        return empty
    for c in SHELF_WO_UPLOAD_COLS:
        if c not in out.columns:
            out[c] = ""
    if "Receivable ID (Inventory Request ID or Purchase Order ID)" in out.columns:
        po = out["Receivable ID (Inventory Request ID or Purchase Order ID)"].astype(str).str.strip()
        rtype = out["Receivable type (InventoryRequest or Purchase)"].astype(str)
        blank = rtype.isin(("", "nan", "None"))
        out.loc[blank & po.ne("") & ~po.str.lower().isin(("nan", "none")),
                "Receivable type (InventoryRequest or Purchase)"] = "Purchase"
    return out[SHELF_WO_UPLOAD_COLS]


def po_coverage_mask(df, choice, *, wo_col="wo_count", grace_days=0):
    """Filter a PO-level (or item) frame by WO coverage choice."""
    if df is None or df.empty or wo_col not in df.columns or choice in (None, WO_COVERAGE_ALL, "All"):
        return df
    wc = pd.to_numeric(df[wo_col], errors="coerce").fillna(0)
    if choice == WO_COVERAGE_HAS:
        return df[wc > 0].copy()
    if choice == WO_COVERAGE_NO_ANY:
        return df[wc <= 0].copy()
    if choice == WO_COVERAGE_NO_ACTIVE:
        state_col = "purchase_state" if "purchase_state" in df.columns else None
        state = df[state_col].astype(str).str.lower() if state_col else pd.Series("", index=df.index)
        date_col = next((c for c in ("order_placed", "order_placed_date") if c in df.columns), None)
        if date_col:
            age = (pd.Timestamp(datetime.now().date())
                   - pd.to_datetime(df[date_col], errors="coerce")).dt.days
        else:
            age = pd.Series(0, index=df.index)
        mask = (
            (wc <= 0)
            & (~state.isin(["ready_to_reconcile", "cancelled", "canceled"]))
            & (age.fillna(0) >= grace_days)
        )
        return df[mask].copy()
    return df


def chase_row(*, kind, po="", wo="", sku="", wh="", note="", status="Not asked"):
    return {
        "Kind": kind,
        "PO #": str(po or "").strip(),
        "WO": str(wo or "").strip(),
        "SKU": str(sku or "").strip(),
        "WH": str(wh or "").strip(),
        "Status": status,
        "Note": str(note or "").strip(),
        "Added": date.today().isoformat(),
    }


def _clean_cell(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none", "<na>"):
        return ""
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def chase_rows_from_pos(frame, *, kind="PO no-WO"):
    if frame is None or getattr(frame, "empty", True):
        return []
    po_col = next((c for c in ("po_number", "PO #") if c in frame.columns), None)
    wh_col = next((c for c in ("warehouse_name", "WH", "warehouse") if c in frame.columns), None)
    sku_col = next((c for c in ("sku", "SKU") if c in frame.columns), None)
    rows = []
    for _, r in frame.iterrows():
        rows.append(chase_row(
            kind=kind,
            po=_clean_cell(r[po_col]) if po_col else "",
            sku=_clean_cell(r[sku_col]) if sku_col else "",
            wh=_clean_cell(r[wh_col]) if wh_col else "",
            note=_clean_cell(r["reason"]) if "reason" in frame.columns else "",
        ))
    return rows


def raise_rows_from_gap(frame):
    """PO-item gap lines → Shelf-shaped raise rows (qty = outstanding)."""
    if frame is None or getattr(frame, "empty", True):
        return []
    rows = []
    for _, r in frame.iterrows():
        sku = _clean_cell(r["sku"] if "sku" in frame.columns else r.get("SKU"))
        mid = _clean_cell(r["master_id"] if "master_id" in frame.columns else r.get("Master ID"))
        product = sku or mid
        if not product:
            continue
        qty = pd.to_numeric(r.get("outstanding_units", r.get("Outstanding")), errors="coerce")
        if pd.isna(qty):
            qty = pd.to_numeric(r.get("left", r.get("Left")), errors="coerce")
        if pd.isna(qty):
            ordered = pd.to_numeric(r.get("ordered_units", r.get("Ordered")), errors="coerce")
            received = pd.to_numeric(r.get("received_units", r.get("Received")), errors="coerce")
            qty = (0 if pd.isna(ordered) else ordered) - (0 if pd.isna(received) else received)
        qty = 0 if pd.isna(qty) else max(int(qty), 0)
        po = _clean_cell(r["po_number"] if "po_number" in frame.columns else r.get("PO #"))
        rows.append({
            "Work Order Item Type": "FBA",
            "Product (Listing ID or Master ID)": product,
            "Request Amount": qty,
            "Ship By Date (MM/DD/YYYY)": "",
            "Prioritized (T/F)": "F",
            "Receivable ID (Inventory Request ID or Purchase Order ID)": po,
            "Receivable type (InventoryRequest or Purchase)": "Purchase" if po else "",
            "SKU": sku,
            "Note": _clean_cell(r["reason"]) if "reason" in frame.columns else "",
        })
    return rows


def render_requests_tab():
    st.markdown("#### 📋 Requests")
    st.caption(
        "Working copies only — **this does not create or complete a WO in Shelf**. "
        "Edit, then Slack / download. Cleared if you reboot the app or use another browser."
    )
    n_raise, n_chase = raise_count(), chase_count()
    sub = st.radio(
        "Request type",
        ["Raise-WO pack", "Chase list"],
        horizontal=True, label_visibility="collapsed", key="requests_sub",
        format_func=lambda x: f"📦 Raise-WO pack ({n_raise})" if x == "Raise-WO pack" else f"📌 Chase list ({n_chase})",
    )
    if sub == "Raise-WO pack":
        _render_raise()
    else:
        _render_chase()


def _render_raise():
    nonce = int(st.session_state.get(RAISE_NONCE) or 0)
    df = raise_df()
    st.caption("Shelf bulk-upload columns. Change qty / listing / ship-by here, then send only these rows.")
    edited = st.data_editor(
        df,
        key=f"raise_ed_{nonce}",
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "Request Amount": st.column_config.NumberColumn("Request Amount", min_value=0, step=1),
            "Work Order Item Type": st.column_config.SelectboxColumn(
                "Work Order Item Type", options=["FBA", "FBM"],
            ),
            "Prioritized (T/F)": st.column_config.SelectboxColumn("Prioritized (T/F)", options=["F", "T"]),
        },
    )
    st.session_state[RAISE_KEY] = (
        edited.to_dict("records") if edited is not None and not edited.empty else []
    )
    send_df = shelf_send_df(edited)
    skipped = 0 if edited is None else max(len(edited) - len(send_df), 0)
    if skipped:
        st.caption(f"{skipped} row(s) with qty 0 will not go in the Slack/Shelf file — type a qty first.")
    c1, c2, c3 = st.columns(3)
    with c1:
        if slack_send_panel_button is not None:
            slack_send_panel_button(
                "slack_raise_pack",
                df=send_df if not send_df.empty else edited,
                filename=f"wo_request_{datetime.now():%Y%m%d}.csv",
                note=_raise_note(send_df if not send_df.empty else edited),
                label="📤 Send raise-WO pack to Slack",
                use_container_width=True,
            )
        else:
            st.caption("Slack is not configured.")
    with c2:
        if not send_df.empty:
            st.download_button(
                "⬇ Shelf WO CSV",
                send_df.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"wo_request_{datetime.now():%Y%m%d}.csv",
                mime="text/csv",
                key="dl_raise_pack",
                use_container_width=True,
            )
        else:
            st.caption("Add rows with qty > 0 to download.")
    with c3:
        if st.button("Clear raise-WO pack", use_container_width=True):
            _set_records(RAISE_KEY, RAISE_NONCE, None)
            st.rerun()


def _render_chase():
    nonce = int(st.session_state.get(CHASE_NONCE) or 0)
    df = chase_df()
    st.caption("Follow-up list. Status is yours — it does not close the warehouse WO.")
    edited = st.data_editor(
        df,
        key=f"chase_ed_{nonce}",
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "Status": st.column_config.SelectboxColumn("Status", options=CHASE_STATUSES),
        },
    )
    st.session_state[CHASE_KEY] = (
        edited.to_dict("records") if edited is not None and not edited.empty else []
    )
    c1, c2, c3 = st.columns(3)
    with c1:
        if slack_send_panel_button is not None:
            slack_send_panel_button(
                "slack_chase_list",
                df=edited,
                filename=f"wo_chase_{datetime.now():%Y%m%d}.csv",
                note=_chase_note(edited),
                label="📤 Send chase list to Slack",
                use_container_width=True,
            )
        else:
            st.caption("Slack is not configured.")
    with c2:
        if edited is not None and not edited.empty:
            st.download_button(
                "⬇ Chase CSV",
                edited.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"wo_chase_{datetime.now():%Y%m%d}.csv",
                mime="text/csv",
                key="dl_chase",
                use_container_width=True,
            )
    with c3:
        if st.button("Clear chase list", use_container_width=True):
            _set_records(CHASE_KEY, CHASE_NONCE, None)
            st.rerun()


def _raise_note(df):
    n = 0 if df is None else len(df)
    pos = []
    if df is not None and not df.empty:
        col = "Receivable ID (Inventory Request ID or Purchase Order ID)"
        if col in df.columns:
            pos = sorted({str(x).strip() for x in df[col] if str(x).strip() and str(x).lower() != "nan"})
    po_bit = f" POs {', '.join(pos[:8])}" + (f" +{len(pos) - 8}" if len(pos) > 8 else "") if pos else ""
    return (
        f"Please raise a WO — {n} line(s) in the attached Shelf upload CSV.{po_bit}\n"
        "This is a request file only; it does not create the WO until uploaded in Shelf."
    )


def _chase_note(df):
    n = 0 if df is None else len(df)
    asked = 0
    if df is not None and not df.empty and "Status" in df.columns:
        asked = int(df["Status"].astype(str).isin(["Asked", "Waiting", "Done chasing"]).sum())
    return (
        f"WO chase list — {n} line(s) ({asked} already asked / waiting / done). "
        "Status is planner follow-up, not a warehouse completion."
    )
