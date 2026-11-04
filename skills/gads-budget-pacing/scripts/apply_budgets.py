#!/usr/bin/env python3
"""
apply_budgets.py - write APPROVED daily budget changes from budget_changes.csv.

Default is validate-only: the API checks every operation and nothing changes.
Add --execute to write. Only run after the user has said yes in the approval queue.

  $PY apply_budgets.py -c 1234567890 --file gads-out/<cid>/<date>/budget_changes.csv            # validate
  $PY apply_budgets.py -c 1234567890 --file gads-out/<cid>/<date>/budget_changes.csv --execute  # write

Safety:
  * Re-reads each budget's live amount and refuses rows where it no longer matches
    current_daily (someone changed it since the analysis). --force overrides.
  * Refuses any single change larger than --max-change (default 50%).
  * Shared budgets are changed once at the budget level; every campaign on them is affected.
Delete rows the user rejected before running.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gads  # noqa: E402


def build_ops(client, rows):
    """CampaignBudgetOperation updates with a field mask on amount_micros only."""
    from google.api_core import protobuf_helpers  # type: ignore

    ops = []
    for r in rows:
        op = client.get_type("CampaignBudgetOperation")
        b = op.update
        b.resource_name = r["budget_resource_name"]
        b.amount_micros = int(round(float(r["new_daily"]) * 1_000_000 / 10_000) * 10_000)  # whole cents
        client.copy_from(op.update_mask, protobuf_helpers.field_mask(None, b._pb))
        ops.append(op)
    return ops


def resolve(cid, rows):
    """Fill missing resource names (offline analysis) and fetch live amounts."""
    live = {}
    for r in gads.query(cid, """
        SELECT campaign.name, campaign_budget.resource_name, campaign_budget.name, campaign_budget.amount_micros
        FROM campaign WHERE campaign.status != 'REMOVED'"""):
        live[r["campaign.name"]] = (r["campaign_budget.resource_name"], gads.micros(r.get("campaign_budget.amount_micros")))
        live["budget:" + r.get("campaign_budget.name", "")] = live[r["campaign.name"]]
    for r in rows:
        key = None
        if not r.get("budget_resource_name"):
            first = r["campaigns"].split(";")[0].strip()
            key = live.get(first) or live.get("budget:" + r["budget_name"])
            if key:
                r["budget_resource_name"] = key[0]
        rn_amt = {v[0]: v[1] for v in live.values()}
        r["live_daily"] = rn_amt.get(r.get("budget_resource_name"))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--customer", "-c", required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--max-change", type=float, default=0.50)
    ap.add_argument("--force", action="store_true", help="Apply even if the live budget differs from current_daily.")
    ap.add_argument("--execute", action="store_true", help="Actually write. Without it, validate only.")
    a = ap.parse_args()

    cid = gads.clean_cid(a.customer)
    rows = [r for r in csv.DictReader(open(a.file)) if r.get("new_daily")]
    if not rows:
        gads.die("No rows to apply.")
    rows = resolve(cid, rows)
    ok = []
    for r in rows:
        cur, new = float(r["current_daily"]), float(r["new_daily"])
        if not r.get("budget_resource_name"):
            print(f"  SKIP {r['budget_name']}: budget not found in account")
            continue
        if r["live_daily"] is not None and abs(r["live_daily"] - cur) > max(0.01 * cur, 0.5) and not a.force:
            print(f"  SKIP {r['budget_name']}: live budget is {r['live_daily']:.2f}, analysis assumed {cur:.2f}. Re-run pacing or pass --force.")
            continue
        if cur and abs(new / cur - 1) > a.max_change:
            print(f"  SKIP {r['budget_name']}: {cur:.2f} -> {new:.2f} exceeds --max-change {a.max_change:.0%}")
            continue
        shared = " (shared: " + r["campaigns"] + ")" if str(r.get("shared")).lower() == "true" else ""
        print(f"  {r['budget_name']}{shared}: {cur:.2f} -> {new:.2f}/day")
        ok.append(r)
    if not ok:
        gads.die("Nothing to apply after safety checks.")

    client = gads.get_client()
    ops = build_ops(client, ok)
    vo = not a.execute
    req = {"customer_id": cid, "operations": ops, "partial_failure": True, "validate_only": vo}
    try:
        resp = client.get_service("CampaignBudgetService").mutate_campaign_budgets(request=req)
    except Exception as e:
        gads.handle_api_error(e)
    pf = getattr(resp, "partial_failure_error", None)
    if pf and pf.message:
        print(f"  partial failures: {pf.message}")
    print(f"{'Applied' if a.execute else 'Validated'} {len(ops)} budget change(s).")
    if vo:
        print("Nothing was changed. Re-run with --execute once the user has approved.")


if __name__ == "__main__":
    main()
