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

import io
import re
import html as _html
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import numpy as np
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

st.markdown("""
<style>
[data-testid="stMetric"] { padding: 0.4rem 0.6rem; }
[data-testid="stMetricValue"] { font-size: 1.3rem !important; line-height: 1.2 !important; }
[data-testid="stMetricLabel"] p { font-size: 0.72rem !important; }
[data-testid="stMetricDelta"] { font-size: 0.68rem !important; }
[data-testid="stMetricDelta"] svg { width: 0.7rem !important; height: 0.7rem !important; }
</style>
""", unsafe_allow_html=True)

QUERY_PATH = Path(__file__).parent / "queries" / "wo_tracker.sql"
CACHE_TTL_SECONDS = 1800  # 30 min

FLAG_ORDER = [
    "🔴 Blocked / Issue", "🟠 Partially Processed",
    "🟡 Approaching ship-by", "🟢 On Track", "✅ Complete",
]


def _safe_int(v, default=0):
    try:
        if v is None or pd.isna(v):
            return default
        return int(v)
    except (ValueError, TypeError):
        return default


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


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def fetch_data():
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
    flag = str(row.get("Flag", row.get("Worst Flag", "")))
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


def copy_popover(display, id_cols, key):
    """Copy values from a column — all of them, or tick just a few. Or
    drag-select cells in the table and Ctrl+C to copy a row/column straight."""
    present = [c for c in id_cols if c in display.columns]
    if not present:
        return
    with st.popover("📋 Copy items", use_container_width=True):
        st.caption(
            "Pick a column, optionally tick just the values you want, then use the copy "
            "icon on the block. Or drag-select cells in the table and press Ctrl+C."
        )
        c = st.selectbox("Column", present, key=f"{key}_col")
        vals = [v for v in display[c].astype(str).tolist() if v and v.strip() and v.lower() != "nan"]
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
    """Count + CSV + Excel + copy-items popover + copy-table button."""
    ts = datetime.now().strftime("%Y%m%d")
    a, b, c, d, e = st.columns([2.6, 1, 1, 1.2, 1.4])
    a.caption(count_label)
    csv_text = display.to_csv(index=False)
    b.download_button("📥 CSV", csv_text.encode("utf-8"),
                      file_name=f"{file_stem}_{ts}.csv", mime="text/csv",
                      use_container_width=True, key=f"{key}_csv")
    if len(display) <= 20000:
        c.download_button("📊 Excel", _xlsx_bytes_from_csv(csv_text),
                          file_name=f"{file_stem}_{ts}.xlsx",
                          mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                          use_container_width=True, key=f"{key}_xlsx")
    else:
        c.caption("Excel: filter <20k")
    with d:
        copy_popover(display, id_cols, key=f"{key}_cp")
    with e:
        copy_table_button(display, key=f"{key}_ct")


# ============================================================
# SHARED FILTER PANEL
# ============================================================
def filter_panel(df, key, *, brand_col=None, ship_col=None, reason_col=None,
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

        # Ship By range + Search
        d1, d2, d3 = st.columns([1, 1, 2])
        if ship_col and ship_col in out.columns:
            sfrom = d1.date_input("Ship by — from", value=None, key=f"{key}_sfrom", format="YYYY-MM-DD")
            sto = d2.date_input("Ship by — to", value=None, key=f"{key}_sto", format="YYYY-MM-DD")
            ship_dt = pd.to_datetime(out[ship_col], errors="coerce")
            if sfrom:
                out = out[ship_dt >= pd.Timestamp(sfrom)]
                ship_dt = pd.to_datetime(out[ship_col], errors="coerce")
            if sto:
                out = out[ship_dt <= (pd.Timestamp(sto) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1))]
        if search_cols:
            q = d3.text_input("Search", "", key=f"{key}_search",
                              placeholder="paste several — comma / new line = match any")
            if q:
                out = out[_str_contains_any(out, search_cols, q)]
    return out


