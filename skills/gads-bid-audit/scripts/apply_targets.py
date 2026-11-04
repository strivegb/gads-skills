#!/usr/bin/env python3
"""
apply_targets.py - push APPROVED target CPA / target ROAS changes from bid_changes.csv.

Campaign-level (standard) strategies only. Portfolio strategies are skipped:
changing a portfolio target moves every campaign in it, so do that by hand.
Strategy switches (manual -> Smart Bidding, brand -> Target Impression Share)
are not applied here; make them in the UI or Editor so the person approving
sees the full settings screen.

Default is validate-only. Add --execute to write, only after an explicit yes.

  $PY apply_targets.py -c 1234567890 --file gads-out/<cid>/<date>/bid_changes.csv [--include-held] [--execute]

Delete rows the user rejected before running. Rows marked held=yes are skipped
unless --include-held (they had a recent change or are in learning).
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gads  # noqa: E402

FIELD = {
    ("TARGET_CPA", "target_cpa"): "target_cpa.target_cpa_micros",
    ("MAXIMIZE_CONVERSIONS", "target_cpa"): "maximize_conversions.target_cpa_micros",
    ("TARGET_ROAS", "target_roas"): "target_roas.target_roas",
    ("MAXIMIZE_CONVERSION_VALUE", "target_roas"): "maximize_conversion_value.target_roas",
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--customer", "-c", required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--include-held", action="store_true")
    ap.add_argument("--max-step", type=float, default=0.20, help="Refuse moves larger than this fraction (default 0.20).")
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args()

    cid = gads.clean_cid(a.customer)
    client = gads.get_client()
    from google.api_core import protobuf_helpers  # type: ignore

    cs = client.get_service("CampaignService")
    live = {str(r["campaign.id"]): r for r in gads.query(cid, """
        SELECT campaign.id, campaign.name, campaign.bidding_strategy_type, campaign.bidding_strategy
        FROM campaign WHERE campaign.status != 'REMOVED'""")}

    ops, skipped = [], []
    for r in csv.DictReader(open(a.file)):
        cid_ = str(r["campaign_id"])
        cur = live.get(cid_)
        if not cur:
            skipped.append(f"{r['campaign']}: not found")
            continue
        if cur.get("campaign.bidding_strategy"):
            skipped.append(f"{r['campaign']}: portfolio strategy, change it on the portfolio")
            continue
        if r.get("held") == "yes" and not a.include_held:
            skipped.append(f"{r['campaign']}: held (recent change or learning)")
            continue
        st = cur.get("campaign.bidding_strategy_type", "")
        path = FIELD.get((st, r["metric"]))
        if not path:
            skipped.append(f"{r['campaign']}: strategy {st} does not take {r['metric']}")
            continue
        old, new = float(r["current"] or 0), float(r["proposed"])
        if old and abs(new - old) / old > a.max_step + 1e-9:
            skipped.append(f"{r['campaign']}: move {old} -> {new} exceeds --max-step {a.max_step:.0%}")
            continue
        op = client.get_type("CampaignOperation")
        camp = op.update
        camp.resource_name = cs.campaign_path(cid, cid_)
        parent, leaf = path.split(".")
        setattr(getattr(camp, parent), leaf, int(round(new * 1_000_000)) if leaf.endswith("micros") else new)
        client.copy_from(op.update_mask, protobuf_helpers.field_mask(None, camp._pb))
        ops.append(op)
        print(f"  {r['campaign']}: {r['metric']} {old} -> {new}")

    for s in skipped:
        print(f"  skip {s}")
    if not ops:
        print("No operations to send.")
        return
    try:
        resp = cs.mutate_campaigns(request={"customer_id": cid, "operations": ops,
                                            "partial_failure": True, "validate_only": not a.execute})
    except Exception as e:
        gads.handle_api_error(e)
    pf = getattr(resp, "partial_failure_error", None)
    if pf and pf.message:
        print(f"  partial failures: {pf.message}")
    print(f"{'Applied' if a.execute else 'Validated (no changes)'}: {len(ops)} target change(s).")
    if not a.execute:
        print("Re-run with --execute once the user has approved.")


if __name__ == "__main__":
    main()
