-- =====================================================================
-- WO Tracking Tool — Production Query
-- =====================================================================
-- Pulls all YTD WOIs across Storage / PO / IR sources
-- Warehouses: Northampton (138) + Wroclaw (146)
-- Includes:
--   - PFS blocked detection (Storage WOs)
--   - PO block flag based on later of WO/PO ship-by + processing %
--   - WO-level processing % aggregates
-- =====================================================================

WITH woi_results AS (
    SELECT
        r.work_order_item_id,
        MIN(r.created_at) AS first_processed_at,
        SUM(CASE WHEN r.shippable_id IS NOT NULL OR tb.shippable_id IS NOT NULL
                 THEN r.quantity ELSE 0 END) AS shipment_created_quantity,
        SUM(CASE WHEN r.departed = TRUE THEN r.quantity ELSE 0 END) AS departed_quantity,
        SUM(CASE WHEN il.deleted_at IS NOT NULL AND r.departed = FALSE
                 THEN r.quantity ELSE 0 END) AS stowed_quantity
    FROM ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDER_ITEM_RESULTS r
    LEFT JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__INVENTORY_LOCATIONS il ON il.id = r.inventory_location_id
    LEFT JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__TRANSIENT_BOXES tb ON tb.transient_carton_id = r.inventory_location_id
    GROUP BY r.work_order_item_id
),
pfs_agg AS (
    SELECT
        WOI_ID,
        MAX(WOI_PROCESSING_STATUS) AS processing_status,
        MAX(WOI_PICK_TYPE) AS pick_type,
        MAX(WOI_AGE_DAYS) AS age_days_from_created,
        MAX(WOI_SHIP_BY_DAYS_OVERDUE) AS days_overdue,
        MAX(FINISHED_GOOD_NAME) AS finished_good_name,
        LISTAGG(DISTINCT COMPONENT_UNPICKABLE_REASON, ' | ')
            WITHIN GROUP (ORDER BY COMPONENT_UNPICKABLE_REASON) AS component_block_reasons
    FROM PATTERN_DB.OPERATIONS.PICK_FROM_STOW_WORK_ORDER_ITEMS
    GROUP BY WOI_ID
),
wo_agg AS (
    SELECT
        woi.work_order_id,
        COUNT(*) AS wo_total_wois,
        SUM(CASE WHEN woi.processed_quantity = 0 THEN 1 ELSE 0 END) AS wo_wois_untouched,
        SUM(CASE WHEN (woi.quantity - COALESCE(woi.processed_quantity,0)) > 0 THEN 1 ELSE 0 END) AS wo_wois_open,
        SUM(woi.original_quantity) AS wo_total_orig_qty,
        SUM(woi.processed_quantity) AS wo_total_processed_qty,
        ROUND(SUM(woi.processed_quantity) * 100.0 / NULLIF(SUM(woi.original_quantity), 0), 1) AS wo_processing_pct
    FROM ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDER_ITEMS woi
    WHERE woi.deleted_at IS NULL AND woi.for_accepted_overage = FALSE
    GROUP BY woi.work_order_id
)
SELECT
    woi.work_order_id                             AS work_order_number,
    woi.id                                        AS work_order_item_id,
    it.name                                       AS work_order_item,
    pfs.finished_good_name                        AS finished_good_name,
    it.master_id                                  AS master_id,
    COALESCE(ir.ir_number, 'PO# ' || p.po_number, 'Storage') AS source,
    CASE
        WHEN wo.receivable_type = 'Purchase'         THEN 'PO'
        WHEN wo.receivable_type = 'InventoryRequest' THEN 'IR'
        ELSE 'Storage'
    END                                           AS source_category,
    p.po_number                                   AS po_number_raw,
    wo.receivable_type                            AS receivable_type,
    pa.name                                       AS source_brand,
    CASE WHEN woi.quantity - COALESCE(woi.processed_quantity, 0) > 0
         THEN 'Open' ELSE 'Closed' END            AS status_simple,
    pfs.processing_status                         AS processing_status,
    CASE WHEN pfs.processing_status LIKE 'Unpickable:%' THEN TRUE ELSE FALSE END AS is_blocked_pfs,
    CASE
        WHEN pfs.processing_status LIKE '%Listing Failed%'             THEN 'Listing Failed'
        WHEN pfs.processing_status LIKE '%Replen Needed%'              THEN 'Replen Needed'
        WHEN pfs.processing_status LIKE '%No Inventory%'               THEN 'No Inventory'
        WHEN pfs.processing_status LIKE '%Listing Not Shippable%'      THEN 'Listing Not Shippable'
        WHEN pfs.processing_status LIKE '%Inventory Expired%'          THEN 'Inventory Expired'
        WHEN pfs.processing_status LIKE '%Inventory Pending Sellable%' THEN 'Pending Sellable'
        WHEN pfs.processing_status LIKE '%Missing Exp Date%'           THEN 'Missing Exp Date'
        WHEN pfs.processing_status LIKE '%Missing Lot Number%'         THEN 'Missing Lot'
        ELSE NULL
    END                                           AS block_reason_pfs,
    pfs.pick_type                                 AS pick_type,
    woi.original_quantity                         AS original_request,
    woi.quantity                                  AS current_request,
    woi.processed_quantity                        AS processed,
    COALESCE(res.shipment_created_quantity, 0)    AS order_created,
    COALESCE(res.departed_quantity, 0)            AS shipped,
    COALESCE(res.stowed_quantity, 0)              AS storage,
    ROUND(woi.processed_quantity * 100.0 / NULLIF(woi.original_quantity, 0), 1) AS woi_processing_pct,
    wagg.wo_total_wois,
    wagg.wo_wois_open,
    wagg.wo_wois_untouched,
    wagg.wo_total_orig_qty,
    wagg.wo_total_processed_qty,
    wagg.wo_processing_pct,
    CASE
        WHEN wo.receivable_type != 'Purchase' THEN NULL
        ELSE GREATEST(
            COALESCE(woi.ship_by_date, p.requested_ship_date),
            COALESCE(p.requested_ship_date, woi.ship_by_date)
        )
    END                                           AS po_ref_ship_by_date,
    CASE WHEN wo.receivable_type = 'Purchase' THEN
        DATEDIFF('day',
            GREATEST(COALESCE(woi.ship_by_date, p.requested_ship_date),
                     COALESCE(p.requested_ship_date, woi.ship_by_date)),
            CURRENT_DATE)
    END                                           AS po_days_past_ref_ship_by,
    CASE
        WHEN wo.receivable_type != 'Purchase' THEN NULL
        WHEN (woi.quantity - COALESCE(woi.processed_quantity, 0)) = 0 THEN '✅ Complete'
        WHEN COALESCE(woi.processed_quantity, 0) = 0
            AND DATEDIFF('day',
                GREATEST(COALESCE(woi.ship_by_date, p.requested_ship_date),
                         COALESCE(p.requested_ship_date, woi.ship_by_date)),
                CURRENT_DATE) >= 21
            THEN '🔴 Blocked / Issue'
        WHEN COALESCE(woi.processed_quantity, 0) < woi.original_quantity
            AND DATEDIFF('day',
                GREATEST(COALESCE(woi.ship_by_date, p.requested_ship_date),
                         COALESCE(p.requested_ship_date, woi.ship_by_date)),
                CURRENT_DATE) >= 14
            THEN '🟠 Partially Processed'
        WHEN DATEDIFF('day',
                GREATEST(COALESCE(woi.ship_by_date, p.requested_ship_date),
                         COALESCE(p.requested_ship_date, woi.ship_by_date)),
                CURRENT_DATE) >= 0
            THEN '🟡 Approaching ship-by'
        ELSE '🟢 On Track'
    END                                           AS po_block_flag,
    p.requested_ship_date                         AS po_requested_ship_date,
    p.requested_delivery_date                     AS po_requested_delivery_date,
    p.placed_at                                   AS po_placed_at,
    p.arrived_at                                  AS po_arrived_at,
    pfs.age_days_from_created                     AS age_days_from_created,
    woi.ship_by_date                              AS ship_by,
    pfs.days_overdue                              AS days_overdue,
    l.listing_id                                  AS listing_id,
    cm.name                                       AS marketplace,
    cm.country_code                               AS marketplace_country,
    wh.warehouse_name                             AS warehouse,
    woi.updated_at                                AS last_edit_at,
    eu.username                                   AS last_edit_by,
    woi.created_at                                AS created_at,
    CASE WHEN cu.username = 'amaczar_app' THEN 'Shelf' ELSE cu.username END AS created_by
