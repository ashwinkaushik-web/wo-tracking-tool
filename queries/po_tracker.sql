-- ============================================================================
-- po_tracker.sql  —  source for the new "PO Details" tab (UK / EU: Northampton + Wroclaw)
-- ----------------------------------------------------------------------------
-- Base object : ANALYTICS_DB.REPORTING.REPORT__BRAND_MANAGEMENT_V7__PURCHASE_ORDERS
--               (TRANSIENT BASE TABLE, rebuilt daily; grain = 1 row per PO x line item)
-- Fill rates are Tableau calcs (not stored) — reproduced here, UNCAPPED (>100% = over-receipts):
--   Demand Fill = RECEIVED_UNITS / ORDERED_UNITS   (recv vs ORIGINAL order)
--   Vendor Fill = RECEIVED_UNITS / CURRENT_UNITS   (recv vs CURRENT/revised order)
-- Label gotcha: report "Original Ordered" = ORDERED_UNITS; report "Ordered" = CURRENT_UNITS.
-- ----------------------------------------------------------------------------
SELECT
      rpt.PO_NUMBER                          AS po_number
    , rpt.PURCHASE_ORDER_TYPE                AS po_type
    , rpt.PURCHASE_STATE                     AS purchase_state
    , rpt.VENDOR_NAME                        AS vendor_name
    , rpt.COUNTRY_NAME                       AS country_name
    , rpt.WAREHOUSE_NAME                     AS warehouse_name
    , rpt.ASIN                               AS asin
    , rpt.SKU                                AS sku
    , rpt.ITEM_ID                            AS item_id
    , rpt.MASTER_ID                          AS master_id
    , rpt.PART_NUMBER                        AS part_number
    , rpt.TITLE                              AS title
    , rpt.NOTE                               AS note
    , rpt.ORDER_PLACED_DATE                  AS order_placed_date
    , rpt.SHIPPED_DATE                       AS ship_date
    , rpt.ARRIVED_DATE                       AS arrived_date
    , rpt.FINISHED_ARRIVED_DATE              AS finished_arrived_date
    , rpt.CANCEL_DATE                        AS cancel_date
    , rpt.PO_LAST_RECEIVED_DATE              AS po_last_received_date
    , rpt.ITEM_LAST_RECEIVED_DATE            AS item_last_received_date
    , rpt.WHOLESALE_PRICE_CURRENT            AS wholesale_price
    , rpt.RETAIL_PRICE_CURRENT               AS retail_price
    , rpt.C_WHOLESALE_ORDERED_UNITS_CURRENT  AS wholesale_ordered
    , rpt.C_WHOLESALE_RECEIVED_UNITS_CURRENT AS wholesale_received
    , rpt.ORDERED_UNITS                      AS original_ordered_units
    , rpt.CURRENT_UNITS                      AS ordered_units
    , rpt.CURRENT_UNITS                      AS current_units
    , GREATEST(rpt.CURRENT_UNITS - rpt.RECEIVED_UNITS, 0) AS current_on_order
    , rpt.RECEIVED_UNITS                     AS received_units
    , rpt.REMAINED_BLANKET_ORDER_QUANTITY    AS remained_blanket_order_quantity
    , rpt.TOTAL_ISSUES                       AS total_issues
    , rpt.ISSUE_COUNTS                       AS issue_counts
    , ROUND(DIV0(rpt.RECEIVED_UNITS, rpt.ORDERED_UNITS) * 100, 1) AS demand_fill_rate_pct
    , ROUND(DIV0(rpt.RECEIVED_UNITS, rpt.CURRENT_UNITS) * 100, 1) AS vendor_fill_rate_pct
FROM ANALYTICS_DB.REPORTING.REPORT__BRAND_MANAGEMENT_V7__PURCHASE_ORDERS rpt
WHERE rpt.WAREHOUSE_NAME IN ('Northampton', 'Wroclaw')
ORDER BY rpt.PO_NUMBER, rpt.ITEM_ID
