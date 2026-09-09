"""Central warehouse configuration for the WO Tracking Tool.

This is the ONLY place warehouses are defined. The Streamlit app (app.py) and
the Slack auto-post script (scripts/post_no_wo_to_slack.py) both read from here,
and every Snowflake query is scoped to these warehouses at runtime. So adding a
warehouse is a one-line edit — no SQL or UI changes needed.

-------------------------------------------------------------------------------
HOW TO ADD A WAREHOUSE
-------------------------------------------------------------------------------
Add one entry to DEFAULT_WAREHOUSES below with:
  - "id":   the Snowflake WAREHOUSES.ID    (used by queries/wo_tracker.sql)
  - "name": the exact WAREHOUSE_NAME value (used by the report-table queries and
            the UI filter — must match Snowflake spelling exactly)

Example — adding Berlin (id 152):

    DEFAULT_WAREHOUSES = [
        {"id": 138, "name": "Northampton"},
        {"id": 146, "name": "Wroclaw"},
        {"id": 152, "name": "Berlin"},
    ]

After editing, commit + push and reboot the Streamlit app (Manage app -> ...
-> Reboot, then hard-refresh). The new warehouse appears in the Warehouse
filter and all data automatically includes it.

You can also override the list at runtime WITHOUT a code change by setting the
WO_WAREHOUSES environment variable (or, in Streamlit, a [warehouses] secret) to
a JSON array, e.g.:  [{"id": 138, "name": "Northampton"}, {"id": 152, "name": "Berlin"}]
"""

from __future__ import annotations

import json
import os
import re

# The default set of warehouses. Add a line here to track another warehouse.
DEFAULT_WAREHOUSES = [
    {"id": 138, "name": "Northampton"},
    {"id": 146, "name": "Wroclaw"},
]


def _coerce(entries):
    """Normalise a raw list into clean {"id": int, "name": str} dicts."""
    out = []
    for w in entries or []:
        name = str(w.get("name", "")).strip()
        if not name:
            continue
        try:
            wid = int(w["id"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"id": wid, "name": name})
    return out


def get_warehouses(override=None):
    """Return the active warehouse list.

    Resolution order:
      1. an explicit ``override`` list passed in (e.g. from st.secrets),
      2. the WO_WAREHOUSES env var (JSON array),
      3. DEFAULT_WAREHOUSES.
    """
    if override:
        coerced = _coerce(override)
        if coerced:
            return coerced

    raw = os.environ.get("WO_WAREHOUSES")
    if raw:
        try:
            coerced = _coerce(json.loads(raw))
            if coerced:
                return coerced
        except (ValueError, TypeError):
            pass

    return [dict(w) for w in DEFAULT_WAREHOUSES]


def warehouse_names(override=None):
    """List of warehouse display names (used for the UI filter)."""
    return [w["name"] for w in get_warehouses(override)]


def _ids_sql(warehouses):
    return ", ".join(str(w["id"]) for w in warehouses)


def _names_sql(warehouses):
    return ", ".join("'" + w["name"].replace("'", "''") + "'" for w in warehouses)


def apply_warehouse_scope(sql: str, override=None) -> str:
    """Rewrite the warehouse-scoping IN(...) clauses in a query to match the
    configured warehouses.

    The .sql files keep valid, standalone IN(...) lists (so they can still be
    pasted straight into Snowflake for debugging). At runtime we swap the list
    for the configured warehouses, matching two patterns:

        wh.id IN (138, 146)                          -> ids   (wo_tracker.sql)
        WAREHOUSE_NAME IN ('Northampton', 'Wroclaw') -> names (report queries)

    Queries without either pattern are returned unchanged.
    """
    warehouses = get_warehouses(override)
    ids_sql = _ids_sql(warehouses)
    names_sql = _names_sql(warehouses)

    sql = re.sub(
        r"(wh\.id\s+IN\s*)\([^)]*\)",
        lambda m: m.group(1) + "(" + ids_sql + ")",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"(WAREHOUSE_NAME\s+IN\s*)\([^)]*\)",
        lambda m: m.group(1) + "(" + names_sql + ")",
        sql,
        flags=re.IGNORECASE,
    )
    return sql
