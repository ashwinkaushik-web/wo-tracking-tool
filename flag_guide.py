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
    {
        "reason": "🔁 Replen Needed (blocked WO item)",
        "meaning": "Units exist in the building but not in a pickable location, so the line can't be "
                   "filled until stock is moved into a pickable bin.",
        "cause": "The inventory rollup classes these units as non-pickable (on-hand but not "
                 "marketplace-order-pickable). The row's resolvable_quantity is how many units a replen unlocks.",
        "action": "Replenish the SKU — move stock into a pickable location. The resolvable_quantity on the "
                  "row tells you how many units that frees up for picking.",
        "confidence": "IS-backed",
        "source_label": "unpickable_reasons.md (id 2) · work_order_items_unpickable.md",
        "source_path": "context/data/02-database/tables/stg_amaczar/unpickable_reasons.md",
    },
    {
        "reason": "🚱 No Inventory (blocked WO item)",
        "meaning": "No usable inventory exists to satisfy the line — nothing pickable, replenishable, or pending.",
        "cause": "Emitted when total resolvable (replen + expiry + lot + pending) = 0 and the stock isn't merely "
                 "expired. These rows carry no resolvable_quantity because there's nothing in the warehouse to resolve.",
        "action": "No in-warehouse move unlocks it — it clears only when new inventory is brought in "
                  "(inbound/receipt). Check sourcing / raise a PO or IR if the demand is still needed.",
        "confidence": "IS-backed (meaning & cause); the sourcing next-step is inferred",
        "source_label": "unpickable_reasons.md (id 1) · work_order_items_unpickable.md",
        "source_path": "context/data/08-github-code/modules/work_order_items_unpickable.md",
    },
    {
        "reason": "⌛ Inventory Expired (blocked WO item)",
        "meaning": "On-hand stock for the item is past its expiration date, so it can't be picked.",
        "cause": "Emitted in the 'nothing resolvable' branch when expired quantity > 0 and all resolvable "
                 "buckets are 0. Carries no resolvable_quantity (a blank there isn't '0 units').",
        "action": "Dispose/remove the expired lots and replenish with in-date stock — no in-system action "
                  "unlocks these units.",
        "confidence": "IS-backed (cause); action inferred",
        "source_label": "unpickable_reasons.md (id 4) · work_order_items_unpickable.md",
        "source_path": "context/data/08-github-code/modules/work_order_items_unpickable.md",
    },
    {
        "reason": "⏳ Pending Sellable (blocked WO item)",
        "meaning": "Inventory is present but in a pending-sellable state (not yet released), so currently "
                   "unpickable but resolvable.",
        "cause": "Driven by the total_pending_quantity bucket; emitted with a resolvable_quantity when the "
                 "pending bucket is non-zero.",
        "action": "Progress the inventory through its sellable transition (quality / putaway / status release) "
                  "so the pending quantity becomes sellable.",
        "confidence": "IS-backed (cause); action inferred",
        "source_label": "unpickable_reasons.md (id 7) · work_order_items_unpickable.md",
        "source_path": "context/data/08-github-code/modules/work_order_items_unpickable.md",
    },
    {
        "reason": "📅 Missing Exp Date (blocked WO item)",
        "meaning": "Stock is on hand but blocked because the required expiration-date data hasn't been entered.",
        "cause": "missing_expiration_date flag → the pickable-qty-needing-exp-date bucket; emitted with a "
                 "resolvable_quantity.",
        "action": "Enter the missing expiration date for the affected lot/location; those units then become pickable.",
        "confidence": "IS-backed",
        "source_label": "unpickable_reasons.md (id 3) · work_order_items_unpickable.md",
        "source_path": "context/data/08-github-code/modules/work_order_items_unpickable.md",
    },
    {
        "reason": "🔖 Missing Lot (blocked WO item)",
        "meaning": "Stock is on hand but blocked because a required lot number hasn't been entered.",
        "cause": "missing_lot_number flag → the pickable-qty-needing-lot-number bucket; emitted with a "
                 "resolvable_quantity.",
        "action": "Enter the missing lot number for the affected inventory; those units then become pickable.",
        "confidence": "IS-backed",
        "source_label": "unpickable_reasons.md (id 8) · work_order_items_unpickable.md",
        "source_path": "context/data/08-github-code/modules/work_order_items_unpickable.md",
    },
]


