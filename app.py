"""
WO Tracking Tool — Streamlit App
================================
Tracks Storage and PO Work Orders with blocked / stalled detection.
Live Snowflake connection, cached 30 min, manual refresh available.

Tables are native st.dataframe — drag-select any cells / rows / columns and
press Ctrl+C to copy (clean, nothing in the way). Filtering is driven by the
panel above each table: Brand, Blocked/Flag, Reason, Ship By (from→to), Status,
and a multi-term Search. Plus a Columns picker, CSV + Excel export, a
"copy a few values" popover, and a one-click full-table copy.
"""

import hashlib
import io
import json
import re
import subprocess
import html as _html
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import numpy as np
import plotly.express as px
import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from datetime import datetime, date, timedelta
import time
from pathlib import Path
from warehouses import get_warehouses, warehouse_names, apply_warehouse_scope
from request_packs import (
    SHELF_WO_UPLOAD_COLS, RAISE_COLS,
    RAISE_KEY, RAISE_NONCE, CHASE_KEY, CHASE_NONCE,
    WO_COVERAGE_OPTIONS, WO_COVERAGE_ALL,
    add_raise_rows, add_chase_rows,
    render_requests_tab, po_coverage_mask,
    chase_rows_from_pos, raise_rows_from_gap,
    shelf_send_df,
)
try:
    from slack_messaging import (slack_messenger_button, slack_send_panel_button,
                                 slack_table_sender)
except Exception:  # feature is optional — never break the app if it's absent
    slack_messenger_button = None
    slack_send_panel_button = None
    slack_table_sender = None
_FLAG_GUIDE_ERROR = None
try:
    from flag_guide import render_flag_guide, render_flag_guide_inline, flag_action
except Exception as _flag_exc:  # keep the app up, but do not hide the failure
    render_flag_guide = None
    render_flag_guide_inline = None
    flag_action = None
    _FLAG_GUIDE_ERROR = f"{type(_flag_exc).__name__}: {_flag_exc}"


def _running_build_label() -> str:
    """Identify the code this process is running — not GitHub `main`.

    Streamlit Cloud often keeps serving an old image after a merge. A git SHA
    (when `.git` is present) plus a content hash of the two files that usually
    drift tells you whether a reboot actually picked up the change.
    """
    parts = []
    root = Path(__file__).resolve().parent
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root, stderr=subprocess.DEVNULL,
        ).decode().strip()
        if sha:
            parts.append(f"git {sha}")
    except Exception:
        pass
    try:
        h = hashlib.sha1()
        for name in ("app.py", "flag_guide.py", "request_packs.py"):
            p = root / name
            if p.exists():
                h.update(p.read_bytes())
        parts.append(f"files {h.hexdigest()[:7]}")
    except Exception:
        pass
    return " · ".join(parts) or "unknown"

