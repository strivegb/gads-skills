#!/usr/bin/env python3
"""
apply_targets.py - push APPROVED target CPA / target ROAS changes from bid_changes.csv.

Campaign-level (standard) strategies only. Portfolio strategies are skipped:
changing a portfolio target moves every campaign in it, so do that by hand.
Strategy switches (manual -> Smart Bidding, brand -> Target Impression Share)
and network settings (Search Partners) are not applied here; make them in the
UI or Editor so the person approving sees the full settings screen.

Three steps (shared write gate in gads.py):
  1. Validate: the API checks every operation with validate_only and nothing changes.
     On success it prints a token tied to this exact file.
  2. Execute: --execute --confirm <token>, only after the user said yes to these rows.
     Editing the file (for example removing rejected rows) means validating again.
  3. Read back: the script re-reads the targets, prints any change that did not land,
     and appends the write to gads-out/<cid>/changes.log.jsonl.

  $PY apply_targets.py -c 1234567890 --file gads-out/<cid>/<date>/bid_changes.csv [--include-held]
  $PY apply_targets.py -c 1234567890 --file .../bid_changes.csv --execute --confirm <token>

Moves above 20% are refused unless --allow-large --reason "<why>" is given; the
reason is printed and logged. --max-step can only lower the limit.
Delete rows the user rejected before validating. Rows marked held=yes are skipped
unless --include-held (they had a recent change or are in learning).
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gads  # noqa: E402

SKILL = "gads-bid-audit"
DEFAULT_LIMIT = 0.20

FIELD = {
    ("TARGET_CPA", "target_cpa"): "target_cpa.target_cpa_micros",
    ("MAXIMIZE_CONVERSIONS", "target_cpa"): "maximize_conversions.target_cpa_micros",
    ("TARGET_ROAS", "target_roas"): "target_roas.target_roas",
    ("MAXIMIZE_CONVERSION_VALUE", "target_roas"): "maximize_conversion_value.target_roas",
}


def check_limit(cur: float, new: float, limit: float = DEFAULT_LIMIT, allow_large: bool = False,
                reason: str = "") -> tuple[bool, str]:
    """Pure check of one target move. Returns (allowed, message).

    A campaign with no current target (cur == 0) is adding a target, which is
    not a percentage move; it passes.
    """
    if new <= 0:
        return False, "proposed target must be above zero"
    if not cur:
        return True, ""
    move = abs(new - cur) / cur
    if move <= limit + 1e-9:
        return True, ""
    if allow_large and reason.strip():
        return True, f"large change {move:.0%} allowed: {reason.strip()}"
    return False, f"{cur} -> {new} is a {move:.0%} move, above the {limit:.0%} limit. Pass --allow-large --reason \"<why>\" if approved."


def check_args(a) -> str | None:
    if a.max_step > DEFAULT_LIMIT + 1e-9:
        return f"--max-step cannot exceed {DEFAULT_LIMIT:.0%}. Use --allow-large --reason for bigger moves."
    if a.allow_large and not a.reason.strip():
        return "--allow-large needs --reason \"<why>\" (it is printed and logged)."
    return None


def read_back(cid, ids) -> dict:
    rows = gads.query(cid, f"""
        SELECT campaign.id, campaign.target_cpa.target_cpa_micros, campaign.maximize_conversions.target_cpa_micros,
               campaign.target_roas.target_roas, campaign.maximize_conversion_value.target_roas
        FROM campaign WHERE campaign.id IN ({", ".join(str(int(i)) for i in ids)})""")
    out = {}
    for r in rows:
        i = str(r["campaign.id"])
        cpa = gads.micros(r.get("campaign.target_cpa.target_cpa_micros")) or gads.micros(r.get("campaign.maximize_conversions.target_cpa_micros"))
        roas = gads.num(r.get("campaign.target_roas.target_roas")) or gads.num(r.get("campaign.maximize_conversion_value.target_roas"))
        out[f"{i}:target_cpa"] = round(cpa, 2)
        out[f"{i}:target_roas"] = round(roas, 4)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--customer", "-c", required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--include-held", action="store_true")
    ap.add_argument("--max-step", "--max-change", dest="max_step", type=float, default=DEFAULT_LIMIT,
                    help="Per-change limit (default and maximum 0.20).")
    ap.add_argument("--allow-large", action="store_true", help="Allow moves above the limit. Needs --reason.")
    ap.add_argument("--reason", default="", help="Why a large move was approved. Printed and logged.")
    gads.add_write_args(ap)
    a = ap.parse_args()

    err = check_args(a)
    if err:
        gads.die(err)
    vo = gads.check_execute(a, a.file)  # dies before any API call if --execute lacks a valid token
    if a.reason:
        print(f"Reason given: {a.reason}")

    cid = gads.clean_cid(a.customer)
    client = gads.get_client()
    from google.api_core import protobuf_helpers  # type: ignore

    cs = client.get_service("CampaignService")
    live = {str(r["campaign.id"]): r for r in gads.query(cid, """
        SELECT campaign.id, campaign.name, campaign.bidding_strategy_type, campaign.bidding_strategy
        FROM campaign WHERE campaign.status != 'REMOVED'""")}

    ops, skipped, applied, large_notes = [], [], [], []
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
        allowed, msg = check_limit(old, new, a.max_step, a.allow_large, a.reason)
        if not allowed:
            skipped.append(f"{r['campaign']}: {msg}")
            continue
        if msg:
            large_notes.append(f"{r['campaign']}: {msg}")
        op = client.get_type("CampaignOperation")
        camp = op.update
        camp.resource_name = cs.campaign_path(cid, cid_)
        parent, leaf = path.split(".")
        setattr(getattr(camp, parent), leaf, int(round(new * 1_000_000)) if leaf.endswith("micros") else new)
        client.copy_from(op.update_mask, protobuf_helpers.field_mask(None, camp._pb))
        ops.append(op)
        applied.append({"campaign_id": cid_, "campaign": r["campaign"], "metric": r["metric"], "from": old, "to": new})
        print(f"  {r['campaign']}: {r['metric']} {old} -> {new}" + (f"  [{msg}]" if msg else ""))

    for s in skipped:
        print(f"  skip {s}")
    if not ops:
        print("No operations to send.")
        return
    try:
        resp = cs.mutate_campaigns(request={"customer_id": cid, "operations": ops,
                                            "partial_failure": True, "validate_only": vo})
    except Exception as e:
        gads.handle_api_error(e)
    pf = getattr(resp, "partial_failure_error", None)
    pf_msg = pf.message if pf and pf.message else ""
    if pf_msg:
        print(f"  partial failures: {pf_msg}")

    if vo:
        if pf_msg:
            gads.die("Validation reported failures; fix or remove those rows, then validate again.")
        gads.record_validation(a.file, len(ops), {"skill": SKILL, "reason": a.reason})
        return

    expected = {f"{x['campaign_id']}:{x['metric']}": (round(x["to"], 2) if x["metric"] == "target_cpa" else round(x["to"], 4))
                for x in applied}
    actual = read_back(cid, {x["campaign_id"] for x in applied})
    bad = gads.verify_readback(expected, actual)
    for b in bad:
        print(f"  NOT LANDED {b}")
    log = gads.log_change(cid, SKILL, {
        "file": str(a.file), "token": a.confirm, "reason": a.reason, "large_changes": large_notes,
        "changes": applied, "skipped": skipped, "partial_failure": pf_msg, "readback_mismatches": bad})
    print(f"Applied {len(ops)} target change(s); {len(ops) - len(bad)} confirmed by read-back. Logged to {log}")


if __name__ == "__main__":
    main()