# Substrings (lowercase) that map a panel's reason/coverage text to a guide entry.
_MATCH = {
    "🧾 PO placed/arriving with no work order": ["no work order", "placed/arriving", "no wo"],
    "🧩 Genuine gap — needs WO raised": ["genuine gap"],
    "✅ Fully received — WO likely not required": ["fully received"],
    "🚫 PO reconciled/cancelled": ["reconcil", "cancel"],
    "📦 Covered via Storage WO (not PO-linked)": ["storage wo"],
    "🟠 Partial WO": ["partial"],
    "🔴 Listing Failed (blocked WO item)": ["listing failed"],
    "🔴 Listing Not Shippable (blocked WO item)": ["not shippable"],
    "📈 Over-receipt (received > ordered)": ["over-receipt", "over receipt", "received > ordered"],
    "🔁 Replen Needed (blocked WO item)": ["replen"],
    "🚱 No Inventory (blocked WO item)": ["no inventory", "no available inventory"],
    "⌛ Inventory Expired (blocked WO item)": ["expired"],
    "⏳ Pending Sellable (blocked WO item)": ["pending sellable"],
    "📅 Missing Exp Date (blocked WO item)": ["missing exp", "expiration date", "requires expiration"],
    "🔖 Missing Lot (blocked WO item)": ["missing lot", "lot number", "requires lot"],
}


def _entries_for(reasons):
    """Guide entries whose match-substrings appear in any of the given reason strings."""
    reasons_l = [str(r).lower() for r in reasons if str(r).strip() and str(r).lower() != "nan"]
    out = []
    for e in FLAG_GUIDE:
        subs = _MATCH.get(e["reason"], [])
        if any(any(s in rl for s in subs) for rl in reasons_l):
            out.append(e)
    return out


def flag_action(text):
    """Short recommended action for a single reason/coverage value (for a per-row
    'What to do' table column). Returns '' if the flag isn't in the guide."""
    entries = _entries_for([text])
    return entries[0]["action"] if entries else ""


def flag_action_detailed(reason, *, marketplace=None, resolvable_qty=None, active_reasons=None):
    """Per-line 'What to do', made specific with this row's own attributes instead of
    the same boilerplate sentence for every row. Falls back to flag_action(reason)
    when no extra detail is available (e.g. the deeper unpickable-detail query didn't
    return a match for this WOI).

    marketplace: "<name> (<country>)" or similar display string, or None/blank.
    resolvable_qty: units a fix would free up, or None/NaN (genuinely blank for
        No-Inventory / Inventory-Expired — don't read that as "0 units").
    active_reasons: the exact stacked reason string for this line (e.g.
        "Listing Failed Eligibility Check | Listing is Marked Not Shippable"),
        or None if only one reason applies / not available.
    """
    base = flag_action(reason)
    if not base:
        return ""

    bits = []
    # Multiple reasons stacked on one line is itself part of the root cause —
    # surface it before the generic action so a "double-blocked" line reads as such.
    if active_reasons and str(active_reasons).strip():
        parts = [p.strip() for p in str(active_reasons).split("|") if p.strip()]
        if len(parts) > 1:
            bits.append(f"This line is blocked by {len(parts)} reasons: {', '.join(parts)}.")

    if marketplace and str(marketplace).strip():
        bits.append(f"Marketplace: {marketplace}.")

    try:
        qty_ok = resolvable_qty is not None and not (
            isinstance(resolvable_qty, float) and resolvable_qty != resolvable_qty  # NaN check, no numpy/pandas dep
        )
    except Exception:
        qty_ok = False
    if qty_ok and str(resolvable_qty).strip():
        try:
            qty_num = float(resolvable_qty)
            bits.append(f"Fixing this unlocks {qty_num:,.0f} unit(s).")
        except (TypeError, ValueError):
            pass

    if not bits:
        return base
    return base + " " + " ".join(bits)


def render_flag_guide_inline(reasons, title="ℹ️ What these flags mean & what to do", expanded=False):
    """Compact, in-panel guidance — only the flags actually present in this panel."""
    entries = _entries_for(reasons)
    if not entries:
        return
    with st.expander(title, expanded=expanded):
        for e in entries:
            st.markdown(
                f"**{e['reason']}** — {e['meaning']}  \n"
                f"→ *{e['action']}*  \n"
                f"<small>Root cause: {e['cause']} "
                f"([{e['source_label']}]({_IS_REPO}{e['source_path']}) · Information Station)</small>",
                unsafe_allow_html=True,
            )


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
