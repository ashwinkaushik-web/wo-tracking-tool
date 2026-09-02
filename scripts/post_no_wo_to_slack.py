#!/usr/bin/env python3
"""
Post the PO-level "no work order" list to Slack on a schedule.
================================================================
Runs OUTSIDE Streamlit (GitHub Actions cron), so all config comes from
environment variables, not st.secrets. It reproduces the app's Overview panel
"POs placed/arriving with no work order", scoped to POs placed in 2026.

Required env vars (set as GitHub repo secrets — see SLACK_AUTOPOST_SETUP.md):
    SNOWFLAKE_USER, SNOWFLAKE_ACCOUNT, SNOWFLAKE_ROLE, SNOWFLAKE_WAREHOUSE
    SNOWFLAKE_DATABASE (default ANALYTICS_DB), SNOWFLAKE_SCHEMA (default STG_AMACZAR)
    SNOWFLAKE_PRIVATE_KEY            (full PEM contents of the .p8 key)
    SNOWFLAKE_PRIVATE_KEY_PASSPHRASE (only if the key is encrypted)
    SLACK_BOT_TOKEN                  (xoxb-… with chat:write)
    SLACK_CHANNEL_ID                 (e.g. C052JR5LBNZ for #merchandise-planning-europe)
"""

import os
import json
import datetime
import urllib.request
import urllib.error

import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

MAX_ROWS = 40  # cap the Slack table so the message isn't oversized

# Same logic as the app's Overview "POs placed/arriving with no work order" panel:
# PO-level rollup from the report table (Northampton + Wroclaw), LEFT JOIN the
# per-PO count of linked work orders, keep POs with zero WOs that aren't
# reconciled/cancelled. Scoped to POs placed in 2026 (the group's ask).
QUERY = r"""
WITH po AS (
    SELECT
        rpt.PO_NUMBER                 AS po_number,
        MAX(rpt.VENDOR_NAME)          AS vendor_name,
        MAX(rpt.COUNTRY_NAME)         AS country_name,
        MAX(rpt.WAREHOUSE_NAME)       AS warehouse_name,
        MAX(rpt.PURCHASE_STATE)       AS purchase_state,
        MAX(rpt.PURCHASE_ORDER_TYPE)  AS po_type,
        MAX(rpt.FULFILLMENT_METHOD)   AS fulfillment,
        MIN(rpt.ORDER_PLACED_DATE)    AS order_placed,
        MIN(rpt.SHIPPED_DATE)         AS ship_date,
        MIN(rpt.ARRIVED_DATE)         AS first_arrival,
        SUM(rpt.ORDERED_UNITS)        AS ordered_units,
        SUM(rpt.RECEIVED_UNITS)       AS received_units
    FROM ANALYTICS_DB.REPORTING.REPORT__BRAND_MANAGEMENT_V7__PURCHASE_ORDERS rpt
    WHERE rpt.WAREHOUSE_NAME IN ('Northampton', 'Wroclaw')
      AND rpt.ORDER_PLACED_DATE >= '2026-01-01'
    GROUP BY rpt.PO_NUMBER
),
wo AS (
    SELECT p.PO_NUMBER AS po_number, COUNT(DISTINCT w.ID) AS wo_count
    FROM ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__PURCHASES p
    JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDERS w
      ON w.RECEIVABLE_ID = p.ID AND w.RECEIVABLE_TYPE = 'Purchase' AND w.DELETED_AT IS NULL
    JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDER_ITEMS woi
      ON woi.WORK_ORDER_ID = w.ID AND woi.DELETED_AT IS NULL
    WHERE w.CREATED_AT >= '2025-07-01'
    GROUP BY p.PO_NUMBER
)
SELECT
    po.po_number,
    po.vendor_name,
    po.country_name,
    po.warehouse_name,
    po.purchase_state,
    po.po_type,
    po.fulfillment,
    po.order_placed,
    po.first_arrival,
    po.ordered_units,
    DATEDIFF('day', po.order_placed, CURRENT_DATE) AS days_since_placed
FROM po
LEFT JOIN wo ON wo.po_number = po.po_number
WHERE COALESCE(wo.wo_count, 0) = 0
  AND LOWER(po.purchase_state) NOT IN ('ready_to_reconcile', 'cancelled', 'canceled')
ORDER BY days_since_placed DESC
"""


