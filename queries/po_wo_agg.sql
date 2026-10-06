-- po_wo_agg.sql — per-PO rollup of the WORK ORDERS linked to each PO.
-- Feeds the WO-side columns on the PO Details top-line (WO Current / Processed /
-- Ship Created / Shipped / Stowed), dest qty by WO item type (FBA/FBB/FBM/ZFS/OCT
-- plus Other for leftover math), and the PO->WO coverage check.
-- Grain: one row per PO_NUMBER. Scoped to WOs created on/after 2025-07-01
-- (WO created date tracks PO placed date, so this matches the PO window).
-- Linkage: PURCHASES.ID = WORK_ORDERS.RECEIVABLE_ID where RECEIVABLE_TYPE='Purchase'.
WITH woi_scope AS (
    SELECT woi.ID              AS woi_id,
           woi.WORK_ORDER_ID   AS wo_id,
           woi.QUANTITY        AS qty,
           woi.PROCESSED_QUANTITY AS processed,
           p.PO_NUMBER         AS po_number,
           CASE
               WHEN UPPER(wt.NAME) LIKE 'FBA%' THEN 'FBA'
               WHEN UPPER(wt.NAME) LIKE 'FBB%' THEN 'FBB'
               WHEN UPPER(wt.NAME) LIKE 'FBM%' THEN 'FBM'
               WHEN UPPER(wt.NAME) = 'ZFS' THEN 'ZFS'
               WHEN UPPER(wt.NAME) = 'OCT' THEN 'OCT'
               ELSE 'Other'
           END                 AS dest
    FROM ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__PURCHASES p
    JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDERS wo
      ON wo.RECEIVABLE_ID = p.ID AND wo.RECEIVABLE_TYPE = 'Purchase' AND wo.DELETED_AT IS NULL
    JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDER_ITEMS woi
      ON woi.WORK_ORDER_ID = wo.ID AND woi.DELETED_AT IS NULL AND woi.FOR_ACCEPTED_OVERAGE = FALSE
    JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDER_ITEM_TYPES wt
      ON wt.ID = woi.WORK_ORDER_ITEM_TYPE_ID
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
    SUM(COALESCE(res.stowed, 0))         AS wo_stowed,
    SUM(CASE WHEN s.dest = 'FBA' THEN s.qty ELSE 0 END) AS wo_fba,
    SUM(CASE WHEN s.dest = 'FBB' THEN s.qty ELSE 0 END) AS wo_fbb,
    SUM(CASE WHEN s.dest = 'FBM' THEN s.qty ELSE 0 END) AS wo_fbm,
    SUM(CASE WHEN s.dest = 'ZFS' THEN s.qty ELSE 0 END) AS wo_zfs,
    SUM(CASE WHEN s.dest = 'OCT' THEN s.qty ELSE 0 END) AS wo_oct,
    SUM(CASE WHEN s.dest = 'Other' THEN s.qty ELSE 0 END) AS wo_other
FROM woi_scope s
LEFT JOIN res ON res.woi_id = s.woi_id
GROUP BY s.po_number
