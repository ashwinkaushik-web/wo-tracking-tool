-- po_item_wo_gap.sql — ITEM-level WO coverage gaps within a PO.
-- Different grain from po_wo_agg.sql (which rolls up to one row per PO).
-- Here: one row per PO x Master ID, comparing outstanding PO quantity against
-- the quantity actually raised on PO-linked work orders for that same item.
--
-- Validated against Snowflake (2025-07-01+, Northampton/Wroclaw):
--   62,893 item-lines fully covered · 3,718 no WO at all · 414 partial.
--   Of the "no WO" lines: 3,470 are already fully received (not actionable —
--   received via a route that predates/bypasses WO tracking), 247 are a
--   genuine gap needing a WO raised, 1 is on a closed/cancelled PO.
--
-- Only returns NO-WO and PARTIAL lines (fully-covered lines are dropped here —
-- the app doesn't need 62k+ healthy rows, only the exceptions).
WITH po_items AS (
    SELECT
        CAST(rpt.PO_NUMBER AS VARCHAR)   AS po_number,
        rpt.MASTER_ID                    AS master_id,
        MAX(rpt.SKU)                     AS sku,
        MAX(rpt.TITLE)                   AS title,
        MAX(rpt.VENDOR_NAME)             AS vendor_name,
        MAX(rpt.COUNTRY_NAME)            AS country_name,
        MAX(rpt.WAREHOUSE_NAME)          AS warehouse_name,
        MAX(rpt.PURCHASE_STATE)          AS purchase_state,
        MIN(rpt.ORDER_PLACED_DATE)       AS order_placed_date,
        SUM(rpt.CURRENT_UNITS)           AS ordered_units,
        SUM(rpt.RECEIVED_UNITS)          AS received_units
    FROM ANALYTICS_DB.REPORTING.REPORT__BRAND_MANAGEMENT_V7__PURCHASE_ORDERS rpt
    WHERE rpt.WAREHOUSE_NAME IN ('Northampton', 'Wroclaw')
      AND rpt.ORDER_PLACED_DATE >= '2025-07-01'
      AND rpt.MASTER_ID IS NOT NULL
    GROUP BY CAST(rpt.PO_NUMBER AS VARCHAR), rpt.MASTER_ID
),
po_woi AS (
    -- Quantity actually raised on PO-linked work orders (receivable_type='Purchase'),
    -- matched to the PO's own items via item -> master_id (WOIs don't carry a
    -- PO-item key directly; they link via the listing/item, same as wo_tracker.sql).
    SELECT
        CAST(p.PO_NUMBER AS VARCHAR) AS po_number,
        it.MASTER_ID                 AS master_id,
        SUM(woi.QUANTITY)            AS wo_qty
    FROM ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__PURCHASES p
    JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDERS wo
      ON wo.RECEIVABLE_ID = p.ID AND wo.RECEIVABLE_TYPE = 'Purchase' AND wo.DELETED_AT IS NULL
    JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDER_ITEMS woi
      ON woi.WORK_ORDER_ID = wo.ID AND woi.DELETED_AT IS NULL AND woi.FOR_ACCEPTED_OVERAGE = FALSE
    LEFT JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__LISTINGS l
      ON l.ID = woi.WORKABLE_ID AND woi.WORKABLE_TYPE = 'Listing'
    LEFT JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__ITEMS it
      ON it.ID = COALESCE(l.ITEM_ID, woi.WORKABLE_ID)
    WHERE p.PO_NUMBER IS NOT NULL
    GROUP BY CAST(p.PO_NUMBER AS VARCHAR), it.MASTER_ID
),
storage_master_ids AS (
    -- Master IDs that have appeared on a Manual/Storage WO (not PO- or
    -- IR-linked) this year — used to flag "handled via Storage WO instead"
    -- rather than silently treating a no-WO item as fully unhandled.
    SELECT DISTINCT it.MASTER_ID AS master_id
    FROM ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDERS wo
    JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDER_ITEMS woi
      ON woi.WORK_ORDER_ID = wo.ID AND woi.DELETED_AT IS NULL AND woi.FOR_ACCEPTED_OVERAGE = FALSE
    LEFT JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__LISTINGS l
      ON l.ID = woi.WORKABLE_ID AND woi.WORKABLE_TYPE = 'Listing'
    LEFT JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__ITEMS it
      ON it.ID = COALESCE(l.ITEM_ID, woi.WORKABLE_ID)
    WHERE wo.DELETED_AT IS NULL
      AND wo.RECEIVABLE_TYPE NOT IN ('Purchase', 'InventoryRequest')
      AND wo.CREATED_AT >= DATE_TRUNC('year', CURRENT_DATE)
      AND it.MASTER_ID IS NOT NULL
)
SELECT
    po_items.po_number,
    po_items.master_id,
    po_items.sku,
    po_items.title,
    po_items.vendor_name,
    po_items.country_name,
    po_items.warehouse_name,
    po_items.purchase_state,
    po_items.order_placed_date,
    po_items.ordered_units,
    po_items.received_units,
    (po_items.ordered_units - po_items.received_units) AS outstanding_units,
    COALESCE(po_woi.wo_qty, 0)                          AS wo_qty,
    CASE
        WHEN po_woi.wo_qty IS NULL THEN 'No WO'
        WHEN po_woi.wo_qty < (po_items.ordered_units - po_items.received_units) THEN 'Partial WO'
        ELSE 'Full WO'
    END                                                  AS coverage_state,
    CASE
        WHEN po_woi.wo_qty IS NOT NULL THEN NULL  -- only rate a reason for true no-WO lines
        WHEN (po_items.ordered_units - po_items.received_units) <= 0 THEN 'Fully received — WO likely not required'
        WHEN LOWER(po_items.purchase_state) IN ('ready_to_reconcile', 'cancelled', 'canceled')
            THEN 'PO reconciled/cancelled'
        WHEN sm.master_id IS NOT NULL THEN 'Covered via Storage WO (not PO-linked)'
        ELSE 'Genuine gap — needs WO raised'
    END                                                  AS reason
FROM po_items
LEFT JOIN po_woi ON po_woi.po_number = po_items.po_number AND po_woi.master_id = po_items.master_id
LEFT JOIN storage_master_ids sm ON sm.master_id = po_items.master_id
WHERE po_woi.wo_qty IS NULL
   OR po_woi.wo_qty < (po_items.ordered_units - po_items.received_units)
ORDER BY po_items.po_number, po_items.master_id