FROM ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDER_ITEMS woi
JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDERS            wo ON wo.id = woi.work_order_id
JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDER_ITEM_TYPES  wt ON wt.id = woi.work_order_item_type_id
LEFT JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__LISTINGS          l  ON l.id = woi.workable_id AND woi.workable_type = 'Listing'
JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__ITEMS                  it ON it.id = COALESCE(l.item_id, woi.workable_id)
LEFT JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__PARTNERS          pa ON pa.id = it.partner_id
LEFT JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__CATALOG_MARKETPLACES cm ON cm.id = l.catalog_marketplace_id
LEFT JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WAREHOUSES        wh ON wh.id = wo.warehouse_id
LEFT JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__INVENTORY_REQUESTS ir
    ON ir.id = wo.receivable_id AND wo.receivable_type = 'InventoryRequest'
LEFT JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__PURCHASES         p
    ON p.id = wo.receivable_id AND wo.receivable_type = 'Purchase'
LEFT JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__USERS             eu ON eu.id = woi.updated_by_id
LEFT JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__USERS             cu ON cu.id = woi.created_by_id
LEFT JOIN woi_results       res  ON res.work_order_item_id = woi.id
LEFT JOIN pfs_agg           pfs  ON pfs.woi_id = woi.id
LEFT JOIN wo_agg            wagg ON wagg.work_order_id = woi.work_order_id
WHERE woi.deleted_at IS NULL
  AND wo.deleted_at IS NULL
  AND woi.for_accepted_overage = FALSE
  AND woi.created_at >= DATE_TRUNC('year', CURRENT_DATE)
  AND wh.id IN (138, 146)
ORDER BY woi.work_order_id, woi.id;
