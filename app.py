"""
WO Tracking Tool — Streamlit App
================================
Tracks Storage and PO Work Orders with blocked / stalled detection.
Live Snowflake connection, cached 30 min, manual refresh available.
"""

import streamlit as st
import pandas as pd
import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from datetime import datetime
from pathlib import Path

# ============================================================
# CONFIG
# ============================================================
st.set_page_config(
    page_title="WO Tracking Tool",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

QUERY_PATH = Path(__file__).parent / "queries" / "wo_tracker.sql"
CACHE_TTL_SECONDS = 1800  # 30 min


def _safe_int(v, default=0):
    """Convert to int, handling NaN/None gracefully."""
    try:
        if v is None or pd.isna(v):
            return default
        return int(v)
    except (ValueError, TypeError):
        return default


def _safe_date_str(v):
    """Convert date to string, NaT/None becomes em-dash."""
    if v is None or pd.isna(v):
        return "—"
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    return str(v)


# ============================================================
# SNOWFLAKE CONNECTION (key-pair auth)
# ============================================================
def _load_private_key():
    """Load and serialize the RSA private key from Streamlit secrets."""
    key_pem = st.secrets["snowflake"]["private_key"].encode("utf-8")
    passphrase = st.secrets["snowflake"].get("private_key_passphrase", None)
    passphrase_bytes = passphrase.encode("utf-8") if passphrase else None
    p_key = serialization.load_pem_private_key(
        key_pem,
        password=passphrase_bytes,
        backend=default_backend(),
    )
    return p_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@st.cache_resource
def get_snowflake_connection():
    """Open Snowflake connection (cached for app session)."""
    return snowflake.connector.connect(
        user=st.secrets["snowflake"]["user"],
        private_key=_load_private_key(),
        account=st.secrets["snowflake"]["account"],
        role=st.secrets["snowflake"]["role"],
        warehouse=st.secrets["snowflake"]["warehouse"],
        database=st.secrets["snowflake"].get("database", "ANALYTICS_DB"),
        schema=st.secrets["snowflake"].get("schema", "STG_AMACZAR"),
        client_session_keep_alive=True,
    )


# ============================================================
# DATA PREP — vectorised, fast
# ============================================================
def _build_wo_aggregates(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate WOIs into one row per WO. Vectorised; runs once per cache cycle."""
    # WO-level rollup
    g = df.groupby("work_order_number", as_index=False).agg(
        source_category=("source_category", "first"),
        source=("source", "first"),
        po_number_raw=("po_number_raw", "first"),
        warehouse=("warehouse", "first"),
        items=("work_order_item_id", "count"),
        open_items=("status_simple", lambda s: (s == "Open").sum()),
        closed_items=("status_simple", lambda s: (s == "Closed").sum()),
        untouched=("processed", lambda s: (s == 0).sum()),
        orig=("original_request", "sum"),
        processed=("processed", "sum"),
        ordered=("order_created", "sum"),
        shipped=("shipped", "sum"),
        stowed=("storage", "sum"),
        max_age=("age_days_from_created", "max"),
        earliest_ship=("ship_by", "min"),
        unique_listings=("listing_id", "nunique"),
        pfs_blocks=("is_blocked_pfs", lambda s: s.fillna(False).sum()),
    )
    g["pct"] = (g["processed"] * 100 / g["orig"].replace(0, pd.NA)).round(1).fillna(0)

    # Worst PO flag per WO — VECTORISED using flag rank + idxmin
    flag_priority = [
        "🔴 Blocked / Issue",
        "🟠 Partially Processed",
        "🟡 Approaching ship-by",
        "🟢 On Track",
        "✅ Complete",
    ]
    flag_rank = {f: i for i, f in enumerate(flag_priority)}
    df_with_rank = df[df["po_block_flag"].notna()].copy()
    if not df_with_rank.empty:
        df_with_rank["_rank"] = df_with_rank["po_block_flag"].map(flag_rank)
        worst_idx = df_with_rank.groupby("work_order_number")["_rank"].idxmin()
        worst_per_wo = df_with_rank.loc[worst_idx, ["work_order_number", "po_block_flag"]]
        worst_map = dict(zip(worst_per_wo["work_order_number"], worst_per_wo["po_block_flag"]))
    else:
        worst_map = {}
    g["worst_po_flag"] = g["work_order_number"].map(worst_map)

    # Top brand per WO — VECTORISED using value_counts groupby
    brand_counts = (
        df.dropna(subset=["source_brand"])
        .groupby(["work_order_number", "source_brand"])
        .size()
        .reset_index(name="_cnt")
        .sort_values(["work_order_number", "_cnt"], ascending=[True, False])
        .drop_duplicates("work_order_number", keep="first")
    )
    brand_map = dict(zip(brand_counts["work_order_number"], brand_counts["source_brand"]))
    g["top_brand"] = g["work_order_number"].map(brand_map).fillna("")

    # Top block reason per WO (only from blocked items) — VECTORISED
    blocked = df[df["is_blocked_pfs"].fillna(False)]
    if not blocked.empty:
        reason_counts = (
            blocked.dropna(subset=["block_reason_pfs"])
            .groupby(["work_order_number", "block_reason_pfs"])
            .size()
            .reset_index(name="_cnt")
            .sort_values(["work_order_number", "_cnt"], ascending=[True, False])
            .drop_duplicates("work_order_number", keep="first")
        )
        reason_map = dict(zip(reason_counts["work_order_number"], reason_counts["block_reason_pfs"]))
    else:
        reason_map = {}
    g["top_block_reason"] = g["work_order_number"].map(reason_map).fillna("")
    return g


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def fetch_data():
    """Run query + build aggregates. Cached 30 min as a single bundle."""
    sql = QUERY_PATH.read_text()
    conn = get_snowflake_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
        cols = [c[0].lower() for c in cur.description]
        df = pd.DataFrame(rows, columns=cols)
    finally:
        cur.close()

    # Coerce date columns
    date_cols = [
        "po_ref_ship_by_date", "po_requested_ship_date",
        "po_requested_delivery_date", "po_placed_at", "po_arrived_at",
        "ship_by", "last_edit_at", "created_at",
    ]
    for col in date_cols:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    # Convert Decimal columns to numeric
    numeric_cols = [
        "original_request", "current_request", "processed",
        "order_created", "shipped", "storage", "woi_processing_pct",
        "age_days_from_created", "days_overdue",
        "wo_total_wois", "wo_wois_open", "wo_wois_untouched",
        "wo_total_orig_qty", "wo_total_processed_qty", "wo_processing_pct",
        "po_days_past_ref_ship_by",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Build aggregates ONCE and return alongside the raw data — both cached
    wos = _build_wo_aggregates(df)
    return df, wos, datetime.now()


# ============================================================
# FAST SEARCH HELPER (vectorised)
# ============================================================
def _str_contains_any(df, cols, query):
    """Vectorised case-insensitive substring search across columns."""
    if not query:
        return pd.Series([True] * len(df), index=df.index)
    q = query.lower()
    mask = pd.Series([False] * len(df), index=df.index)
    for c in cols:
        if c in df.columns:
            mask = mask | df[c].astype(str).str.lower().str.contains(q, na=False, regex=False)
    return mask


# ============================================================
# SIDEBAR
# ============================================================
def sidebar(last_refresh):
    st.sidebar.title("📊 WO Tracker")
    st.sidebar.caption("Live Snowflake snapshot")

    if last_refresh:
        delta_min = (datetime.now() - last_refresh).total_seconds() / 60
        st.sidebar.metric("Last refresh", last_refresh.strftime("%H:%M:%S"), f"{delta_min:.0f} min ago")

    if st.sidebar.button("🔄 Refresh now", use_container_width=True, type="primary"):
        fetch_data.clear()
        st.rerun()

    st.sidebar.markdown("---")
    st.sidebar.markdown(
        "**Coverage**\n\n"
        "- All WOs created since Jan 1 this year\n"
        "- Northampton (138) + Wroclaw (146)\n"
        "- Auto-refresh every 30 min\n"
    )
    st.sidebar.markdown("---")
    st.sidebar.markdown(
        "**Block flag thresholds (PO)**\n\n"
        "- 🔴 21+ days past ship-by, 0% processed\n"
        "- 🟠 14+ days past ship-by, partial\n"
        "- 🟡 0–13 days past\n"
        "- 🟢 Before ship-by\n"
    )


# ============================================================
# KPI STRIP
# ============================================================
def kpi_strip(df, wos):
    storage_wos = wos[wos["source_category"] == "Storage"]
    po_wos = wos[wos["source_category"] == "PO"]
    storage_items = df[df["source_category"] == "Storage"]
    po_items = df[df["source_category"] == "PO"]

    storage_blocked = int(storage_items["is_blocked_pfs"].fillna(False).sum())
    po_blocked = int((po_items["po_block_flag"] == "🔴 Blocked / Issue").sum())
    po_partial = int((po_items["po_block_flag"] == "🟠 Partially Processed").sum())

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total WOs YTD", f"{len(wos):,}", f"{len(storage_wos)} Storage · {len(po_wos)} PO")
    c2.metric("Total Items YTD", f"{len(df):,}", f"{len(storage_items):,} S · {len(po_items):,} P")
    c3.metric("Storage Blocked", storage_blocked, "via PFS")
    c4.metric("PO Blocked 🔴", f"{po_blocked:,}", "21+ days, 0% processed")
    c5.metric("PO Partial 🟠", po_partial, "14+ days, not complete")


# ============================================================
# STORAGE TAB
# ============================================================
def storage_tab(df, wos):
    s_wos = wos[wos["source_category"] == "Storage"].copy()
    s_items = df[df["source_category"] == "Storage"].copy()

    view = st.radio(
        "View",
        ["📋 WO Level", "📄 Item Level"],
        horizontal=True,
        key="storage_view",
        label_visibility="collapsed",
    )

    st.caption("Blocked detection: **PFS table** (Listing Failed, Replen Needed, etc.)")

    if view == "📋 WO Level":
        storage_wo_view(s_wos, s_items)
    else:
        storage_item_view(s_items)


def storage_wo_view(s_wos, s_items):
    c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
    open_filter = c1.selectbox("Has Open", ["All", "With Open items", "All Closed"], index=1, key="sf_open")
    blk_filter = c2.selectbox("Has Blocked", ["All", "With Blocked", "No Blocked"], key="sf_blk")
    wh_filter = c3.selectbox("Warehouse", ["Both", "Northampton", "Wroclaw"], key="sf_wh")
    search = c4.text_input("Search", "", placeholder="WO, brand, reason...", key="sf_search")

    filtered = s_wos.copy()
    if open_filter == "With Open items":
        filtered = filtered[filtered["open_items"] > 0]
    elif open_filter == "All Closed":
        filtered = filtered[filtered["open_items"] == 0]
    if blk_filter == "With Blocked":
        filtered = filtered[filtered["pfs_blocks"] > 0]
    elif blk_filter == "No Blocked":
        filtered = filtered[filtered["pfs_blocks"] == 0]
    if wh_filter != "Both":
        filtered = filtered[filtered["warehouse"] == wh_filter]
    if search:
        mask = _str_contains_any(filtered, ["work_order_number", "top_brand", "top_block_reason"], search)
        filtered = filtered[mask]

    st.caption(f"{len(filtered)} of {len(s_wos)} WOs")
    filtered = filtered.sort_values("pfs_blocks", ascending=False)

    display = filtered[
        ["work_order_number", "warehouse", "top_brand", "items", "open_items",
         "pfs_blocks", "top_block_reason", "orig", "processed", "pct",
         "max_age", "earliest_ship"]
    ].rename(columns={
        "work_order_number": "WO", "warehouse": "WH", "top_brand": "Brand",
        "items": "Items", "open_items": "Open", "pfs_blocks": "Blocked (PFS)",
        "top_block_reason": "Top Reason", "orig": "Orig", "processed": "Processed",
        "pct": "% Processed", "max_age": "Age (d)", "earliest_ship": "Ship By",
    })

    event = st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        height=500,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "% Processed": st.column_config.ProgressColumn(
                "% Processed", min_value=0, max_value=100, format="%.1f%%"
            ),
            "Blocked (PFS)": st.column_config.NumberColumn("Blocked (PFS)", help="Items blocked per PFS"),
            "Ship By": st.column_config.DateColumn("Ship By"),
        },
    )

    if event.selection.rows:
        selected_wo = display.iloc[event.selection.rows[0]]["WO"]
        storage_wo_drilldown(selected_wo, s_items, s_wos)


def storage_wo_drilldown(wo_id, s_items, s_wos):
    wo_row = s_wos[s_wos["work_order_number"] == wo_id].iloc[0]
    st.markdown("---")
    st.markdown(f"### WO {wo_id} — Storage · {wo_row['warehouse']}")
    st.caption(f"Top brand: {wo_row['top_brand']} · {wo_row['unique_listings']} unique listings")

    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    c1.metric("Items", _safe_int(wo_row["items"]))
    c2.metric("Open", _safe_int(wo_row["open_items"]))
    c3.metric("Blocked (PFS)", _safe_int(wo_row["pfs_blocks"]))
    c4.metric("Orig Qty", f"{_safe_int(wo_row['orig']):,}")
    c5.metric("Processed", f"{_safe_int(wo_row['processed']):,}", f"{wo_row['pct']:.1f}%")
    c6.metric("Stowed", f"{_safe_int(wo_row['stowed']):,}")
    c7.metric("Max Age", f"{_safe_int(wo_row['max_age'])}d")

    items = s_items[s_items["work_order_number"] == wo_id].copy()

    st.markdown("#### Items in this WO")
    c1, c2 = st.columns([1, 2])
    status_f = c1.selectbox("Status", ["All", "Open", "Closed"], key=f"sdf_status_{wo_id}")
    search = c2.text_input("Search items", "", placeholder="Listing, brand...", key=f"sdf_search_{wo_id}")

    if status_f != "All":
        items = items[items["status_simple"] == status_f]
    if search:
        mask = _str_contains_any(items, ["listing_id", "source_brand", "finished_good_name"], search)
        items = items[mask]

    st.caption(f"{len(items)} items")
    display = items[
        ["listing_id", "finished_good_name", "source_brand", "status_simple",
         "processing_status", "block_reason_pfs",
         "original_request", "processed", "woi_processing_pct",
         "shipped", "storage", "age_days_from_created", "ship_by"]
    ].rename(columns={
        "listing_id": "Listing", "finished_good_name": "Item Name",
        "source_brand": "Brand", "status_simple": "Status",
        "processing_status": "Processing Status", "block_reason_pfs": "Block Reason",
        "original_request": "Orig", "processed": "Processed",
        "woi_processing_pct": "%", "shipped": "Shipped", "storage": "Stowed",
        "age_days_from_created": "Age (d)", "ship_by": "Ship By",
    })
    st.dataframe(display, use_container_width=True, hide_index=True, height=400)


def storage_item_view(s_items):
    c1, c2, c3, c4, c5 = st.columns(5)
    status_f = c1.selectbox("Status", ["All", "Open", "Closed"], index=1, key="sif_status")
    blk_f = c2.selectbox("Block", ["All", "Blocked only", "Pickable only"], key="sif_blk")
    wh_f = c3.selectbox("Warehouse", ["Both", "Northampton", "Wroclaw"], key="sif_wh")
    reasons = sorted(s_items.loc[s_items["block_reason_pfs"].notna(), "block_reason_pfs"].unique().tolist())
    reason_f = c4.selectbox("Reason", ["All"] + reasons, key="sif_reason")
    search = c5.text_input("Search", "", placeholder="Listing...", key="sif_search")

    filtered = s_items.copy()
    if status_f != "All":
        filtered = filtered[filtered["status_simple"] == status_f]
    if blk_f == "Blocked only":
        filtered = filtered[filtered["is_blocked_pfs"].fillna(False)]
    elif blk_f == "Pickable only":
        filtered = filtered[~filtered["is_blocked_pfs"].fillna(False)]
    if wh_f != "Both":
        filtered = filtered[filtered["warehouse"] == wh_f]
    if reason_f != "All":
        filtered = filtered[filtered["block_reason_pfs"] == reason_f]
    if search:
        mask = _str_contains_any(filtered, ["listing_id", "source_brand", "finished_good_name", "work_order_number"], search)
        filtered = filtered[mask]

    st.caption(f"{len(filtered):,} items")

    display = filtered[
        ["work_order_number", "listing_id", "finished_good_name", "source_brand",
         "warehouse", "status_simple", "processing_status", "block_reason_pfs",
         "original_request", "processed", "woi_processing_pct",
         "age_days_from_created", "ship_by"]
    ].rename(columns={
        "work_order_number": "WO", "listing_id": "Listing",
        "finished_good_name": "Item Name", "source_brand": "Brand",
        "warehouse": "WH", "status_simple": "Status",
        "processing_status": "Processing Status", "block_reason_pfs": "Reason",
        "original_request": "Orig", "processed": "Processed",
        "woi_processing_pct": "%", "age_days_from_created": "Age (d)",
        "ship_by": "Ship By",
    })
    st.dataframe(display, use_container_width=True, hide_index=True, height=600)


# ============================================================
# PO TAB
# ============================================================
def po_tab(df, wos):
    p_wos = wos[wos["source_category"] == "PO"].copy()
    p_items = df[df["source_category"] == "PO"].copy()

    view = st.radio(
        "View",
        ["📋 WO Level", "📄 Item Level"],
        horizontal=True,
        key="po_view",
        label_visibility="collapsed",
    )

    st.caption("Block flag: **14/21 days past later of WO/PO ship-by**")

    if view == "📋 WO Level":
        po_wo_view(p_wos, p_items)
    else:
        po_item_view(p_items)


def po_wo_view(p_wos, p_items):
    flag_options = ["All", "🔴 Blocked / Issue", "🟠 Partially Processed",
                    "🟡 Approaching ship-by", "🟢 On Track", "✅ Complete"]
    c1, c2, c3, c4 = st.columns([1.2, 1, 1, 2])
    flag_f = c1.selectbox("Worst Flag", flag_options, key="pf_flag")
    open_f = c2.selectbox("Has Open", ["All", "With Open", "All Closed"], index=1, key="pf_open")
    wh_f = c3.selectbox("Warehouse", ["Both", "Northampton", "Wroclaw"], key="pf_wh")
    search = c4.text_input("Search", "", placeholder="WO, PO#, brand...", key="pf_search")

    filtered = p_wos.copy()
    if flag_f != "All":
        filtered = filtered[filtered["worst_po_flag"] == flag_f]
    if open_f == "With Open":
        filtered = filtered[filtered["open_items"] > 0]
    elif open_f == "All Closed":
        filtered = filtered[filtered["open_items"] == 0]
    if wh_f != "Both":
        filtered = filtered[filtered["warehouse"] == wh_f]
    if search:
        mask = _str_contains_any(filtered, ["work_order_number", "po_number_raw", "top_brand"], search)
        filtered = filtered[mask]

    st.caption(f"{len(filtered)} of {len(p_wos)} WOs")

    display = filtered[
        ["work_order_number", "po_number_raw", "warehouse", "top_brand",
         "items", "open_items", "untouched", "orig", "processed", "pct",
         "worst_po_flag", "max_age", "earliest_ship"]
    ].rename(columns={
        "work_order_number": "WO", "po_number_raw": "PO #",
        "warehouse": "WH", "top_brand": "Brand",
        "items": "Items", "open_items": "Open", "untouched": "Untouched",
        "orig": "Orig", "processed": "Processed", "pct": "% Processed",
        "worst_po_flag": "Worst Flag", "max_age": "Age (d)",
        "earliest_ship": "Ship By",
    })

    event = st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        height=500,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "% Processed": st.column_config.ProgressColumn(
                "% Processed", min_value=0, max_value=100, format="%.1f%%"
            ),
            "Ship By": st.column_config.DateColumn("Ship By"),
        },
    )

    if event.selection.rows:
        selected_wo = display.iloc[event.selection.rows[0]]["WO"]
        po_wo_drilldown(selected_wo, p_items, p_wos)


def po_wo_drilldown(wo_id, p_items, p_wos):
    wo_row = p_wos[p_wos["work_order_number"] == wo_id].iloc[0]
    st.markdown("---")
    st.markdown(f"### WO {wo_id} — PO# {wo_row['po_number_raw']} · {wo_row['warehouse']} · {wo_row['worst_po_flag'] or ''}")
    st.caption(f"Top brand: {wo_row['top_brand']} · {wo_row['unique_listings']} unique listings")

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Items", _safe_int(wo_row["items"]))
    c2.metric("Open", _safe_int(wo_row["open_items"]))
    c3.metric("Untouched", _safe_int(wo_row["untouched"]))
    c4.metric("Orig Qty", f"{_safe_int(wo_row['orig']):,}")
    c5.metric("Processed", f"{_safe_int(wo_row['processed']):,}", f"{wo_row['pct']:.1f}%")
    c6.metric("Ship By", _safe_date_str(wo_row["earliest_ship"]))

    items = p_items[p_items["work_order_number"] == wo_id].copy()
    flag_counts = items["po_block_flag"].value_counts().to_dict()
    if flag_counts:
        breakdown = " · ".join([f"{k}: {v}" for k, v in flag_counts.items()])
        st.markdown(f"**Flag breakdown:** {breakdown}")

    st.markdown("#### Items in this PO WO")
    flag_options = ["All", "🔴 Blocked / Issue", "🟠 Partially Processed",
                    "🟡 Approaching ship-by", "🟢 On Track", "✅ Complete"]
    c1, c2 = st.columns([1, 2])
    flag_f = c1.selectbox("Flag", flag_options, key=f"pdf_flag_{wo_id}")
    search = c2.text_input("Search items", "", placeholder="Listing, brand...", key=f"pdf_search_{wo_id}")

    if flag_f != "All":
        items = items[items["po_block_flag"] == flag_f]
    if search:
        mask = _str_contains_any(items, ["listing_id", "source_brand", "finished_good_name"], search)
        items = items[mask]

    st.caption(f"{len(items)} items")
    items = items.sort_values("po_days_past_ref_ship_by", ascending=False)
    display = items[
        ["listing_id", "finished_good_name", "source_brand", "status_simple",
         "po_block_flag", "po_days_past_ref_ship_by",
         "original_request", "processed", "woi_processing_pct", "po_ref_ship_by_date"]
    ].rename(columns={
        "listing_id": "Listing", "finished_good_name": "Item Name",
        "source_brand": "Brand", "status_simple": "Status",
        "po_block_flag": "Flag", "po_days_past_ref_ship_by": "Days Past",
        "original_request": "Orig", "processed": "Processed",
        "woi_processing_pct": "%", "po_ref_ship_by_date": "Ref Ship-by",
    })
    st.dataframe(display, use_container_width=True, hide_index=True, height=400)


def po_item_view(p_items):
    flag_options = ["All", "🔴 Blocked / Issue", "🟠 Partially Processed",
                    "🟡 Approaching ship-by", "🟢 On Track", "✅ Complete"]
    c1, c2, c3, c4, c5 = st.columns(5)
    flag_f = c1.selectbox("Flag", flag_options, key="pif_flag")
    status_f = c2.selectbox("Status", ["All", "Open", "Closed"], index=1, key="pif_status")
    wh_f = c3.selectbox("Warehouse", ["Both", "Northampton", "Wroclaw"], key="pif_wh")
    pos = ["All"] + sorted(p_items["po_number_raw"].dropna().unique().tolist())
    po_f = c4.selectbox("PO #", pos, key="pif_po")
    search = c5.text_input("Search", "", placeholder="Listing, brand...", key="pif_search")

    filtered = p_items.copy()
    if flag_f != "All":
        filtered = filtered[filtered["po_block_flag"] == flag_f]
    if status_f != "All":
        filtered = filtered[filtered["status_simple"] == status_f]
    if wh_f != "Both":
        filtered = filtered[filtered["warehouse"] == wh_f]
    if po_f != "All":
        filtered = filtered[filtered["po_number_raw"] == po_f]
    if search:
        mask = _str_contains_any(filtered, ["listing_id", "source_brand", "finished_good_name", "work_order_number"], search)
        filtered = filtered[mask]

    st.caption(f"{len(filtered):,} items")
    filtered = filtered.sort_values("po_days_past_ref_ship_by", ascending=False)
    display = filtered[
        ["work_order_number", "po_number_raw", "listing_id", "finished_good_name",
         "source_brand", "warehouse", "status_simple", "po_block_flag",
         "po_days_past_ref_ship_by", "original_request", "processed",
         "woi_processing_pct", "ship_by"]
    ].rename(columns={
        "work_order_number": "WO", "po_number_raw": "PO #",
        "listing_id": "Listing", "finished_good_name": "Item Name",
        "source_brand": "Brand", "warehouse": "WH",
        "status_simple": "Status", "po_block_flag": "Flag",
        "po_days_past_ref_ship_by": "Days Past",
        "original_request": "Orig", "processed": "Processed",
        "woi_processing_pct": "%", "ship_by": "Ship By",
    })
    st.dataframe(display, use_container_width=True, hide_index=True, height=600)


# ============================================================
# MAIN
# ============================================================
def main():
    st.title("📊 WO Tracking Tool")
    st.caption("Storage and PO Work Order tracking · live Snowflake snapshot · auto-refresh every 30 min")

    try:
        with st.spinner("Loading WO data from Snowflake..."):
            df, wos, last_refresh = fetch_data()
    except Exception as e:
        st.error(f"Failed to fetch data from Snowflake: {e}")
        st.info("Check `.streamlit/secrets.toml` — see README for setup.")
        st.stop()

    sidebar(last_refresh)
    kpi_strip(df, wos)

    st.markdown("---")

    tab_storage, tab_po = st.tabs([
        f"📦 Storage WOs ({len(wos[wos['source_category']=='Storage'])})",
        f"🚚 PO WOs ({len(wos[wos['source_category']=='PO'])})",
    ])
    with tab_storage:
        storage_tab(df, wos)
    with tab_po:
        po_tab(df, wos)


if __name__ == "__main__":
    main()
