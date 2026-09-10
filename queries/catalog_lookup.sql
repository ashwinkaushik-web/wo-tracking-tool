-- catalog_lookup.sql — Catalogue Lookup, Search by ID
-- Used by build_catalog_query() in app.py. Three CTEs plus a full outer join:
--   q1  listings (STG_CATALOG) + DNO settings
--   q2  catalog view (PATTERN_DB product/listings)
--   q3  latest DNO history flag (per matched listing only — not a full-table MAX scan)
--
-- Placeholders replaced at runtime by app.py (quoted SQL literals, never a Python list):
--   id_values  — VALUES rows: ('SKU1'), ('L0ABC'), ...
--   upper_list — quoted IN-list, if this file still uses IN (upper_list)
--
-- Prefer looking up SKU / Listing ID. Master ID matches every marketplace listing
-- for that product and makes this query slow.
WITH search_ids AS (
    SELECT column1 AS id FROM VALUES {id_values}
),
q1 AS (
    SELECT c.name AS marketplace, par.name AS vendor, a.Listing_MP_Primary_ID AS sku,
        a.LISTING_FULFILLMENT_TYPE AS listing_fulfillment_type, a.LISTING_ID AS listing_id,
        b.MASTER_ID AS master_id, b.MPN AS mpn, a.LISTING_MP_PAGE_ID AS asin,
        a.LISTING_MP_SECONDARY_ID AS fnsku,
        CASE
            WHEN a.LISTING_FULFILLMENT_TYPE <> 'FBA' THEN NULL
            WHEN a.LISTING_MP_SECONDARY_ID = a.LISTING_MP_PAGE_ID AND a.Listing_is_commingled = TRUE THEN 'Commingled'
            WHEN a.LISTING_MP_SECONDARY_ID <> a.LISTING_MP_PAGE_ID AND a.Listing_is_commingled = FALSE THEN 'NOT Commingled'
            WHEN a.LISTING_MP_SECONDARY_ID <> a.LISTING_MP_PAGE_ID AND a.Listing_is_commingled = TRUE THEN 'Amazon not set to commingled, but Pattern flagged'
            WHEN a.LISTING_MP_SECONDARY_ID = a.LISTING_MP_PAGE_ID AND a.Listing_is_commingled = FALSE THEN 'Amazon set as commingled, but Pattern flag not on'
            WHEN a.LISTING_MP_SECONDARY_ID IS NULL THEN 'Missing FNSKU for analysis'
            ELSE 'Check'
        END AS commingled_status,
        dno.DNO_NOTE AS dno_note,
        dno_rc.DNO_REASON_CODE AS dno_reason_code
    FROM ANALYTICS_DB.STG_CATALOG.STG_CATALOG__LISTINGS a
    LEFT JOIN ANALYTICS_DB.STG_CATALOG.STG_CATALOG__PRODUCTS b ON b.ID = a.PRODUCT_ID
    LEFT JOIN ANALYTICS_DB.STG_CATALOG.STG_CATALOG__MARKETPLACES c ON a.MARKETPLACE_ID = c.ID
    LEFT JOIN ANALYTICS_DB.STG_CATALOG.STG_CATALOG__PARTNERS par ON par.ID = b.PARTNER_ID
    LEFT JOIN ANALYTICS_DB.STG_CATALOG.STG_CATALOG__DNO_SETTINGS dno ON dno.ID = a.DNO_SETTING_ID
    LEFT JOIN ANALYTICS_DB.STG_CATALOG.STG_CATALOG__DNO_REASON_CODES dno_rc ON dno_rc.ID = dno.DNO_REASON_CODE_ID
    WHERE UPPER(a.Listing_MP_Primary_ID) IN (SELECT id FROM search_ids)
       OR UPPER(a.LISTING_ID) IN (SELECT id FROM search_ids)
       OR UPPER(a.LISTING_MP_PAGE_ID) IN (SELECT id FROM search_ids)
       OR UPPER(a.LISTING_MP_SECONDARY_ID) IN (SELECT id FROM search_ids)
       OR UPPER(b.MASTER_ID) IN (SELECT id FROM search_ids)
       OR UPPER(b.MPN) IN (SELECT id FROM search_ids)
),
q2 AS (
    SELECT pc.MARKETPLACE_NAME AS marketplace, pc.VENDOR_NAME AS vendor, pc.MARKETPLACE_PRIMARY_ID AS sku,
        pc.FULFILLMENT_TYPE AS listing_fulfillment_type, pc.LISTING_ID AS listing_id,
        pc.LISTING_IS_SHIPABLE AS shippable_tag, pc.LISTING_TYPE AS listing_type,
        pc.IS_ACTIVE AS is_active, pc.IS_DISCONTINUED AS is_discontinued,
        pc.PRODUCT_NAME AS product_name, pc.UPC AS upc, pc.EAN AS ean,
        pc.CAN_EXPIRE AS can_expire,
        pc.FINANCE_APPROVED_WHOLESALE_PRICE_W_CURRENCY AS wholesale_price,
        pc.MAP_W_CURRENCY AS map_price, pc.RETAIL_W_CURRENCY AS retail_price,
        pc.MSRP_W_CURRENCY AS msrp_price,
        pc.SELLER_NAME AS seller_name
    FROM PATTERN_DB.PUBLIC.PRODUCT_CATALOG_PRODUCTS_AND_LISTINGS_VIEW pc
    WHERE UPPER(pc.MARKETPLACE_PRIMARY_ID) IN (SELECT id FROM search_ids)
       OR UPPER(pc.LISTING_ID) IN (SELECT id FROM search_ids)
       OR UPPER(pc.UPC) IN (SELECT id FROM search_ids)
       OR UPPER(pc.EAN) IN (SELECT id FROM search_ids)
       OR pc.LISTING_ID IN (SELECT listing_id FROM q1 WHERE listing_id IS NOT NULL)
),
q3 AS (
    SELECT h.LISTING_ID AS listing_id, h.IS_DNO AS is_dno
    FROM PATTERN_DB.PUBLIC.CATALOG_LISTING_STATUS_HISTORY h
    WHERE h.LISTING_ID IN (
        SELECT listing_id FROM q1 WHERE listing_id IS NOT NULL
        UNION
        SELECT listing_id FROM q2 WHERE listing_id IS NOT NULL
    )
    QUALIFY ROW_NUMBER() OVER (PARTITION BY h.LISTING_ID ORDER BY h."DATE" DESC) = 1
),
base AS (
    SELECT COALESCE(q2.marketplace, q1.marketplace) AS MARKETPLACE,
        COALESCE(q2.vendor, q1.vendor) AS VENDOR,
        COALESCE(q2.sku, q1.sku) AS SKU,
        COALESCE(q2.listing_fulfillment_type, q1.listing_fulfillment_type) AS LISTING_FULFILLMENT_TYPE,
        COALESCE(q2.listing_id, q1.listing_id) AS LISTING_ID,
        q1.master_id AS MASTER_ID, q1.mpn AS MPN,
        q1.asin AS ASIN, q1.fnsku AS FNSKU,
        q1.commingled_status AS COMMINGLED_STATUS,
        q2.shippable_tag AS SHIPPABLE_TAG,
        q2.listing_type AS LISTING_TYPE,
        COALESCE(q3.is_dno, FALSE) AS IS_DNO,
        q2.is_active AS IS_ACTIVE, q2.is_discontinued AS IS_DISCONTINUED,
        q2.product_name AS PRODUCT_NAME, q2.upc AS UPC, q2.ean AS EAN,
        q2.can_expire AS CAN_EXPIRE, q2.wholesale_price AS WHOLESALE_PRICE,
        q2.map_price AS MAP_PRICE, q2.retail_price AS RETAIL_PRICE,
        q2.msrp_price AS MSRP_PRICE, q1.dno_note AS DNO_NOTE,
        q1.dno_reason_code AS DNO_REASON_CODE,
        q2.seller_name AS MARKETPLACE_SELLER
    FROM q1 FULL OUTER JOIN q2 ON q1.listing_id = q2.listing_id
    LEFT JOIN q3 ON q3.listing_id = COALESCE(q1.listing_id, q2.listing_id)
)
SELECT * FROM base
WHERE UPPER(SKU) IN (SELECT id FROM search_ids)
   OR UPPER(LISTING_ID) IN (SELECT id FROM search_ids)
   OR UPPER(ASIN) IN (SELECT id FROM search_ids)
   OR UPPER(MPN) IN (SELECT id FROM search_ids)
   OR UPPER(MASTER_ID) IN (SELECT id FROM search_ids)
   OR UPPER(FNSKU) IN (SELECT id FROM search_ids)
ORDER BY MARKETPLACE, VENDOR, SKU
