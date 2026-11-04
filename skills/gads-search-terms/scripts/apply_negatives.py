#!/usr/bin/env python3
"""
apply_negatives.py - push APPROVED negatives from negatives.csv to the account.

Default is validate-only: the API checks every operation and nothing changes.
Add --execute to write. Only run after the user has said yes in the approval queue.

  $PY apply_negatives.py -c 1234567890 --file gads-out/<cid>/<date>/negatives.csv \
      --shared-list "Negatives - Junk themes" [--attach-all-search] [--only-level campaign|shared] [--execute]

Rows with level 'shared list' go into the named shared negative keyword list
(created if missing). Rows with level 'campaign' go onto that campaign by campaign_id.
Delete rows from the CSV for anything the user rejected before running.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gads  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--customer", "-c", required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--shared-list", default="Negatives - Junk themes (gads-search-terms)")
    ap.add_argument("--attach-all-search", action="store_true", help="Attach the shared list to every enabled Search campaign.")
    ap.add_argument("--only-level", choices=["campaign", "shared"])
    ap.add_argument("--execute", action="store_true", help="Actually write. Without it, validate only.")
    a = ap.parse_args()

    cid = gads.clean_cid(a.customer)
    client = gads.get_client()
    MT = client.enums.KeywordMatchTypeEnum
    rows = list(csv.DictReader(open(a.file)))
    shared = [r for r in rows if r["level"].startswith("shared") and a.only_level in (None, "shared")]
    camp = [r for r in rows if r["level"] == "campaign" and a.only_level in (None, "campaign")]
    vo = not a.execute
    mode = "EXECUTE" if a.execute else "VALIDATE-ONLY (no changes)"
    print(f"{mode}: {len(shared)} shared-list negatives, {len(camp)} campaign negatives")

    def send(service_name, method, ops):
        if not ops:
            return
        svc = client.get_service(service_name)
        for i in range(0, len(ops), 1000):
            req = {"customer_id": cid, "operations": ops[i:i + 1000], "partial_failure": True, "validate_only": vo}
            try:
                resp = getattr(svc, method)(request=req)
            except Exception as e:
                gads.handle_api_error(e)
            pf = getattr(resp, "partial_failure_error", None)
            if pf and pf.message:
                print(f"  partial failures: {pf.message}")
            print(f"  {method}: {len(ops[i:i + 1000])} ops {'validated' if vo else 'applied'}")

    if shared:
        name = a.shared_list
        found = gads.query(cid, f"""SELECT shared_set.resource_name, shared_set.name FROM shared_set
            WHERE shared_set.type = 'NEGATIVE_KEYWORDS' AND shared_set.status = 'ENABLED'
              AND shared_set.name = {gads.gaql_str(name)}""")
        if found:
            ss_rn = found[0]["shared_set.resource_name"]
        elif vo:
            ss_rn = None
            print(f"  shared list '{name}' does not exist; it will be created on --execute")
        else:
            op = client.get_type("SharedSetOperation")
            op.create.name = name
            op.create.type_ = client.enums.SharedSetTypeEnum.NEGATIVE_KEYWORDS
            ss_rn = client.get_service("SharedSetService").mutate_shared_sets(customer_id=cid, operations=[op]).results[0].resource_name
            print(f"  created shared list {ss_rn}")
        if ss_rn:
            ops = []
            for r in shared:
                op = client.get_type("SharedCriterionOperation")
                op.create.shared_set = ss_rn
                op.create.keyword.text = r["text"]
                op.create.keyword.match_type = getattr(MT, r["match_type"].upper())
                ops.append(op)
            send("SharedCriterionService", "mutate_shared_criteria", ops)
            if a.attach_all_search:
                camps = gads.query(cid, """SELECT campaign.resource_name FROM campaign
                    WHERE campaign.status = 'ENABLED' AND campaign.advertising_channel_type = 'SEARCH'""")
                ops = []
                for c in camps:
                    op = client.get_type("CampaignSharedSetOperation")
                    op.create.campaign = c["campaign.resource_name"]
                    op.create.shared_set = ss_rn
                    ops.append(op)
                send("CampaignSharedSetService", "mutate_campaign_shared_sets", ops)

    if camp:
        cs = client.get_service("CampaignService")
        ops, skipped = [], 0
        for r in camp:
            if not r.get("campaign_id"):
                skipped += 1
                continue
            op = client.get_type("CampaignCriterionOperation")
            op.create.campaign = cs.campaign_path(cid, r["campaign_id"])
            op.create.negative = True
            op.create.keyword.text = r["text"]
            op.create.keyword.match_type = getattr(MT, r["match_type"].upper())
            ops.append(op)
        if skipped:
            print(f"  skipped {skipped} rows without campaign_id (offline CSV); add them in Editor instead")
        send("CampaignCriterionService", "mutate_campaign_criteria", ops)

    if vo:
        print("Nothing was changed. Re-run with --execute once the user has approved.")


if __name__ == "__main__":
    main()