# ============================================================
# NATIVE TABLE RENDERER
# ============================================================
def render_table(display, *, key, selectable=False, pct_cols=(), date_cols=(),
                 pin_cols=(), color_rows=False, height=480):
    """Native st.dataframe. Drag-select cells/rows/cols + Ctrl+C copies cleanly.
    Selectable tables show a tick column; returns the ticked WO (or None)."""
    colcfg = {}
    for c in pct_cols:
        if c in display.columns:
            colcfg[c] = st.column_config.ProgressColumn(c, min_value=0, max_value=100, format="%.1f%%")
    for c in date_cols:
        if c in display.columns and c not in colcfg:
            colcfg[c] = st.column_config.DateColumn(c, format="YYYY-MM-DD")
    for c in pin_cols:
        if c in display.columns and c not in colcfg:
            try:
                colcfg[c] = st.column_config.Column(c, pinned="left")
            except TypeError:
                pass

    if selectable:
        event = st.dataframe(
            display, use_container_width=True, hide_index=True, height=height,
            on_select="rerun", selection_mode="single-row", column_config=colcfg, key=key,
        )
        rows = event.selection.rows
        if rows and "WO" in display.columns:
            return display.iloc[rows[0]]["WO"]
        return None

    data = display
    if color_rows:
        try:
            data = display.style.apply(_row_style, axis=1)
        except Exception:
            data = display
    st.dataframe(data, use_container_width=True, hide_index=True, height=height, column_config=colcfg)
    return None


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
        _reset_selection()
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
        "**Table tips**\n\n"
        "- Drag-select cells / a row / a column, then Ctrl+C to copy\n"
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

    st.markdown("##### 📊 Overall Totals (Storage + PO + IR)")
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

    st.markdown("##### 📦 Storage Volume")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Storage WOs", f"{len(s_wos):,}")
    c2.metric("Storage Items", f"{len(s_items):,}", f"{open_items:,} Open · {closed_items:,} Closed")
    c3.metric("Original Qty", f"{total_orig:,}")
    c4.metric("Current Qty", f"{total_current:,}")
    c5.metric("Processed Qty", f"{total_processed:,}", f"{pct_processed:.1f}%")

    st.markdown(f"##### 🚫 Storage Block Reasons (PFS) — {total_blocked:,} blocked · {pickable:,} pickable")
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

    st.markdown("##### 🚚 PO Volume")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("PO WOs", f"{len(p_wos):,}", f"{unique_pos:,} unique POs")
    c2.metric("PO Items", f"{len(p_items):,}", f"{open_items:,} Open · {closed_items:,} Closed")
    c3.metric("Original Qty", f"{total_orig:,}")
    c4.metric("Current Qty", f"{total_current:,}")
    c5.metric("Processed Qty", f"{total_processed:,}", f"{pct_processed:.1f}%")

    st.markdown(f"##### 🚦 PO Block Flag Breakdown — {blocked + partial:,} items need attention")
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

    storage_kpi_strip(s_items, s_wos)
    st.markdown("---")
    view = st.radio("View", ["📋 WO Level", "📄 Item Level"], horizontal=True,
                    key="storage_view", label_visibility="collapsed")
    st.caption("Blocked detection: **PFS table** · 💡 Tick a row then press **Open WO** · drag-select cells + Ctrl+C to copy")
    if view == "📋 WO Level":
        storage_wo_view(s_wos, s_items)
    else:
        storage_item_view(s_items)


def storage_wo_view(s_wos, s_items):
    filtered = filter_panel(
        s_wos, "fp_swo", brand_col="top_brand", ship_col="earliest_ship",
        reason_col="top_block_reason", blocked_kind="wo", status_kind="wo",
        search_cols=["work_order_number", "top_brand", "top_block_reason"],
    )
    filtered = filtered.sort_values("pfs_blocks", ascending=False)
    display = filtered[
        ["work_order_number", "warehouse", "top_brand", "items", "open_items",
         "pfs_blocks", "top_block_reason", "orig", "processed", "pct", "max_age", "earliest_ship"]
    ].rename(columns={
        "work_order_number": "WO", "warehouse": "WH", "top_brand": "Brand",
        "items": "Items", "open_items": "Open", "pfs_blocks": "Blocked (PFS)",
        "top_block_reason": "Top Reason", "orig": "Orig", "processed": "Processed",
        "pct": "% Processed", "max_age": "Age (d)", "earliest_ship": "Ship By",
    })
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
    display = filtered[
        ["work_order_item_id", "listing_id", "finished_good_name", "source_brand", "status_simple",
         "processing_status", "block_reason_pfs", "original_request", "processed",
         "woi_processing_pct", "shipped", "storage", "age_days_from_created", "ship_by"]
    ].rename(columns={
        "work_order_item_id": "WOI ID", "listing_id": "Listing", "finished_good_name": "Item Name",
        "source_brand": "Brand", "status_simple": "Status", "processing_status": "Block Status",
        "block_reason_pfs": "Reason", "original_request": "Orig", "processed": "Processed",
        "woi_processing_pct": "%", "shipped": "Shipped", "storage": "Stowed",
        "age_days_from_created": "Age (d)", "ship_by": "Ship By",
    })
    cols = column_picker(list(display.columns), key=f"cols_swo_items_{wo_id}", required=["WOI ID"])
    display = display[cols]

    table_toolbar(display, key=f"tb_swo_items_{wo_id}", file_stem=f"wo_{wo_id}_items",
                  id_cols=["WOI ID", "Listing"], count_label=f"{len(display)} items")
    render_table(
        display, key=_grid_key(f"grid_swo_items_{wo_id}"),
        pct_cols=["%"], date_cols=["Ship By"], pin_cols=["WOI ID"], color_rows=True, height=480,
    )