# ============================================================
# CONFIG
# ============================================================
st.set_page_config(
    page_title="WO Tracking Tool",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
html, body, [data-testid="stAppViewContainer"] {
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
[data-testid="stMetric"] { padding: 0.4rem 0.6rem; }
[data-testid="stMetricValue"] { font-size: 1.3rem !important; line-height: 1.2 !important; }
[data-testid="stMetricLabel"] p { font-size: 0.8rem !important; color: #222 !important; }
[data-testid="stMetricDelta"] { font-size: 0.72rem !important; }
[data-testid="stMetricDelta"] svg { width: 0.7rem !important; height: 0.7rem !important; }

/* Filters / pickers: Streamlit defaults are small, grey, and clip long labels. */
[data-testid="stWidgetLabel"] p,
[data-testid="stWidgetLabel"] label {
  font-size: 0.92rem !important;
  font-weight: 600 !important;
  color: #161616 !important;
  opacity: 1 !important;
  white-space: normal !important;
  overflow: visible !important;
  line-height: 1.3 !important;
}
[data-testid="stCaptionContainer"],
[data-testid="stCaptionContainer"] p {
  font-size: 0.86rem !important;
  color: #333 !important;
  opacity: 1 !important;
}
[data-testid="stExpander"] summary,
[data-testid="stExpander"] summary p {
  font-size: 0.95rem !important;
  font-weight: 600 !important;
  color: #161616 !important;
}
div[data-baseweb="select"] {
  font-size: 0.92rem !important;
}
div[data-baseweb="tag"] span {
  font-size: 0.9rem !important;
  font-weight: 600 !important;
  color: #161616 !important;
}
div[data-baseweb="tag"] {
  max-width: none !important;
}
li[role="option"],
li[role="option"] span,
ul[role="listbox"] li {
  font-size: 0.95rem !important;
  font-weight: 500 !important;
  color: #161616 !important;
  line-height: 1.35 !important;
}
[data-testid="stRadio"] label p {
  font-size: 0.9rem !important;
  font-weight: 600 !important;
  color: #161616 !important;
  white-space: nowrap !important;
}
</style>
""", unsafe_allow_html=True)

QUERY_PATH = Path(__file__).parent / "queries" / "wo_tracker.sql"
PO_QUERY_PATH = Path(__file__).parent / "queries" / "po_tracker.sql"
PO_WO_AGG_PATH = Path(__file__).parent / "queries" / "po_wo_agg.sql"
PO_ITEM_GAP_PATH = Path(__file__).parent / "queries" / "po_item_wo_gap.sql"
UNPICK_DETAIL_PATH = Path(__file__).parent / "queries" / "wo_unpickable_detail.sql"
CATALOG_QUERY_PATH = Path(__file__).parent / "queries" / "catalog_lookup.sql"
CACHE_TTL_SECONDS = 1800  # 30 min
MAX_CATALOG_IDS = 500
NAV_CATALOG = "🔎 Catalogue Lookup"
NAV_REQUESTS = "📋 Requests"
SUGGESTED_SHIP_LEAD_DAYS = 7  # if the PO has no future ship date, suggest today + this
NO_WO_GRACE_DAYS = 0   # 0 = flag POs the moment they're placed (per Owen: get ahead early)
OV_AGING_DAYS = 21
OV_MAX_ROWS = 100
GENUINE_GAP_REASON = "Genuine gap — needs WO raised"


def _read_sql(path):
    """Load a .sql file. Reject Python accidentally pasted into queries/*.sql."""
    text = Path(path).read_text(encoding="utf-8-sig")
    stripped = text.lstrip()
    if stripped.startswith(('"""', "'''", "import ", "from ", "def ")):
        raise ValueError(
            f"{Path(path).name} looks like Python, not SQL. On GitHub, replace "
            f"queries/{Path(path).name} with the real SQL file — it must start "
            "with -- or WITH/SELECT, not a Python docstring."
        )
    if stripped[:1] in {'"', "'"}:
        raise ValueError(
            f"{Path(path).name} starts with a quote, so Snowflake cannot run it. "
            "Paste the SQL without wrapping the whole file in quotes."
        )
    return text


PO_REPORT_LAG = (
    "PO ordered / received figures are a **daily report**, not live Shelf. "
    "Shelf can already show receipts or a different current qty. "
    "Open the PO in Shelf before chasing."
)


def _warehouse_override():
    """Optional [warehouses] override from Streamlit secrets (a list of
    {id, name}). Lets someone add a warehouse via the Streamlit dashboard
    without editing code. Falls back to warehouses.py / the env var."""
    try:
        raw = st.secrets.get("warehouses")
    except Exception:
        return None
    if not raw:
        return None
    return [dict(w) for w in raw]


def _active_warehouses():
    return get_warehouses(_warehouse_override())


def _scope_sql(sql: str, override=None) -> str:
    """Scope a query's warehouse IN(...) clauses to the configured warehouses."""
    return apply_warehouse_scope(sql, override if override is not None else _warehouse_override())


def _override_from_scope(wh_scope):
    """Turn a cache-friendly ((id, name), ...) tuple into the override list."""
    if not wh_scope:
        return _warehouse_override()
    return [{"id": int(i), "name": n} for i, n in wh_scope]

FLAG_ORDER = [
    "🔴 Blocked / Issue", "🟠 Partially Processed",
    "🟡 Approaching ship-by", "🟢 On Track", "✅ Complete",
]

# Unified lifecycle status vocabulary (PO Details + future areas). Worst → best.
PO_STATUS_ORDER = [
    "🔴 Issue", "🟠 Partial", "🟡 Placed", "🟢 In progress", "✅ Complete",
]

# ============================================================
# COLUMN GLOSSARY
# ============================================================
# Plain-English meaning for every display column label used across the tables.
# Keyed by the *renamed* label that the user actually sees (not the raw SQL
# column). Drives two things:
#   1. the hover "?" tooltip on each table header (see render_table), and
#   2. the "📖 Column glossary" reference in the sidebar.
# Keep this in sync with §7 of WO_Tracking_Spec_and_Glossary.md.
COLUMN_GLOSSARY = {
    # --- identifiers ---
    "WO": "Work Order number — the WO id.",
    "WOI ID": "Work Order Item id — the unique key for a single line of a WO.",
    "PO #": "Purchase Order number. Copy this cell for the number only; use Shelf to open it.",
    "Shelf": "Opens this PO in Shelf. Does not copy — copy the PO # column for the number.",
    "Master ID": "Master / catalog identifier for the product.",
    "Listing": "Listing ID for the product on the marketplace.",
    "Item Name": "Product name (from the PFS table or the catalog).",
    "Source": "Where the WO came from: 'Storage', 'PO# <n>', or an IR number.",
    # --- context ---
    "WH": "Warehouse the WO / PO belongs to (see the Warehouse filter for the tracked set).",
    "Brand": "WO level: most common brand in the WO. Item level: the item's brand.",
    "Marketplace": "Marketplace the listing / item is on (Amazon UK, Amazon DE, …).",
    "Country": "Marketplace country.",
    "Pick Type": "Single or Bundle pick.",
    "Created By": "User who created the item ('amaczar_app' shows as Shelf).",
    "Last Edit By": "User who last edited the item.",
    # --- counts (WO level) ---
    "Items": "Number of WO items in this Work Order.",
    "Open": "Items still open (quantity remaining > 0).",
    "Untouched": "Items with 0 processed so far.",
    "Blocked (PFS)": "Count of items the warehouse system (PFS) marks Unpickable — "
                     "ground-truth Storage block.",
    "Top Reason": "Most common PFS block reason across the blocked items in this WO.",
    # --- status / flags ---
    "Status": "Open or Closed.",
    "Block Status": "Raw PFS processing status (Storage), e.g. 'Unpickable: ...'.",
    "Reason": "Clean PFS block reason (Storage): Replen Needed, No Inventory, "
              "Listing Failed, etc.",
    "Flag": "PO heuristic status for this item — 🔴 Blocked / 🟠 Partial / "
            "🟡 Approaching / 🟢 On Track / ✅ Complete.",
    "Worst Flag": "The single worst-case PO flag across the WO (drives row colour).",
    # --- quantities ---
    "Orig": "Original requested quantity.",
    "Orig Ordered": "Original ordered units on the PO (Snowflake ORDERED_UNITS — before revisions).",
    "Current Ordered": "Current / revised ordered units on the PO (Snowflake CURRENT_UNITS).",
    "Ordered": "On PO tables this is current ordered units. Orig Ordered is the original placement.",
    "Current": "Current requested quantity (after any revisions).",
    "Received": "Units received against the PO line or PO so far.",
    "Left": "Current ordered minus received (units still outstanding).",
    "Processed": "Quantity processed so far.",
    "% Processed": "Processed ÷ Original × 100, for the whole WO.",
    "%": "Processed ÷ Original × 100, for this item.",
    # --- pipeline ---
    "Ship Created": "Pipeline: a shipment has been created for the item.",
    "Shipped": "Pipeline: the item has departed (shipped out).",
    "Stowed": "Pipeline: item has landed in a real storage location "
              "(derived from the soft-deleted transient location — see spec §6.1).",
    # --- dates ---
    "Ship By": "Ship-by date (WO level: the earliest item's ship-by).",
    "Ref Ship-by": "PO reference ship-by — the LATER of the WO ship-by and the "
                   "PO requested ship date. Drives the PO flag.",
    "Req Ship Date": "PO requested ship date (from the Purchase Order).",
    "Req Delivery Date": "PO requested delivery date.",
    "Placed At": "When the Purchase Order was placed.",
    "Arrived At": "When the PO stock arrived.",
    "Created At": "When the WO item was created.",
    "Last Edit At": "When the WO item was last edited.",
    # --- aging ---
    "Age (d)": "Age in days since created (WO level: the oldest item).",
    "Days Overdue": "Days past the item's own ship-by date.",
    "Days Past": "Days past the PO reference ship-by (later of WO & PO ship-by).",
    # --- catalogue lookup ---
    "Listing ID": "Catalog listing id — the id used to raise a work order.",
    "SKU": "Marketplace primary id (seller SKU).",
    "FNSKU": "Amazon FNSKU (LISTING_MP_SECONDARY_ID). Blank on non-FBA.",
    "MPN": "Manufacturer part number.",
    "ASIN": "Amazon ASIN (LISTING_MP_PAGE_ID).",
    "Commingled": "FBA commingled vs stickered status (Amazon vs Pattern flag).",
    "Shippable": "Whether the listing is tagged shippable in the catalog.",
    "Listing Type": "Catalog listing type.",
    "DNO": "Do Not Order — latest catalog status-history flag.",
    "Active": "Whether the listing is currently active.",
    "Discontinued": "Whether the product is discontinued.",
    "Product Name": "Catalog product name.",
    "UPC": "UPC barcode.",
    "EAN": "EAN barcode.",
    "Can Expire": "Whether the product can expire (lot / expiry tracking).",
    "Wholesale": "Finance-approved wholesale price (with currency).",
    "MAP": "Minimum advertised price (with currency).",
    "Retail": "Retail price (with currency).",
    "MSRP": "Manufacturer suggested retail price (with currency).",
    "DNO Note": "Do-Not-Order note from the catalog DNO setting.",
    "DNO Reason": "Do-Not-Order reason code.",
    "Seller": "Marketplace seller name.",
    "Fulfillment": "Listing fulfillment type (FBA, FBM, …) or the PO fulfillment method.",
    "FBA": "Units raised on FBA work-order items (WO current qty). Not the PO report fulfillment method.",
    "FBB": "Units raised on FBB work-order items (WO current qty).",
    "FBM": "Units raised on FBM work-order items (WO current qty).",
    "ZFS": "Units raised on ZFS work-order items (WO current qty).",
    "OCT": "Units raised on OCT work-order items (WO current qty).",
    "To Stow": "Current ordered minus FBA/FBB/FBM/ZFS/OCT and any other WO qty — leftover with no marketplace WO.",
    "WO Type": "Work-order item type from Shelf (FBA, FBB, FBM, ZFS, OCT, …) — where those units are going.",
    "PO Qty": "Outstanding PO units (ordered − received) for that Master ID / SKU. "
              "Shown on every listing of the master in Catalogue Lookup.",
    "Request Amount": "Units to request on the Shelf WO upload / raise-WO pack. "
                      "Rows with qty 0 are kept for editing but are not Slacked or downloaded.",
    "Coverage": "Item-level WO coverage for that PO × Master ID: No WO, Partial WO, or full.",
    "WOs": "Count of work orders linked to this purchase order.",
    "PO Type": "Purchase order type from the PO report.",
    "PO WOs": "Count of work orders already linked to this PO (item-level rows inherit the PO count).",
}


def _safe_int(v, default=0):
    try:
        if v is None or pd.isna(v):
            return default
        return int(v)
    except (ValueError, TypeError):
        return default


_ORDERED_QTY_COLS = (
    "original_ordered_units", "original_ordered", "Orig Ordered",
    "ordered_units", "ordered", "current_units", "Current Ordered", "Ordered",
)


def _drop_zero_ordered(df):
    """Drop rows with no original or current ordered qty (noise, not a real line)."""
    if df is None or getattr(df, "empty", True):
        return df
    cols = [c for c in _ORDERED_QTY_COLS if c in df.columns]
    if not cols:
        return df
    mx = pd.concat(
        [pd.to_numeric(df[c], errors="coerce").fillna(0) for c in cols],
        axis=1,
    ).max(axis=1)
    return df.loc[mx > 0].copy()


def _safe_date_str(v):
    if v is None or pd.isna(v):
        return "—"
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    return str(v)


def _to_wo(v):
    try:
        return int(v)
    except (ValueError, TypeError):
        return v


def _reset_selection():
    st.session_state.pop("selected_storage_wo", None)
    st.session_state.pop("selected_po_wo", None)
    st.session_state.pop("selected_po_detail", None)
    st.session_state["grid_nonce"] = st.session_state.get("grid_nonce", 0) + 1


# ============================================================
# SNOWFLAKE CONNECTION (key-pair auth)
# ============================================================
def _load_private_key():
    key_pem = st.secrets["snowflake"]["private_key"].encode("utf-8")
    passphrase = st.secrets["snowflake"].get("private_key_passphrase", None)
    passphrase_bytes = passphrase.encode("utf-8") if passphrase else None
    p_key = serialization.load_pem_private_key(key_pem, password=passphrase_bytes, backend=default_backend())
    return p_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@st.cache_resource
def get_snowflake_connection():
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


def _sf_query(sql):
    """Run one read-only Snowflake statement; return a DataFrame."""
    conn = get_snowflake_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
        cols = [c[0].lower() for c in cur.description]
        return pd.DataFrame(rows, columns=cols)
    finally:
        cur.close()


def _build_wo_aggregates(df: pd.DataFrame) -> pd.DataFrame:
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
        earliest_ref_ship=("po_ref_ship_by_date", "min"),
        unique_listings=("listing_id", "nunique"),
        pfs_blocks=("is_blocked_pfs", lambda s: s.fillna(False).sum()),
    )
    g["pct"] = np.where(
        g["orig"].fillna(0) > 0,
        (g["processed"].fillna(0) * 100.0 / g["orig"].replace(0, np.nan)).round(1),
        0,
    )
    g["pct"] = pd.to_numeric(g["pct"], errors="coerce").fillna(0)

    flag_rank = {f: i for i, f in enumerate(FLAG_ORDER)}
    df_with_rank = df[df["po_block_flag"].notna()].copy()
    if not df_with_rank.empty:
        df_with_rank["_rank"] = df_with_rank["po_block_flag"].map(flag_rank)
        worst_idx = df_with_rank.groupby("work_order_number")["_rank"].idxmin()
        worst_per_wo = df_with_rank.loc[worst_idx, ["work_order_number", "po_block_flag"]]
        worst_map = dict(zip(worst_per_wo["work_order_number"], worst_per_wo["po_block_flag"]))
    else:
        worst_map = {}
    g["worst_po_flag"] = g["work_order_number"].map(worst_map)

    brand_counts = (
        df.dropna(subset=["source_brand"])
        .groupby(["work_order_number", "source_brand"])
        .size().reset_index(name="_cnt")
        .sort_values(["work_order_number", "_cnt"], ascending=[True, False])
        .drop_duplicates("work_order_number", keep="first")
    )
    brand_map = dict(zip(brand_counts["work_order_number"], brand_counts["source_brand"]))
    g["top_brand"] = g["work_order_number"].map(brand_map).fillna("")

    blocked = df[df["is_blocked_pfs"].fillna(False)]
    if not blocked.empty:
        reason_counts = (
            blocked.dropna(subset=["block_reason_pfs"])
            .groupby(["work_order_number", "block_reason_pfs"])
            .size().reset_index(name="_cnt")
            .sort_values(["work_order_number", "_cnt"], ascending=[True, False])
            .drop_duplicates("work_order_number", keep="first")
        )
        reason_map = dict(zip(reason_counts["work_order_number"], reason_counts["block_reason_pfs"]))
    else:
        reason_map = {}
    g["top_block_reason"] = g["work_order_number"].map(reason_map).fillna("")
    return g


def _usable_warehouse_name(name):
    """Pattern 3PLs only — the dim table also has Amazon FCs and liquidator rows."""
    n = (name or "").strip()
    if not n:
        return False
    low = n.lower()
    if low.startswith("amazon"):
        return False
    if "liquidator" in low:
        return False
    if n.startswith("["):
        return False
    return True


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def fetch_warehouse_dim():
    """Live warehouse id + name from Snowflake. Extra warehouses can be switched
    on in the UI without a code change. Falls back to warehouses.py on error."""
    sql = (
        "SELECT id, warehouse_name "
        "FROM ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WAREHOUSES "
        "WHERE warehouse_name IS NOT NULL "
        "ORDER BY warehouse_name"
    )
    conn = get_snowflake_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
    finally:
        cur.close()
    out = []
    for rid, name in rows:
        name = str(name).strip() if name is not None else ""
        if not _usable_warehouse_name(name):
            continue
        try:
            out.append({"id": int(rid), "name": name})
        except (TypeError, ValueError):
            continue
    return out


def _warehouse_picker_options():
    """Configured defaults plus Pattern 3PLs from the warehouse dim table."""
    configured = get_warehouses(_warehouse_override())
    try:
        dim = fetch_warehouse_dim()
    except Exception:
        dim = []
    by_name = {w["name"]: w for w in dim}
    for w in configured:
        by_name[w["name"]] = w
    return sorted(by_name.values(), key=lambda w: (w["name"] or "").lower())


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def fetch_data(wh_scope=()):
    sql = _scope_sql(_read_sql(QUERY_PATH), _override_from_scope(wh_scope))
    conn = get_snowflake_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
        cols = [c[0].lower() for c in cur.description]
        df = pd.DataFrame(rows, columns=cols)
    finally:
        cur.close()

    date_cols = [
        "po_ref_ship_by_date", "po_requested_ship_date",
        "po_requested_delivery_date", "po_placed_at", "po_arrived_at",
        "ship_by", "last_edit_at", "created_at",
    ]
    for col in date_cols:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

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

    if "woi_type" not in df.columns and "work_order_item_id" in df.columns:
        try:
            types = fetch_woi_types(wh_scope)
        except Exception:
            types = None
        if types is not None and not types.empty:
            df["work_order_item_id"] = pd.to_numeric(df["work_order_item_id"], errors="coerce")
            df = df.merge(types, on="work_order_item_id", how="left")

    wos = _build_wo_aggregates(df)
    return df, wos, datetime.now()


# ============================================================
# SEARCH + EXPORT HELPERS
# ============================================================
def _str_contains_any(df, cols, query):
    """Multi-term search. Split on commas / new lines / semicolons and match a
    row if ANY term appears in ANY column (paste several values at once)."""
    if not query or not str(query).strip():
        return pd.Series([True] * len(df), index=df.index)
    terms = [t.strip().lower() for t in re.split(r"[,\n;]+", str(query)) if t.strip()]
    if not terms:
        return pd.Series([True] * len(df), index=df.index)
    combined = pd.Series([""] * len(df), index=df.index)
    for c in cols:
        if c in df.columns:
            combined = combined + " " + df[c].astype(str).str.lower()
    mask = pd.Series([False] * len(df), index=df.index)
    for t in terms:
        mask = mask | combined.str.contains(re.escape(t), na=False, regex=True)
    return mask


def _df_to_csv_bytes(df):
    return df.to_csv(index=False).encode("utf-8")


@st.cache_data(show_spinner=False)
def _xlsx_bytes_from_csv(csv_text: str) -> bytes:
    buf = io.BytesIO()
    pd.read_csv(io.StringIO(csv_text), dtype=str).to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()


def _grid_key(base):
    return f"{base}_{st.session_state.get('grid_nonce', 0)}"


def _row_style(row):
    """Background colour by status for the coloured (non-clickable) tables."""
    flag = str(row.get("Flag", row.get("Worst Flag", row.get("Status", ""))))
    color = ""
    if "🔴" in flag:
        color = "rgba(239,68,68,0.13)"
    elif "🟠" in flag:
        color = "rgba(245,158,11,0.13)"
    elif "🟡" in flag:
        color = "rgba(234,179,8,0.10)"
    elif "🟢" in flag:
        color = "rgba(34,197,94,0.07)"
    elif "✅" in flag:
        color = "rgba(34,197,94,0.04)"
    else:
        bp = row.get("Blocked (PFS)")
        try:
            if bp is not None and not pd.isna(bp) and float(bp) > 0:
                color = "rgba(239,68,68,0.11)"
        except (ValueError, TypeError):
            pass
        if not color and "Unpickable" in str(row.get("Block Status", "")):
            color = "rgba(239,68,68,0.11)"
    return [f"background-color: {color}"] * len(row) if color else [""] * len(row)


_COPY_TABLE_TEMPLATE = """
<div style="font-family:sans-serif;">
  <button id="__BID__" style="width:100%;background:#5B4DF0;color:#fff;border:none;border-radius:6px;padding:8px 10px;font-size:13px;font-weight:600;cursor:pointer;">📋 Copy table</button>
  <textarea id="__BID___data" style="position:absolute;left:-9999px;top:-9999px;">__DATA__</textarea>
  <script>
  (function(){
    var b = document.getElementById("__BID__");
    var t = document.getElementById("__BID___data");
    if(!b) return;
    b.onclick = function(){
      t.select(); t.setSelectionRange(0, 999999999);
      try {
        navigator.clipboard.writeText(t.value).then(function(){
          b.textContent = "✅ Copied __NOTE__";
          setTimeout(function(){ b.textContent = "📋 Copy table"; }, 2500);
        });
      } catch(e) {
        document.execCommand("copy");
        b.textContent = "✅ Copied";
        setTimeout(function(){ b.textContent = "📋 Copy table"; }, 2500);
      }
    };
  })();
  </script>
</div>
"""


def copy_table_button(display, key):
    """One-click copy of the whole (visible) table as TSV with headers. Capped 5,000 rows."""
    cap = 5000
    d = display.head(cap)
    tsv = d.to_csv(index=False, sep="\t")
    note = f"{len(d)}x{len(d.columns)}" + (" (first 5k)" if len(display) > cap else "")
    bid = f"cbtn{abs(hash((key, len(display)))) % 10**9}"
    html_out = (
        _COPY_TABLE_TEMPLATE
        .replace("__BID__", bid)
        .replace("__DATA__", _html.escape(tsv))
        .replace("__NOTE__", note)
    )
    components.html(html_out, height=46)


def _plain_copy_value(col, value):
    """Value to put on the clipboard. PO # must be the number, never the Shelf URL."""
    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "<na>"):
        return ""
    if col in ("PO #", "Shelf") or col in PO_LINK_COLUMNS:
        m = re.search(r"/details/(\d+)", s)
        if m:
            return m.group(1)
        s = s.split(".")[0] if s.endswith(".0") and s[:-2].isdigit() else s
    return s


def copy_popover(display, id_cols, key):
    """Copy values from a column — all of them, or tick just a few. Or
    drag-select cells in the table and Ctrl+C to copy a row/column straight."""
    present = [c for c in id_cols if c in display.columns]
    if not present:
        return
    with st.popover("📋 Copy items", use_container_width=True):
        st.caption(
            "Pick a column, optionally tick just the values you want, then use the copy "
            "icon on the block. PO # copies the number only (not the Shelf URL). "
            "Or drag-select PO # cells in the table and press Ctrl+C."
        )
        c = st.selectbox("Column", present, key=f"{key}_col")
        vals = [_plain_copy_value(c, v) for v in display[c].tolist()]
        vals = [v for v in vals if v]
        seen = set()
        uniq = [x for x in vals if not (x in seen or seen.add(x))]
        picked = st.multiselect(
            "Pick a few (optional — empty = all)", uniq[:1000],
            key=f"{key}_pick", placeholder="All values",
        )
        out = picked if picked else uniq
        st.caption(f"{len(out):,} value(s)")
        st.code("\n".join(out[:5000]) if out else "—", language="text")


def column_picker(all_labels, key, default_labels=None, required=()):
    """👁 Columns — choose which columns show. Required columns always kept."""
    default_labels = default_labels if default_labels is not None else all_labels
    with st.expander("👁 Columns"):
        chosen = st.multiselect(
            "Show columns", options=all_labels, default=default_labels,
            key=key, label_visibility="collapsed",
        )
    chosen_set = set(chosen) | set(required)
    ordered = [c for c in all_labels if c in chosen_set]
    return ordered if ordered else list(all_labels)


def table_toolbar(display, *, key, file_stem, id_cols, count_label):
    """Count + CSV + Excel + copy-items popover + copy-table button.

    Excel and Copy-table are built only when the user ticks them on — generating
    them on every rerun is what made big tables (e.g. PO items) crawl."""
    ts = datetime.now().strftime("%Y%m%d")
    a, b, c, d, e = st.columns([2.6, 1, 1.1, 1.2, 1.4])
    a.caption(count_label)

    # CSV is cheap, keep it one-click.
    b.download_button("📥 CSV", _df_to_csv_bytes(display),
                      file_name=f"{file_stem}_{ts}.csv", mime="text/csv",
                      use_container_width=True, key=f"{key}_csv")

    # Excel (openpyxl) is slow — only build it when asked.
    with c:
        if len(display) > 20000:
            st.caption("Excel: filter <20k")
        elif st.checkbox("📊 Excel", key=f"{key}_xlsx_go", help="Build an .xlsx to download"):
            st.download_button("⬇ Download", _xlsx_bytes_from_csv(display.to_csv(index=False)),
                               file_name=f"{file_stem}_{ts}.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               use_container_width=True, key=f"{key}_xlsx")
    with d:
        copy_popover(display, id_cols, key=f"{key}_cp")

    # Copy-table injects the whole table into the page — only render on demand.
    with e:
        if st.checkbox("📋 Copy table", key=f"{key}_ct_go", help="Show a copy-to-clipboard button"):
            copy_table_button(display, key=f"{key}_ct")


# ============================================================
# SHARED FILTER PANEL
# ============================================================
def filter_panel(df, key, *, brand_col=None, ship_col=None, ship_fallback_col=None,
                 ship_label="Ship by", reason_col=None,
                 blocked_kind=None, flag_col=None, status_kind=None, search_cols=None):
    """Standard filters in an expander. Returns the filtered DataFrame.
    blocked_kind: 'wo' (pfs_blocks count) or 'item' (is_blocked_pfs bool) or None.
    flag_col: a column to filter via the 5-way Flag dropdown (PO) or None.
    status_kind: 'wo' (open_items) or 'item' (status_simple) or None."""
    out = df.copy()
    with st.expander("🔎 Filters", expanded=True):
        c1, c2, c3, c4 = st.columns(4)

        # Brand
        if brand_col and brand_col in out.columns:
            brands = sorted([b for b in out[brand_col].dropna().astype(str).unique() if b and b != "nan"])
            pick = c1.multiselect("Brand", brands, key=f"{key}_brand", placeholder="All brands")
            if pick:
                out = out[out[brand_col].astype(str).isin(pick)]

        # Blocked toggle (Storage) or Flag dropdown (PO)
        if flag_col and flag_col in out.columns:
            avail = [f for f in FLAG_ORDER if (out[flag_col] == f).any()]
            fp = c2.selectbox("Flag", ["All"] + avail, key=f"{key}_flag")
            if fp != "All":
                out = out[out[flag_col] == fp]
        elif blocked_kind == "wo":
            bp = c2.selectbox("Blocked", ["All", "Blocked", "Not blocked"], key=f"{key}_blk")
            if bp == "Blocked":
                out = out[out["pfs_blocks"] > 0]
            elif bp == "Not blocked":
                out = out[out["pfs_blocks"] == 0]
        elif blocked_kind == "item":
            bp = c2.selectbox("Blocked", ["All", "Blocked", "Not blocked"], key=f"{key}_blk")
            if bp == "Blocked":
                out = out[out["is_blocked_pfs"].fillna(False)]
            elif bp == "Not blocked":
                out = out[~out["is_blocked_pfs"].fillna(False)]

        # Reason (where present)
        if reason_col and reason_col in out.columns and out[reason_col].notna().any():
            reasons = sorted([r for r in out[reason_col].dropna().astype(str).unique() if r and r != "nan"])
            if reasons:
                rp = c3.multiselect("Reason", reasons, key=f"{key}_reason", placeholder="All reasons")
                if rp:
                    out = out[out[reason_col].astype(str).isin(rp)]

        # Status
        if status_kind == "item":
            sp = c4.selectbox("Status", ["Open", "All", "Closed"], key=f"{key}_status")
            if sp == "Open":
                out = out[out["status_simple"] == "Open"]
            elif sp == "Closed":
                out = out[out["status_simple"] == "Closed"]
        elif status_kind == "wo":
            sp = c4.selectbox("Show", ["With Open", "All", "Closed"], key=f"{key}_status")
            if sp == "With Open":
                out = out[out["open_items"] > 0]
            elif sp == "Closed":
                out = out[out["open_items"] == 0]

        # Ship-by range + Search
        d1, d2, d3 = st.columns([1, 1, 2])
        if ship_col and ship_col in out.columns:
            sfrom = d1.date_input(f"{ship_label} — from", value=None, key=f"{key}_sfrom", format="YYYY-MM-DD")
            sto = d2.date_input(f"{ship_label} — to", value=None, key=f"{key}_sto", format="YYYY-MM-DD")
            ship_dt = pd.to_datetime(out[ship_col], errors="coerce")
            # Many POs have no ship_by — fall back to the reference ship-by date.
            if ship_fallback_col and ship_fallback_col in out.columns:
                ship_dt = ship_dt.fillna(pd.to_datetime(out[ship_fallback_col], errors="coerce"))
            if sfrom:
                out = out[ship_dt >= pd.Timestamp(sfrom)]
                ship_dt = ship_dt.loc[out.index]
            if sto:
                out = out[ship_dt <= (pd.Timestamp(sto) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1))]
        if search_cols:
            q = d3.text_input("Search", "", key=f"{key}_search",
                              placeholder="paste several — comma / new line = match any")
            if q:
                out = out[_str_contains_any(out, search_cols, q)]
    return out


def hide_unlisted(df, key):
    """Hide item rows with no Listing — their brand mapping is unreliable.
    A checkbox restores them if needed."""
    if "listing_id" not in df.columns:
        return df
    lid = df["listing_id"].astype(str).str.strip().str.lower()
    has = df["listing_id"].notna() & lid.ne("") & lid.ne("none") & lid.ne("nan")
    n_hidden = int((~has).sum())
    if n_hidden == 0:
        return df
    show = st.checkbox(f"Show {n_hidden:,} item(s) with no Listing (brand may be unreliable)",
                       value=False, key=key)
    return df if show else df[has]


# ============================================================
# NATIVE TABLE RENDERER
# ============================================================
COLOR_ROW_LIMIT = 1500  # above this many rows, skip per-row colouring (Styler is slow)
_STREAMLIT_QTY_COLS = {
    "FBA", "FBB", "FBM", "ZFS", "OCT", "To Stow", "To stow",
    "Orig Ordered", "Current Ordered", "Ordered", "Received", "Left",
    "On Order", "Outstanding", "WO Qty", "WOs", "Lines",
}


def _frame_for_streamlit(df):
    """PyArrow rejects mixed numeric/string columns (e.g. FBA qty + the label 'FBA')."""
    if df is None or getattr(df, "empty", True):
        return df
    out = df.copy()
    if out.columns.duplicated().any():
        out = out.loc[:, ~out.columns.duplicated()].copy()
    for c in list(out.columns):
        if c in _STREAMLIT_QTY_COLS or str(c).endswith("_units") or str(c).startswith("wo_"):
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0).astype("int64")
            continue
        s = out[c]
        if getattr(s, "dtype", None) is not None and getattr(s.dtype, "kind", "") == "O":
            as_num = pd.to_numeric(s, errors="coerce")
            nonempty = s.notna() & ~s.astype(str).str.strip().str.lower().isin(
                ("", "nan", "none", "<na>")
            )
            if bool((nonempty & as_num.isna()).any()):
                out[c] = s.fillna("").astype(str).replace({"nan": "", "None": "", "<NA>": ""})
    return out


def render_table(display, *, key, selectable=False, select_col="WO", pct_cols=(), numpct_cols=(),
                 date_cols=(), datetime_cols=(), pin_cols=(), color_rows=False, height=480,
                 link_cols=None, slack=True, slack_label_cols=None, slack_whole=True,
                 slack_filename="wo_tracker_table.csv", multi_select=False):
    """Native st.dataframe. Drag-select cells/rows/cols + Ctrl+C copies cleanly.
    Selectable tables show a tick column; returns the ticked WO (or None).
    multi_select=True returns the ticked rows as a DataFrame (empty if none).

    Every column also gets a hover "?" tooltip pulled from COLUMN_GLOSSARY, so
    going to a column header tells you what it actually means.

    link_cols: {column_name: display_text} — render that column (which holds URLs)
    as a clickable link showing display_text (e.g. "↗ Shelf").

    PO # stays a plain number so Ctrl+C copies 135378, not the Shelf URL. A
    separate Shelf column holds the link."""
    link_cols = link_cols or {}
    colcfg = {}
    pin_set = set(pin_cols)
    po_cols = [c for c in display.columns if c in PO_LINK_COLUMNS]
    for c in display.columns:
        help_txt = COLUMN_GLOSSARY.get(c)  # None = no tooltip, which is fine
        if c in po_cols:
            # Plain number — Ctrl+C / Copy items copy 135378, not the URL.
            kwargs = {"help": help_txt}
            if c in pin_set:
                kwargs["pinned"] = "left"
            try:
                colcfg[c] = st.column_config.TextColumn(c, **kwargs)
            except TypeError:
                colcfg[c] = st.column_config.TextColumn(c, help=help_txt)
        elif c in link_cols:
            colcfg[c] = st.column_config.LinkColumn(c, help=help_txt, display_text=link_cols[c])
        elif c in pct_cols:
            colcfg[c] = st.column_config.ProgressColumn(
                c, help=help_txt, min_value=0, max_value=100, format="%.1f%%")
        elif c in numpct_cols:
            colcfg[c] = st.column_config.NumberColumn(c, help=help_txt, format="%.1f%%")
        elif c in date_cols:
            colcfg[c] = st.column_config.DateColumn(c, help=help_txt, format="YYYY-MM-DD")
        elif c in datetime_cols:
            colcfg[c] = st.column_config.DatetimeColumn(c, help=help_txt, format="YYYY-MM-DD HH:mm")
        elif c in pin_set:
            try:
                colcfg[c] = st.column_config.Column(c, help=help_txt, pinned="left")
            except TypeError:  # older Streamlit without pinned=
                colcfg[c] = st.column_config.Column(c, help=help_txt)
        elif help_txt:
            colcfg[c] = st.column_config.Column(c, help=help_txt)

    def _slack(df):
        # Universal "📤 Send to Slack" control under every table (whole table or a
        # single line, attached as CSV). No-op if the feature module isn't loaded.
        if slack and slack_table_sender is not None:
            slack_table_sender(key, df, label_cols=slack_label_cols,
                               filename=slack_filename, allow_whole=slack_whole)

    # PO # stays the number (copyable). Add a Shelf link column for click-through.
    render_df = display
    if po_cols:
        render_df = display.copy()
        first = po_cols[0]
        if SHELF_COL not in render_df.columns:
            loc = list(render_df.columns).index(first) + 1
            render_df.insert(loc, SHELF_COL, _po_link_series(render_df[first]))
            colcfg[SHELF_COL] = st.column_config.LinkColumn(
                SHELF_COL,
                help=COLUMN_GLOSSARY.get(SHELF_COL),
                display_text="Open",
            )
    render_df = _frame_for_streamlit(render_df)

    if selectable or multi_select:
        mode = "multi-row" if multi_select else "single-row"
        event = st.dataframe(
            render_df, use_container_width=True, hide_index=True, height=height,
            on_select="rerun", selection_mode=mode, column_config=colcfg, key=key,
        )
        _slack(display)
        rows = []
        if event is not None and getattr(event, "selection", None) is not None:
            rows = list(event.selection.rows or [])
        if multi_select:
            return display.iloc[rows].copy() if rows else display.iloc[0:0].copy()
        if rows and select_col in display.columns:
            return display.iloc[rows[0]][select_col]
        return None

    data = render_df
    if color_rows:
        if len(render_df) <= COLOR_ROW_LIMIT:
            try:
                data = render_df.style.apply(_row_style, axis=1)
            except Exception:
                data = render_df
        else:
            st.caption(
                f"Row colours are hidden above {COLOR_ROW_LIMIT:,} rows to keep the table fast — "
                "filter or search to narrow it down and the colours come back."
            )
    st.dataframe(data, use_container_width=True, hide_index=True, height=height, column_config=colcfg)
    _slack(display)
    return None


# ============================================================
# SIDEBAR
# ============================================================
def sidebar(last_refresh):
    st.sidebar.title("📊 WO Tracker")
    st.sidebar.caption("Snowflake snapshot · two clocks")
    if last_refresh:
        delta_min = (datetime.now() - last_refresh).total_seconds() / 60
        st.sidebar.metric("WO snapshot", last_refresh.strftime("%H:%M:%S"),
                          f"{delta_min:.0f} min ago · ~30 min cache")
    po_stamp = _po_extract_stamp()
    if po_stamp:
        st.sidebar.caption(f"PO report · {po_stamp} · daily extract, not live Shelf.")
    else:
        st.sidebar.caption("PO ordered/received is a daily report. Shelf can already be ahead.")
    if st.sidebar.button("🔄 Refresh now", use_container_width=True, type="primary"):
        fetch_data.clear()
        fetch_po_data.clear()
        fetch_po_extract_asof.clear()
        fetch_po_item_wo_gap.clear()
        fetch_po_wo_agg.clear()
        fetch_wo_unpickable_detail.clear()
        fetch_catalog_lookup.clear()
        fetch_warehouse_dim.clear()
        _reset_selection()
        st.rerun()
    wo_s = st.session_state.get("_wo_fetch_s")
    if wo_s is not None:
        st.sidebar.caption(f"Last WO query {wo_s:.1f}s" + (" (cached)" if wo_s < 0.4 else ""))
    cat_s = st.session_state.get("_cat_fetch_s")
    if cat_s is not None:
        st.sidebar.caption(f"Last catalogue query {cat_s:.1f}s")
    _scope = _wh_scope_arg()
    if _scope:
        _wh_list = ", ".join(f"{n} ({i})" for i, n in _scope)
    else:
        _wh_list = ", ".join(f"{w['name']} ({w['id']})" for w in _active_warehouses())
    st.sidebar.markdown(
        "**Coverage**\n\n"
        "- All WOs created since Jan 1 this year\n"
        f"- {_wh_list}\n"
        "- Catalogue Lookup is live catalog search (not warehouse-filtered)\n"
        "- Requests (raise-WO pack / chase list) is session-only — it does not create or complete WOs in Shelf\n"
        "- Work orders auto-refresh about every 30 min; PO ordered/received is a daily report\n"
    )
    st.sidebar.markdown("---")
    st.sidebar.markdown(
        "**Table tips**\n\n"
        "- Drag-select cells / a row / a column, then Ctrl+C to copy\n"
        "- PO # copies the number; click **Shelf → Open** to open it in Shelf\n"
        "- Click a column header to sort\n"
        "- 👁 Columns to show/hide · 📋 to copy lists\n"
        "- Paste several values in Search to match any\n"
        "- Tick a WO row, then press **Open WO** to drill in\n"
    )
    st.sidebar.markdown("---")
    st.sidebar.markdown(
        "**Block flag thresholds (PO)**\n\n"
        "- 🔴 21+ days past ship-by, 0% processed\n"
        "- 🟠 14+ days past ship-by, partial\n"
        "- 🟡 0–13 days past\n"
        "- 🟢 Before ship-by\n"
    )
    st.sidebar.markdown("---")
    with st.sidebar.expander("📖 Column glossary"):
        st.caption(
            "What every column means. Tip: in any table you can also hover the "
            "**?** on a column header to see this without leaving the table."
        )
        gloss_df = pd.DataFrame(
            [(label, meaning) for label, meaning in COLUMN_GLOSSARY.items()],
            columns=["Column", "Meaning"],
        )
        st.dataframe(
            gloss_df, hide_index=True, use_container_width=True, height=320,
            column_config={
                "Column": st.column_config.TextColumn("Column", width="small"),
                "Meaning": st.column_config.TextColumn("Meaning", width="large"),
            },
        )


# ============================================================
# KPI STRIPS
# ============================================================
def kpi_strip(df, wos, warehouse_label):
    storage_wos = wos[wos["source_category"] == "Storage"]
    po_wos = wos[wos["source_category"] == "PO"]
    ir_wos = wos[wos["source_category"] == "IR"]
    storage_items = df[df["source_category"] == "Storage"]
    po_items = df[df["source_category"] == "PO"]

    total_orig = int(df["original_request"].fillna(0).sum())
    total_current = int(df["current_request"].fillna(0).sum())
    total_processed = int(df["processed"].fillna(0).sum())
    pct_processed = (total_processed / total_orig * 100) if total_orig > 0 else 0
    unique_pos = int(po_items["po_number_raw"].dropna().nunique())

    try:
        date_range = f"{df['created_at'].min().strftime('%Y-%m-%d')} → {df['created_at'].max().strftime('%Y-%m-%d')}"
    except Exception:
        date_range = "—"
    st.caption(f"📅 **Coverage**: {date_range} · **{unique_pos:,} unique POs** · Warehouse: **{warehouse_label}**")

    st.markdown("#### 📊 Overall Totals (Storage + PO + IR)")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Work Orders", f"{len(wos):,}", f"{len(storage_wos)} S · {len(po_wos)} PO · {len(ir_wos)} IR")
    c2.metric("WO Items", f"{len(df):,}", f"{len(storage_items):,} S · {len(po_items):,} PO")
    c3.metric("Original Request", f"{total_orig:,}")
    c4.metric("Current Request", f"{total_current:,}")
    c5.metric("Processed", f"{total_processed:,}", f"{pct_processed:.1f}%")


def storage_kpi_strip(s_items, s_wos):
    total_orig = int(s_items["original_request"].fillna(0).sum())
    total_current = int(s_items["current_request"].fillna(0).sum())
    total_processed = int(s_items["processed"].fillna(0).sum())
    pct_processed = (total_processed / total_orig * 100) if total_orig > 0 else 0
    open_items = int((s_items["status_simple"] == "Open").sum())
    closed_items = int((s_items["status_simple"] == "Closed").sum())
    blocked_items = s_items[s_items["is_blocked_pfs"].fillna(False)]
    total_blocked = len(blocked_items)
    pickable = open_items - total_blocked

    st.markdown("#### 📦 Storage Volume")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Storage WOs", f"{len(s_wos):,}")
    c2.metric("Storage Items", f"{len(s_items):,}", f"{open_items:,} Open · {closed_items:,} Closed")
    c3.metric("Original Qty", f"{total_orig:,}")
    c4.metric("Current Qty", f"{total_current:,}")
    c5.metric("Processed Qty", f"{total_processed:,}", f"{pct_processed:.1f}%")

    st.markdown(f"#### 🚫 Storage Block Reasons (PFS) — {total_blocked:,} blocked · {pickable:,} pickable")
    reason_counts = blocked_items["block_reason_pfs"].dropna().value_counts()
    top4 = list(reason_counts.head(4).items())
    while len(top4) < 4:
        top4.append(("—", 0))
    other_count = int(reason_counts.iloc[4:].sum()) if len(reason_counts) > 4 else 0
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric(top4[0][0], int(top4[0][1]))
    c2.metric(top4[1][0], int(top4[1][1]))
    c3.metric(top4[2][0], int(top4[2][1]))
    c4.metric(top4[3][0], int(top4[3][1]))
    c5.metric("Other reasons", other_count)


def po_kpi_strip(p_items, p_wos):
    total_orig = int(p_items["original_request"].fillna(0).sum())
    total_current = int(p_items["current_request"].fillna(0).sum())
    total_processed = int(p_items["processed"].fillna(0).sum())
    pct_processed = (total_processed / total_orig * 100) if total_orig > 0 else 0
    unique_pos = int(p_items["po_number_raw"].dropna().nunique())
    open_items = int((p_items["status_simple"] == "Open").sum())
    closed_items = int((p_items["status_simple"] == "Closed").sum())

    blocked = int((p_items["po_block_flag"] == "🔴 Blocked / Issue").sum())
    partial = int((p_items["po_block_flag"] == "🟠 Partially Processed").sum())
    approaching = int((p_items["po_block_flag"] == "🟡 Approaching ship-by").sum())
    ontrack = int((p_items["po_block_flag"] == "🟢 On Track").sum())
    complete = int((p_items["po_block_flag"] == "✅ Complete").sum())

    st.markdown("#### 🚚 PO Volume")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("PO WOs", f"{len(p_wos):,}", f"{unique_pos:,} unique POs")
    c2.metric("PO Items", f"{len(p_items):,}", f"{open_items:,} Open · {closed_items:,} Closed")
    c3.metric("Original Qty", f"{total_orig:,}")
    c4.metric("Current Qty", f"{total_current:,}")
    c5.metric("Processed Qty", f"{total_processed:,}", f"{pct_processed:.1f}%")

    st.markdown(f"#### 🚦 PO Block Flag Breakdown — {blocked + partial:,} items need attention")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Blocked 🔴", f"{blocked:,}", "21+ days, 0%")
    c2.metric("Partial 🟠", f"{partial:,}", "14+ days, partial")
    c3.metric("Approaching 🟡", f"{approaching:,}", "0–13 days past")
    c4.metric("On Track 🟢", f"{ontrack:,}", "Before ship-by")
    c5.metric("Complete ✅", f"{complete:,}", "Fully processed")


# ============================================================
# STORAGE TAB
# ============================================================
def storage_tab(df, wos):
    s_wos = wos[wos["source_category"] == "Storage"].copy()
    s_items = df[df["source_category"] == "Storage"].copy()

    sel = st.session_state.get("selected_storage_wo")
    if sel and sel in s_wos["work_order_number"].values:
        storage_wo_drilldown(sel, s_items, s_wos)
        return

    view = st.radio("View", ["📋 WO Level", "📄 Item Level"], horizontal=True,
                    key="storage_view", label_visibility="collapsed")
    st.caption("Blocked detection: **PFS table** · 💡 Tick a row then press **Open WO** · drag-select cells + Ctrl+C to copy · cards react to the filters")
    if view == "📋 WO Level":
        storage_wo_view(s_wos, s_items)
    else:
        storage_item_view(s_items, s_wos)


def storage_wo_view(s_wos, s_items):
    filtered = filter_panel(
        s_wos, "fp_swo", brand_col="top_brand", ship_col="earliest_ship",
        reason_col="top_block_reason", blocked_kind="wo", status_kind="wo",
        search_cols=["work_order_number", "top_brand", "top_block_reason"],
    )
    filtered = filtered.sort_values("pfs_blocks", ascending=False)
    fitems = s_items[s_items["work_order_number"].isin(filtered["work_order_number"])]
    storage_kpi_strip(fitems, filtered)
    if render_flag_guide_inline is not None and "top_block_reason" in filtered.columns:
        render_flag_guide_inline(set(filtered["top_block_reason"].dropna()))
    display = filtered[
        ["work_order_number", "earliest_ship", "warehouse", "top_brand", "items", "open_items",
         "pfs_blocks", "top_block_reason", "orig", "processed", "pct", "max_age"]
    ].rename(columns={
        "work_order_number": "WO", "warehouse": "WH", "top_brand": "Brand",
        "items": "Items", "open_items": "Open", "pfs_blocks": "Blocked (PFS)",
        "top_block_reason": "Top Reason", "orig": "Orig", "processed": "Processed",
        "pct": "% Processed", "max_age": "Age (d)", "earliest_ship": "Ship By",
    })
    if flag_action is not None and "Top Reason" in display.columns:
        display["What to do"] = display["Top Reason"].map(flag_action)
    cols = column_picker(list(display.columns), key="cols_swo", required=["WO"])
    display = display[cols]

    table_toolbar(display, key="tb_swo", file_stem="storage_wos",
                  id_cols=["WO"], count_label=f"{len(display)} of {len(s_wos)} WOs")
    sel_wo = render_table(
        display, key=_grid_key("grid_swo"), selectable=True,
        pct_cols=["% Processed"], date_cols=["Ship By"], pin_cols=["WO"], height=500,
    )
    if sel_wo is not None:
        if st.button(f"➡ Open WO {_to_wo(sel_wo)}", type="primary", key="open_swo", use_container_width=True):
            st.session_state.selected_storage_wo = _to_wo(sel_wo)
            st.rerun()


def render_root_cause_detail(items, key):
    """Per-item root-cause drill-down for the blocked items in a WO: exact block
    status, marketplace, active unpickable reason(s) + resolvable qty, and the
    recommended action. Degrades gracefully if the deeper query is unavailable."""
    if items is None or len(items) == 0 or "is_blocked_pfs" not in items.columns:
        return
    blk = items[items["is_blocked_pfs"].fillna(False)].copy()
    if blk.empty:
        return
    with st.expander(f"🔎 Root-cause detail ({len(blk)} blocked item(s))"):
        base_cols = [c for c in ["work_order_item_id", "source_brand", "block_reason_pfs",
                                 "marketplace", "marketplace_country", "pick_type",
                                 "processing_status", "listing_id", "master_id"] if c in blk.columns]
        d = blk[base_cols].copy()

        det = fetch_wo_unpickable_detail()
        if det is not None and "woi_id" in det.columns and "work_order_item_id" in d.columns:
            keep = [c for c in ["woi_id", "active_reasons", "resolvable_qty"] if c in det.columns]
            d = d.merge(det[keep], left_on="work_order_item_id", right_on="woi_id",
                        how="left").drop(columns=["woi_id"], errors="ignore")
        else:
            st.caption("Deeper unpickable detail (resolvable qty / exact reasons) is unavailable — "
                       "showing the block attributes from the main feed only.")

        if flag_action is not None and "block_reason_pfs" in d.columns:
            d["What to do"] = d["block_reason_pfs"].map(flag_action)

        if {"marketplace", "marketplace_country"}.issubset(d.columns):
            d["Marketplace"] = d.apply(
                lambda r: f"{'' if pd.isna(r['marketplace']) else r['marketplace']}"
                          + (f" ({r['marketplace_country']})"
                             if pd.notna(r.get("marketplace_country")) and str(r.get("marketplace_country")).strip()
                             else ""), axis=1)
            d = d.drop(columns=["marketplace", "marketplace_country"])
        elif "marketplace" in d.columns:
            d = d.rename(columns={"marketplace": "Marketplace"})

        d = d.rename(columns={
            "work_order_item_id": "WOI ID", "source_brand": "Brand", "block_reason_pfs": "Reason",
            "pick_type": "Pick Type", "processing_status": "Raw Block Status",
            "active_reasons": "Active Reasons", "resolvable_qty": "Resolvable Qty",
            "listing_id": "Listing", "master_id": "Master ID"})
        order = [c for c in ["WOI ID", "Brand", "Reason", "Marketplace", "Active Reasons",
                             "Resolvable Qty", "Raw Block Status", "Pick Type", "Listing",
                             "Master ID", "What to do"] if c in d.columns]
        st.dataframe(d[order], use_container_width=True, hide_index=True)
        st.caption("Active unpickable rows only (deleted_at IS NULL). Resolvable Qty is blank for "
                   "No-Inventory / Expired — nothing in the warehouse resolves those.")


def storage_wo_drilldown(wo_id, s_items, s_wos):
    wo_row = s_wos[s_wos["work_order_number"] == wo_id].iloc[0]
    top1, top2 = st.columns([1, 5])
    if top1.button("← Back to list", use_container_width=True, key="back_swo"):
        _reset_selection()
        st.rerun()
    top2.markdown(f"### WO {wo_id} — Storage · {wo_row['warehouse']}")
    st.caption(f"Top brand: **{wo_row['top_brand']}** · {wo_row['unique_listings']} unique listings")

    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    c1.metric("Items", _safe_int(wo_row["items"]))
    c2.metric("Open", _safe_int(wo_row["open_items"]))
    c3.metric("Blocked (PFS)", _safe_int(wo_row["pfs_blocks"]))
    c4.metric("Orig Qty", f"{_safe_int(wo_row['orig']):,}")
    c5.metric("Processed", f"{_safe_int(wo_row['processed']):,}", f"{wo_row['pct']:.1f}%")
    c6.metric("Stowed", f"{_safe_int(wo_row['stowed']):,}")
    c7.metric("Max Age", f"{_safe_int(wo_row['max_age'])}d")

    items = s_items[s_items["work_order_number"] == wo_id].copy()
    st.markdown("---")
    st.markdown(f"#### 📄 Items in WO {wo_id}")
    filtered = filter_panel(
        items, f"fp_swo_items_{wo_id}", brand_col="source_brand", ship_col="ship_by",
        reason_col="block_reason_pfs", blocked_kind="item", status_kind="item",
        search_cols=["work_order_item_id", "listing_id", "source_brand", "finished_good_name"],
    )
    filtered = hide_unlisted(filtered, f"hl_swo_items_{wo_id}")
    render_root_cause_detail(filtered, f"rc_swo_{wo_id}")
    display = filtered[
        ["work_order_item_id", "ship_by", "created_at", "last_edit_at", "master_id",
         "listing_id", "finished_good_name", "source_brand",
         "status_simple", "pick_type", "processing_status", "block_reason_pfs", "original_request",
         "current_request", "processed", "order_created", "shipped", "storage", "woi_processing_pct",
         "age_days_from_created", "days_overdue"]
    ].rename(columns={
        "work_order_item_id": "WOI ID", "ship_by": "Ship By", "created_at": "Created At",
        "last_edit_at": "Last Edit At", "master_id": "Master ID",
        "listing_id": "Listing", "finished_good_name": "Item Name", "source_brand": "Brand",
        "status_simple": "Status", "pick_type": "Pick Type", "processing_status": "Block Status",
        "block_reason_pfs": "Reason", "original_request": "Orig", "current_request": "Current",
        "processed": "Processed", "order_created": "Ship Created", "shipped": "Shipped",
        "storage": "Stowed", "woi_processing_pct": "%", "age_days_from_created": "Age (d)",
        "days_overdue": "Days Overdue",
    })
    cols = column_picker(list(display.columns), key=f"cols_swo_items_{wo_id}", required=["WOI ID"])
    display = display[cols]

    table_toolbar(display, key=f"tb_swo_items_{wo_id}", file_stem=f"wo_{wo_id}_items",
                  id_cols=["WOI ID", "Listing", "Master ID"], count_label=f"{len(display)} items")
    render_table(
        display, key=_grid_key(f"grid_swo_items_{wo_id}"),
        pct_cols=["%"], date_cols=["Ship By"],
        datetime_cols=["Created At", "Last Edit At"],
        pin_cols=["WOI ID"], color_rows=True, height=480,
    )


def storage_item_view(s_items, s_wos):
    filtered = filter_panel(
        s_items, "fp_sit", brand_col="source_brand", ship_col="ship_by",
        reason_col="block_reason_pfs", blocked_kind="item", status_kind="item",
        search_cols=["work_order_item_id", "listing_id", "source_brand", "finished_good_name", "work_order_number"],
    )
    filtered = hide_unlisted(filtered, "hl_sit")
    fwos = s_wos[s_wos["work_order_number"].isin(filtered["work_order_number"])]
    storage_kpi_strip(filtered, fwos)
    if render_flag_guide_inline is not None and "block_reason_pfs" in filtered.columns:
        render_flag_guide_inline(set(filtered["block_reason_pfs"].dropna()))
    display = filtered[
        ["work_order_item_id", "ship_by", "created_at", "last_edit_at", "work_order_number",
         "master_id", "listing_id", "finished_good_name", "source_brand",
         "warehouse", "status_simple", "pick_type",
         "processing_status", "block_reason_pfs", "original_request", "current_request",
         "processed", "order_created", "shipped", "storage", "woi_processing_pct",
         "age_days_from_created", "days_overdue"]
    ].rename(columns={
        "work_order_item_id": "WOI ID", "ship_by": "Ship By", "created_at": "Created At",
        "last_edit_at": "Last Edit At", "work_order_number": "WO",
        "master_id": "Master ID", "listing_id": "Listing", "finished_good_name": "Item Name",
        "source_brand": "Brand", "warehouse": "WH", "status_simple": "Status", "pick_type": "Pick Type",
        "processing_status": "Block Status", "block_reason_pfs": "Reason",
        "original_request": "Orig", "current_request": "Current", "processed": "Processed",
        "order_created": "Ship Created", "shipped": "Shipped", "storage": "Stowed",
        "woi_processing_pct": "%", "age_days_from_created": "Age (d)", "days_overdue": "Days Overdue",
    })
    if flag_action is not None and "Reason" in display.columns:
        display["What to do"] = display["Reason"].map(flag_action)
    cols = column_picker(list(display.columns), key="cols_sit", required=["WOI ID"])
    display = display[cols]

    table_toolbar(display, key="tb_sit", file_stem="storage_items",
                  id_cols=["WOI ID", "Listing", "WO", "Master ID"], count_label=f"{len(display):,} items")
    render_table(
        display, key=_grid_key("grid_sit"),
        pct_cols=["%"], date_cols=["Ship By"],
        datetime_cols=["Created At", "Last Edit At"],
        pin_cols=["WOI ID"], color_rows=True, height=600,
    )


# ============================================================
# PO TAB
# ============================================================
def po_tab(df, wos):
    p_wos = wos[wos["source_category"] == "PO"].copy()
    p_items = df[df["source_category"] == "PO"].copy()

    sel = st.session_state.get("selected_po_wo")
    if sel and sel in p_wos["work_order_number"].values:
        po_wo_drilldown(sel, p_items, p_wos)
        return

    view = st.radio("View", ["📋 WO Level", "📄 Item Level"], horizontal=True,
                    key="po_view", label_visibility="collapsed")
    st.caption("Block flag: **14/21 days past later of WO/PO ship-by** · 💡 Tick a row then press **Open WO** · drag-select cells + Ctrl+C to copy · cards react to the filters")
    if view == "📋 WO Level":
        po_wo_view(p_wos, p_items)
    else:
        po_item_view(p_items, p_wos)


def po_wo_view(p_wos, p_items):
    filtered = filter_panel(
        p_wos, "fp_pwo", brand_col="top_brand", ship_col="earliest_ref_ship",
        ship_label="Ref ship-by", flag_col="worst_po_flag", status_kind="wo",
        search_cols=["work_order_number", "po_number_raw", "top_brand"],
    )
    fitems = p_items[p_items["work_order_number"].isin(filtered["work_order_number"])]
    po_kpi_strip(fitems, filtered)
    display = filtered[
        ["work_order_number", "po_number_raw", "earliest_ref_ship", "warehouse", "top_brand", "items", "open_items",
         "untouched", "orig", "processed", "pct", "worst_po_flag", "max_age"]
    ].rename(columns={
        "work_order_number": "WO", "po_number_raw": "PO #", "warehouse": "WH", "top_brand": "Brand",
        "items": "Items", "open_items": "Open", "untouched": "Untouched",
        "orig": "Orig", "processed": "Processed", "pct": "% Processed",
        "worst_po_flag": "Worst Flag", "max_age": "Age (d)", "earliest_ref_ship": "Ref Ship-by",
    })
    cols = column_picker(list(display.columns), key="cols_pwo", required=["WO"])
    display = display[cols]

    table_toolbar(display, key="tb_pwo", file_stem="po_wos",
                  id_cols=["WO", "PO #"], count_label=f"{len(display)} of {len(p_wos)} WOs")
    sel_wo = render_table(
        display, key=_grid_key("grid_pwo"), selectable=True,
        pct_cols=["% Processed"], date_cols=["Ref Ship-by"], pin_cols=["WO"], height=500,
    )
    if sel_wo is not None:
        if st.button(f"➡ Open WO {_to_wo(sel_wo)}", type="primary", key="open_pwo", use_container_width=True):
            st.session_state.selected_po_wo = _to_wo(sel_wo)
            st.rerun()


def po_wo_drilldown(wo_id, p_items, p_wos):
    wo_row = p_wos[p_wos["work_order_number"] == wo_id].iloc[0]
    top1, top2 = st.columns([1, 5])
    if top1.button("← Back to list", use_container_width=True, key="back_pwo"):
        _reset_selection()
        st.rerun()
    top2.markdown(f"### WO {wo_id} — PO# {wo_row['po_number_raw']} · {wo_row['warehouse']} · {wo_row['worst_po_flag'] or ''}")
    st.caption(f"Top brand: **{wo_row['top_brand']}** · {wo_row['unique_listings']} unique listings")

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Items", _safe_int(wo_row["items"]))
    c2.metric("Open", _safe_int(wo_row["open_items"]))
    c3.metric("Untouched", _safe_int(wo_row["untouched"]))
    c4.metric("Orig Qty", f"{_safe_int(wo_row['orig']):,}")
    c5.metric("Processed", f"{_safe_int(wo_row['processed']):,}", f"{wo_row['pct']:.1f}%")
    c6.metric("Ref Ship-by", _safe_date_str(wo_row["earliest_ref_ship"]))

    items = p_items[p_items["work_order_number"] == wo_id].copy()
    flag_counts = items["po_block_flag"].value_counts().to_dict()
    if flag_counts:
        breakdown = " · ".join([f"{k}: **{v}**" for k, v in flag_counts.items()])
        st.markdown(f"**Flag breakdown:** {breakdown}")
    render_root_cause_detail(items, f"rc_pwo_{wo_id}")

    st.markdown("---")
    st.markdown(f"#### 📄 Items in WO {wo_id} (PO# {wo_row['po_number_raw']})")
    filtered = filter_panel(
        items, f"fp_pwo_items_{wo_id}", brand_col="source_brand", ship_col="po_ref_ship_by_date",
        ship_label="Ref ship-by", flag_col="po_block_flag", status_kind="item",
        search_cols=["work_order_item_id", "listing_id", "source_brand", "finished_good_name"],
    )
    filtered = hide_unlisted(filtered, f"hl_pwo_items_{wo_id}")
    filtered = filtered.sort_values("po_days_past_ref_ship_by", ascending=False)
    item_cols = [
        "work_order_item_id", "po_ref_ship_by_date",
        "po_requested_delivery_date", "po_placed_at", "po_arrived_at",
        "master_id", "listing_id", "woi_type", "finished_good_name", "source_brand",
        "status_simple", "po_block_flag",
        "original_request", "current_request", "processed", "order_created", "shipped", "storage",
        "woi_processing_pct", "po_days_past_ref_ship_by",
    ]
    display = filtered[[c for c in item_cols if c in filtered.columns]].rename(columns={
        "work_order_item_id": "WOI ID", "po_ref_ship_by_date": "Ref Ship-by",
        "po_requested_delivery_date": "Req Delivery Date",
        "po_placed_at": "Placed At", "po_arrived_at": "Arrived At",
        "master_id": "Master ID",
        "listing_id": "Listing", "woi_type": "WO Type", "finished_good_name": "Item Name", "source_brand": "Brand",
        "status_simple": "Status",
        "po_block_flag": "Flag", "original_request": "Orig",
        "current_request": "Current", "processed": "Processed", "order_created": "Ship Created",
        "shipped": "Shipped", "storage": "Stowed", "woi_processing_pct": "%",
        "po_days_past_ref_ship_by": "Days Past",
    })
    cols = column_picker(list(display.columns), key=f"cols_pwo_items_{wo_id}", required=["WOI ID"])
    display = display[cols]

    table_toolbar(display, key=f"tb_pwo_items_{wo_id}", file_stem=f"wo_{wo_id}_items",
                  id_cols=["WOI ID", "Listing", "Master ID"], count_label=f"{len(display)} items")
    render_table(
        display, key=_grid_key(f"grid_pwo_items_{wo_id}"),
        pct_cols=["%"],
        date_cols=["Ref Ship-by", "Req Delivery Date"],
        datetime_cols=["Placed At", "Arrived At"],
        pin_cols=["WOI ID"], color_rows=True, height=480,
    )


def po_item_view(p_items, p_wos):
    filtered = filter_panel(
        p_items, "fp_pit", brand_col="source_brand", ship_col="po_ref_ship_by_date",
        ship_label="Ref ship-by", flag_col="po_block_flag", status_kind="item",
        search_cols=["work_order_item_id", "listing_id", "source_brand", "finished_good_name", "work_order_number"],
    )
    filtered = hide_unlisted(filtered, "hl_pit")
    filtered = filtered.sort_values("po_days_past_ref_ship_by", ascending=False)
    fwos = p_wos[p_wos["work_order_number"].isin(filtered["work_order_number"])]
    po_kpi_strip(filtered, fwos)
    pit_cols = [
        "work_order_item_id", "work_order_number", "po_number_raw",
        "po_ref_ship_by_date", "po_requested_delivery_date",
        "po_placed_at", "po_arrived_at",
        "master_id", "listing_id", "woi_type", "finished_good_name", "source_brand",
        "warehouse", "status_simple", "po_block_flag",
        "original_request", "current_request", "processed", "order_created", "shipped", "storage",
        "woi_processing_pct", "po_days_past_ref_ship_by",
    ]
    display = filtered[[c for c in pit_cols if c in filtered.columns]].rename(columns={
        "work_order_item_id": "WOI ID", "work_order_number": "WO", "po_number_raw": "PO #",
        "po_ref_ship_by_date": "Ref Ship-by",
        "po_requested_delivery_date": "Req Delivery Date", "po_placed_at": "Placed At",
        "po_arrived_at": "Arrived At",
        "master_id": "Master ID", "listing_id": "Listing", "woi_type": "WO Type",
        "finished_good_name": "Item Name", "source_brand": "Brand",
        "warehouse": "WH", "status_simple": "Status",
        "po_block_flag": "Flag", "original_request": "Orig",
        "current_request": "Current", "processed": "Processed", "order_created": "Ship Created",
        "shipped": "Shipped", "storage": "Stowed", "woi_processing_pct": "%",
        "po_days_past_ref_ship_by": "Days Past",
    })
    cols = column_picker(list(display.columns), key="cols_pit", required=["WOI ID"])
    display = display[cols]

    table_toolbar(display, key="tb_pit", file_stem="po_items",
                  id_cols=["WOI ID", "Listing", "WO", "PO #", "Master ID"], count_label=f"{len(display):,} items")
    render_table(
        display, key=_grid_key("grid_pit"),
        pct_cols=["%"],
        date_cols=["Ref Ship-by", "Req Delivery Date"],
        datetime_cols=["Placed At", "Arrived At"],
        pin_cols=["WOI ID"], color_rows=True, height=600,
    )


# ============================================================
# PO DETAILS (Phase 2)  —  inbound purchase orders (UK/EU)
# Source: queries/po_tracker.sql  ->  ANALYTICS_DB.REPORTING.REPORT__BRAND_MANAGEMENT_V7__PURCHASE_ORDERS
# Grain: one row per PO x line item. Fill rates are UNCAPPED (>100% = over-receipts).
# ============================================================
@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def fetch_po_data(wh_scope=()):
    sql = _scope_sql(_read_sql(PO_QUERY_PATH), _override_from_scope(wh_scope))
    conn = get_snowflake_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
        cols = [c[0].lower() for c in cur.description]
        df = pd.DataFrame(rows, columns=cols)
    finally:
        cur.close()

    date_cols = [
        "order_placed_date", "ship_date", "arrived_date", "finished_arrived_date",
        "cancel_date", "po_last_received_date", "item_last_received_date",
    ]
    for col in date_cols:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    numeric_cols = [
        "po_number", "item_id",
        "wholesale_price", "retail_price", "wholesale_ordered", "wholesale_received",
        "original_ordered_units", "ordered_units", "current_units", "current_on_order",
        "received_units", "remained_blanket_order_quantity",
        "total_issues", "demand_fill_rate_pct", "vendor_fill_rate_pct",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["po_status"] = _po_item_status(df)
    pos = _build_po_aggregates(df)
    return df, pos, datetime.now()


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def fetch_po_extract_asof():
    """When Snowflake last altered the daily PO Tableau extract, if readable."""
    try:
        conn = get_snowflake_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT LAST_ALTERED "
                "FROM ANALYTICS_DB.INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = 'REPORTING' "
                "  AND TABLE_NAME = 'REPORT__BRAND_MANAGEMENT_V7__PURCHASE_ORDERS'"
            )
            row = cur.fetchone()
        finally:
            cur.close()
        if not row or row[0] is None:
            return None
        ts = pd.to_datetime(row[0], errors="coerce", utc=True)
        if pd.isna(ts):
            return None
        return ts.to_pydatetime()
    except Exception:
        return None


def _po_extract_stamp():
    """Short label for PO-report freshness (empty if the as-of query is unavailable)."""
    try:
        ts = fetch_po_extract_asof()
    except Exception:
        ts = None
    if not ts:
        return ""
    if getattr(ts, "tzinfo", None) is not None:
        ts = ts.replace(tzinfo=None)
    return f"Extract {ts.strftime('%d %b %Y %H:%M')} UTC"


def _po_lag_caption(*, extra=""):
    stamp = _po_extract_stamp()
    bits = [PO_REPORT_LAG]
    if stamp:
        bits.append(stamp + ".")
    if extra:
        bits.append(extra)
    st.caption(" ".join(bits))


def fetch_po_wo_agg():
    """Per-PO rollup of the work orders linked to each PO (queries/po_wo_agg.sql)."""
    return _cached_po_wo_agg(_read_sql(PO_WO_AGG_PATH))


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def _cached_po_wo_agg(sql):
    """SQL text is the cache key so editing po_wo_agg.sql actually refetches."""
    conn = get_snowflake_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
        cols = [c[0].lower() for c in cur.description]
        agg = pd.DataFrame(rows, columns=cols)
    finally:
        cur.close()
    for c in agg.columns:
        agg[c] = pd.to_numeric(agg[c], errors="coerce")
    return agg


# Dest + WO type live in app.py so they still work if only app.py is pasted
# and queries/po_wo_agg.sql / wo_tracker.sql on Cloud are the older files.
_PO_DEST_SQL = """
SELECT
    p.PO_NUMBER AS po_number,
    SUM(CASE WHEN UPPER(wt.NAME) LIKE 'FBA%' THEN woi.QUANTITY ELSE 0 END) AS wo_fba,
    SUM(CASE WHEN UPPER(wt.NAME) LIKE 'FBB%' THEN woi.QUANTITY ELSE 0 END) AS wo_fbb,
    SUM(CASE WHEN UPPER(wt.NAME) LIKE 'FBM%' THEN woi.QUANTITY ELSE 0 END) AS wo_fbm,
    SUM(CASE WHEN UPPER(wt.NAME) = 'ZFS' THEN woi.QUANTITY ELSE 0 END) AS wo_zfs,
    SUM(CASE WHEN UPPER(wt.NAME) = 'OCT' THEN woi.QUANTITY ELSE 0 END) AS wo_oct,
    SUM(CASE WHEN UPPER(wt.NAME) NOT LIKE 'FBA%'
              AND UPPER(wt.NAME) NOT LIKE 'FBB%'
              AND UPPER(wt.NAME) NOT LIKE 'FBM%'
              AND UPPER(wt.NAME) NOT IN ('ZFS', 'OCT')
             THEN woi.QUANTITY ELSE 0 END) AS wo_other
FROM ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__PURCHASES p
JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDERS wo
  ON wo.RECEIVABLE_ID = p.ID AND wo.RECEIVABLE_TYPE = 'Purchase' AND wo.DELETED_AT IS NULL
JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDER_ITEMS woi
  ON woi.WORK_ORDER_ID = wo.ID AND woi.DELETED_AT IS NULL AND woi.FOR_ACCEPTED_OVERAGE = FALSE
JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDER_ITEM_TYPES wt
  ON wt.ID = woi.WORK_ORDER_ITEM_TYPE_ID
WHERE wo.CREATED_AT >= '2025-07-01'
GROUP BY p.PO_NUMBER
"""
_WOI_TYPE_SQL = """
SELECT woi.id AS work_order_item_id, wt.name AS woi_type
FROM ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDER_ITEMS woi
JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDERS wo
  ON wo.id = woi.work_order_id AND wo.deleted_at IS NULL
JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDER_ITEM_TYPES wt
  ON wt.id = woi.work_order_item_type_id
JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WAREHOUSES wh
  ON wh.id = wo.warehouse_id
WHERE woi.deleted_at IS NULL
  AND woi.for_accepted_overage = FALSE
  AND woi.created_at >= DATE_TRUNC('year', CURRENT_DATE)
  AND wh.id IN (138, 146)
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def fetch_po_dest_agg():
    """Per-PO dest qty from WO item types. Independent of po_wo_agg.sql shape."""
    dest = _sf_query(_PO_DEST_SQL)
    for c in dest.columns:
        dest[c] = pd.to_numeric(dest[c], errors="coerce")
    return dest


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def fetch_woi_types(wh_scope=()):
    """WO item type names when wo_tracker.sql does not SELECT woi_type."""
    sql = _scope_sql(_WOI_TYPE_SQL, _override_from_scope(wh_scope))
    types = _sf_query(sql)
    types["work_order_item_id"] = pd.to_numeric(types["work_order_item_id"], errors="coerce")
    return types[["work_order_item_id", "woi_type"]]


_WO_AGG_COLS = [
    "wo_count", "woi_count", "wo_current", "wo_processed",
    "wo_ship_created", "wo_shipped", "wo_stowed",
    "wo_fba", "wo_fbb", "wo_fbm", "wo_zfs", "wo_oct", "wo_other",
]
_DEST_MAP = (
    ("wo_fba", "fba_units"),
    ("wo_fbb", "fbb_units"),
    ("wo_fbm", "fbm_units"),
    ("wo_zfs", "zfs_units"),
    ("wo_oct", "oct_units"),
    ("wo_other", "other_units"),
)
_DEST_UNIT_COLS = [dst for _, dst in _DEST_MAP]
_DEST_SHOW_KEYS = ("FBA", "FBB", "FBM", "ZFS", "OCT")
_DEST_SHOW_COLS = {
    "FBA": "fba_units", "FBB": "fbb_units", "FBM": "fbm_units",
    "ZFS": "zfs_units", "OCT": "oct_units", "Stow": "stow_units",
}


def _wo_dest_bucket_series(s):
    """Map Shelf WO item type to FBA / FBB / FBM / ZFS / OCT / Other."""
    raw = s.astype(str).str.strip().str.upper()
    out = pd.Series("Other", index=s.index)
    out = out.mask(raw.str.startswith("FBA", na=False), "FBA")
    out = out.mask(raw.str.startswith("FBB", na=False), "FBB")
    out = out.mask(raw.str.startswith("FBM", na=False), "FBM")
    out = out.mask(raw.eq("ZFS"), "ZFS")
    out = out.mask(raw.eq("OCT"), "OCT")
    return out


def _dest_from_wo_items(wo_df):
    """Per-PO dest qty from already-loaded WO items when po_wo_agg dest cols are absent."""
    if wo_df is None or getattr(wo_df, "empty", True) or "woi_type" not in wo_df.columns:
        return None
    po_col = next((c for c in ("po_number_raw", "po_number") if c in wo_df.columns), None)
    qty_col = next((c for c in ("current_request", "original_request") if c in wo_df.columns), None)
    if po_col is None or qty_col is None:
        return None
    w = wo_df[[po_col, "woi_type", qty_col]].copy()
    w["po_number"] = pd.to_numeric(w[po_col], errors="coerce")
    w = w.dropna(subset=["po_number"])
    if w.empty:
        return None
    w["_bucket"] = _wo_dest_bucket_series(w["woi_type"])
    w["_qty"] = pd.to_numeric(w[qty_col], errors="coerce").fillna(0)
    g = w.groupby(["po_number", "_bucket"], as_index=False)["_qty"].sum()
    rename = {
        "FBA": "wo_fba", "FBB": "wo_fbb", "FBM": "wo_fbm",
        "ZFS": "wo_zfs", "OCT": "wo_oct", "Other": "wo_other",
    }
    return (
        g.pivot_table(index="po_number", columns="_bucket", values="_qty",
                      aggfunc="sum", fill_value=0)
        .reindex(columns=list(rename), fill_value=0)
        .rename(columns=rename)
        .reset_index()
    )


def _enrich_po_pos_with_wo(po_pos, wo_df=None):
    """Attach WO counts and destination qty; stow = leftover current ordered."""
    if po_pos is None or getattr(po_pos, "empty", True):
        return po_pos
    out = po_pos.copy()
    try:
        wo_agg = fetch_po_wo_agg()
    except Exception:
        wo_agg = None
    if wo_agg is not None and not wo_agg.empty and "po_number" in wo_agg.columns:
        keep = ["po_number"] + [c for c in _WO_AGG_COLS if c in wo_agg.columns]
        drop_existing = [c for c in keep if c != "po_number" and c in out.columns]
        if drop_existing:
            out = out.drop(columns=drop_existing)
        out = out.merge(wo_agg[keep], on="po_number", how="left")
        _warn_missing_columns(
            wo_agg,
            ["po_number", "wo_count"],
            "PO→WO rollup (queries/po_wo_agg.sql)",
        )
    dest_needed = [src for src, _ in _DEST_MAP]
    if any(c not in out.columns for c in dest_needed):
        extra = None
        try:
            extra = fetch_po_dest_agg()
        except Exception:
            extra = None
        if extra is None or extra.empty:
            extra = _dest_from_wo_items(wo_df)
        if extra is not None and not extra.empty:
            add = ["po_number"] + [c for c in dest_needed if c in extra.columns]
            extra = extra[add].copy()
            extra["po_number"] = pd.to_numeric(extra["po_number"], errors="coerce")
            out["po_number"] = pd.to_numeric(out["po_number"], errors="coerce")
            drop_existing = [c for c in add if c != "po_number" and c in out.columns]
            if drop_existing:
                out = out.drop(columns=drop_existing)
            out = out.merge(extra, on="po_number", how="left")
    for cc in _WO_AGG_COLS:
        if cc in out.columns:
            out[cc] = pd.to_numeric(out[cc], errors="coerce").fillna(0).astype(int)
    out = _finalize_po_destination(out)
    if "wo_count" not in out.columns:
        raised = sum(pd.to_numeric(out[c], errors="coerce").fillna(0) for c in _DEST_UNIT_COLS
                     if c in out.columns)
        out["wo_count"] = (raised > 0).astype(int)
    return out


def _finalize_po_destination(po_pos):
    """Named dest from WO item types; To stow = current ordered minus all WO qty."""
    if po_pos is None or getattr(po_pos, "empty", True):
        return po_pos
    out = po_pos
    for src, dst in _DEST_MAP:
        if src in out.columns:
            out[dst] = pd.to_numeric(out[src], errors="coerce").fillna(0).astype(int)
        elif dst not in out.columns:
            out[dst] = 0
    ordered = pd.to_numeric(out["ordered"], errors="coerce").fillna(0) if "ordered" in out.columns else 0
    raised = sum(pd.to_numeric(out[c], errors="coerce").fillna(0) for c in _DEST_UNIT_COLS)
    out["stow_units"] = (ordered - raised).clip(lower=0).astype(int)
    return out


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def fetch_po_item_wo_gap(wh_scope=()):
    """Item-level (PO x Master ID) WO coverage gaps — No WO / Partial WO lines
    only (queries/po_item_wo_gap.sql). Different grain from fetch_po_wo_agg,
    which rolls up to one row per whole PO."""
    sql = _scope_sql(_read_sql(PO_ITEM_GAP_PATH), _override_from_scope(wh_scope))
    conn = get_snowflake_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
        cols = [c[0].lower() for c in cur.description]
        df = pd.DataFrame(rows, columns=cols)
    finally:
        cur.close()

    if "order_placed_date" in df.columns:
        df["order_placed_date"] = pd.to_datetime(df["order_placed_date"], errors="coerce")

    if "ship_date" in df.columns:
        df["ship_date"] = pd.to_datetime(df["ship_date"], errors="coerce")

    numeric_cols = ["po_number", "original_ordered_units", "ordered_units",
                    "received_units", "outstanding_units", "wo_qty"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def fetch_wo_unpickable_detail():
    """Per-WOI active unpickable-reason detail (queries/wo_unpickable_detail.sql):
    reason name(s) + resolvable quantity. Defensive — returns None if the query
    file or table is unavailable, so the drill-down degrades instead of crashing."""
    try:
        sql = _read_sql(UNPICK_DETAIL_PATH)
        conn = get_snowflake_connection()
        cur = conn.cursor()
        try:
            cur.execute(sql)
            rows = cur.fetchall()
            cols = [c[0].lower() for c in cur.description]
            df = pd.DataFrame(rows, columns=cols)
        finally:
            cur.close()
        for col in ("woi_id", "resolvable_qty", "reason_rows"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception:
        return None


# ============================================================
# CATALOGUE LOOKUP — Search by ID (listings / SKU / ASIN / Master ID / …)
# ============================================================
def parse_catalog_ids(raw):
    """Split a pasted blob into unique IDs (comma / newline / semicolon / space)."""
    seen = set()
    out = []
    for term in re.split(r"[\s,;]+", str(raw or "")):
        token = term.strip().upper()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= MAX_CATALOG_IDS:
            break
    return out


def _sql_quote_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _sql_in_list(ids):
    """Quoted IN-list: 'SKU1', 'SKU2' — never a Python list repr."""
    return ", ".join(_sql_quote_literal(i) for i in ids)


def _sql_values_rows(ids):
    """Snowflake VALUES rows: ('SKU1'), ('SKU2')."""
    return ", ".join("(" + _sql_quote_literal(i) + ")" for i in ids)


def build_catalog_query(ids):
    """Catalogue Lookup Search-by-ID. Pushes the ID list into q1/q2 so Snowflake
    does not scan the whole catalog. ``ids`` must already be uppercased.

    Fills both placeholders used in the wild:
      {id_values}  — GitHub SQL ``FROM VALUES {id_values}``
      {upper_list} — older SQL ``IN ({upper_list})``
    """
    if not ids:
        raise ValueError("No IDs to look up.")
    sql = _read_sql(CATALOG_QUERY_PATH)
    sql = sql.replace("{id_values}", _sql_values_rows(ids))
    sql = sql.replace("{upper_list}", _sql_in_list(ids))
    leftover = [p for p in ("{id_values}", "{upper_list}") if p in sql]
    if leftover:
        raise ValueError(
            "queries/catalog_lookup.sql still has unreplaced placeholders "
            f"{leftover}. Copy wo-tracking-tool/queries/catalog_lookup.sql "
            "and the matching app.py together."
        )
    if "VALUES [" in sql or "IN ([" in sql:
        raise ValueError(
            "Catalogue SQL id list was built as a Python list. "
            "IDs must be quoted SQL literals."
        )
    return sql


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def fetch_catalog_lookup(ids):
    """Run the Search-by-ID catalog query. ``ids`` is a tuple so Streamlit can cache it."""
    sql = build_catalog_query(list(ids))
    conn = get_snowflake_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
        cols = [c[0].lower() for c in cur.description]
        df = pd.DataFrame(rows, columns=cols)
    finally:
        cur.close()
    for col in ("sku", "listing_id", "master_id", "mpn", "asin", "fnsku", "upc", "ean",
                "marketplace", "vendor", "product_name", "dno_note", "dno_reason_code",
                "marketplace_seller", "listing_fulfillment_type", "commingled_status",
                "listing_type"):
        if col in df.columns:
            df[col] = df[col].astype("string").fillna("")
    return df


def _catalog_bool(series):
    if series is None:
        return None
    if str(series.dtype) == "bool":
        return series.fillna(False)
    return series.astype(str).str.strip().str.lower().isin(("true", "1", "t", "yes"))


def _wh_scope_arg():
    return tuple(st.session_state.get("_wh_scope_tuple") or ())


def _wh_is_all(wh=None):
    if wh is None:
        wh = st.session_state.get("global_wh", "All in scope")
    return wh in ("Both", "All in scope", None, "")


def _warn_missing_columns(df, expected, label):
    """Trust check: app.py and queries/*.sql must ship the same columns.

    Returns the list of missing column names (empty if the frame is usable).
    Callers must not assume expected columns exist after a warning — a
    diagnostic SQL file can still return rows, just the wrong shape.
    """
    if df is None:
        return list(expected)
    missing = [c for c in expected if c not in df.columns]
    if not missing:
        return []
    cols = {str(c).lower() for c in df.columns}
    extra = ""
    if "coverage_state" in missing and (
        {"workable_type", "work_order_item_id", "workable_id"} & cols
    ):
        extra = (
            " This looks like the diagnostic `SELECT * FROM po_woi` dump, not the "
            "production gap query. On GitHub, replace `queries/po_item_wo_gap.sql` "
            "with the Streamlit file (one row per PO × Master ID; must SELECT "
            "coverage_state, sku, warehouse_name, reason)."
        )
    st.warning(
        f"{label} is missing column(s) {missing}. "
        "SQL and app.py are out of sync — redeploy the query file in the same commit."
        + extra
    )
    return missing


def _uniq_filter_vals(series, cap=80):
    vals = []
    seen = set()
    for v in series.dropna():
        s = str(v).strip()
        if not s or s.lower() in ("nan", "none"):
            continue
        if s not in seen:
            seen.add(s)
            vals.append(s)
    vals.sort(key=str.lower)
    return vals[:cap]


def _catalog_is_raisable(df):
    """Rows that are safe to attach to a raise-WO request."""
    mask = pd.Series(True, index=df.index)
    if "Status" in df.columns:
        s = df["Status"].astype(str)
        mask &= ~s.str.contains("DNO", na=False)
        mask &= ~s.str.contains("Inactive", na=False)
        mask &= ~s.str.contains("Discontinued", na=False)
    if "Shippable" in df.columns:
        ship = _catalog_bool(df["Shippable"])
        if ship is not None:
            # Unknown shippable (neither true nor false) stays in — only drop explicit No.
            known = df["Shippable"].notna() & ~df["Shippable"].astype(str).str.strip().eq("")
            mask &= ~(known & ~ship)
    return mask


def catalog_filter_panel(df, key):
    """Filters for Catalogue Lookup: drop DNO / inactive / marketplace / seller / etc."""
    out = df.copy()
    with st.expander("🔎 Filters", expanded=True):
        preset_key = f"{key}_hide_bad"
        if st.session_state.get("cat_lookup_pack_mode") and preset_key not in st.session_state:
            st.session_state[preset_key] = True
        hide_bad = st.checkbox(
            "Hide listings that shouldn't get a WO (DNO / inactive / discontinued / not shippable)",
            key=preset_key,
            help="Keeps Active + shippable rows. Uncheck to audit DNO or inactive listings.",
        )
        if hide_bad:
            out = out[_catalog_is_raisable(out)]

        r1a, r1b = st.columns(2)
        if "Marketplace" in out.columns:
            mkts = _uniq_filter_vals(df["Marketplace"])
            pick = r1a.multiselect("Marketplace", mkts, key=f"{key}_mkt", placeholder="All marketplaces")
            if pick:
                out = out[out["Marketplace"].astype(str).isin(pick)]
        if "Vendor" in out.columns:
            vends = _uniq_filter_vals(df["Vendor"])
            pick = r1b.multiselect("Vendor", vends, key=f"{key}_vendor", placeholder="All vendors")
            if pick:
                out = out[out["Vendor"].astype(str).isin(pick)]
        r1c, r1d = st.columns(2)
        if "Fulfillment" in out.columns:
            fulf = _uniq_filter_vals(df["Fulfillment"])
            pick = r1c.multiselect("Fulfillment", fulf, key=f"{key}_fulf", placeholder="All types")
            if pick:
                out = out[out["Fulfillment"].astype(str).isin(pick)]
        if "Status" in out.columns:
            stats = _uniq_filter_vals(df["Status"])
            pick = r1d.multiselect("Status", stats, key=f"{key}_status", placeholder="All statuses")
            if pick:
                out = out[out["Status"].astype(str).isin(pick)]

        r2a, r2b = st.columns(2)
        if "Seller" in out.columns:
            sellers = _uniq_filter_vals(df["Seller"])
            pick = r2a.multiselect("Seller", sellers, key=f"{key}_seller", placeholder="All sellers")
            if pick:
                out = out[out["Seller"].astype(str).isin(pick)]
        if "Commingled" in out.columns:
            comm = _uniq_filter_vals(df["Commingled"])
            pick = r2b.multiselect("Commingled", comm, key=f"{key}_comm", placeholder="All")
            if pick:
                out = out[out["Commingled"].astype(str).isin(pick)]

        def _yn(col, label, widget):
            nonlocal out
            if col not in df.columns:
                return
            choice = widget.selectbox(label, ["All", "Yes", "No"], key=f"{key}_{col}")
            if choice == "All":
                return
            b = _catalog_bool(out[col])
            if b is None:
                return
            out = out[b] if choice == "Yes" else out[~b]

        r2c, r2d, r2e = st.columns(3)
        _yn("DNO", "DNO", r2c)
        _yn("Active", "Active", r2d)
        _yn("Shippable", "Shippable", r2e)

        q = st.text_input(
            "Search", "", key=f"{key}_search",
            placeholder="paste several — comma / new line = match any",
        )
        if q:
            search_cols = [c for c in [
                "SKU", "Listing ID", "Master ID", "ASIN", "FNSKU", "MPN",
                "Product Name", "UPC", "EAN", "Marketplace", "Vendor", "Seller",
            ] if c in out.columns]
            out = out[_str_contains_any(out, search_cols, q)]
    return out


def _ids_from_frame(frame, cols):
    if frame is None or getattr(frame, "empty", True):
        return []
    out = []
    for c in cols:
        if c in frame.columns:
            out.extend(frame[c].dropna().astype(str).tolist())
    return out


def _catalog_ids_for_lookup(frame):
    """SKUs only when present. Master ID / ASIN match every marketplace listing
    for the product and make Catalogue Lookup slow (hundreds of extra rows)."""
    if frame is None or getattr(frame, "empty", True):
        return []
    for col in ("sku", "SKU"):
        if col not in frame.columns:
            continue
        vals = []
        seen = set()
        for v in frame[col].dropna().astype(str):
            s = v.strip()
            if not s or s.lower() in ("nan", "none"):
                continue
            key = s.upper()
            if key in seen:
                continue
            seen.add(key)
            vals.append(s)
        if vals:
            return vals
    return _ids_from_frame(frame, ["listing_id", "asin", "master_id"])


def _po_qty_lookup(frame):
    """Map MASTER_ID / SKU → outstanding PO units for a raise-WO request.

    Outstanding = ordered − received, or ``outstanding_units`` when the gap
    query already computed it. Keys are uppercased so catalog rows match.
    """
    if frame is None or getattr(frame, "empty", True):
        return {}
    df = frame.copy()
    if "outstanding_units" in df.columns:
        qty = pd.to_numeric(df["outstanding_units"], errors="coerce")
    else:
        ordered = pd.to_numeric(df["ordered_units"], errors="coerce") if "ordered_units" in df.columns else None
        if ordered is None and "current_units" in df.columns:
            ordered = pd.to_numeric(df["current_units"], errors="coerce")
        received = pd.to_numeric(df["received_units"], errors="coerce") if "received_units" in df.columns else None
        if ordered is None:
            return {}
        qty = ordered if received is None else ordered.fillna(0) - received.fillna(0)
    df = df.assign(_wo_qty=pd.to_numeric(qty, errors="coerce").fillna(0).clip(lower=0))
    out = {}
    for col in ("master_id", "sku"):
        if col not in df.columns:
            continue
        keys = df[col].astype(str).str.strip().str.upper()
        ok = keys.ne("") & ~keys.isin(("NAN", "NONE", "<NA>"))
        summed = df.loc[ok].groupby(keys[ok], sort=False)["_wo_qty"].sum()
        for k, v in summed.items():
            out[str(k)] = int(round(float(v)))
    return out


def _clean_po_id(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none", "<na>"):
        return ""
    if s.endswith(".0"):
        s = s[:-2]
    return s.split(".")[0] if s.replace(".", "", 1).isdigit() else s


def _suggested_ship_by(raw):
    """Shelf upload date as MM/DD/YYYY. Use the PO ship date when it's in the
    future; otherwise suggest today + SUGGESTED_SHIP_LEAD_DAYS."""
    today = date.today()
    d = None
    ts = pd.to_datetime(raw, errors="coerce") if raw is not None else pd.NaT
    if pd.notna(ts):
        d = ts.date()
    if d is None or d < today:
        d = today + timedelta(days=SUGGESTED_SHIP_LEAD_DAYS)
    return d.strftime("%m/%d/%Y")


def _po_line_meta(frame):
    """Per Master ID / SKU: qty, PO number, ship date — for the Shelf WO CSV."""
    qty_map = _po_qty_lookup(frame)
    meta = {}
    if not qty_map or frame is None or getattr(frame, "empty", True):
        return qty_map, meta
    df = frame.copy()
    ship = None
    for c in ("ship_date", "Ship Date", "order_placed_date", "order_placed"):
        if c in df.columns:
            ship = pd.to_datetime(df[c], errors="coerce")
            break
    po_col = next((c for c in ("po_number", "PO #") if c in df.columns), None)
    key_cols = [c for c in ("master_id", "sku") if c in df.columns]
    if not key_cols:
        return qty_map, meta
    for col in key_cols:
        keys = df[col].astype(str).str.strip().str.upper()
        ok = keys.ne("") & ~keys.isin(("NAN", "NONE", "<NA>"))
        for k, g in df.loc[ok].groupby(keys[ok], sort=False):
            rec = dict(meta.get(str(k), {}))
            rec["qty"] = qty_map.get(str(k), rec.get("qty", 0))
            if po_col and not rec.get("po_number"):
                rec["po_number"] = (
                    _clean_po_id(g[po_col].dropna().iloc[0]) if g[po_col].notna().any() else ""
                )
            if ship is not None and rec.get("ship_date") is None:
                sd = ship.loc[g.index].dropna()
                rec["ship_date"] = sd.min() if len(sd) else None
            meta[str(k)] = rec
    return qty_map, meta


def _wo_item_type(fulfillment):
    s = str(fulfillment or "").strip().upper()
    if "FBA" in s:
        return "FBA"
    if "FBM" in s:
        return "FBM"
    return "FBA"


def _shelf_qty_for_row(r, meta, qty_map):
    mid = str(r.get("Master ID") or "").strip()
    sku = str(r.get("SKU") or "").strip().upper()
    info = meta.get(mid.upper(), {}) or meta.get(sku, {})
    qty = info.get("qty")
    if qty is None:
        qty = qty_map.get(mid.upper()) or qty_map.get(sku) or r.get("PO Qty")
    return int(pd.to_numeric(qty, errors="coerce") or 0), info


def _shelf_wo_layout_df(display, *, drop_zero=False, one_per_master=False):
    """Shelf-shaped rows from catalogue listings.

    one_per_master: collapse to one FBA listing per Master ID (legacy Slack file).
    drop_zero: skip qty ≤ 0 (upload files). The editor keeps qty 0 so you can type a qty.
    """
    empty = pd.DataFrame(columns=RAISE_COLS)
    if display is None or getattr(display, "empty", True):
        return empty
    meta = st.session_state.get("cat_lookup_meta") or {}
    qty_map = st.session_state.get("cat_lookup_qty") or {}
    ctx_po = ""
    m = re.search(r"PO\s+(\d+)", str(st.session_state.get("cat_lookup_context") or ""), re.I)
    if m:
        ctx_po = m.group(1)

    df = display.copy()
    fulf = df["Fulfillment"].astype(str) if "Fulfillment" in df.columns else pd.Series("", index=df.index)
    df["_fba"] = fulf.str.upper().str.contains("FBA", na=False)
    mid_col = "Master ID" if "Master ID" in df.columns else None

    if one_per_master and mid_col:
        picked = []
        for mid, g in df.groupby(df[mid_col].astype(str).str.strip(), sort=False):
            if not mid or mid.lower() in ("nan", "none"):
                picked.extend(g.index.tolist())
                continue
            g2 = g.sort_values("_fba", ascending=False)
            picked.append(g2.index[0])
        rows_src = df.loc[picked]
    else:
        rows_src = df

    out_rows = []
    for _, r in rows_src.iterrows():
        lid = str(r.get("Listing ID") or "").strip()
        mid = str(r.get("Master ID") or "").strip()
        sku = str(r.get("SKU") or "").strip()
        product = lid if lid and lid.lower() not in ("nan", "none") else mid
        if not product or product.lower() in ("nan", "none"):
            continue
        qty, info = _shelf_qty_for_row(r, meta, qty_map)
        if drop_zero and qty <= 0:
            continue
        po = info.get("po_number") or ctx_po
        out_rows.append({
            "Work Order Item Type": _wo_item_type(r.get("Fulfillment")),
            "Product (Listing ID or Master ID)": product,
            "Request Amount": qty,
            "Ship By Date (MM/DD/YYYY)": _suggested_ship_by(info.get("ship_date")),
            "Prioritized (T/F)": "F",
            "Receivable ID (Inventory Request ID or Purchase Order ID)": po,
            "Receivable type (InventoryRequest or Purchase)": "Purchase" if po else "",
            "SKU": sku,
            "Note": "",
        })
    return pd.DataFrame(out_rows, columns=RAISE_COLS) if out_rows else empty


def _shelf_wo_upload_df(display):
    """Legacy: one FBA listing per Master ID, qty > 0 only (Shelf upload file)."""
    layout = _shelf_wo_layout_df(display, drop_zero=True, one_per_master=True)
    return shelf_send_df(layout)


def _attach_po_qty(display):
    """Put PO outstanding qty on every listing that shares a SKU or Master ID."""
    qty_map = st.session_state.get("cat_lookup_qty") or {}
    if not qty_map or display is None or display.empty:
        return display
    out = display.copy()
    sku_q = None
    mid_q = None
    if "SKU" in out.columns:
        sku_q = out["SKU"].astype(str).str.strip().str.upper().map(qty_map)
    if "Master ID" in out.columns:
        mid_q = out["Master ID"].astype(str).str.strip().str.upper().map(qty_map)
    if sku_q is None and mid_q is None:
        return display
    if sku_q is None:
        combined = mid_q
    elif mid_q is None:
        combined = sku_q
    else:
        combined = sku_q.fillna(mid_q)
    out["PO Qty"] = pd.to_numeric(combined, errors="coerce")
    return out


def _jump_to_catalog_lookup(ids, *, context="", pack=False, qty_frame=None):
    """Switch to Catalogue Lookup with these IDs already pasted, ready to search."""
    parsed = parse_catalog_ids("\n".join(str(i) for i in ids))
    if not parsed:
        st.warning("No SKU / ASIN / Master ID found on those rows to look up.")
        return
    text = "\n".join(parsed)
    st.session_state["cat_lookup_q"] = text
    st.session_state["cat_lookup_submitted"] = text
    st.session_state["cat_lookup_context"] = context or ""
    st.session_state["cat_lookup_pack_mode"] = bool(pack)
    qty_map, meta = _po_line_meta(qty_frame) if qty_frame is not None else ({}, {})
    if qty_map:
        st.session_state["cat_lookup_qty"] = qty_map
    else:
        st.session_state.pop("cat_lookup_qty", None)
    if meta:
        st.session_state["cat_lookup_meta"] = meta
    else:
        st.session_state.pop("cat_lookup_meta", None)
    st.session_state["pending_nav"] = NAV_CATALOG
    st.rerun()


def _jump_to_requests(sub: str = "Raise-WO pack") -> None:
    """Switch to the Requests tab (raise-WO pack or chase list)."""
    st.session_state["pending_nav"] = NAV_REQUESTS
    st.session_state["requests_sub"] = sub
    st.rerun()


def _attach_line_search(po_pos, po_df):
    """Let PO-level Search match SKU / Master ID / title from the line items."""
    if po_pos is None or getattr(po_pos, "empty", True):
        return po_pos
    out = po_pos
    if po_df is None or getattr(po_df, "empty", True) or "po_number" not in po_df.columns:
        return out
    parts = [c for c in ("sku", "asin", "master_id", "title") if c in po_df.columns]
    if not parts:
        return out
    blob = (
        po_df[parts].fillna("").astype(str)
        .agg(" ".join, axis=1)
        .groupby(po_df["po_number"])
        .agg(lambda s: " ".join(dict.fromkeys(x for x in s if str(x).strip())))
    )
    out = out.copy()
    out["_line_search"] = out["po_number"].map(blob).fillna("")
    return out


def _po_pack_context(po, row):
    """One-line PO context for a raise-WO Slack note."""
    vendor = str(row.get("vendor_name") or row.get("Vendor") or "").strip()
    wh = str(row.get("warehouse_name") or row.get("WH") or "").strip()
    ship = row.get("ship_date") if row.get("ship_date") is not None else row.get("Ship Date")
    placed = row.get("order_placed") if row.get("order_placed") is not None else row.get("Order Placed")
    bits = [f"PO {po}"]
    if vendor:
        bits.append(vendor)
    if wh:
        bits.append(wh)
    if ship is not None and str(ship) not in ("", "NaT", "nan", "None"):
        bits.append(f"req ship {_safe_date_str(ship)}")
    elif placed is not None and str(placed) not in ("", "NaT", "nan", "None"):
        bits.append(f"placed {_safe_date_str(placed)}")
    return " · ".join(bits)


def _catalog_slack_note(display, context="", skipped=0):
    """Short raise-WO prefill — listing IDs, master IDs, and PO qty + CSV."""
    n = len(display)
    head = "Please raise a WO"
    if context:
        head = f"Please raise a WO — {context}"
    lines = [f"{head}."]
    qty_map = st.session_state.get("cat_lookup_qty") or {}
    has_mid = "Master ID" in display.columns
    has_lid = "Listing ID" in display.columns
    has_qty = "PO Qty" in display.columns or bool(qty_map)

    groups = []
    if has_mid:
        for mid, g in display.groupby(display["Master ID"].astype(str).str.strip(), sort=False):
            if not mid or mid.lower() in ("nan", "none"):
                continue
            qty = qty_map.get(mid.upper())
            if qty is None and has_qty and "PO Qty" in g.columns:
                q = pd.to_numeric(g["PO Qty"], errors="coerce").max()
                qty = 0 if pd.isna(q) else int(q)
            if qty is None:
                qty = 0
            lids = []
            if has_lid:
                seen = set()
                for v in g["Listing ID"].astype(str):
                    s = v.strip()
                    if not s or s.lower() in ("nan", "none") or s in seen:
                        continue
                    seen.add(s)
                    lids.append(s)
            groups.append((int(qty), mid, lids))
        qty_total = sum(q for q, _, _ in groups)
        extra_qty = f" · {qty_total:,} units outstanding" if has_qty else ""
        lines.append(
            f"{n} listing(s) · {len(groups)} master ID(s){extra_qty}. "
            "CSV has listing ID, master ID, and qty."
        )
    else:
        lines.append(f"{n} listing ID(s) in the attached CSV.")
    if skipped:
        lines.append(f"{skipped} hidden by filters.")
    if groups:
        lines.append("")
        lines.append("Qty by master ID (same qty on every listing of that master):")
        groups.sort(key=lambda row: -row[0])
        cap = 12
        for qty, mid, lids in groups[:cap]:
            lid_bit = ", ".join(lids[:4]) + (f" +{len(lids) - 4}" if len(lids) > 4 else "")
            if has_qty:
                lines.append(f"{mid} ×{qty}" + (f" — {lid_bit}" if lid_bit else ""))
            else:
                lines.append(f"{mid} — {lid_bit}" if lid_bit else str(mid))
        rest = len(groups) - cap
        if rest > 0:
            lines.append(f"+{rest} more master IDs in the CSV")
    return "\n".join(lines)


def _po_item_status(df):
    """Unified lifecycle status at PO line-item grain (see PO_STATUS_ORDER)."""
    recv = pd.to_numeric(df.get("received_units"), errors="coerce").fillna(0)
    cur = pd.to_numeric(df.get("current_units"), errors="coerce").fillna(0)
    iss = pd.to_numeric(df.get("total_issues"), errors="coerce").fillna(0)
    status = pd.Series("🟢 In progress", index=df.index)
    status = status.mask(recv <= 0, "🟡 Placed")
    status = status.mask((recv > 0) & (recv < cur), "🟠 Partial")
    status = status.mask((cur > 0) & (recv >= cur), "✅ Complete")
    status = status.mask(iss > 0, "🔴 Issue")   # issue overrides — highest attention
    return status


def _destination_totals_from_pos(po_pos):
    """Sum destination columns already on the PO rollup."""
    totals = {k: 0 for k in (*_DEST_SHOW_KEYS, "Stow", "Other")}
    if po_pos is None or getattr(po_pos, "empty", True):
        return totals, 0
    colmap = dict(_DEST_SHOW_COLS)
    colmap["Other"] = "other_units"
    for k, c in colmap.items():
        if c in po_pos.columns:
            totals[k] = int(pd.to_numeric(po_pos[c], errors="coerce").fillna(0).sum())
    ordered = 0
    if "ordered" in po_pos.columns:
        ordered = int(pd.to_numeric(po_pos["ordered"], errors="coerce").fillna(0).sum())
    elif "ordered_units" in po_pos.columns:
        ordered = int(pd.to_numeric(po_pos["ordered_units"], errors="coerce").fillna(0).sum())
    return totals, ordered


def _destination_from_wo_items(wo_items, ordered):
    """Named dest from WO current qty; stow = leftover of current ordered."""
    totals = {k: 0 for k in (*_DEST_SHOW_KEYS, "Stow", "Other")}
    ordered = int(ordered or 0)
    if wo_items is not None and not getattr(wo_items, "empty", True) and "woi_type" in wo_items.columns:
        qty_col = "current_request" if "current_request" in wo_items.columns else "original_request"
        if qty_col in wo_items.columns:
            qty = pd.to_numeric(wo_items[qty_col], errors="coerce").fillna(0)
            summed = qty.groupby(_wo_dest_bucket_series(wo_items["woi_type"])).sum()
            for k in (*_DEST_SHOW_KEYS, "Other"):
                totals[k] = int(round(float(summed.get(k, 0))))
    raised = sum(totals[k] for k in (*_DEST_SHOW_KEYS, "Other"))
    totals["Stow"] = max(ordered - raised, 0)
    return totals, ordered


def _attach_item_wo_destination(items, wo_df):
    """Per PO line: dest qty from WO items on the same PO × Master ID."""
    if items is None or getattr(items, "empty", True):
        return items
    out = items.copy()
    ordered = pd.to_numeric(out["ordered_units"], errors="coerce").fillna(0) if "ordered_units" in out.columns else 0
    unit_cols = _DEST_UNIT_COLS
    for c in unit_cols:
        out[c] = 0
    can_join = (
        wo_df is not None and not getattr(wo_df, "empty", True)
        and "woi_type" in wo_df.columns and "master_id" in out.columns
        and "po_number_raw" in wo_df.columns and "po_number" in out.columns
    )
    if can_join:
        w = wo_df.copy()
        qty_col = "current_request" if "current_request" in w.columns else "original_request"
        if qty_col in w.columns:
            w["_po"] = _ov_po_str(w["po_number_raw"])
            w["_mid"] = w["master_id"].astype(str).str.strip().str.upper()
            w["_bucket"] = _wo_dest_bucket_series(w["woi_type"])
            w["_qty"] = pd.to_numeric(w[qty_col], errors="coerce").fillna(0)
            g = w.groupby(["_po", "_mid", "_bucket"], as_index=False)["_qty"].sum()
            rename = {
                "FBA": "fba_units", "FBB": "fbb_units", "FBM": "fbm_units",
                "ZFS": "zfs_units", "OCT": "oct_units", "Other": "other_units",
            }
            pvt = (
                g.pivot_table(index=["_po", "_mid"], columns="_bucket", values="_qty",
                              aggfunc="sum", fill_value=0)
                .reindex(columns=list(rename), fill_value=0)
                .rename(columns=rename)
                .reset_index()
            )
            out["_po"] = _ov_po_str(out["po_number"])
            out["_mid"] = out["master_id"].astype(str).str.strip().str.upper()
            out = out.merge(pvt, on=["_po", "_mid"], how="left", suffixes=("", "_wo"))
            for c in unit_cols:
                wo_c = f"{c}_wo"
                if wo_c in out.columns:
                    out[c] = pd.to_numeric(out[wo_c], errors="coerce").fillna(0)
                    out = out.drop(columns=[wo_c])
                else:
                    out[c] = pd.to_numeric(out.get(c), errors="coerce").fillna(0)
            out = out.drop(columns=["_po", "_mid"])
    for c in unit_cols:
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0).astype(int)
    raised = sum(out[c] for c in unit_cols)
    out["stow_units"] = (ordered - raised).clip(lower=0).astype(int)
    return out


def render_po_destination_split(df=None, *, wo_items=None, ordered=None):
    """KPI row: where current ordered units are headed (from WO types)."""
    if df is not None and not getattr(df, "empty", True) and "fba_units" in df.columns:
        totals, ordered_n = _destination_totals_from_pos(df)
    else:
        totals, ordered_n = _destination_from_wo_items(wo_items, ordered or 0)

    def _pct(n):
        return f"{n * 100.0 / ordered_n:.0f}% of current ordered" if ordered_n else None

    st.markdown("##### Where current ordered units are going")
    c = st.columns(6)
    c[0].metric("FBA", f"{totals['FBA']:,}", _pct(totals["FBA"]), delta_color="off")
    c[1].metric("FBB", f"{totals['FBB']:,}", _pct(totals["FBB"]), delta_color="off")
    c[2].metric("FBM", f"{totals['FBM']:,}", _pct(totals["FBM"]), delta_color="off")
    c[3].metric("ZFS", f"{totals['ZFS']:,}", _pct(totals["ZFS"]), delta_color="off")
    c[4].metric("OCT", f"{totals['OCT']:,}", _pct(totals["OCT"]), delta_color="off")
    c[5].metric("To stow", f"{totals['Stow']:,}", "leftover after WO types", delta_color="off")
    st.caption(
        "FBA / FBB / FBM / ZFS / OCT are **work-order item types** (WO current qty). "
        "**To stow** is current ordered minus all WO qty — leftover with no marketplace WO. "
        "The PO report fulfillment method is often blank, so it is not used here."
    )


def _build_po_aggregates(df):
    """Roll line items up to one row per PO."""
    g = df.groupby("po_number", as_index=False).agg(
        vendor_name=("vendor_name", "first"),
        country_name=("country_name", "first"),
        warehouse_name=("warehouse_name", "first"),
        purchase_state=("purchase_state", "first"),
        po_type=("po_type", "first"),
        fulfillment=("fulfillment_method",
                     lambda s: next((str(x).strip() for x in s
                                     if pd.notna(x) and str(x).strip()), "")),
        lines=("item_id", "count"),
        original_ordered=("original_ordered_units", "sum"),
        ordered=("ordered_units", "sum"),
        received=("received_units", "sum"),
        on_order=("current_on_order", "sum"),
        order_placed=("order_placed_date", "min"),
        ship_date=("ship_date", "min"),
        first_arrival=("arrived_date", "min"),
        last_received=("po_last_received_date", "max"),
        issues=("total_issues", "sum"),
    )
    g["left"] = (g["ordered"] - g["received"]).clip(lower=0)
    g["demand_fill_pct"] = np.where(
        g["original_ordered"].fillna(0) > 0,
        (g["received"].fillna(0) * 100.0 / g["original_ordered"].replace(0, np.nan)).round(1), 0)
    g["vendor_fill_pct"] = np.where(
        g["ordered"].fillna(0) > 0,
        (g["received"].fillna(0) * 100.0 / g["ordered"].replace(0, np.nan)).round(1), 0)
    g["demand_fill_pct"] = pd.to_numeric(g["demand_fill_pct"], errors="coerce").fillna(0)
    g["vendor_fill_pct"] = pd.to_numeric(g["vendor_fill_pct"], errors="coerce").fillna(0)

    sev = {s: i for i, s in enumerate(PO_STATUS_ORDER)}
    tmp = df[["po_number", "po_status"]].copy()
    tmp["_sev"] = tmp["po_status"].map(sev).fillna(len(PO_STATUS_ORDER))
    worst = (tmp.sort_values("_sev").drop_duplicates("po_number", keep="first")
             .set_index("po_number")["po_status"])
    g["status"] = g["po_number"].map(worst)
    return g


def po_filter_panel(df, key, *, date_col=None, status_col=None, search_cols=None,
                    ship_col="ship_date"):
    """Filters for PO Details: vendor/country/WH/state, type, fulfillment, WO coverage, dates, search."""
    out = df.copy()
    ftype_col = next((c for c in ("fulfillment", "fulfillment_method") if c in out.columns), None)

    def _multi(col, label, cont):
        nonlocal out
        if col not in out.columns:
            return
        opts = sorted([str(v) for v in out[col].dropna().unique()
                       if str(v).strip() and str(v).lower() != "nan"])
        pick = cont.multiselect(label, opts, key=f"{key}_{col}", placeholder=f"All {label.lower()}")
        if pick:
            out = out[out[col].astype(str).isin(pick)]

    with st.expander("🔎 Filters", expanded=True):
        c1, c2, c3, c4 = st.columns(4)
        _multi("vendor_name", "Vendor", c1)
        _multi("country_name", "Country", c2)
        _multi("warehouse_name", "Warehouse", c3)
        _multi("purchase_state", "State", c4)
        d1, d2, d3, d4 = st.columns(4)
        if status_col and status_col in out.columns:
            sopts = [s for s in PO_STATUS_ORDER if (out[status_col] == s).any()]
            sp = d1.selectbox("Status", ["All"] + sopts, key=f"{key}_status")
            if sp != "All":
                out = out[out[status_col] == sp]
        _multi("po_type", "PO Type", d2)
        if ftype_col:
            _multi(ftype_col, "Fulfillment", d3)
        wo_cov = WO_COVERAGE_ALL
        if "wo_count" in out.columns:
            wo_cov = d4.selectbox(
                "WO coverage", WO_COVERAGE_OPTIONS, key=f"{key}_woc",
                help="No WO (any) = every PO with 0 work orders. "
                     "No WO (active) matches Overview (excludes reconciled/cancelled).",
            )
        else:
            d4.caption("WO coverage needs the PO→WO rollup.")
        e1, e2, e3, e4 = st.columns(4)
        if date_col and date_col in out.columns:
            sfrom = e1.date_input("Order placed — from", value=None, key=f"{key}_from", format="YYYY-MM-DD")
            sto = e2.date_input("Order placed — to", value=None, key=f"{key}_to", format="YYYY-MM-DD")
            dt = pd.to_datetime(out[date_col], errors="coerce")
            if sfrom:
                out = out[dt >= pd.Timestamp(sfrom)]
                dt = dt.loc[out.index]
            if sto:
                out = out[dt <= (pd.Timestamp(sto) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1))]
        if ship_col and ship_col in out.columns:
            sbfrom = e3.date_input("Ship-by — from", value=None, key=f"{key}_sbfrom", format="YYYY-MM-DD")
            sbto = e4.date_input("Ship-by — to", value=None, key=f"{key}_sbto", format="YYYY-MM-DD")
            sdt = pd.to_datetime(out[ship_col], errors="coerce")
            if sbfrom:
                out = out[sdt >= pd.Timestamp(sbfrom)]
                sdt = sdt.loc[out.index]
            if sbto:
                out = out[sdt <= (pd.Timestamp(sbto) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1))]
        if "wo_count" in out.columns:
            out = po_coverage_mask(out, wo_cov, grace_days=NO_WO_GRACE_DAYS)
        if search_cols:
            q = st.text_input(
                "Search  (PO # · vendor · SKU · Master ID · title)",
                "", key=f"{key}_search",
                placeholder="paste several — comma / new line = match any",
            )
            if q:
                cols = [c for c in search_cols if c in out.columns]
                if cols:
                    out = out[_str_contains_any(out, cols, q)]
    if "_line_search" in out.columns:
        out = out.drop(columns="_line_search")
    return out


_PO_ITEM_COLS = [
    ("po_number", "PO #"), ("po_status", "Status"),
    ("order_placed_date", "Order Placed"), ("ship_date", "Ship Date"), ("arrived_date", "Arrived"),
    ("po_last_received_date", "PO Last Recv"), ("item_last_received_date", "Item Last Recv"),
    ("finished_arrived_date", "Finished Arrived"), ("cancel_date", "Cancel Date"),
    ("sku", "SKU"), ("asin", "ASIN"), ("master_id", "Master ID"), ("item_id", "Item ID"),
    ("part_number", "Part #"), ("title", "Title"),
    ("vendor_name", "Vendor"), ("country_name", "Country"), ("warehouse_name", "WH"),
    ("purchase_state", "State"), ("po_type", "PO Type"),
    ("fulfillment_method", "Fulfillment"), ("note", "Note"),
    ("original_ordered_units", "Orig Ordered"), ("ordered_units", "Current Ordered"),
    ("current_on_order", "On Order"),
    ("received_units", "Received"),
    ("fba_units", "FBA"), ("fbb_units", "FBB"), ("fbm_units", "FBM"),
    ("zfs_units", "ZFS"), ("oct_units", "OCT"),
    ("stow_units", "To Stow"), ("remained_blanket_order_quantity", "Remained Blanket"),
    ("wholesale_price", "Wholesale £"), ("retail_price", "Retail £"),
    ("wholesale_ordered", "WS Ordered £"), ("wholesale_received", "WS Received £"),
    ("demand_fill_rate_pct", "Demand Fill %"), ("vendor_fill_rate_pct", "Vendor Fill %"),
    ("total_issues", "Issues"), ("wo_count", "PO WOs"),
]
_PO_ITEM_DEFAULT = ["PO #", "Status", "Order Placed", "Ship Date", "PO Last Recv", "SKU", "ASIN",
                    "Master ID", "Title", "Vendor", "WH", "Fulfillment",
                    "Orig Ordered", "Current Ordered", "Received",
                    "FBA", "FBB", "FBM", "ZFS", "OCT", "To Stow", "On Order",
                    "Demand Fill %", "Vendor Fill %", "Issues", "PO WOs"]
_PO_ITEM_DATE_COLS = ["Order Placed", "Ship Date", "Arrived", "PO Last Recv", "Item Last Recv",
                      "Finished Arrived", "Cancel Date"]


def _po_item_display(df, include_po=True):
    d = df.copy()
    for c in ("po_number", "item_id"):
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce").astype("Int64").astype(str).replace("<NA>", "")
    pairs = [(r, l) for r, l in _PO_ITEM_COLS if r in d.columns and (include_po or r != "po_number")]
    disp = d[[r for r, _ in pairs]].rename(columns=dict(pairs))
    default = [l for l in _PO_ITEM_DEFAULT if l in disp.columns and (include_po or l != "PO #")]
    return disp, default


def po_details_kpi(lines, po_pos=None):
    """PO summary cards computed from the (filtered) PO line-item frame."""
    orig = pd.to_numeric(lines.get("original_ordered_units"), errors="coerce").fillna(0).sum()
    ordered = pd.to_numeric(lines.get("ordered_units"), errors="coerce").fillna(0).sum()
    received = pd.to_numeric(lines.get("received_units"), errors="coerce").fillna(0).sum()
    issues = int((pd.to_numeric(lines.get("total_issues"), errors="coerce").fillna(0) > 0).sum())
    fill = (received * 100.0 / ordered) if ordered else 0
    n_pos = lines["po_number"].nunique() if "po_number" in lines.columns else 0
    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    c1.metric("POs", f"{n_pos:,}")
    c2.metric("Line items", f"{len(lines):,}")
    c3.metric("Orig ordered", f"{int(orig):,}")
    c4.metric("Current ordered", f"{int(ordered):,}")
    c5.metric("Received", f"{int(received):,}")
    c6.metric("Vendor fill", f"{fill:.0f}%")
    c7.metric("Lines w/ issues", f"{issues:,}")
    dest = None
    if po_pos is not None and not getattr(po_pos, "empty", True) and "fba_units" in po_pos.columns:
        dest = po_pos
    elif "fba_units" in lines.columns:
        dest = lines
    if dest is not None and not dest.empty:
        render_po_destination_split(dest)
    else:
        render_po_destination_split(ordered=int(ordered))


def po_details_list(po_pos, po_df):
    src = _attach_line_search(po_pos, po_df)
    filtered = po_filter_panel(
        src, "fp_pod", date_col="order_placed", status_col="status",
        search_cols=["po_number", "vendor_name", "country_name", "warehouse_name",
                     "po_type", "fulfillment", "_line_search"],
        ship_col="ship_date",
    )
    filtered = _drop_zero_ordered(filtered)
    sev = {s: i for i, s in enumerate(PO_STATUS_ORDER)}
    filtered = (filtered.assign(_sev=filtered["status"].map(sev).fillna(len(PO_STATUS_ORDER)))
                .sort_values(["_sev", "po_number"]).drop(columns="_sev"))
    lines = po_df[po_df["po_number"].isin(filtered["po_number"])] if po_df is not None else po_df
    po_details_kpi(lines, po_pos=filtered)
    if "wo_count" in filtered.columns:
        nwith = int((filtered["wo_count"] > 0).sum())
        n_no = int((filtered["wo_count"] == 0).sum())
        st.caption(f"🔗 {nwith:,} of {len(filtered):,} POs have matching work orders "
                   f"({nwith * 100 // max(len(filtered), 1)}%). "
                   f"{n_no:,} currently have no WO — use WO coverage in Filters, or add them to the chase list.")
        act = st.columns(2)
        with act[0]:
            if st.button(f"📌 Add {n_no:,} no-WO PO(s) to chase list", key="pod_chase",
                         disabled=n_no == 0, use_container_width=True,
                         help="Follow-up list only — does not create a WO in Shelf."):
                add_chase_rows(chase_rows_from_pos(filtered[filtered["wo_count"] == 0], kind="PO no-WO"))
                _jump_to_requests("Chase list")
        with act[1]:
            if st.button("📦 Look up listings for filtered POs", key="pod_list_cat_all",
                         use_container_width=True,
                         help="Open Catalogue Lookup with SKUs from the filtered POs."):
                ids = _catalog_ids_for_lookup(lines)
                _jump_to_catalog_lookup(
                    ids,
                    context=f"{len(filtered):,} filtered PO(s) from PO Details",
                    pack=True, qty_frame=lines,
                )
    base_cols = ["po_number", "status", "vendor_name", "country_name", "warehouse_name", "purchase_state",
                 "po_type", "fulfillment",
                 "order_placed", "ship_date", "first_arrival", "last_received", "lines",
                 "original_ordered", "ordered", "received",
                 "fba_units", "fbb_units", "fbm_units", "zfs_units", "oct_units", "stow_units",
                 "left", "on_order", "demand_fill_pct", "vendor_fill_pct"]
    base_cols = [c for c in base_cols if c in filtered.columns]
    wo_cols = [c for c in ["wo_count", "wo_current", "wo_processed", "wo_ship_created", "wo_shipped", "wo_stowed"]
               if c in filtered.columns]
    display = filtered[base_cols + wo_cols].rename(columns={
        "po_number": "PO #", "status": "Status", "vendor_name": "Vendor", "country_name": "Country",
        "warehouse_name": "WH", "purchase_state": "State", "po_type": "PO Type",
        "fulfillment": "Fulfillment", "order_placed": "Order Placed",
        "ship_date": "Ship Date", "first_arrival": "First Arrival",
        "last_received": "Last Received", "lines": "Lines", "original_ordered": "Orig Ordered",
        "ordered": "Current Ordered", "received": "Received", "left": "Left", "on_order": "On Order",
        "fba_units": "FBA", "fbb_units": "FBB", "fbm_units": "FBM",
        "zfs_units": "ZFS", "oct_units": "OCT",
        "stow_units": "To Stow",
        "demand_fill_pct": "Demand Fill %", "vendor_fill_pct": "Vendor Fill %",
        "wo_count": "WOs", "wo_current": "WO Current", "wo_processed": "WO Processed",
        "wo_ship_created": "WO Ship Created", "wo_shipped": "WO Shipped", "wo_stowed": "WO Stowed",
    })
    display["PO #"] = pd.to_numeric(display["PO #"], errors="coerce").astype("Int64").astype(str).replace("<NA>", "")
    cols = column_picker(list(display.columns), key="cols_pod_dest", required=["PO #"])
    display = display[cols]

    table_toolbar(display, key="tb_pod", file_stem="po_details",
                  id_cols=["PO #", "Vendor"], count_label=f"{len(display)} of {len(po_pos)} POs")
    sel = render_table(
        display, key=_grid_key("grid_pod"), selectable=True, select_col="PO #",
        numpct_cols=["Demand Fill %", "Vendor Fill %"],
        date_cols=["Order Placed", "Ship Date", "First Arrival", "Last Received"], pin_cols=["PO #"],
        color_rows=True, height=740,
    )
    if sel is not None:
        po = _safe_int(sel)
        b1, b2, b3 = st.columns(3)
        with b1:
            if st.button(f"➡ Open PO {po}", type="primary", key="open_pod", use_container_width=True):
                st.session_state.selected_po_detail = po
                st.rerun()
        lines = po_df[po_df["po_number"] == po] if po_df is not None else None
        ids = _catalog_ids_for_lookup(lines)
        ctx_row = filtered[filtered["po_number"] == po]
        ctx = _po_pack_context(po, ctx_row.iloc[0]) if ctx_row is not None and not ctx_row.empty else f"PO {po}"
        with b2:
            if st.button("🔎 Look up listings", key="pod_list_cat", use_container_width=True,
                         help="Open Catalogue Lookup with this PO's SKUs / Master IDs"):
                _jump_to_catalog_lookup(ids, context=ctx, qty_frame=lines)
        with b3:
            if st.button("📦 Raise-WO pack", key="pod_list_pack", use_container_width=True,
                         help="Catalogue Lookup with DNO/inactive/not-shippable hidden, ready to Slack"):
                _jump_to_catalog_lookup(ids, context=ctx, pack=True, qty_frame=lines)


def po_details_items(po_df, po_pos=None, wo_df=None):
    items = po_df.copy()
    if po_pos is not None and "wo_count" in po_pos.columns and "po_number" in items.columns:
        if "wo_count" not in items.columns:
            items = items.merge(
                po_pos[["po_number", "wo_count"]].drop_duplicates("po_number"),
                on="po_number", how="left",
            )
            items["wo_count"] = pd.to_numeric(items["wo_count"], errors="coerce").fillna(0)
    filtered = po_filter_panel(
        items, "fp_podi", date_col="order_placed_date", status_col="po_status",
        search_cols=["po_number", "sku", "asin", "master_id", "title", "vendor_name"],
        ship_col="ship_date",
    )
    filtered = _drop_zero_ordered(filtered)
    filtered = _attach_item_wo_destination(filtered, wo_df)
    po_details_kpi(filtered)
    ia, ib, ic = st.columns(3)
    with ia:
        if st.button("🔎 Look up listings for these items", key="podi_cat",
                     use_container_width=True,
                     help="Open Catalogue Lookup with the SKUs / Master IDs currently in this table"):
            _jump_to_catalog_lookup(
                _catalog_ids_for_lookup(filtered),
                context=f"{len(filtered):,} PO item(s) from PO Details",
                pack=True, qty_frame=filtered,
            )
    with ib:
        if st.button("📌 Add these items to chase list", key="podi_chase",
                     use_container_width=True,
                     help="Follow-up list only — does not create a WO in Shelf."):
            add_chase_rows(chase_rows_from_pos(filtered, kind="PO item"))
            _jump_to_requests("Chase list")
    with ic:
        if st.button("📦 Add to raise-WO pack", key="podi_raise",
                     use_container_width=True,
                     help="Copies outstanding qty into 📋 Requests. Does not create a WO in Shelf."):
            add_raise_rows(raise_rows_from_gap(filtered))
            _jump_to_requests("Raise-WO pack")
    disp, default = _po_item_display(filtered, include_po=True)
    cols = column_picker(list(disp.columns), key="cols_podi_dest", default_labels=default, required=["PO #"])
    disp = disp[cols]
    table_toolbar(disp, key="tb_podi", file_stem="po_items",
                  id_cols=["PO #", "SKU", "ASIN", "Master ID"], count_label=f"{len(disp):,} line items")
    render_table(
        disp, key=_grid_key("grid_podi"),
        numpct_cols=["Demand Fill %", "Vendor Fill %"],
        date_cols=_PO_ITEM_DATE_COLS, pin_cols=["PO #"], color_rows=True, height=720,
    )


def po_details_drilldown(po, po_df, po_pos, wo_df):
    row = po_pos[po_pos["po_number"] == po].iloc[0]
    top1, top2 = st.columns([1, 5])
    if top1.button("← Back to list", use_container_width=True, key="back_pod"):
        _reset_selection()
        st.rerun()
    top2.markdown(f"### PO {po} — {row['vendor_name']} · {row['warehouse_name']} · {row['status']}")
    st.caption(f"{row['country_name']} · state: {row['purchase_state']} · "
               f"{_safe_int(row['lines'])} line(s) · placed {_safe_date_str(row['order_placed'])}")
    pack_l, pack_r = st.columns(2)
    items_for_ids = po_df[po_df["po_number"] == po]
    ids = _catalog_ids_for_lookup(items_for_ids)
    ctx = _po_pack_context(po, row)
    with pack_l:
        if st.button("🔎 Look up listings", key=f"pod_dd_cat_{po}", use_container_width=True):
            _jump_to_catalog_lookup(ids, context=ctx, qty_frame=items_for_ids)
    with pack_r:
        if st.button("📦 Raise-WO pack", key=f"pod_dd_pack_{po}", use_container_width=True,
                     help="Catalogue Lookup with unsellable listings hidden, PO details in the Slack note"):
            _jump_to_catalog_lookup(ids, context=ctx, pack=True, qty_frame=items_for_ids)

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Orig Ordered", f"{_safe_int(row['original_ordered']):,}")
    c2.metric("Current Ordered", f"{_safe_int(row['ordered']):,}")
    c3.metric("Received", f"{_safe_int(row['received']):,}")
    c4.metric("Left", f"{_safe_int(row['left']):,}")
    c5.metric("Demand Fill", f"{row['demand_fill_pct']:.0f}%")
    c6.metric("Vendor Fill", f"{row['vendor_fill_pct']:.0f}%")
    if "po_number_raw" in wo_df.columns:
        awo = wo_df[wo_df["po_number_raw"].astype(str) == str(po)].copy()
    else:
        awo = wo_df.iloc[0:0].copy() if wo_df is not None else None
    render_po_destination_split(
        po_pos[po_pos["po_number"] == po],
        wo_items=awo,
        ordered=_safe_int(row.get("ordered")),
    )

    items = _drop_zero_ordered(po_df[po_df["po_number"] == po].copy())
    items = _attach_item_wo_destination(items, awo)
    st.markdown("---")
    st.markdown(f"#### 📄 Items in PO {po}")
    filtered = po_filter_panel(
        items, f"fp_pod_items_{po}", date_col="order_placed_date", status_col="po_status",
        search_cols=["sku", "asin", "master_id", "title"],
    )
    disp, default = _po_item_display(filtered, include_po=False)
    cols = column_picker(list(disp.columns), key=f"cols_pod_items_dest_{po}", default_labels=default, required=["SKU"])
    disp = disp[cols]
    table_toolbar(disp, key=f"tb_pod_items_{po}", file_stem=f"po_{po}_items",
                  id_cols=["SKU", "ASIN", "Master ID"], count_label=f"{len(disp)} line items")
    render_table(
        disp, key=_grid_key(f"grid_pod_items_{po}"),
        numpct_cols=["Demand Fill %", "Vendor Fill %"],
        date_cols=_PO_ITEM_DATE_COLS, pin_cols=["SKU"], color_rows=True, height=560,
    )

    st.markdown("---")
    st.markdown("#### 🔗 Associated Work Orders")
    if awo is None or awo.empty:
        st.caption("No work orders linked to this PO in the current WO dataset (year-to-date).")
    else:
        awo_pairs = [
            ("work_order_item_id", "WOI ID"), ("work_order_number", "WO"),
            ("woi_type", "WO Type"),
            ("master_id", "Master ID"), ("listing_id", "Listing"),
            ("finished_good_name", "Item Name"), ("source_brand", "Brand"), ("warehouse", "WH"),
            ("status_simple", "Status"), ("po_block_flag", "Flag"),
            ("processing_status", "Block Status"), ("block_reason_pfs", "Reason"),
            ("original_request", "Orig"), ("current_request", "Current"), ("processed", "Processed"),
            ("order_created", "Ship Created"), ("shipped", "Shipped"), ("storage", "Stowed"),
            ("woi_processing_pct", "%"), ("po_ref_ship_by_date", "Ref Ship-by"),
            ("po_days_past_ref_ship_by", "Days Past"), ("days_overdue", "Days Overdue"),
            ("created_at", "Created At"),
        ]
        pairs = [(r, l) for r, l in awo_pairs if r in awo.columns]
        awo_disp = awo[[r for r, _ in pairs]].rename(columns=dict(pairs))
        awo_default = [l for l in ["WOI ID", "WO", "WO Type", "Master ID", "Listing", "Item Name", "Brand",
                                   "WH", "Status", "Flag", "Block Status", "Reason", "Orig",
                                   "Current", "Processed", "%", "Ref Ship-by", "Days Past",
                                   "Days Overdue"] if l in awo_disp.columns]
        acols = column_picker(list(awo_disp.columns), key=f"cols_pod_awo_dest_{po}",
                              default_labels=awo_default, required=["WOI ID"])
        awo_disp = awo_disp[acols]
        table_toolbar(awo_disp, key=f"tb_pod_awo_{po}", file_stem=f"po_{po}_workorders",
                      id_cols=["WO", "WOI ID", "Listing", "Master ID"], count_label=f"{len(awo_disp)} WO item(s)")
        render_table(
            awo_disp, key=_grid_key(f"grid_pod_awo_{po}"),
            pct_cols=["%"], date_cols=["Ref Ship-by"], datetime_cols=["Created At"],
            pin_cols=["WOI ID"], color_rows=True, height=340,
        )


def po_details_tab(wo_df):
    try:
        with st.spinner("Loading PO data from Snowflake..."):
            po_df, po_pos, _ = fetch_po_data(_wh_scope_arg())
    except Exception as e:
        st.error(f"Couldn't load PO Details: {e}")
        st.caption("Confirm the app can read ANALYTICS_DB.REPORTING and that queries/po_tracker.sql is deployed.")
        return

    wh = st.session_state.get("global_wh", "All in scope")
    if not _wh_is_all(wh):
        po_df = po_df[po_df["warehouse_name"] == wh].copy()
        po_pos = po_pos[po_pos["warehouse_name"] == wh].copy()

    # Enrich the PO rollup with WO counts and dest qty (FBA/FBB/FBM/ZFS/OCT).
    po_pos = _enrich_po_pos_with_wo(po_pos, wo_df)

    sel = st.session_state.get("selected_po_detail")
    if sel is not None and sel in po_pos["po_number"].values:
        po_details_drilldown(sel, po_df, po_pos, wo_df)
        return

    view = st.radio("View", ["📋 PO Level", "📄 Item Level"], horizontal=True,
                    key="po_details_view", label_visibility="collapsed")
    st.caption("Inbound POs (UK/EU) · fill rates are uncapped (>100% = over-receipts) · "
               "💡 tick a PO then Open PO · drag-select + Ctrl+C to copy · cards react to the filters")
    if view == "📋 PO Level":
        po_details_list(po_pos, po_df)
    else:
        po_details_items(po_df, po_pos, wo_df)


# ============================================================
# OVERVIEW (Phase 3a) — exceptions-first landing
# ============================================================

def _ov_brand_opts(*frames):
    brands = set()
    for frame, col in frames:
        if frame is None or getattr(frame, "empty", True) or col not in frame.columns:
            continue
        brands.update(
            str(v).strip() for v in frame[col].dropna().unique()
            if str(v).strip() and str(v).lower() != "nan"
        )
    return sorted(brands)


def _scope_one(frame, pick, q, *, brand_cols, search_cols):
    if frame is None or getattr(frame, "empty", True):
        return frame
    out = frame
    brand_present = [c for c in brand_cols if c in out.columns]
    if pick and brand_present:
        mask = pd.Series(False, index=out.index)
        for c in brand_present:
            mask = mask | out[c].astype(str).isin(pick)
        out = out[mask]
    if q:
        cols = [c for c in search_cols if c in out.columns]
        if cols:
            out = out[_str_contains_any(out, cols, q)]
    return out


def _overview_quick_filters(df, wos, po_df, po_pos):
    opts = _ov_brand_opts(
        (df, "source_brand"), (wos, "source_brand"),
        (po_df, "vendor_name"), (po_pos, "vendor_name"),
    )
    with st.expander("🔎 Overview filters", expanded=True):
        pick = st.multiselect(
            "Brand / vendor", opts, key="fp_ov_brand",
            placeholder="All brands / vendors",
        )
        q = st.text_input(
            "Search", "", key="fp_ov_search",
            placeholder="PO # · SKU · Master ID · WO · title · vendor — paste several to match any",
        )
        st.caption(
            "Applies to the tiles and every Needs-attention panel (full match, then each table "
            f"shows the top {OV_MAX_ROWS}). Does not change the header Warehouse picker."
        )
    return pick, q


def _ov_adv_filters(df, key, *, fields, qty_col=None, qty_label="Min qty"):
    """Collapsed vendor / WH / state / coverage filters plus search for one panel."""
    if df is None or getattr(df, "empty", True):
        return df
    out = df
    with st.expander("Advanced filters", expanded=False):
        n = max(len(fields), 1)
        cols = st.columns(min(4, n))
        for i, (label, col) in enumerate(fields):
            if col not in out.columns:
                continue
            opts = sorted(
                str(v).strip() for v in out[col].dropna().unique()
                if str(v).strip() and str(v).lower() not in ("nan", "none", "<na>")
            )
            if not opts:
                continue
            pick = cols[i % len(cols)].multiselect(
                label, opts, key=f"ovadv_{key}_{col}",
                placeholder=f"All {label.lower()}",
            )
            if pick:
                out = out[out[col].astype(str).isin(pick)]
        q1, q2 = st.columns([2, 1])
        q = q1.text_input(
            "Find in this panel", "", key=f"ovq_{key}",
            placeholder="PO # · SKU · vendor · title",
        )
        min_qty = 0
        if qty_col and qty_col in df.columns:
            min_qty = q2.number_input(
                qty_label, min_value=0, value=0, step=1, key=f"ovadv_{key}_minqty",
            )
        if q:
            out = out[_str_contains_any(out, list(out.columns), q)]
        if min_qty and qty_col in out.columns:
            out = out[pd.to_numeric(out[qty_col], errors="coerce").fillna(0) >= int(min_qty)]
    n0, n1 = len(df), len(out)
    if n1 != n0:
        st.caption(f"Showing **{n1:,}** of {n0:,} after advanced filters.")
    return out


def _ov_po_str(s):
    """Format PO numbers as plain digit strings. Works on a Series or a scalar.

    Do not use Series.map(this) — that passes scalars and blows up on .astype.
    """
    if isinstance(s, (pd.Series, pd.Index)):
        out = pd.to_numeric(s, errors="coerce").astype("Int64").astype(str)
        return out.replace({"<NA>": "", "nan": "", "None": ""})
    n = pd.to_numeric(s, errors="coerce")
    if pd.isna(n):
        return ""
    return str(int(n))


def _issue_types(val):
    """Turn the report's Issue Counts JSON (e.g. {"Concealed Damage": 3}) into 'Type (n)' text."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    try:
        d = val if isinstance(val, dict) else json.loads(str(val))
    except Exception:
        return str(val)
    if not isinstance(d, dict) or not d:
        return ""
    return ", ".join(f"{k} ({v})" for k, v in d.items())


def _ov_render(dfx, key, *, date_cols=(), numpct_cols=(), pin_cols=(), color_rows=False, height=420,
               link_cols=None, slack=True, slack_whole=True, slack_label_cols=None,
               slack_filename="wo_tracker_table.csv", multi_select=False):
    return render_table(dfx, key=_grid_key(key), date_cols=date_cols, numpct_cols=numpct_cols,
                        pin_cols=pin_cols, color_rows=color_rows, height=height, link_cols=link_cols,
                        slack=slack, slack_whole=slack_whole, slack_label_cols=slack_label_cols,
                        slack_filename=slack_filename, multi_select=multi_select)


# Shelf (order-management) deep link for a PO, keyed on the plain PO number.
# render_table() keeps PO # as the number (copyable) and adds a Shelf link column.
SHELF_PO_URL = "https://www.useshelf.com/order-management/po/preview/details/{}"
PO_LINK_COLUMNS = {"PO #"}
SHELF_COL = "Shelf"


def _po_link_series(s):
    """Map a PO-number column to Shelf URLs (blank for non-numeric/empty)."""
    def _u(x):
        x = str(x).strip()
        if not x or x.lower() == "nan":
            return ""
        x = x.split(".")[0]  # tolerate "149348.0"
        return SHELF_PO_URL.format(x) if x.isdigit() else ""
    return s.map(_u)


def _active_nowo_pos(po_pos):
    """Active POs with no linked work order (Overview rule)."""
    if po_pos is None or getattr(po_pos, "empty", True) or "wo_count" not in po_pos.columns:
        return None
    nowo = po_pos.copy()
    nowo["_age"] = (pd.Timestamp(datetime.now().date())
                    - pd.to_datetime(nowo["order_placed"], errors="coerce")).dt.days
    state = nowo["purchase_state"].astype(str).str.lower()
    nowo = nowo[(nowo["wo_count"] == 0)
                & (~state.isin(["ready_to_reconcile", "cancelled", "canceled"]))
                & (nowo["_age"].fillna(0) >= NO_WO_GRACE_DAYS)]
    return _drop_zero_ordered(nowo)


def _po_lines_for_pos(po_df, pos):
    """Line items for a set of PO numbers, zeros already dropped."""
    if po_df is None or getattr(po_df, "empty", True) or not pos:
        return None
    want = {str(p).strip() for p in pos if str(p).strip() and str(p).lower() not in ("nan", "none", "<na>")}
    if not want or "po_number" not in po_df.columns:
        return None
    key = _ov_po_str(po_df["po_number"])
    return _drop_zero_ordered(po_df[key.isin(want)].copy())


def _fetch_overview_item_gap(wh, pick, ov_q, po_pos):
    """Item-level No WO / Partial WO coverage (queries/po_item_wo_gap.sql)."""
    try:
        item_gap = fetch_po_item_wo_gap(_wh_scope_arg())
        _warn_missing_columns(
            item_gap,
            ["coverage_state", "po_number", "sku", "warehouse_name", "reason"],
            "PO item WO gap (queries/po_item_wo_gap.sql)",
        )
        gap_ok = item_gap is not None and "coverage_state" in item_gap.columns
        if gap_ok and not _wh_is_all(wh) and "warehouse_name" in item_gap.columns:
            item_gap = item_gap[item_gap["warehouse_name"] == wh].copy()
        if gap_ok:
            item_gap = _scope_one(
                item_gap, pick, ov_q,
                brand_cols=["vendor_name"],
                search_cols=["po_number", "sku", "asin", "master_id", "title",
                             "vendor_name", "warehouse_name", "reason", "coverage_state"],
            )
            if (item_gap is not None and po_pos is not None and not po_pos.empty
                    and "po_number" in item_gap.columns and "po_number" in po_pos.columns):
                want = set(_ov_po_str(po_pos["po_number"]))
                want.discard("")
                item_gap = item_gap[_ov_po_str(item_gap["po_number"]).isin(want)].copy()
            item_gap = _drop_zero_ordered(item_gap)
        return item_gap, gap_ok
    except Exception:
        return None, False


def _actionable_nowo_pos(df):
    """POs that still have units left to receive — the raise-WO default."""
    if df is None or getattr(df, "empty", True):
        return df
    if "left" in df.columns:
        return df[pd.to_numeric(df["left"], errors="coerce").fillna(0) > 0].copy()
    return df


def _actionable_item_gap(df):
    """Genuine no-WO gaps and partial coverage, outstanding qty > 0."""
    if df is None or getattr(df, "empty", True):
        return df
    out = df
    if "outstanding_units" in out.columns:
        qty = pd.to_numeric(out["outstanding_units"], errors="coerce").fillna(0)
        out = out[qty > 0]
    if out.empty:
        return out
    genuine = pd.Series(False, index=out.index)
    if "reason" in out.columns:
        genuine = out["reason"].astype(str) == GENUINE_GAP_REASON
    partial = pd.Series(False, index=out.index)
    if "coverage_state" in out.columns:
        partial = out["coverage_state"].astype(str) == "Partial WO"
    if genuine.any() or partial.any() or "reason" in out.columns:
        out = out[genuine | partial]
    return out


def _picked_rows(sel, shown):
    """Ticked rows, or every visible row when nothing is ticked."""
    if sel is not None and not getattr(sel, "empty", True):
        return sel
    return shown


def _lines_with_outstanding(lines):
    """Keep PO lines that still have qty to raise."""
    if lines is None or getattr(lines, "empty", True):
        return lines
    if "outstanding_units" in lines.columns:
        q = pd.to_numeric(lines["outstanding_units"], errors="coerce").fillna(0)
        return lines[q > 0].copy()
    if "ordered_units" in lines.columns and "received_units" in lines.columns:
        q = (
            pd.to_numeric(lines["ordered_units"], errors="coerce").fillna(0)
            - pd.to_numeric(lines["received_units"], errors="coerce").fillna(0)
        )
        return lines[q > 0].copy()
    if "left" in lines.columns:
        q = pd.to_numeric(lines["left"], errors="coerce").fillna(0)
        return lines[q > 0].copy()
    return lines


def _overview_po_context(pos, *, item_n=None):
    uniq = list(dict.fromkeys(p for p in pos if p))
    shown = ", ".join(uniq[:12])
    extra = f" (+{len(uniq) - 12} more)" if len(uniq) > 12 else ""
    if item_n is not None:
        head = f"{item_n:,} PO item(s) without full WO coverage"
        return head + (f" (POs {shown}{extra})" if shown else "")
    return f"POs with no work order: {shown}{extra}"


def _overview_lookup_raise(key, qty_frame, context):
    """Primary Overview action: Catalogue Lookup with qty / PO / ship-by attached."""
    if st.button(
        "🔎 Look up listings & raise WO",
        key=key,
        type="primary",
        use_container_width=True,
        help="Opens Catalogue Lookup with these SKUs, qty, PO # and ship-by. "
             "Edit the Shelf grid there, then Slack. Does not create a WO in Shelf.",
    ):
        if qty_frame is None or getattr(qty_frame, "empty", True):
            st.warning("No outstanding line items on the selected rows to look up.")
            return
        ids = _catalog_ids_for_lookup(qty_frame)
        _jump_to_catalog_lookup(ids, context=context, pack=True, qty_frame=qty_frame)


def _overview_send_wo_request(key, lines, *, filename):
    """Slack a Shelf WO upload CSV built from Overview PO/item rows."""
    if slack_send_panel_button is None:
        st.caption("Slack is not configured.")
        return
    rows = raise_rows_from_gap(lines)
    pack = pd.DataFrame(rows) if rows else pd.DataFrame(columns=SHELF_WO_UPLOAD_COLS)
    send_df = shelf_send_df(pack)
    skipped = 0 if pack.empty else max(len(pack) - len(send_df), 0)
    if skipped:
        st.caption(f"{skipped:,} line(s) with qty 0 omitted from the Shelf file.")
    n = 0 if send_df is None else len(send_df)
    pos = []
    if send_df is not None and not send_df.empty:
        col = "Receivable ID (Inventory Request ID or Purchase Order ID)"
        if col in send_df.columns:
            pos = sorted({str(x).strip() for x in send_df[col] if str(x).strip()})
    po_bit = f" POs {', '.join(pos[:8])}" + (f" +{len(pos) - 8}" if len(pos) > 8 else "") if pos else ""
    slack_send_panel_button(
        key,
        df=send_df if send_df is not None and not send_df.empty else pack,
        filename=filename,
        note=(
            f"Please raise a WO — {n:,} outstanding line(s) in the attached Shelf upload CSV.{po_bit}\n"
            "This is a request file only; it does not create the WO until uploaded in Shelf."
        ),
        label="📤 Send WO request to Slack",
        use_container_width=True,
    )


def overview_tab(df, wos):
    po_df = po_pos = None
    po_fetch_error = None
    wh = st.session_state.get("global_wh", "All in scope")
    try:
        po_df, po_pos, _ = fetch_po_data(_wh_scope_arg())
        if not _wh_is_all(wh):
            po_df = po_df[po_df["warehouse_name"] == wh].copy()
            po_pos = po_pos[po_pos["warehouse_name"] == wh].copy()
    except Exception as exc:
        po_df = po_pos = None
        po_fetch_error = f"{type(exc).__name__}: {exc}"
    else:
        try:
            po_pos = _enrich_po_pos_with_wo(po_pos, df)
        except Exception:
            try:
                po_pos = _finalize_po_destination(po_pos)
            except Exception:
                pass

    pick, ov_q = _overview_quick_filters(df, wos, po_df, po_pos)
    wo_search = [
        "work_order_number", "source_brand", "listing_id", "finished_good_name",
        "master_id", "sku", "po_number_raw", "warehouse",
    ]
    po_search = [
        "po_number", "vendor_name", "sku", "asin", "master_id", "title",
        "warehouse_name", "country_name", "po_type", "fulfillment", "_line_search",
    ]
    df = _scope_one(df, pick, ov_q, brand_cols=["source_brand"], search_cols=wo_search)
    if wos is not None and df is not None and "work_order_number" in getattr(df, "columns", []):
        if df.empty:
            wos = wos.iloc[0:0].copy()
        elif "work_order_number" in wos.columns:
            wos = wos[wos["work_order_number"].isin(df["work_order_number"])].copy()
    if po_pos is not None:
        po_pos = _attach_line_search(po_pos, po_df)
    po_df = _scope_one(po_df, pick, ov_q, brand_cols=["vendor_name"], search_cols=po_search)
    po_pos = _scope_one(po_pos, pick, ov_q, brand_cols=["vendor_name"], search_cols=po_search)
    if po_pos is not None and "_line_search" in po_pos.columns:
        po_pos = po_pos.drop(columns="_line_search")
    if po_df is not None and po_pos is not None and "po_number" in po_df.columns:
        if po_df.empty:
            po_pos = po_pos.iloc[0:0].copy()
        else:
            po_pos = po_pos[po_pos["po_number"].isin(po_df["po_number"])].copy()

    kpi_strip(df, wos, st.session_state.get("global_wh", "All in scope"))
    st.markdown("---")

    def _panel(dshow, key, *, date_cols=(), numpct_cols=(), pin_cols=(), color_rows=False,
               link_cols=None, slack=True, slack_whole=True, slack_label_cols=None,
               slack_filename="wo_tracker_table.csv", multi_select=False, height=420):
        # Cap rendered rows for speed; the full total is already in the panel header.
        shown = dshow.head(OV_MAX_ROWS)
        sel = _ov_render(shown, key, date_cols=date_cols,
                         numpct_cols=numpct_cols, pin_cols=pin_cols, color_rows=color_rows,
                         link_cols=link_cols, slack=slack, slack_whole=slack_whole,
                         slack_label_cols=slack_label_cols, slack_filename=slack_filename,
                         multi_select=multi_select, height=height)
        if len(dshow) > OV_MAX_ROWS:
            st.caption(
                f"Showing the top {OV_MAX_ROWS} of {len(dshow):,}. "
                "Filter first — ticks apply to the rows on screen."
            )
        elif multi_select:
            st.caption(
                "Tick one or more rows, then look up listings. "
                "If nothing is ticked, the visible rows are used."
            )
        return shown, sel

    # ================= Purchase orders (tiles) =================
    st.markdown("#### 📥 Purchase orders (placed since 2025-07-01)")
    if po_fetch_error:
        st.warning(
            "PO data failed to load (this is not an empty result). "
            f"{po_fetch_error}"
        )
    if po_df is not None:
        po_ordered = int(pd.to_numeric(po_df["ordered_units"], errors="coerce").fillna(0).sum())
        po_received = int(pd.to_numeric(po_df["received_units"], errors="coerce").fillna(0).sum())
        po_fill = (po_received * 100.0 / po_ordered) if po_ordered else 0
        po_iss = int((pd.to_numeric(po_df["total_issues"], errors="coerce").fillna(0) > 0).sum())
        c = st.columns(5)
        c[0].metric("Total POs", f"{len(po_pos):,}")
        c[1].metric("Units ordered", f"{po_ordered:,}")
        c[2].metric("Units received", f"{po_received:,}")
        c[3].metric("Vendor fill", f"{po_fill:.0f}%")
        c[4].metric("Lines w/ issues", f"{po_iss:,}")
        render_po_destination_split(po_pos)
        _po_lag_caption()
    else:
        st.caption("PO data unavailable — check queries/po_tracker.sql.")

    # ================= Work orders (tiles) =================
    st.markdown("#### 🏭 Work orders (year-to-date)")
    cat = df["source_category"].astype(str)
    open_woi = int((df["status_simple"] == "Open").sum())
    blocked_woi = int(df["is_blocked_pfs"].fillna(False).sum())
    c = st.columns(5)
    c[0].metric("WO items", f"{len(df):,}")
    c[1].metric("PO WOs", f"{int((cat == 'PO').sum()):,}")
    c[2].metric("Manual/Storage WOs", f"{int((cat == 'Storage').sum()):,}")
    c[3].metric("Open", f"{open_woi:,}")
    c[4].metric("Blocked", f"{blocked_woi:,}")
    st.caption("Work-order items refresh about every 30 minutes. Blocked / overdue lists use this snapshot.")

    # ================= Charts (Plotly) — off by default; 6 charts on every rerun is slow.
    st.markdown("---")
    show_charts = st.checkbox("Show Overview charts", key="ov_show_charts",
                              help="Six Plotly charts. Leave off for a faster Overview.")
    if show_charts:
        st.markdown("#### 📊 At a glance")
        with st.expander("ℹ️ What each chart shows"):
            st.markdown(
                "- **PO status mix** — share of POs by lifecycle state (placed / receiving / arrived / "
                "reconciled). Source: PO report, one row per PO (UK/EU, placed since 2025-07-01).\n"
                "- **WO items: open / blocked / done** — warehouse processing split. Blocked = PFS "
                "'Unpickable'; Open = open & not blocked; Done = closed. Source: WO data (year-to-date).\n"
                "- **Top vendors — units outstanding** — vendors with the most units still to receive "
                "(ordered − received), top 10. Source: PO report rolled up per vendor.\n"
                "- **Blocked WO items by reason** — why WO items are blocked (Listing Failed, Replen "
                "Needed, No Inventory…). Source: WO data, blocked items.\n"
                "- **Units ordered vs received by month** — inbound flow: units ordered vs received by "
                "PO placed month. Source: PO report.\n"
                "- **PO vendor-fill distribution** — POs bucketed by fill level (received ÷ current "
                "order): <80% under-fill · 80–99% · 100% on-target · >100% over-receipt. Source: PO report."
            )

        def _fig(fig, title, show_legend=False):
            fig.update_layout(
                title=dict(text=title, font=dict(size=15)),
                margin=dict(l=8, r=8, t=44, b=8), height=300,
                showlegend=show_legend, legend_title_text="",
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            )
            fig.update_xaxes(showgrid=False)
            fig.update_yaxes(showgrid=True, gridcolor="rgba(128,128,128,0.15)")
            return fig

        def _donut(fig, title):
            fig.update_traces(hole=0.55, textinfo="label+value", textposition="inside", sort=False)
            fig.update_layout(
                title=dict(text=title, font=dict(size=15)),
                margin=dict(l=8, r=8, t=44, b=8), height=300, showlegend=True,
                legend=dict(orientation="h", y=-0.08), legend_title_text="",
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            )
            return fig

        r1 = st.columns(2)
        with r1[0]:
            if po_pos is not None and not po_pos.empty:
                s = po_pos["purchase_state"].astype(str).str.lower().value_counts()
                fig = px.pie(names=s.index, values=s.values,
                             color_discrete_sequence=px.colors.qualitative.Set2)
                st.plotly_chart(_donut(fig, "PO status mix"), use_container_width=True)
            else:
                st.caption("PO data unavailable.")
        with r1[1]:
            blk = df["is_blocked_pfs"].fillna(False)
            wo_mix = pd.Series({
                "Open": int(((df["status_simple"] == "Open") & (~blk)).sum()),
                "Blocked": int(blk.sum()),
                "Done": int((df["status_simple"] == "Closed").sum()),
            })
            fig = px.pie(names=wo_mix.index, values=wo_mix.values, color=wo_mix.index,
                         color_discrete_map={"Open": "#f4a259", "Blocked": "#d1495b", "Done": "#4c9f70"})
            st.plotly_chart(_donut(fig, "WO items: open / blocked / done"), use_container_width=True)

        r2 = st.columns(2)
        with r2[0]:
            if po_pos is not None and not po_pos.empty and "left" in po_pos.columns:
                tv = (po_pos.groupby("vendor_name")["left"].sum()
                      .sort_values(ascending=False).head(10).sort_values())
                fig = px.bar(x=tv.values, y=tv.index, orientation="h", text=tv.values,
                             color_discrete_sequence=["#3b7dd8"])
                fig.update_traces(textposition="outside")
                fig.update_layout(xaxis_title="Units outstanding", yaxis_title=None)
                st.plotly_chart(_fig(fig, "Top vendors — units outstanding"), use_container_width=True)
            else:
                st.caption("PO data unavailable.")
        with r2[1]:
            br = df[df["is_blocked_pfs"].fillna(False)]["block_reason_pfs"].dropna().value_counts().sort_values()
            if not br.empty:
                fig = px.bar(x=br.values, y=br.index, orientation="h", text=br.values,
                             color_discrete_sequence=["#d1495b"])
                fig.update_traces(textposition="outside")
                fig.update_layout(xaxis_title="Blocked items", yaxis_title=None)
                st.plotly_chart(_fig(fig, "Blocked WO items by reason"), use_container_width=True)
            else:
                st.caption("None blocked.")

        r3 = st.columns(2)
        with r3[0]:
            if po_pos is not None and not po_pos.empty:
                tmp = po_pos.copy()
                tmp["_m"] = pd.to_datetime(tmp["order_placed"], errors="coerce").dt.to_period("M").astype(str)
                tmp = tmp[tmp["_m"] != "NaT"]
                g = (tmp.groupby("_m").agg(Ordered=("original_ordered", "sum"),
                                           Received=("received", "sum")).reset_index().sort_values("_m"))
                gm = g.melt(id_vars="_m", value_vars=["Ordered", "Received"],
                            var_name="Metric", value_name="Units")
                fig = px.line(gm, x="_m", y="Units", color="Metric", markers=True,
                              color_discrete_map={"Ordered": "#3b7dd8", "Received": "#4c9f70"})
                fig.update_layout(xaxis_title=None, yaxis_title="Units")
                st.plotly_chart(_fig(fig, "Units ordered vs received by month", show_legend=True), use_container_width=True)
            else:
                st.caption("PO data unavailable.")
        with r3[1]:
            if po_pos is not None and not po_pos.empty and "vendor_fill_pct" in po_pos.columns:
                vf = pd.to_numeric(po_pos["vendor_fill_pct"], errors="coerce").fillna(0)
                order = ["<80%", "80–99%", "100%", ">100%"]
                dist = (pd.cut(vf, bins=[-0.1, 79.999, 99.999, 100.001, float("inf")], labels=order)
                        .value_counts().reindex(order).fillna(0).astype(int))
                fig = px.bar(x=list(dist.index), y=list(dist.values), text=list(dist.values),
                             color=list(dist.index),
                             color_discrete_map={"<80%": "#d1495b", "80–99%": "#f4a259",
                                                 "100%": "#4c9f70", ">100%": "#3b7dd8"})
                fig.update_traces(textposition="outside")
                fig.update_layout(xaxis_title=None, yaxis_title="POs")
                st.plotly_chart(_fig(fig, "PO vendor-fill distribution"), use_container_width=True)
            else:
                st.caption("PO data unavailable.")

    st.markdown("---")
    st.markdown("#### 🚨 Needs attention")
    st.caption(
        "**1.** See POs / items without a work order. **2.** Tick one or more rows. "
        "**3.** Look up listings — qty, PO # and ship-by come with them. "
        "**4.** Edit the Shelf grid if needed. **5.** Slack the request file. "
        "That does **not** create a WO in Shelf. "
        f"Each table shows the top {OV_MAX_ROWS}. Use **Advanced filters** to narrow."
    )
    if po_pos is not None and not getattr(po_pos, "empty", True):
        render_po_destination_split(po_pos)
    if render_flag_guide is not None:
        render_flag_guide()
    elif _FLAG_GUIDE_ERROR:
        with st.expander("ℹ️ Flag guide — failed to load", expanded=True):
            st.error("Flag guide did not import. This is not a missing-data state.")
            st.code(_FLAG_GUIDE_ERROR)
            st.caption(
                "If this persists after Manage app → Reboot + hard-refresh, the "
                "module is missing from the image or crashed on import."
            )

    nowo_all = _active_nowo_pos(po_pos)
    nowo_act = _actionable_nowo_pos(nowo_all) if nowo_all is not None else nowo_all
    _n_nowo_all = 0 if nowo_all is None else len(nowo_all)
    _n_nowo_act = 0 if nowo_act is None else len(nowo_act)
    with st.expander(
        f"🧾 POs placed/arriving with no work order ({_n_nowo_act:,} with units left)",
        expanded=(_n_nowo_act > 0),
    ):
        st.caption(
            "Active POs with no linked work order. Tick one or more POs, then look up "
            "listings — outstanding qty, PO # and ship-by travel with the items. "
            "Edit and Slack from Catalogue Lookup. Does not create a WO in Shelf."
        )
        if nowo_all is None:
            st.caption("PO→WO link unavailable (couldn't load the WO rollup).")
        elif nowo_all.empty:
            st.caption("None — every active PO with ordered units has a work order.")
        else:
            nowo_only = st.checkbox(
                "Show only POs with units still outstanding",
                value=True, key="ov_nowo_actionable",
                help="Hides fully received POs that still have no WO.",
            )
            nowo = nowo_act if nowo_only else nowo_all
            if nowo_only and _n_nowo_all > _n_nowo_act:
                st.caption(
                    f"{_n_nowo_all - _n_nowo_act:,} fully received PO(s) hidden. "
                    "Uncheck to see them."
                )
            nowo = _ov_adv_filters(
                nowo, "nowo",
                fields=[
                    ("Vendor", "vendor_name"),
                    ("Warehouse", "warehouse_name"),
                    ("Country", "country_name"),
                    ("State", "purchase_state"),
                    ("PO Type", "po_type"),
                    ("Fulfillment", "fulfillment"),
                ],
                qty_col="left",
                qty_label="Min outstanding",
            )
            if nowo is None or nowo.empty:
                st.caption("No POs match these filters.")
            else:
                keep = [
                    "po_number", "vendor_name", "warehouse_name",
                    "original_ordered", "ordered", "received", "left",
                    "fba_units", "fbb_units", "fbm_units", "zfs_units", "oct_units", "stow_units",
                    "demand_fill_pct", "vendor_fill_pct",
                    "_age", "order_placed", "ship_date", "first_arrival",
                    "country_name", "purchase_state", "po_type", "fulfillment",
                ]
                keep = [c for c in keep if c in nowo.columns]
                d = nowo.sort_values("_age", ascending=False)[keep].rename(columns={
                    "po_number": "PO #", "vendor_name": "Vendor", "warehouse_name": "WH",
                    "original_ordered": "Orig Ordered", "ordered": "Current Ordered",
                    "received": "Received", "left": "Left",
                    "fba_units": "FBA", "fbb_units": "FBB", "fbm_units": "FBM",
                    "zfs_units": "ZFS", "oct_units": "OCT",
                    "stow_units": "To Stow",
                    "demand_fill_pct": "Demand Fill %", "vendor_fill_pct": "Vendor Fill %",
                    "_age": "Days Since Placed", "order_placed": "Order Placed",
                    "ship_date": "Ship Date", "first_arrival": "First Arrival",
                    "country_name": "Country", "purchase_state": "State",
                    "po_type": "PO Type", "fulfillment": "Fulfillment",
                })
                d["PO #"] = _ov_po_str(d["PO #"])
                shown, sel = _panel(
                    d, "ov_nowo",
                    date_cols=[c for c in ["Order Placed", "Ship Date", "First Arrival"] if c in d.columns],
                    numpct_cols=[c for c in ["Demand Fill %", "Vendor Fill %"] if c in d.columns],
                    pin_cols=["PO #"], color_rows=True, slack=False, multi_select=True,
                    height=560,
                )
                picked = _picked_rows(sel, shown)
                pos = [x for x in picked["PO #"].astype(str).tolist() if x] if "PO #" in picked.columns else []
                try:
                    lines = _lines_with_outstanding(_po_lines_for_pos(po_df, pos))
                except Exception as exc:
                    st.warning(f"Couldn't load PO lines for a WO request: {type(exc).__name__}: {exc}")
                    lines = None
                n_tick = 0 if sel is None else len(sel)
                st.caption(
                    f"{n_tick:,} PO(s) ticked · {len(pos):,} will be looked up"
                    + ("" if n_tick else " (all visible rows).")
                )
                _overview_lookup_raise(
                    "jump_nowo_cat", lines, _overview_po_context(pos),
                )
                with st.expander("More — download / chase"):
                    m1, m2 = st.columns(2)
                    with m1:
                        st.download_button(
                            "⬇ PO list CSV", d.to_csv(index=False).encode("utf-8"),
                            file_name=f"pos_without_wo_{datetime.now():%Y%m%d}.csv",
                            mime="text/csv", key="dl_nowo", use_container_width=True)
                    with m2:
                        if st.button("📌 Add to chase list", key="ov_nowo_chase",
                                     use_container_width=True,
                                     help="Follow-up checklist only — does not complete a WO in Shelf."):
                            chase_src = nowo
                            if "po_number" in nowo.columns:
                                chase_src = nowo[_ov_po_str(nowo["po_number"]).isin(pos)]
                            add_chase_rows(chase_rows_from_pos(chase_src, kind="PO no-WO"))
                            _jump_to_requests("Chase list")

    item_gap, gap_ok = _fetch_overview_item_gap(wh, pick, ov_q, po_pos)
    item_gap_act = _actionable_item_gap(item_gap) if gap_ok and item_gap is not None else item_gap
    if not gap_ok or item_gap is None:
        _n_gap_all = _n_gap_act = _n_nowo_items = 0
    else:
        _n_gap_all = int(len(item_gap))
        _n_gap_act = int(len(item_gap_act)) if item_gap_act is not None else 0
        _cs_all = item_gap["coverage_state"].astype(str) if "coverage_state" in item_gap.columns else None
        _n_nowo_items = int((_cs_all == "No WO").sum()) if _cs_all is not None else 0
    with st.expander(
        f"🧩 Item-level: no WO / incomplete coverage ({_n_gap_act:,} need a WO)",
        expanded=_n_nowo_items > 0,
    ):
        st.caption(
            "One row per PO × SKU. Default view is genuine gaps and partial coverage "
            "with outstanding qty. Tick rows, look up listings (qty and PO details come "
            "with them), edit, then Slack. Does not create a WO in Shelf."
        )
        if item_gap is None:
            st.caption("Item-level WO gap data unavailable (couldn't load queries/po_item_wo_gap.sql).")
        elif not gap_ok:
            st.error(
                "GitHub `queries/po_item_wo_gap.sql` is not the production gap query. "
                "It must return **coverage_state** (No WO / Partial WO), **sku**, "
                "**warehouse_name**, and **reason** — one row per PO × Master ID. "
                "Overwrite it with `wo-tracking-tool/queries/po_item_wo_gap.sql` "
                "(do not use the diagnostic `SELECT * FROM po_woi` dump), then reboot."
            )
        elif item_gap.empty:
            st.caption("None — every PO item has full work-order coverage.")
        else:
            gap_only = st.checkbox(
                "Show only rows that still need a WO (genuine gap or partial coverage, outstanding > 0)",
                value=True, key="ov_gap_actionable",
                help="Hides fully received, cancelled, and Storage-WO-covered lines.",
            )
            view = item_gap_act if gap_only else item_gap
            if gap_only and _n_gap_all > _n_gap_act:
                st.caption(
                    f"{_n_gap_all - _n_gap_act:,} non-actionable line(s) hidden "
                    "(fully received / cancelled / Storage WO). Uncheck to see them."
                )
            view = _ov_adv_filters(
                view, "item_gap",
                fields=[
                    ("Vendor", "vendor_name"),
                    ("Warehouse", "warehouse_name"),
                    ("State", "purchase_state"),
                    ("Coverage", "coverage_state"),
                    ("Reason", "reason"),
                ],
                qty_col="outstanding_units",
                qty_label="Min outstanding",
            )
            if view is None or view.empty:
                st.caption("No item lines match these filters.")
            else:
                _cs = view["coverage_state"].astype(str) if "coverage_state" in view.columns else None
                _n_gap = int(len(view))
                _n_nowo_view = int((_cs == "No WO").sum()) if _cs is not None else 0
                _n_partial_view = int((_cs == "Partial WO").sum()) if _cs is not None else 0
                mcol = st.columns(3)
                mcol[0].metric("Item-lines shown", f"{_n_gap:,}")
                mcol[1].metric("No WO", f"{_n_nowo_view:,}")
                mcol[2].metric("Partial WO", f"{_n_partial_view:,}")
                keep = ["po_number", "sku", "master_id", "title", "vendor_name", "warehouse_name",
                        "original_ordered_units", "ordered_units", "received_units",
                        "outstanding_units", "wo_qty", "coverage_state", "reason",
                        "order_placed_date", "ship_date", "purchase_state", "fulfillment_method"]
                keep = [c for c in keep if c in view.columns]
                sort_cols = [c for c in ["coverage_state", "outstanding_units"] if c in view.columns]
                sorted_gap = (
                    view.sort_values(sort_cols, ascending=[True, False][:len(sort_cols)])
                    if sort_cols else view
                ).reset_index(drop=True)
                d = sorted_gap[keep].rename(columns={
                    "po_number": "PO #", "sku": "SKU", "master_id": "Master ID",
                    "title": "Title", "vendor_name": "Vendor",
                    "warehouse_name": "WH",
                    "original_ordered_units": "Orig Ordered", "ordered_units": "Current Ordered",
                    "received_units": "Received", "outstanding_units": "Outstanding",
                    "wo_qty": "WO Qty", "coverage_state": "Coverage", "reason": "Reason",
                    "order_placed_date": "Order Placed", "ship_date": "Ship Date",
                    "purchase_state": "State", "fulfillment_method": "Fulfillment"})
                if "PO #" in d.columns:
                    d["PO #"] = _ov_po_str(d["PO #"])
                if flag_action is not None and "Coverage" in d.columns:
                    _src = (
                        d["Reason"].astype(str) if "Reason" in d.columns
                        else pd.Series("", index=d.index)
                    )
                    _src = _src.where(_src.str.strip().ne("") & _src.str.lower().ne("nan"), d["Coverage"])
                    d["What to do"] = _src.map(flag_action)
                shown, sel = _panel(
                    d, "ov_item_gap",
                    date_cols=[c for c in ["Order Placed", "Ship Date"] if c in d.columns],
                    pin_cols=["PO #"], color_rows=True, slack=False, multi_select=True,
                    height=520,
                )
                picked = _picked_rows(sel, shown)
                if sel is not None and not sel.empty:
                    gap_lines = sorted_gap.reindex(picked.index).dropna(how="all")
                else:
                    gap_lines = sorted_gap.head(len(shown))
                gap_lines = _lines_with_outstanding(gap_lines)
                po_nums = []
                if gap_lines is not None and "po_number" in gap_lines.columns:
                    po_nums = [x for x in _ov_po_str(gap_lines["po_number"]).tolist() if x]
                n_tick = 0 if sel is None else len(sel)
                n_lines = 0 if gap_lines is None else len(gap_lines)
                st.caption(
                    f"{n_tick:,} item(s) ticked · {n_lines:,} will be looked up"
                    + ("" if n_tick else " (all visible rows).")
                )
                _overview_lookup_raise(
                    "jump_item_gap_cat",
                    gap_lines,
                    _overview_po_context(po_nums, item_n=n_lines),
                )
                with st.expander("More — download / chase"):
                    g1, g2 = st.columns(2)
                    with g1:
                        st.download_button(
                            "⬇ Download this list (CSV)", d.to_csv(index=False).encode("utf-8"),
                            file_name=f"po_items_without_full_wo_{datetime.now():%Y%m%d}.csv",
                            mime="text/csv", key="dl_item_gap", use_container_width=True)
                    with g2:
                        if st.button("📌 Add to chase list", key="ov_gap_chase",
                                     use_container_width=True,
                                     help="Follow-up checklist only — does not complete a WO in Shelf."):
                            add_chase_rows(chase_rows_from_pos(
                                gap_lines if gap_lines is not None else view, kind="Item gap"))
                            _jump_to_requests("Chase list")



# ============================================================
# SKU JOURNEY (Phase 3b) — one Master ID across POs + work orders
# ============================================================
def sku_journey_tab(df, wos):
    po_df = None
    po_fetch_error = None
    try:
        po_df, _po_pos, _ = fetch_po_data(_wh_scope_arg())
        wh = st.session_state.get("global_wh", "All in scope")
        if not _wh_is_all(wh):
            po_df = po_df[po_df["warehouse_name"] == wh].copy()
    except Exception as exc:
        po_df = None
        po_fetch_error = f"{type(exc).__name__}: {exc}"

    st.markdown("#### 🧭 SKU Journey — one product across POs and work orders")
    st.caption("Bridged on **Master ID**. Search by Master ID, SKU or title, then pick the product.")
    if po_fetch_error:
        st.warning(f"PO data failed to load: {po_fetch_error}")

    q = st.text_input("Search Master ID / SKU / title", key="skuj_q",
                      placeholder="e.g. P0EAVTZV or 'Sorel Caribou'")
    if not q or not q.strip():
        st.info("Type a Master ID, SKU or title above to trace a product end to end.")
        return

    mids = set()
    if po_df is not None:
        mp = _str_contains_any(po_df, ["master_id", "sku", "asin", "title", "vendor_name"], q)
        mids |= set(po_df.loc[mp, "master_id"].dropna().astype(str))
    mw = _str_contains_any(df, ["master_id", "listing_id", "finished_good_name", "source_brand", "work_order_item_id"], q)
    mids |= set(df.loc[mw, "master_id"].dropna().astype(str))
    mids.discard("")
    mids.discard("nan")
    mids = sorted(mids)

    if not mids:
        st.warning("No product matched that search.")
        return
    if len(mids) > 300:
        st.info(f"{len(mids):,} products match — narrow the search.")
        return

    labels = {m: m for m in mids}

    def _fill_titles(frame, title_col):
        if frame is None or frame.empty or title_col not in frame.columns:
            return
        sub = frame[frame["master_id"].astype(str).isin(mids)][["master_id", title_col]].dropna(subset=[title_col])
        if sub.empty:
            return
        sub = sub.copy()
        sub["master_id"] = sub["master_id"].astype(str)
        sub = sub.drop_duplicates("master_id")
        for m, t in zip(sub["master_id"], sub[title_col]):
            if labels.get(m, m) == m and str(t).strip():
                labels[m] = f"{m} — {t}"

    _fill_titles(po_df, "title")
    _fill_titles(df, "finished_good_name")

    mid = st.selectbox("Product", mids, format_func=lambda m: labels.get(m, m), key="skuj_pick")
    if mid:
        _sku_journey_render(mid, df, po_df)


def _sku_journey_render(mid, df, po_df):
    wo_rows = df[df["master_id"].astype(str) == mid].copy()
    po_rows = po_df[po_df["master_id"].astype(str) == mid].copy() if po_df is not None else None

    title = brand = ""
    if po_rows is not None and not po_rows.empty:
        if po_rows["title"].notna().any():
            title = str(po_rows["title"].dropna().iloc[0])
        if po_rows["vendor_name"].notna().any():
            brand = str(po_rows["vendor_name"].dropna().iloc[0])
    if not title and not wo_rows.empty and wo_rows["finished_good_name"].notna().any():
        title = str(wo_rows["finished_good_name"].dropna().iloc[0])
    if not brand and not wo_rows.empty and wo_rows["source_brand"].notna().any():
        brand = str(wo_rows["source_brand"].dropna().iloc[0])

    st.markdown(f"### {title or mid}")
    st.caption(f"Master ID **{mid}**" + (f" · {brand}" if brand else ""))

    ordered = received = n_pos = 0
    if po_rows is not None and not po_rows.empty:
        ordered = int(pd.to_numeric(po_rows["ordered_units"], errors="coerce").fillna(0).sum())
        received = int(pd.to_numeric(po_rows["received_units"], errors="coerce").fillna(0).sum())
        n_pos = int(po_rows["po_number"].nunique())
    n_woi = len(wo_rows)
    wo_open = int((wo_rows["status_simple"] == "Open").sum()) if not wo_rows.empty else 0
    wo_blocked = int(wo_rows["is_blocked_pfs"].fillna(False).sum()) if not wo_rows.empty else 0

    m = st.columns(6)
    m[0].metric("POs", f"{n_pos:,}")
    m[1].metric("Units ordered", f"{ordered:,}")
    m[2].metric("Units received", f"{received:,}")
    m[3].metric("WO items", f"{n_woi:,}")
    m[4].metric("WO open", f"{wo_open:,}")
    m[5].metric("WO blocked", f"{wo_blocked:,}")

    st.markdown("---")
    st.markdown("#### 🧾 Purchase orders")
    if po_rows is None:
        st.caption("PO data unavailable (check queries/po_tracker.sql).")
    elif po_rows.empty:
        st.caption("No POs found for this Master ID.")
    else:
        d = po_rows.sort_values("order_placed_date", ascending=False)[
            [c for c in [
                "po_number", "po_status", "vendor_name", "country_name", "warehouse_name",
                "purchase_state", "fulfillment_method", "order_placed_date",
                "ordered_units", "received_units", "demand_fill_rate_pct",
                "vendor_fill_rate_pct", "total_issues",
            ] if c in po_rows.columns]
        ].rename(columns={
            "po_number": "PO #", "po_status": "Status", "vendor_name": "Vendor", "country_name": "Country",
            "warehouse_name": "WH", "purchase_state": "State", "fulfillment_method": "Fulfillment",
            "order_placed_date": "Order Placed",
            "ordered_units": "Ordered", "received_units": "Received", "demand_fill_rate_pct": "Demand Fill %",
            "vendor_fill_rate_pct": "Vendor Fill %", "total_issues": "Issues"})
        d["PO #"] = _ov_po_str(d["PO #"])
        render_po_destination_split(wo_items=wo_rows, ordered=ordered)
        _ov_render(d, f"skuj_po_{mid}", date_cols=["Order Placed"],
                   numpct_cols=["Demand Fill %", "Vendor Fill %"], pin_cols=["PO #"],
                   color_rows=True, height=300)

    st.markdown("#### 📦 Work orders")
    if wo_rows.empty:
        st.caption("No work orders found for this Master ID (WO data is year-to-date).")
    else:
        wo_keep = [c for c in [
            "work_order_item_id", "work_order_number", "woi_type", "source_category", "source",
            "status_simple", "po_block_flag", "processing_status", "original_request", "processed",
            "woi_processing_pct", "ship_by", "warehouse",
        ] if c in wo_rows.columns]
        d = wo_rows.sort_values("ship_by")[wo_keep].rename(columns={
            "work_order_item_id": "WOI ID", "work_order_number": "WO", "woi_type": "WO Type",
            "source_category": "Type",
            "source": "Source", "status_simple": "Status", "po_block_flag": "Flag",
            "processing_status": "Block Status", "original_request": "Orig", "processed": "Processed",
            "woi_processing_pct": "%", "ship_by": "Ship By", "warehouse": "WH"})
        render_table(d, key=_grid_key(f"skuj_wo_{mid}"), pct_cols=["%"], date_cols=["Ship By"],
                     pin_cols=["WOI ID"], color_rows=True, height=320)


# ============================================================
# CATALOGUE LOOKUP TAB — paste IDs, look up listings, send with a WO request
# ============================================================
CATALOG_COL_LABELS = {
    "status": "Status",
    "marketplace": "Marketplace",
    "vendor": "Vendor",
    "sku": "SKU",
    "listing_fulfillment_type": "Fulfillment",
    "listing_id": "Listing ID",
    "master_id": "Master ID",
    "mpn": "MPN",
    "asin": "ASIN",
    "fnsku": "FNSKU",
    "commingled_status": "Commingled",
    "shippable_tag": "Shippable",
    "listing_type": "Listing Type",
    "is_dno": "DNO",
    "is_active": "Active",
    "is_discontinued": "Discontinued",
    "product_name": "Product Name",
    "upc": "UPC",
    "ean": "EAN",
    "can_expire": "Can Expire",
    "wholesale_price": "Wholesale",
    "map_price": "MAP",
    "retail_price": "Retail",
    "msrp_price": "MSRP",
    "dno_note": "DNO Note",
    "dno_reason_code": "DNO Reason",
    "marketplace_seller": "Seller",
}

CATALOG_DEFAULT_COLS = [
    "Status", "SKU", "Listing ID", "Master ID", "PO Qty", "Product Name", "Marketplace",
    "Vendor", "Fulfillment", "ASIN", "FNSKU", "Commingled", "Shippable",
    "DNO", "Active", "Seller",
]


def catalog_lookup_tab():
    st.markdown("#### 🔎 Catalogue Lookup — search by ID")
    st.caption(
        "Paste SKUs, Listing IDs, ASINs, FNSKUs, Master IDs, MPNs, UPCs or EANs. "
        "From Overview, tick POs or items without WOs and **Look up listings & raise WO** — "
        "qty, PO # and ship-by come with them. Edit the Shelf grid below, then Slack. "
        "That does **not** create a WO in Shelf until someone uploads the CSV."
    )

    raw = st.text_area(
        "Paste IDs",
        key="cat_lookup_q",
        height=120,
        placeholder="One per line, or comma-separated — e.g.\nB0ABC123DE\nP0EAVTZV\n1234567890",
        help="Split on commas, new lines, semicolons or spaces. Max "
             f"{MAX_CATALOG_IDS} IDs per search.",
    )
    b1, b2, _ = st.columns([1.2, 1, 4])
    search = b1.button("Search catalogue", type="primary", use_container_width=True)
    clear = b2.button("Clear", use_container_width=True)

    if clear:
        st.session_state["cat_lookup_q"] = ""
        st.session_state.pop("cat_lookup_submitted", None)
        st.session_state.pop("cat_lookup_context", None)
        st.session_state.pop("cat_lookup_pack_mode", None)
        st.session_state.pop("cat_lookup_qty", None)
        st.session_state.pop("cat_lookup_meta", None)
        st.rerun()
    if search:
        prev = st.session_state.get("cat_lookup_submitted", "")
        st.session_state["cat_lookup_submitted"] = raw
        if parse_catalog_ids(raw) != parse_catalog_ids(prev):
            st.session_state["cat_lookup_context"] = ""
            st.session_state.pop("cat_lookup_qty", None)
            st.session_state.pop("cat_lookup_meta", None)

    submitted = st.session_state.get("cat_lookup_submitted", "")
    if not submitted or not str(submitted).strip():
        st.info("Paste one or more IDs above and press **Search catalogue**.")
        return

    ids = parse_catalog_ids(submitted)
    if not ids:
        st.warning("No IDs found in what you pasted.")
        return
    n_raw = len([t for t in re.split(r"[\s,;]+", str(submitted).strip()) if t.strip()])
    if n_raw > MAX_CATALOG_IDS:
        st.caption(f"Using the first {MAX_CATALOG_IDS:,} of {n_raw:,} pasted IDs.")

    try:
        t0 = time.perf_counter()
        with st.spinner(f"Looking up {len(ids):,} ID(s) in the catalogue…"):
            cat = fetch_catalog_lookup(tuple(ids))
        st.session_state["_cat_fetch_s"] = time.perf_counter() - t0
    except Exception as exc:
        st.error(f"Catalogue lookup failed: {type(exc).__name__}: {exc}")
        st.caption("Needs read access to ANALYTICS_DB.STG_CATALOG and "
                   "PATTERN_DB.PUBLIC product-catalog views. Check the Snowflake role.")
        return

    if cat is None or cat.empty:
        st.warning("No catalogue rows matched those IDs.")
        return

    _warn_missing_columns(
        cat,
        ["listing_id", "sku", "master_id", "is_dno", "marketplace"],
        "Catalogue Lookup (queries/catalog_lookup.sql)",
    )

    unmatched = []
    hay = set()
    for c in ("sku", "listing_id", "asin", "mpn", "master_id", "fnsku", "upc", "ean"):
        if c in cat.columns:
            hay |= {str(v).strip().upper() for v in cat[c].dropna() if str(v).strip()}
    unmatched = [i for i in ids if i not in hay]

    dno = _catalog_bool(cat["is_dno"]) if "is_dno" in cat.columns else None
    disc = _catalog_bool(cat["is_discontinued"]) if "is_discontinued" in cat.columns else None
    active = _catalog_bool(cat["is_active"]) if "is_active" in cat.columns else None
    status = pd.Series("🟢 Active", index=cat.index)
    if active is not None:
        status = status.mask(~active.fillna(True), "🟡 Inactive")
    if disc is not None:
        status = status.mask(disc.fillna(False), "🟠 Discontinued")
    if dno is not None:
        status = status.mask(dno.fillna(False), "🔴 DNO")
    cat = cat.copy()
    cat.insert(0, "status", status)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Rows", f"{len(cat):,}")
    m2.metric("Listings", f"{cat['listing_id'].replace('', pd.NA).nunique():,}"
              if "listing_id" in cat.columns else "—")
    n_dno = int(dno.fillna(False).sum()) if dno is not None else 0
    m3.metric("DNO", f"{n_dno:,}")
    m4.metric("IDs unmatched", f"{len(unmatched):,}")
    if unmatched:
        with st.expander(f"{len(unmatched):,} pasted ID(s) did not match a catalogue row"):
            st.code("\n".join(unmatched), language="text")

    display = cat.rename(columns=CATALOG_COL_LABELS)
    ordered, seen = [], set()
    for lab in CATALOG_COL_LABELS.values():
        if lab in display.columns and lab not in seen:
            ordered.append(lab)
            seen.add(lab)
    display = display[ordered]
    display = _attach_po_qty(display)
    if "PO Qty" in display.columns and "Master ID" in display.columns:
        cols = [c for c in display.columns if c != "PO Qty"]
        cols.insert(cols.index("Master ID") + 1, "PO Qty")
        display = display[cols]
        n_qty = int(pd.to_numeric(display["PO Qty"], errors="coerce").fillna(0).gt(0).sum())
        st.caption(
            f"**PO Qty** is outstanding PO units (ordered − received) for that Master ID / SKU — "
            f"the same qty is shown on every listing of the master. {n_qty:,} row(s) have a qty."
        )

    ctx = st.session_state.get("cat_lookup_context") or ""
    if ctx:
        st.caption(f"Context: **{ctx}**")

    filtered = catalog_filter_panel(display, "fp_cat")
    skipped = max(len(display) - len(filtered), 0)
    if skipped:
        st.caption(f"Showing **{len(filtered):,}** of {len(display):,} rows after filters ({skipped:,} hidden).")

    st.markdown("##### 📦 Shelf WO layout — filtered listings")
    st.caption(
        "One row per **filtered listing** (not collapsed to one FBA per master). "
        "Qty 0 stays so you can type a request amount. Slack / download / raise-WO pack "
        "only include qty > 0. This does **not** create the WO in Shelf."
    )
    layout = _shelf_wo_layout_df(filtered, drop_zero=False, one_per_master=False)
    if layout.empty:
        st.info("No Shelf rows from these listings — they need a Listing ID or Master ID.")
        edited_layout = layout
        send_df = pd.DataFrame(columns=SHELF_WO_UPLOAD_COLS)
    else:
        n_zero = int(pd.to_numeric(layout["Request Amount"], errors="coerce").fillna(0).le(0).sum())
        if n_zero == len(layout):
            st.warning(
                "Every row has qty 0 (no outstanding PO qty on these listings). "
                "Type a Request Amount, then Slack or add to the raise-WO pack."
            )
        elif n_zero:
            st.caption(f"{n_zero:,} row(s) have qty 0 — they stay here to edit but will not be sent.")
        sig_lids = layout["Product (Listing ID or Master ID)"].astype(str)
        sig = f"{len(layout)}_{sig_lids.iloc[0]}_{sig_lids.iloc[-1]}"
        seed_key = f"cat_wo_seed_{sig}"
        if seed_key not in st.session_state:
            st.session_state[seed_key] = layout
        edited_layout = st.data_editor(
            st.session_state[seed_key],
            key=f"cat_wo_ed_{sig}",
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
            },
        )
        if edited_layout is not None:
            st.session_state[seed_key] = edited_layout
        send_df = shelf_send_df(edited_layout)

    po_ids = []
    if send_df is not None and not send_df.empty:
        po_col = "Receivable ID (Inventory Request ID or Purchase Order ID)"
        po_ids = sorted({str(x).strip() for x in send_df[po_col] if str(x).strip()})
    stem = f"wo_request_PO{po_ids[0]}" if len(po_ids) == 1 else f"wo_request_{datetime.now():%Y%m%d}"
    wo_name = f"{stem}.csv"

    send_cols = st.columns([1.4, 1.4, 1.4, 2])
    with send_cols[0]:
        if slack_send_panel_button is not None:
            slack_send_panel_button(
                "slack_catalog_wo",
                df=send_df if send_df is not None and not send_df.empty else edited_layout,
                filename=wo_name,
                note=_catalog_slack_note(filtered, ctx, skipped=skipped),
                label="📤 Send Shelf WO layout to Slack",
                use_container_width=True,
            )
        else:
            st.caption("Slack send is not configured — download the CSV and paste into Slack.")
    with send_cols[1]:
        if send_df is not None and not send_df.empty:
            st.download_button(
                "⬇ Shelf WO CSV",
                send_df.to_csv(index=False).encode("utf-8-sig"),
                file_name=wo_name,
                mime="text/csv",
                key="dl_shelf_wo",
                use_container_width=True,
                help="Same columns as the Shelf bulk WO upload. Qty 0 rows are omitted.",
            )
        else:
            st.caption("No Shelf rows with qty > 0 yet — type a Request Amount.")
    with send_cols[2]:
        if st.button("📦 Add to raise-WO pack", key="cat_add_raise",
                     use_container_width=True,
                     help="Copies the edited grid into 📋 Requests. Does not create a WO in Shelf."):
            rows = [] if edited_layout is None or edited_layout.empty else edited_layout.to_dict("records")
            add_raise_rows(rows)
            _jump_to_requests("Raise-WO pack")
    with send_cols[3]:
        st.caption(
            "Shelf file is the **edited grid** above: listing, qty, ship-by, FBA/FBM, PO #. "
            f"Suggested ship-by is the PO ship date if it’s in the future, otherwise today + "
            f"{SUGGESTED_SHIP_LEAD_DAYS} days."
        )

    if slack_send_panel_button is not None:
        slack_send_panel_button(
            "slack_catalog_table",
            df=filtered,
            filename=f"catalogue_listings_{datetime.now():%Y%m%d}.csv",
            note=_catalog_slack_note(filtered, ctx, skipped=skipped),
            label="📤 Send visible catalogue table to Slack",
            use_container_width=True,
        )

    default_cols = [c for c in CATALOG_DEFAULT_COLS if c in filtered.columns]
    cols = column_picker(list(filtered.columns), key="cols_cat",
                         default_labels=default_cols, required=["Listing ID"] if "Listing ID" in filtered.columns else ())
    filtered = filtered[cols]
    table_toolbar(filtered, key="tb_cat", file_stem="catalogue_listings",
                  id_cols=["Listing ID", "SKU", "Master ID", "ASIN", "FNSKU"],
                  count_label=f"{len(filtered):,} listing row(s)")
    render_table(
        filtered, key=_grid_key("cat_lookup"), pin_cols=["Listing ID", "SKU"],
        color_rows=True, height=480,
        slack_label_cols=["Listing ID", "SKU", "Master ID", "PO Qty", "Product Name"],
        slack_filename="catalogue_listings.csv",
    )


# ============================================================
# MAIN
# ============================================================
def main():
    # Keep filter + view selections alive across drill-in → back navigation.
    # Streamlit drops widget state for widgets not rendered during a drilldown,
    # which otherwise wipes the filters when you return to the list. Re-assigning
    # the relevant keys to themselves marks them as user-set so they survive.
    for _k in list(st.session_state.keys()):
        if _k.startswith("fp_") or _k in ("global_wh", "main_nav", "po_subnav",
                                          "storage_view", "po_view", "po_details_view",
                                          "skuj_q", "skuj_pick",
                                          "cat_lookup_q", "cat_lookup_submitted",
                                          "cat_lookup_context", "cat_lookup_pack_mode",
                                          "cat_lookup_qty", "cat_lookup_meta",
                                          "wh_scope", "requests_sub",
                                          RAISE_KEY, RAISE_NONCE, CHASE_KEY, CHASE_NONCE):
            st.session_state[_k] = st.session_state[_k]
    # Honour a tab jump requested from a button on another view (must run
    # before the nav radio is instantiated).
    if "pending_nav" in st.session_state:
        st.session_state["main_nav"] = st.session_state.pop("pending_nav")

    options = _warehouse_picker_options()
    option_names = [w["name"] for w in options]
    default_names = warehouse_names(_warehouse_override())
    kept = [n for n in (st.session_state.get("wh_scope") or default_names) if n in option_names]
    if not kept:
        kept = [n for n in default_names if n in option_names] or list(option_names[:2])
    if st.session_state.get("wh_scope") != kept:
        st.session_state["wh_scope"] = kept

    h1, h2, h3 = st.columns([2.2, 2.4, 0.8])
    with h1:
        st.title("📊 WO Tracking Tool")
        st.caption("Storage and PO Work Order tracking · live Snowflake snapshot · auto-refresh every 30 min")
    with h2:
        st.markdown("##### 🏭 Warehouse")
        picked = [n for n in (st.session_state.get("wh_scope") or default_names) if n in option_names]
        if not picked:
            picked = [n for n in default_names if n in option_names] or option_names[:1]
        all_label = "All in scope"
        if st.session_state.get("global_wh") == "Both":
            st.session_state["global_wh"] = all_label
        _wh_options = ([all_label] + picked) if len(picked) > 1 else picked
        if st.session_state.get("global_wh") not in _wh_options:
            st.session_state["global_wh"] = _wh_options[0] if _wh_options else all_label
        if len(_wh_options) <= 4:
            warehouse = st.radio(
                "Warehouse", _wh_options,
                horizontal=True, label_visibility="collapsed", key="global_wh",
            )
        else:
            warehouse = st.selectbox(
                "Warehouse", _wh_options,
                label_visibility="collapsed", key="global_wh",
            )
    with h3:
        if slack_messenger_button:
            slack_messenger_button()

    with st.expander("➕ Add warehouses to this query"):
        st.multiselect(
            "Warehouses to load from Snowflake",
            options=option_names,
            key="wh_scope",
            help="Northampton + Wroclaw by default. Add other Pattern 3PLs (e.g. Dubai). "
                 "Amazon fulfillment centers are hidden from this list.",
        )
        st.caption("Pattern warehouses only — not Amazon FCs. The header filter slices what is already loaded.")

    picked_names = [n for n in (st.session_state.get("wh_scope") or default_names) if n in option_names]
    by_name = {w["name"]: w for w in options}
    override_list = [by_name[n] for n in picked_names if n in by_name]
    if not override_list:
        override_list = get_warehouses(_warehouse_override())
    wh_scope = tuple((int(w["id"]), w["name"]) for w in override_list)
    st.session_state["_wh_scope_tuple"] = wh_scope

    skip_wo = st.session_state.get("main_nav") in (NAV_CATALOG, NAV_REQUESTS)
    last_refresh = st.session_state.get("_wo_last_refresh")
    if skip_wo:
        df = pd.DataFrame()
        wos = pd.DataFrame(columns=["source_category", "warehouse"])
    else:
        try:
            t0 = time.perf_counter()
            with st.spinner("Loading WO data from Snowflake..."):
                df, wos, last_refresh = fetch_data(wh_scope)
            st.session_state["_wo_fetch_s"] = time.perf_counter() - t0
            st.session_state["_wo_last_refresh"] = last_refresh
            if "source_category" in wos.columns:
                st.session_state["_n_po"] = int((wos["source_category"] == "PO").sum())
                st.session_state["_n_storage"] = int((wos["source_category"] == "Storage").sum())
        except Exception as e:
            st.error(f"Failed to fetch data from Snowflake: {e}")
            st.info("Check `.streamlit/secrets.toml` — see README for setup.")
            st.stop()

        if warehouse not in ("All in scope", "Both") and "warehouse" in df.columns:
            df = df[df["warehouse"] == warehouse].copy()
            wos = wos[wos["warehouse"] == warehouse].copy()

    sidebar(last_refresh)
    st.markdown("---")

    # Single-page nav (only the selected view runs — st.tabs renders every tab body
    # on each rerun, which blew past Streamlit Cloud's memory). POs are one section
    # with a PO Details / PO WOs sub-toggle; SKU Journey sits up front as a lookup.
    n_po = int(st.session_state.get("_n_po") or 0) if skip_wo else (
        len(wos[wos["source_category"] == "PO"]) if "source_category" in wos.columns else 0
    )
    n_storage = int(st.session_state.get("_n_storage") or 0) if skip_wo else (
        len(wos[wos["source_category"] == "Storage"]) if "source_category" in wos.columns else 0
    )
    nav_overview = "📊 Overview"
    nav_sku = "🧭 SKU Journey"
    nav_pos = "🚚 POs"
    nav_storage = f"📦 Manual/Storage WOs ({n_storage})"
    choice = st.radio(
        "View", [nav_overview, nav_sku, nav_pos, nav_storage, NAV_CATALOG, NAV_REQUESTS],
        horizontal=True, label_visibility="collapsed", key="main_nav",
    )
    st.markdown("---")
    if choice == nav_overview:
        overview_tab(df, wos)
    elif choice == nav_sku:
        sku_journey_tab(df, wos)
    elif choice == nav_pos:
        sub_pod = "🧾 PO Details"
        sub_pwo = f"🚚 PO WOs ({n_po})"
        sub = st.radio("PO view", [sub_pod, sub_pwo], horizontal=True,
                       label_visibility="collapsed", key="po_subnav")
        if sub == sub_pod:
            po_details_tab(df)
        else:
            po_tab(df, wos)
    elif choice == nav_storage:
        storage_tab(df, wos)
    elif choice == NAV_CATALOG:
        catalog_lookup_tab()
    elif choice == NAV_REQUESTS:
        render_requests_tab()

    st.caption(
        f"Build `{_running_build_label()}` · if a merged change is missing, "
        "reboot Streamlit Cloud (Manage app → ⋮ → Reboot) then hard-refresh."
    )


if __name__ == "__main__":
    main()

