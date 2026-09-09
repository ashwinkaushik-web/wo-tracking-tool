# WO_TRA — PO ↔ work-order gap query

Verification brief for the Instock Streamlit **WO tool**: `queries/po_item_wo_gap.sql` (PO line vs work-order coverage) and the ℹ️ Flag guide on Overview → Needs attention.

Imported from the Claude Code WO_TRA thread, plus the Snowflake `WORKABLE_TYPE` counts you already ran.

## Verdict

- **SQL is sound.** Correct `STG_AMACZAR__LISTINGS` table, `RECEIVABLE_TYPE = 'Purchase'` linkage, Item-workable (FBM) fallback via `COALESCE(l.ITEM_ID, woi.WORKABLE_ID)`.
- **No bundle-component fix.** Purchase-path WOIs are only `Listing` and `Item`. Coverage lost: 17 units of ~126.2M.
- **Flag guide still missing in the UI.** PR #8 is on main. Reboot Streamlit first. If it is still gone, stop swallowing the import error and stamp the deployed commit (`flag_guide_fix.py`).

Snowflake was not re-run in this environment (MCP down, no service-account key). The table in the app is your live result.

## Run the brief

```bash
npm install
npm run dev
```

[http://127.0.0.1:4317](http://127.0.0.1:4317)

## SQL artifacts


| File                          | What                                         |
| ----------------------------- | -------------------------------------------- |
| `queries/dq_bundle_check.sql` | Diagnostic you already ran                   |
| `queries/po_item_wo_gap.sql`  | Coverage join as reviewed                    |
| `flag_guide_fix.py`           | Proposed Streamlit wiring (private app repo) |