def storage_item_view(s_items):
    filtered = filter_panel(
        s_items, "fp_sit", brand_col="source_brand", ship_col="ship_by",
        reason_col="block_reason_pfs", blocked_kind="item", status_kind="item",
        search_cols=["work_order_item_id", "listing_id", "source_brand", "finished_good_name", "work_order_number"],
    )
    display = filtered[
        ["work_order_number", "work_order_item_id", "listing_id", "finished_good_name", "source_brand", "warehouse",
         "status_simple", "processing_status", "block_reason_pfs", "original_request",
         "processed", "woi_processing_pct", "age_days_from_created", "ship_by"]
    ].rename(columns={
        "work_order_number": "WO", "work_order_item_id": "WOI ID", "listing_id": "Listing",
        "finished_good_name": "Item Name", "source_brand": "Brand", "warehouse": "WH",
        "status_simple": "Status", "processing_status": "Block Status", "block_reason_pfs": "Reason",
        "original_request": "Orig", "processed": "Processed", "woi_processing_pct": "%",
        "age_days_from_created": "Age (d)", "ship_by": "Ship By",
    })
    cols = column_picker(list(display.columns), key="cols_sit", required=["WOI ID"])
    display = display[cols]

    table_toolbar(display, key="tb_sit", file_stem="storage_items",
                  id_cols=["WOI ID", "Listing", "WO"], count_label=f"{len(display):,} items")
    render_table(
        display, key=_grid_key("grid_sit"),
        pct_cols=["%"], date_cols=["Ship By"], pin_cols=["WOI ID"], color_rows=True, height=600,
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

    po_kpi_strip(p_items, p_wos)
    st.markdown("---")
    view = st.radio("View", ["📋 WO Level", "📄 Item Level"], horizontal=True,
                    key="po_view", label_visibility="collapsed")
    st.caption("Block flag: **14/21 days past later of WO/PO ship-by** · 💡 Tick a row then press **Open WO** · drag-select cells + Ctrl+C to copy")
    if view == "📋 WO Level":
        po_wo_view(p_wos, p_items)
    else:
        po_item_view(p_items)


def po_wo_view(p_wos, p_items):
    filtered = filter_panel(
        p_wos, "fp_pwo", brand_col="top_brand", ship_col="earliest_ship",
        flag_col="worst_po_flag", status_kind="wo",
        search_cols=["work_order_number", "po_number_raw", "top_brand"],
    )
    display = filtered[
        ["work_order_number", "po_number_raw", "warehouse", "top_brand", "items", "open_items",
         "untouched", "orig", "processed", "pct", "worst_po_flag", "max_age", "earliest_ship"]
    ].rename(columns={
        "work_order_number": "WO", "po_number_raw": "PO #", "warehouse": "WH", "top_brand": "Brand",
        "items": "Items", "open_items": "Open", "untouched": "Untouched",
        "orig": "Orig", "processed": "Processed", "pct": "% Processed",
        "worst_po_flag": "Worst Flag", "max_age": "Age (d)", "earliest_ship": "Ship By",
    })
    cols = column_picker(list(display.columns), key="cols_pwo", required=["WO"])
    display = display[cols]

    table_toolbar(display, key="tb_pwo", file_stem="po_wos",
                  id_cols=["WO", "PO #"], count_label=f"{len(display)} of {len(p_wos)} WOs")
    sel_wo = render_table(
        display, key=_grid_key("grid_pwo"), selectable=True,
        pct_cols=["% Processed"], date_cols=["Ship By"], pin_cols=["WO"], height=500,
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
    c6.metric("Ship By", _safe_date_str(wo_row["earliest_ship"]))

    items = p_items[p_items["work_order_number"] == wo_id].copy()
    flag_counts = items["po_block_flag"].value_counts().to_dict()
    if flag_counts:
        breakdown = " · ".join([f"{k}: **{v}**" for k, v in flag_counts.items()])
        st.markdown(f"**Flag breakdown:** {breakdown}")

    st.markdown("---")
    st.markdown(f"#### 📄 Items in WO {wo_id} (PO# {wo_row['po_number_raw']})")
    filtered = filter_panel(
        items, f"fp_pwo_items_{wo_id}", brand_col="source_brand", ship_col="ship_by",
        flag_col="po_block_flag", status_kind="item",
        search_cols=["work_order_item_id", "listing_id", "source_brand", "finished_good_name"],
    )
    filtered = filtered.sort_values("po_days_past_ref_ship_by", ascending=False)
    display = filtered[
        ["work_order_item_id", "listing_id", "finished_good_name", "source_brand", "status_simple", "po_block_flag",
         "po_days_past_ref_ship_by", "original_request", "processed", "woi_processing_pct", "po_ref_ship_by_date"]
    ].rename(columns={
        "work_order_item_id": "WOI ID", "listing_id": "Listing", "finished_good_name": "Item Name",
        "source_brand": "Brand", "status_simple": "Status", "po_block_flag": "Flag",
        "po_days_past_ref_ship_by": "Days Past", "original_request": "Orig", "processed": "Processed",
        "woi_processing_pct": "%", "po_ref_ship_by_date": "Ship By",
    })
    cols = column_picker(list(display.columns), key=f"cols_pwo_items_{wo_id}", required=["WOI ID"])
    display = display[cols]

    table_toolbar(display, key=f"tb_pwo_items_{wo_id}", file_stem=f"wo_{wo_id}_items",
                  id_cols=["WOI ID", "Listing"], count_label=f"{len(display)} items")
    render_table(
        display, key=_grid_key(f"grid_pwo_items_{wo_id}"),
        pct_cols=["%"], date_cols=["Ship By"], pin_cols=["WOI ID"], color_rows=True, height=480,
    )


def po_item_view(p_items):
    filtered = filter_panel(
        p_items, "fp_pit", brand_col="source_brand", ship_col="ship_by",
        flag_col="po_block_flag", status_kind="item",
        search_cols=["work_order_item_id", "listing_id", "source_brand", "finished_good_name", "work_order_number"],
    )
    filtered = filtered.sort_values("po_days_past_ref_ship_by", ascending=False)
    display = filtered[
        ["work_order_number", "work_order_item_id", "po_number_raw", "listing_id", "finished_good_name", "source_brand",
         "warehouse", "status_simple", "po_block_flag", "po_days_past_ref_ship_by", "original_request",
         "processed", "woi_processing_pct", "ship_by"]
    ].rename(columns={
        "work_order_number": "WO", "work_order_item_id": "WOI ID", "po_number_raw": "PO #", "listing_id": "Listing",
        "finished_good_name": "Item Name", "source_brand": "Brand", "warehouse": "WH",
        "status_simple": "Status", "po_block_flag": "Flag",
        "po_days_past_ref_ship_by": "Days Past", "original_request": "Orig",
        "processed": "Processed", "woi_processing_pct": "%", "ship_by": "Ship By",
    })
    cols = column_picker(list(display.columns), key="cols_pit", required=["WOI ID"])
    display = display[cols]

    table_toolbar(display, key="tb_pit", file_stem="po_items",
                  id_cols=["WOI ID", "Listing", "WO", "PO #"], count_label=f"{len(display):,} items")
    render_table(
        display, key=_grid_key("grid_pit"),
        pct_cols=["%"], date_cols=["Ship By"], pin_cols=["WOI ID"], color_rows=True, height=600,
    )


# ============================================================
# MAIN
# ============================================================
def main():
    h1, h2 = st.columns([3, 1.3])
    with h1:
        st.title("📊 WO Tracking Tool")
        st.caption("Storage and PO Work Order tracking · live Snowflake snapshot · auto-refresh every 30 min")
    with h2:
        st.markdown("##### 🏭 Warehouse")
        warehouse = st.radio(
            "Warehouse", ["Both", "Northampton", "Wroclaw"],
            horizontal=True, label_visibility="collapsed", key="global_wh",
        )

    try:
        with st.spinner("Loading WO data from Snowflake..."):
            df, wos, last_refresh = fetch_data()
    except Exception as e:
        st.error(f"Failed to fetch data from Snowflake: {e}")
        st.info("Check `.streamlit/secrets.toml` — see README for setup.")
        st.stop()

    if warehouse != "Both":
        df = df[df["warehouse"] == warehouse].copy()
        wos = wos[wos["warehouse"] == warehouse].copy()

    sidebar(last_refresh)
    kpi_strip(df, wos, warehouse)
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
