-- po_wo_agg.sql — per-PO rollup of the WORK ORDERS linked to each PO.
-- Feeds the WO-side columns on the PO Details top-line (WO Current / Processed /
-- Ship Created / Shipped / Stowed) and the PO->WO coverage check.
-- Grain: one row per PO_NUMBER. Scoped to WOs created on/after 2025-07-01
-- (WO created date tracks PO placed date, so this matches the PO window).
-- Linkage: PURCHASES.ID = WORK_ORDERS.RECEIVABLE_ID where RECEIVABLE_TYPE='Purchase'.
WITH woi_scope AS (
    SELECT woi.ID              AS woi_id,
           woi.WORK_ORDER_ID   AS wo_id,
           woi.QUANTITY        AS qty,
           woi.PROCESSED_QUANTITY AS processed,
           p.PO_NUMBER         AS po_number
    FROM ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__PURCHASES p
    JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDERS wo
      ON wo.RECEIVABLE_ID = p.ID AND wo.RECEIVABLE_TYPE = 'Purchase' AND wo.DELETED_AT IS NULL
    JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDER_ITEMS woi
      ON woi.WORK_ORDER_ID = wo.ID AND woi.DELETED_AT IS NULL
    WHERE wo.CREATED_AT >= '2025-07-01'
),
res AS (
    SELECT r.WORK_ORDER_ITEM_ID AS woi_id,
        SUM(CASE WHEN r.SHIPPABLE_ID IS NOT NULL OR tb.SHIPPABLE_ID IS NOT NULL
                 THEN r.QUANTITY ELSE 0 END)                              AS ship_created,
        SUM(CASE WHEN r.DEPARTED = TRUE THEN r.QUANTITY ELSE 0 END)       AS shipped,
        SUM(CASE WHEN il.DELETED_AT IS NOT NULL AND r.DEPARTED = FALSE
                 THEN r.QUANTITY ELSE 0 END)                              AS stowed
    FROM ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDER_ITEM_RESULTS r
    LEFT JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__INVENTORY_LOCATIONS il
           ON il.ID = r.INVENTORY_LOCATION_ID
    LEFT JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__TRANSIENT_BOXES tb
           ON tb.TRANSIENT_CARTON_ID = r.INVENTORY_LOCATION_ID
    WHERE r.WORK_ORDER_ITEM_ID IN (SELECT woi_id FROM woi_scope)
    GROUP BY r.WORK_ORDER_ITEM_ID
)
SELECT
    s.po_number                          AS po_number,
    COUNT(DISTINCT s.wo_id)              AS wo_count,
    COUNT(*)                             AS woi_count,
    SUM(s.qty)                           AS wo_current,
    SUM(s.processed)                     AS wo_processed,
    SUM(COALESCE(res.ship_created, 0))   AS wo_ship_created,
    SUM(COALESCE(res.shipped, 0))        AS wo_shipped,
    SUM(COALESCE(res.stowed, 0))         AS wo_stowed
FROM woi_scope s
LEFT JOIN res ON res.woi_id = s.woi_id
GROUP BY s.po_number
