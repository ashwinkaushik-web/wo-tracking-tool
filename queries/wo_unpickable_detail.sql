-- wo_unpickable_detail.sql — active unpickable-reason detail per work-order item.
-- Feeds the row drill-down "Root-cause detail" (which reason(s) block the line and
-- how many units a resolution would unlock).
--
-- CRITICAL: deleted_at IS NULL. The Amaczar worker UPSERTs and soft-deletes every
-- reason it doesn't re-emit each run, so only ~1,670 of ~233K rows are active.
-- Without this filter you overstate current blockers ~140x.
--
-- RESOLVABLE_QUANTITY is deliberately NOT populated for reason 1 (No Available
-- Inventory) and reason 4 (Inventory is Expired) — a blank there is not "0 units".
--
-- Rolled up to one row per work-order item: the distinct active reason names and
-- the total resolvable quantity across them.
SELECT
    ur.WORK_ORDER_ITEM_ID                                            AS woi_id,
    LISTAGG(DISTINCT r.NAME, ' | ') WITHIN GROUP (ORDER BY r.NAME)   AS active_reasons,
    COUNT(*)                                                         AS reason_rows,
    SUM(ur.RESOLVABLE_QUANTITY)                                      AS resolvable_qty
FROM ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__WORK_ORDER_ITEM_UNPICKABLE_REASONS ur
JOIN ANALYTICS_DB.STG_AMACZAR.STG_AMACZAR__UNPICKABLE_REASONS r
  ON r.ID = ur.UNPICKABLE_REASON_ID
WHERE ur.DELETED_AT IS NULL
GROUP BY ur.WORK_ORDER_ITEM_ID
