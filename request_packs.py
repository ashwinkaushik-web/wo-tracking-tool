"""Session-only raise-WO pack and chase list.

These are working copies for Slack / Shelf CSV. They do not create or complete
work orders in Shelf, and they do not write to Snowflake. Streamlit Cloud
clears them on reboot or a new browser session.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

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


def _drop_keys(prefix):
    for k in list(st.session_state.keys()):
        if str(k).startswith(prefix):
            del st.session_state[k]


def _blank(v):
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(v, pd.Timestamp):
        if pd.isna(v):
            return ""
        return v.strftime("%m/%d/%Y")
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none", "<na>", "nat", "null"):
        return ""
    if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
        return s[:-2]
    return s


def _qty_cell(v):
    n = pd.to_numeric(_blank(v) if not isinstance(v, (int, float)) else v, errors="coerce")
    if pd.isna(n):
        return 0
    return max(int(n), 0)


def _wo_type_cell(v):
    s = _blank(v).upper()
    if "FBM" in s:
        return "FBM"
    return "FBA"


def _prio_cell(v):
    s = _blank(v).upper()
    return "T" if s in ("T", "TRUE", "1", "Y", "YES") else "F"


def _ship_cell(v):
    today = date.today()
    s = _blank(v)
    ts = pd.to_datetime(s, errors="coerce") if s else pd.NaT
    if pd.isna(ts):
        d = today + timedelta(days=7)
    else:
        d = ts.date()
        if d < today:
            d = today + timedelta(days=7)
    return d.strftime("%m/%d/%Y")


def _po_cell(v):
    s = _blank(v)
    n = pd.to_numeric(s, errors="coerce")
    if pd.notna(n):
        return str(int(n))
    return s


def _status_cell(v):
    s = _blank(v)
    return s if s in CHASE_STATUSES else "Not asked"


def _records(key, columns):
    raw = st.session_state.get(key) or []
    if not raw:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(raw)
    for c in columns:
        if c not in df.columns:
            df[c] = ""
    return df[columns]


def _df_to_records(df, columns):
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for rec in df.to_dict("records"):
        row = {}
        for c in columns:
            val = rec.get(c, "")
            row[c] = _qty_cell(val) if c == "Request Amount" else _blank(val)
        out.append(row)
    return out


def _bump_nonce(nonce_key, *drop_prefixes):
    st.session_state[nonce_key] = int(st.session_state.get(nonce_key) or 0) + 1
    for prefix in drop_prefixes:
        _drop_keys(prefix)


def _set_records(key, nonce_key, df):
    columns = RAISE_COLS if key == RAISE_KEY else CHASE_COLS
    if key == RAISE_KEY:
        st.session_state[key] = _df_to_records(_normalize_raise_df(df), columns)
        _bump_nonce(nonce_key, "_raise_seed_", "raise_ed_")
    else:
        st.session_state[key] = _df_to_records(_normalize_chase_df(df), columns)
        _bump_nonce(nonce_key, "_chase_seed_", "chase_ed_")


def raise_df():
    return _normalize_raise_df(_records(RAISE_KEY, RAISE_COLS))


def chase_df():
    return _normalize_chase_df(_records(CHASE_KEY, CHASE_COLS))


def raise_count():
    return len(st.session_state.get(RAISE_KEY) or [])


def chase_count():
    return len(st.session_state.get(CHASE_KEY) or [])


def _commit_open_editor(editor_prefix, nonce_key, store_key, normalizer, columns):
    """If the grid is currently mounted, copy its edits into the pack before we replace it."""
    nonce = int(st.session_state.get(nonce_key) or 0)
    val = st.session_state.get(f"{editor_prefix}_{nonce}")
    if isinstance(val, pd.DataFrame):
        st.session_state[store_key] = _df_to_records(normalizer(val), columns)


def add_raise_rows(rows):
    _commit_open_editor("raise_ed", RAISE_NONCE, RAISE_KEY, _normalize_raise_df, RAISE_COLS)
    cur = list(st.session_state.get(RAISE_KEY) or [])
    for row in rows or []:
        rec = {c: "" for c in RAISE_COLS}
        rec.update({k: v for k, v in dict(row).items() if k in rec})
        rec = _normalize_raise_record(rec)
        if not rec.get("Product (Listing ID or Master ID)", "").strip():
            continue
        cur.append(rec)
    st.session_state[RAISE_KEY] = cur
    _bump_nonce(RAISE_NONCE, "_raise_seed_", "raise_ed_")
    return len(cur)


def add_chase_rows(rows):
    _commit_open_editor("chase_ed", CHASE_NONCE, CHASE_KEY, _normalize_chase_df, CHASE_COLS)
    today = date.today().isoformat()
    cur = list(st.session_state.get(CHASE_KEY) or [])
    for row in rows or []:
        rec = {c: "" for c in CHASE_COLS}
        rec.update({k: v for k, v in dict(row).items() if k in rec})
        rec = _normalize_chase_record(rec)
        rec["Added"] = rec.get("Added") or today
        cur.append(rec)
    st.session_state[CHASE_KEY] = cur
    _bump_nonce(CHASE_NONCE, "_chase_seed_", "chase_ed_")
    return len(cur)


def _normalize_raise_record(rec):
    out = {c: "" for c in RAISE_COLS}
    out.update(rec or {})
    out["Work Order Item Type"] = _wo_type_cell(out.get("Work Order Item Type"))
    out["Product (Listing ID or Master ID)"] = _blank(out.get("Product (Listing ID or Master ID)"))
    out["Request Amount"] = _qty_cell(out.get("Request Amount"))
    out["Ship By Date (MM/DD/YYYY)"] = _ship_cell(out.get("Ship By Date (MM/DD/YYYY)"))
    out["Prioritized (T/F)"] = _prio_cell(out.get("Prioritized (T/F)"))
    out["Receivable ID (Inventory Request ID or Purchase Order ID)"] = _po_cell(
        out.get("Receivable ID (Inventory Request ID or Purchase Order ID)")
    )
    po = out["Receivable ID (Inventory Request ID or Purchase Order ID)"]
    rtype = _blank(out.get("Receivable type (InventoryRequest or Purchase)"))
    if po and rtype.lower() not in ("purchase", "inventoryrequest"):
        rtype = "Purchase"
    out["Receivable type (InventoryRequest or Purchase)"] = rtype
    out["SKU"] = _blank(out.get("SKU"))
    out["Note"] = _blank(out.get("Note"))
    return out


def _normalize_chase_record(rec):
    out = {c: "" for c in CHASE_COLS}
    out.update(rec or {})
    out["Kind"] = _blank(out.get("Kind"))
    out["PO #"] = _po_cell(out.get("PO #"))
    out["WO"] = _blank(out.get("WO"))
    out["SKU"] = _blank(out.get("SKU"))
    out["WH"] = _blank(out.get("WH"))
    out["Status"] = _status_cell(out.get("Status"))
    out["Note"] = _blank(out.get("Note"))
    out["Added"] = _blank(out.get("Added")) or date.today().isoformat()
    return out


def _normalize_raise_df(df):
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame(columns=RAISE_COLS)
    rows = [_normalize_raise_record(rec) for rec in df.to_dict("records")]
    out = pd.DataFrame(rows, columns=RAISE_COLS)
    out["Request Amount"] = pd.to_numeric(out["Request Amount"], errors="coerce").fillna(0).astype(int)
    return out


def _normalize_chase_df(df):
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame(columns=CHASE_COLS)
    rows = [_normalize_chase_record(rec) for rec in df.to_dict("records")]
    return pd.DataFrame(rows, columns=CHASE_COLS)


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


def _row_get(r, *keys):
    for key in keys:
        if hasattr(r, "index") and key in r.index:
            val = r[key]
            if val is not None and str(val).strip().lower() not in ("", "nan", "none", "<na>"):
                return val
        if hasattr(r, "get"):
            val = r.get(key)
            if val is not None and str(val).strip().lower() not in ("", "nan", "none", "<na>"):
                return val
    return None


def _qty_from_row(r):
    qty = pd.to_numeric(_row_get(r, "outstanding_units", "Outstanding", "outstanding_wo_units",
                                 "left", "Left"), errors="coerce")
    if pd.isna(qty):
        ordered = pd.to_numeric(
            _row_get(r, "ordered_units", "current_units", "Current Ordered", "Ordered"),
            errors="coerce",
        )
        received = pd.to_numeric(_row_get(r, "received_units", "Received"), errors="coerce")
        wo = pd.to_numeric(_row_get(r, "wo_qty", "WO Qty", "WO qty"), errors="coerce")
        qty = (0 if pd.isna(ordered) else ordered) - (0 if pd.isna(received) else received)
        if pd.notna(wo):
            qty = qty - wo
    return 0 if pd.isna(qty) else max(int(qty), 0)


def _ship_by_from_row(r):
    today = date.today()
    raw = None
    for key in ("ship_date", "Ship Date", "order_placed_date", "order_placed", "Order Placed"):
        if key in getattr(r, "index", []) or (hasattr(r, "get") and r.get(key) is not None):
            raw = r.get(key) if hasattr(r, "get") else r[key]
            if raw is not None and str(raw) not in ("", "nan", "NaT", "None"):
                break
            raw = None
    d = None
    ts = pd.to_datetime(raw, errors="coerce") if raw is not None else pd.NaT
    if pd.notna(ts):
        d = ts.date()
    if d is None or d < today:
        d = today + timedelta(days=7)
    return d.strftime("%m/%d/%Y")


def _wo_type_from_row(r):
    ful = str(
        r.get("fulfillment_method", r.get("fulfillment", r.get("Fulfillment", ""))) or ""
    ).upper()
    return "FBM" if "FBM" in ful else "FBA"


def raise_rows_from_gap(frame):
    """PO-item / PO-line rows → Shelf-shaped raise rows (qty = outstanding)."""
    if frame is None or getattr(frame, "empty", True):
        return []
    rows = []
    for _, r in frame.iterrows():
        sku = _clean_cell(r["sku"] if "sku" in frame.columns else r.get("SKU"))
        mid = _clean_cell(r["master_id"] if "master_id" in frame.columns else r.get("Master ID"))
        product = sku or mid
        if not product:
            continue
        qty = _qty_from_row(r)
        po = _clean_cell(r["po_number"] if "po_number" in frame.columns else r.get("PO #"))
        rows.append({
            "Work Order Item Type": _wo_type_from_row(r),
            "Product (Listing ID or Master ID)": product,
            "Request Amount": qty,
            "Ship By Date (MM/DD/YYYY)": _ship_by_from_row(r),
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
        "Edit qty / listing / ship-by here, then Slack or download. "
        "Cleared if you reboot the app or use another browser."
    )
    n_raise, n_chase = raise_count(), chase_count()
    sub = st.radio(
        "Request type",
        ["Raise-WO pack", "Chase list"],
        horizontal=True, label_visibility="collapsed", key="requests_sub",
    )
    st.caption(f"📦 Raise-WO pack: **{n_raise}** row(s) · 📌 Chase list: **{n_chase}** row(s)")
    if sub == "Raise-WO pack":
        _render_raise()
    else:
        _render_chase()


def _editor_seed(seed_prefix, nonce, loader, editor_prefix):
    seed_key = f"{seed_prefix}{nonce}"
    editor_key = f"{editor_prefix}_{nonce}"
    # Widget state is dropped when this view isn't rendered. Reload from the
    # saved pack so we don't paint a stale seed and wipe edits.
    if editor_key not in st.session_state or seed_key not in st.session_state:
        st.session_state[seed_key] = loader()
    return seed_key


def _persist_editor(seed_key, store_key, edited, normalizer, columns):
    """Write pack rows for Slack / leaving the tab. Do not touch the editor seed."""
    cleaned = normalizer(edited)
    st.session_state[store_key] = _df_to_records(cleaned, columns)
    return cleaned


@st.fragment
def _render_raise():
    nonce = int(st.session_state.get(RAISE_NONCE) or 0)
    seed_key = _editor_seed("_raise_seed_", nonce, raise_df, "raise_ed")
    st.caption("Shelf bulk-upload columns. Change a cell and stay on it — the page should not reload.")
    edited = st.data_editor(
        st.session_state[seed_key],
        key=f"raise_ed_{nonce}",
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "Request Amount": st.column_config.NumberColumn("Request Amount", min_value=0, step=1),
            "Work Order Item Type": st.column_config.SelectboxColumn(
                "Work Order Item Type", options=["FBA", "FBM"], required=True,
            ),
            "Prioritized (T/F)": st.column_config.SelectboxColumn(
                "Prioritized (T/F)", options=["F", "T"], required=True,
            ),
            "Ship By Date (MM/DD/YYYY)": st.column_config.TextColumn("Ship By Date (MM/DD/YYYY)"),
            "Receivable ID (Inventory Request ID or Purchase Order ID)": st.column_config.TextColumn(
                "Receivable ID (Inventory Request ID or Purchase Order ID)",
            ),
        },
    )
    cleaned = _persist_editor(seed_key, RAISE_KEY, edited, _normalize_raise_df, RAISE_COLS)
    send_df = shelf_send_df(cleaned)
    skipped = 0 if cleaned is None else max(len(cleaned) - len(send_df), 0)
    if skipped:
        st.caption(f"{skipped} row(s) with qty 0 will not go in the Slack/Shelf file — type a qty first.")
    c1, c2, c3 = st.columns(3)
    with c1:
        if slack_send_panel_button is not None:
            slack_send_panel_button(
                "slack_raise_pack",
                df=send_df if not send_df.empty else cleaned,
                filename=f"wo_request_{datetime.now():%Y%m%d}.csv",
                note=_raise_note(send_df if not send_df.empty else cleaned),
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
            st.rerun(scope="app")


@st.fragment
def _render_chase():
    nonce = int(st.session_state.get(CHASE_NONCE) or 0)
    seed_key = _editor_seed("_chase_seed_", nonce, chase_df, "chase_ed")
    st.caption("Follow-up list. Status is yours — it does not close the warehouse WO.")
    edited = st.data_editor(
        st.session_state[seed_key],
        key=f"chase_ed_{nonce}",
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "Status": st.column_config.SelectboxColumn(
                "Status", options=CHASE_STATUSES, required=True,
            ),
        },
    )
    cleaned = _persist_editor(seed_key, CHASE_KEY, edited, _normalize_chase_df, CHASE_COLS)
    c1, c2, c3 = st.columns(3)
    with c1:
        if slack_send_panel_button is not None:
            slack_send_panel_button(
                "slack_chase_list",
                df=cleaned,
                filename=f"wo_chase_{datetime.now():%Y%m%d}.csv",
                note=_chase_note(cleaned),
                label="📤 Send chase list to Slack",
                use_container_width=True,
            )
        else:
            st.caption("Slack is not configured.")
    with c2:
        if cleaned is not None and not cleaned.empty:
            st.download_button(
                "⬇ Chase CSV",
                cleaned.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"wo_chase_{datetime.now():%Y%m%d}.csv",
                mime="text/csv",
                key="dl_chase",
                use_container_width=True,
            )
    with c3:
        if st.button("Clear chase list", use_container_width=True):
            _set_records(CHASE_KEY, CHASE_NONCE, None)
            st.rerun(scope="app")


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
