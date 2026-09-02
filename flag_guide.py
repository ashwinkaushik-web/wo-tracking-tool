"""
Flag guide — why each exception happens & what to do.
=====================================================
A curated, Information-Station-grounded reference for the flags/reasons the tool
shows. Content is distilled from Pattern's Information Station (private repo
patterninc/information-station); each entry links to the source doc for anyone
who wants to dig deeper.

Baked in (not fetched live) because the app runs on Streamlit Community Cloud and
IS is a private repo — this keeps deploys credential-free. Update this file when
the underlying IS docs change.
"""

import streamlit as st

_IS_REPO = "https://github.com/patterninc/information-station/blob/main/"

# Each entry: reason, meaning, cause, action, confidence, source_label, source_path
FLAG_GUIDE = [
    {
        "reason": "🧾 PO placed/arriving with no work order",
        "meaning": "A PO is placed/arriving but no work order routes its units to a marketplace destination.",
        "cause": "A WO is auto-created at placement only for lines that have a shippable listing destination. "
                 "Lines with no listing destination get no WO — their units are received sellable and stowed "
                 "(Pattern-owned), which shows up as the stow reason \"Is Missing WOI for PO/IR\".",
        "action": "Confirm whether the line is meant to have a marketplace destination. If yes, raise the WO. "
                  "If the units are legitimately going to storage (no listing), stow is expected — no action.",
        "confidence": "IS-backed",
        "source_label": "warehouse.md · ordering.md",
        "source_path": "context/data/01-architecture/end_to_end_flows/flow_graph/warehouse.md",
    },
    {
        "reason": "🧩 Genuine gap — needs WO raised",
        "meaning": "Received/ordered units exceed the units any work order covers, and nothing is routing them out.",
        "cause": "The \"Is Missing WOI for PO/IR\" coverage gap — received units exceed WO coverage "
                 "(bundle qty × component ratio). It's a coverage gap, not a system error.",
        "action": "Check BOTH paths for the item: a direct WOI on this PO's listing AND bundle WOIs where it's a "
                  "component (components can sell standalone). If genuinely uncovered and marketplace-bound, raise the WO.",
        "confidence": "IS-backed",
        "source_label": "work_orders.md (Business Rules)",
        "source_path": "context/data/06-analytics/domains/work_orders.md",
    },
    {
        "reason": "✅ Fully received — WO likely not required",
        "meaning": "Ordered quantity is fully received with nothing outstanding, so no WO is expected.",
        "cause": "Many received units legitimately have no WO — vendor-owned / Pattern-owned stow without a WO is "
                 "common and expected; reconcile overwrites current quantity with received quantity.",
        "action": "Informational — no action. Only investigate if a marketplace destination was expected for these units.",
        "confidence": "IS facts; the \"likely not required\" call is a tool judgment, not an IS rule",
        "source_label": "warehouse.md · purchase_order_state_manager.md",
        "source_path": "context/data/01-architecture/end_to_end_flows/flow_graph/warehouse.md",
    },
    {
        "reason": "🚫 PO reconciled/cancelled",
        "meaning": "The PO has reached a terminal state — ready_to_reconcile/closed, or cancelled.",
        "cause": "Reconcile → closed is the normal end of life. Cancel is only reachable from drafted/created — "
                 "a placed or received PO cannot be cancelled.",
        "action": "No WO action — the PO is closing out. If a \"cancelled\" PO actually received units, escalate "
                  "(data inconsistency).",
        "confidence": "IS-backed",
        "source_label": "purchase_order_state_manager.md",
        "source_path": "context/data/08-github-code/modules/purchase_order_state_manager.md",
    },
    {
        "reason": "📦 Covered via Storage WO (not PO-linked)",
        "meaning": "The outstanding units are already covered by a storage/pick WO that isn't tied to this PO.",
        "cause": "Storage/pick WOs carry PO_NUMBER = NULL (the receivable is Purchase, InventoryRequest, or a "
                 "NULL pick). Coverage can come from a no-receivable pick WO rather than the PO.",
        "action": "Treat as covered — no new PO-linked WO needed. Verify via the storage-WO path (PO_NUMBER IS NULL) "
                  "rather than the PO join.",
        "confidence": "IS-backed",
        "source_label": "work_orders.md · warehouse.md",
        "source_path": "context/data/06-analytics/domains/work_orders.md",
    },
    {
        "reason": "🟠 Partial WO",
        "meaning": "A work order exists for the item but covers fewer units than are outstanding/received.",
        "cause": "Same coverage-gap mechanism as \"Is Missing WOI\": WO coverage is less than received units and the "
                 "uncovered remainder stows. Expected behaviour unless the WO was created with the wrong quantity.",
        "action": "Decide on the remainder — raise/extend a WO if those units need a marketplace, or accept the stow. "
                  "Investigate only if the WO quantity itself looks wrong.",
        "confidence": "IS-backed",
        "source_label": "work_orders.md · warehouse.md",
        "source_path": "context/data/06-analytics/domains/work_orders.md",
    },
    {
        "reason": "🔴 Listing Failed (blocked WO item)",
        "meaning": "The WO line can't be picked because its listing failed its inbound eligibility check.",
        "cause": "Unpickable reason \"Listing Failed Eligibility Check\": the listing's most-recent eligibility "
                 "check failed at the listing level (account-level failures excluded).",
        "action": "Resolve the listing's FBA/FBT eligibility problem at the marketplace/catalog level. The 10-minute "
                  "cron clears the flag once the latest check passes.",
        "confidence": "IS-backed",
        "source_label": "unpickable_reasons.md (id 5)",
        "source_path": "context/data/02-database/tables/stg_amaczar/unpickable_reasons.md",
    },
    {
        "reason": "🔴 Listing Not Shippable (blocked WO item)",
        "meaning": "The WO line can't be picked because the listing is flagged not-shippable.",
        "cause": "Unpickable reason \"Listing is Marked Not Shippable\" (Listing.not_shippable). The same flag also "
                 "gates PO placement and WO creation.",
        "action": "Fix the listing's shippability status (or route via a shippable sibling listing). The flag clears "
                  "on the next cron run once shippable.",
        "confidence": "IS-backed",
        "source_label": "unpickable_reasons.md (id 6) · ordering.md",
        "source_path": "context/data/02-database/tables/stg_amaczar/unpickable_reasons.md",
    },
    {
        "reason": "📈 Over-receipt (received > ordered)",
        "meaning": "More units were received than were ordered on the PO line.",
        "cause": "Receiving more than ordered creates an overage (status pending/accepted/rejected). At receipt "
                 "allocation a second pass auto-accepts overage WOIs for the leftover quantity.",
        "action": "IM accepts or rejects the overage in the Predict UI; accepted quantity is recorded, then decide "
                  "WO coverage for the accepted extra units.",
        "confidence": "IS-backed",
        "source_label": "po_wo_flow.md (Overage Detection)",
        "source_path": "context/data/01-architecture/flows/po_wo_flow.md",
    },
]


def render_flag_guide(expanded=False):
    """Render the Flag guide as one expandable Overview section."""
    with st.expander("ℹ️ Flag guide — why these happen & what to do", expanded=expanded):
        st.caption("Plain-English meaning, likely root cause, and the recommended next step for each flag. "
                   "Grounded in Pattern's Information Station; each entry links to its source doc.")
        for e in FLAG_GUIDE:
            st.markdown(f"**{e['reason']}**")
            st.markdown(
                f"- **What it means:** {e['meaning']}\n"
                f"- **Root cause:** {e['cause']}\n"
                f"- **What to do:** {e['action']}\n"
                f"- **Confidence:** {e['confidence']}  ·  "
                f"**Source:** [{e['source_label']}]({_IS_REPO}{e['source_path']}) *(Information Station)*"
            )
            st.markdown("")
        st.caption("Source: Pattern Information Station (patterninc/information-station). Baked into the app and "
                   "refreshed manually when the underlying docs change.")