def _private_key_der():
    pem = os.environ["SNOWFLAKE_PRIVATE_KEY"].encode("utf-8")
    passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") or None
    key = serialization.load_pem_private_key(
        pem, password=passphrase.encode("utf-8") if passphrase else None,
        backend=default_backend())
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())


def fetch_rows():
    conn = snowflake.connector.connect(
        user=os.environ["SNOWFLAKE_USER"],
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        role=os.environ.get("SNOWFLAKE_ROLE"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
        database=os.environ.get("SNOWFLAKE_DATABASE", "ANALYTICS_DB"),
        schema=os.environ.get("SNOWFLAKE_SCHEMA", "STG_AMACZAR"),
        private_key=_private_key_der(),
    )
    try:
        cur = conn.cursor()
        cur.execute(QUERY)
        cols = [c[0].lower() for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    return rows


def _d(v):
    """Format a date-ish value as YYYY-MM-DD, or '—'."""
    if v is None:
        return "—"
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.strftime("%Y-%m-%d")
    return str(v)[:10]


def build_message(rows):
    today = datetime.date.today().strftime("%a %d %b %Y")
    n = len(rows)
    if n == 0:
        return (f":white_check_mark: *No-WO check — {today}*\n"
                "Every 2026 PO at Northampton/Wroclaw has a linked work order. Nothing to raise. :tada:")

    header = (f":clipboard: *POs placed/arriving with NO work order — {today}*\n"
              f"{n} PO(s) placed in 2026 with no linked WO (Northampton + Wroclaw). "
              "Please review and raise WOs.\n")

    shown = rows[:MAX_ROWS]
    # Fixed-width table inside a code block so Slack keeps the columns aligned.
    cols = [
        ("PO #",     lambda r: str(r["po_number"])[:8],            8),
        ("Vendor",   lambda r: (r.get("vendor_name") or "")[:18], 18),
        ("Ctry",     lambda r: (r.get("country_name") or "")[:10], 10),
        ("WH",       lambda r: (r.get("warehouse_name") or "")[:11], 11),
        ("State",    lambda r: (r.get("purchase_state") or "")[:10], 10),
        ("Fulfil",   lambda r: (r.get("fulfillment") or "")[:6],   6),
        ("Placed",   lambda r: _d(r.get("order_placed")),          10),
        ("Arrival",  lambda r: _d(r.get("first_arrival")),         10),
        ("Ordered",  lambda r: f"{int(r.get('ordered_units') or 0):,}", 9),
        ("Days",     lambda r: str(int(r.get("days_since_placed") or 0)), 4),
    ]
    line = "  ".join(h.ljust(w) for h, _, w in cols)
    sep = "  ".join("-" * w for _, _, w in cols)
    body = [line, sep]
    for r in shown:
        body.append("  ".join(fn(r).ljust(w) for _, fn, w in cols))
    table = "```\n" + "\n".join(body) + "\n```"

    footer = ""
    if n > MAX_ROWS:
        footer = f"\n_…and {n - MAX_ROWS} more. Full list in the WO Tracking Tool._"

    return header + table + footer


def post_to_slack(text):
    token = os.environ["SLACK_BOT_TOKEN"]
    channel = os.environ["SLACK_CHANNEL_ID"]
    data = json.dumps({"channel": channel, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage", data=data,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": f"Bearer {token}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok"):
        raise SystemExit(f"Slack API error: {body.get('error')}")
    print(f"Posted to {channel}.")


def main():
    rows = fetch_rows()
    print(f"Fetched {len(rows)} no-WO PO(s).")
    post_to_slack(build_message(rows))


if __name__ == "__main__":
    main()
