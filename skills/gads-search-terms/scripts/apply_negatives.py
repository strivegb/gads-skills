#!/usr/bin/env python3
"""
apply_negatives.py - push APPROVED negatives from negatives.csv to the account.

Three steps, enforced by the shared write gate in gads.py:
  1. Validate (default): the API checks every operation with validate_only=True.
     Nothing changes. On success it prints a token (a hash of the CSV).
  2. Execute: --execute --confirm <token>, only after the user said yes to
     those items. Refused if the CSV changed since it was validated.
  3. Read back: the negatives are re-read from the account, any that did not
     land are printed, and the write is appended to gads-out/<cid>/changes.log.jsonl.

  $PY apply_negatives.py -c 1234567890 --file gads-out/<cid>/<date>/negatives.csv \
      --shared-list "Negatives - Junk themes" [--attach-all-search] [--only-level campaign|shared]
  $PY apply_negatives.py ... --execute --confirm <token>

Rows with level 'shared list' go into the named shared negative keyword list
(created if missing). Rows with level 'campaign' go onto that campaign by campaign_id.
Delete rows from the CSV for anything the user rejected, then validate again.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gads  # noqa: E402

SKILL = "gads-search-terms"


def kw_key(prefix: str, text: str, match: str) -> str:
    return f"{prefix}|{' '.join(str(text).lower().split())}|{str(match).upper()}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--customer", "-c", required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--shared-list", default="Negatives - Junk themes (gads-search-terms)")
    ap.add_argument("--attach-all-search", action="store_true", help="Attach the shared list to every enabled Search campaign.")
    ap.add_argument("--only-level", choices=["campaign", "shared"])
    gads.add_write_args(ap)
    a = ap.parse_args()

    vo = gads.check_execute(a, a.file)
    cid = gads.clean_cid(a.customer)
    client = gads.get_client()
    MT = client.enums.KeywordMatchTypeEnum
    with open(a.file) as f:
        rows = list(csv.DictReader(f))
    shared = [r for r in rows if r["level"].startswith("shared") and a.only_level in (None, "shared")]
    camp = [r for r in rows if r["level"] == "campaign" and a.only_level in (None, "campaign")]
    print(f"{'EXECUTE' if not vo else 'VALIDATE-ONLY (no changes)'}: {len(shared)} shared-list negatives, {len(camp)} campaign negatives")

    n_ops, failures = 0, 0
    expected: dict[str, bool] = {}
    ss_rn, attach_ids = None, []

    def report_partial(resp):
        nonlocal failures
        pf = getattr(resp, "partial_failure_error", None)
        if pf and pf.message:
            failures += 1
            print(f"  partial failures: {pf.message}")

    def send(service_name, method, ops):
        nonlocal n_ops
        if not ops:
            return
        svc = client.get_service(service_name)
        for i in range(0, len(ops), 1000):
            chunk = ops[i:i + 1000]
            req = {"customer_id": cid, "operations": chunk, "partial_failure": True, "validate_only": vo}
            try:
                resp = getattr(svc, method)(request=req)
            except Exception as e:
                gads.handle_api_error(e)
            report_partial(resp)
            n_ops += len(chunk)
            print(f"  {method}: {len(chunk)} ops {'validated' if vo else 'sent'}")

    def search_campaigns():
        return gads.query(cid, """SELECT campaign.id, campaign.resource_name FROM campaign
            WHERE campaign.status = 'ENABLED' AND campaign.advertising_channel_type = 'SEARCH'""") if a.attach_all_search else []

    if shared:
        name = a.shared_list
        found = gads.query(cid, f"""SELECT shared_set.resource_name, shared_set.name FROM shared_set
            WHERE shared_set.type = 'NEGATIVE_KEYWORDS' AND shared_set.status = 'ENABLED'
              AND shared_set.name = {gads.gaql_str(name)}""")
        camps = search_campaigns()
        attach_ids = [str(c["campaign.id"]) for c in camps]
        for r in shared:
            expected[kw_key("shared", r["text"], r["match_type"])] = True
        for i in attach_ids:
            expected[f"attach|{i}"] = True
        if found:
            ss_rn = found[0]["shared_set.resource_name"]
            ops = []
            for r in shared:
                op = client.get_type("SharedCriterionOperation")
                op.create.shared_set = ss_rn
                op.create.keyword.text = r["text"]
                op.create.keyword.match_type = getattr(MT, r["match_type"].upper())
                ops.append(op)
            send("SharedCriterionService", "mutate_shared_criteria", ops)
            ops = []
            for c in camps:
                op = client.get_type("CampaignSharedSetOperation")
                op.create.campaign = c["campaign.resource_name"]
                op.create.shared_set = ss_rn
                ops.append(op)
            send("CampaignSharedSetService", "mutate_campaign_shared_sets", ops)
        else:
            # New list: one atomic GoogleAdsService.mutate with a temporary ID, so the
            # list, its keywords and attachments can all be validated before it exists.
            tmp = f"customers/{cid}/sharedSets/-1"
            mops = []
            mo = client.get_type("MutateOperation")
            mo.shared_set_operation.create.resource_name = tmp
            mo.shared_set_operation.create.name = name
            mo.shared_set_operation.create.type_ = client.enums.SharedSetTypeEnum.NEGATIVE_KEYWORDS
            mops.append(mo)
            for r in shared:
                mo = client.get_type("MutateOperation")
                mo.shared_criterion_operation.create.shared_set = tmp
                mo.shared_criterion_operation.create.keyword.text = r["text"]
                mo.shared_criterion_operation.create.keyword.match_type = getattr(MT, r["match_type"].upper())
                mops.append(mo)
            for c in camps:
                mo = client.get_type("MutateOperation")
                mo.campaign_shared_set_operation.create.campaign = c["campaign.resource_name"]
                mo.campaign_shared_set_operation.create.shared_set = tmp
                mops.append(mo)
            try:
                resp = client.get_service("GoogleAdsService").mutate(
                    request={"customer_id": cid, "mutate_operations": mops, "validate_only": vo})
            except Exception as e:
                gads.handle_api_error(e)
            n_ops += len(mops)
            print(f"  new shared list '{name}' + {len(shared)} negatives + {len(camps)} attachments: "
                  f"{len(mops)} ops {'validated' if vo else 'sent'}")
            if not vo:
                ss_rn = resp.mutate_operation_responses[0].shared_set_result.resource_name
                print(f"  created shared list {ss_rn}")

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
            expected[kw_key(f"campaign:{r['campaign_id']}", r["text"], r["match_type"])] = True
        if skipped:
            print(f"  skipped {skipped} rows without campaign_id (offline CSV); add them in Editor instead")
        send("CampaignCriterionService", "mutate_campaign_criteria", ops)

    if vo:
        if failures:
            gads.die("Validation found errors (see above). Fix or remove those rows, then validate again.")
        if not n_ops:
            gads.die("Nothing to validate.")
        gads.record_validation(a.file, n_ops)
        return

    # ---- read back what the account now holds
    actual: dict[str, bool] = {}
    if ss_rn:
        for r in gads.query(cid, f"""SELECT shared_criterion.keyword.text, shared_criterion.keyword.match_type
            FROM shared_criterion WHERE shared_criterion.shared_set = {gads.gaql_str(ss_rn)}"""):
            actual[kw_key("shared", r.get("shared_criterion.keyword.text", ""), r.get("shared_criterion.keyword.match_type", ""))] = True
        if attach_ids:
            for r in gads.query(cid, f"""SELECT campaign.id FROM campaign_shared_set
                WHERE campaign_shared_set.shared_set = {gads.gaql_str(ss_rn)} AND campaign_shared_set.status = 'ENABLED'"""):
                actual[f"attach|{r.get('campaign.id')}"] = True
    camp_ids = sorted({str(r["campaign_id"]) for r in camp if str(r.get("campaign_id", "")).isdigit()})
    if camp_ids:
        for r in gads.query(cid, f"""SELECT campaign.id, campaign_criterion.keyword.text, campaign_criterion.keyword.match_type
            FROM campaign_criterion
            WHERE campaign_criterion.negative = TRUE AND campaign_criterion.type = 'KEYWORD'
              AND campaign.id IN ({', '.join(camp_ids)})"""):
            actual[kw_key(f"campaign:{r.get('campaign.id')}", r.get("campaign_criterion.keyword.text", ""),
                          r.get("campaign_criterion.keyword.match_type", ""))] = True
    mismatches = gads.verify_readback(expected, actual)
    if mismatches:
        print(f"Read back: {len(mismatches)} of {len(expected)} changes are not in the account:")
        for m in mismatches[:50]:
            print(f"  - {m}")
    else:
        print(f"Read back: all {len(expected)} changes are in the account.")
    p = gads.log_change(cid, SKILL, {
        "action": "add_negatives", "file": str(a.file), "token": gads.plan_token(a.file),
        "shared_list": a.shared_list if shared else "", "shared_set": ss_rn or "",
        "shared_negatives": len(shared), "campaign_negatives": len(camp), "attached_campaigns": len(attach_ids),
        "operations": n_ops, "partial_failures": failures, "mismatches": mismatches,
    })
    print(f"Logged to {p}")


if __name__ == "__main__":
    main()
