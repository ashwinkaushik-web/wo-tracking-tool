-- po_item_wo_gap.sql — PO line vs work-order coverage (reconstructed from main as
-- reviewed in the Claude Code WO_TRA session).
--
-- Verified 2026-09-02 against Instock (IS) notes + live Snowflake counts:
--   1. LISTINGS table is STG_AMACZAR__LISTINGS (not STG_CATALOG__LISTINGS)
--   2. PO↔WO join is RECEIVABLE_TYPE='Purchase' / RECEIVABLE_ID (not PURCHASE_ID)
--   3. Item-workable (FBM) WOIs are kept via COALESCE(l.ITEM_ID, woi.WORKABLE_ID)
--   4. No bundle WORKABLE_TYPE on the Purchase path — component over-flag risk is ~0
--
-- This file is the coverage join as inspected. The Streamlit panel also groups
-- these quantities against PO item master_ids to flag "No WO" / "Partial WO".

WITH po_woi AS (
    SELECT
        p.PO_NUMBER,
        p.ID AS purchase_id,
        wo.ID AS work_order_id,
        woi.ID AS work_order_item_id,
        woi.WORKABLE_TYPE,
        woi.WORKABLE_ID,
        woi.QUANTITY,
        l.ID AS listing_id,
        l.ITEM_ID AS listing_item_id,
        it.MASTER_ID
    FROM ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__PURCHASES p
    JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDERS wo
      ON wo.RECEIVABLE_ID = p.ID
     AND wo.RECEIVABLE_TYPE = 'Purchase'
     AND wo.DELETED_AT IS NULL
    JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDER_ITEMS woi
      ON woi.WORK_ORDER_ID = wo.ID
     AND woi.DELETED_AT IS NULL
     AND woi.FOR_ACCEPTED_OVERAGE = FALSE
    LEFT JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__LISTINGS l
      ON l.ID = woi.WORKABLE_ID
     AND woi.WORKABLE_TYPE = 'Listing'
    LEFT JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__ITEMS it
      ON it.ID = COALESCE(l.ITEM_ID, woi.WORKABLE_ID)
    WHERE p.PO_NUMBER IS NOT NULL
      AND wo.CREATED_AT >= '2025-07-01'
)
SELECT *
FROM po_woi;
