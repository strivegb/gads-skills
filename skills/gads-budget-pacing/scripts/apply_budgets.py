#!/usr/bin/env python3
"""
apply_budgets.py - write APPROVED daily budget changes from budget_changes.csv.

Three steps (shared write gate in gads.py):
  1. Validate: the API checks every operation with validate_only and nothing changes.
     On success it prints a token tied to this exact file.
  2. Execute: --execute --confirm <token>, only after the user said yes to these rows.
     Editing the file (for example removing rejected rows) means validating again.
  3. Read back: the script re-reads the budgets, prints any change that did not land,
     and appends the write to gads-out/<cid>/changes.log.jsonl.

  $PY apply_budgets.py -c 1234567890 --file gads-out/<cid>/<date>/budget_changes.csv
  $PY apply_budgets.py -c 1234567890 --file .../budget_changes.csv --execute --confirm <token>

Safety:
  * Re-reads each budget's live amount and refuses rows where it no longer matches
    current_daily (someone changed it since the analysis). --force overrides.
  * Refuses any single change above 20%. Larger moves need --allow-large and
    --reason "<why>"; the reason is printed and logged. --max-change can only lower the limit.
  * Shared budgets are changed once at the budget level; every campaign on them is affected.
Delete rows the user rejected before validating.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gads  # noqa: E402

SKILL = "gads-budget-pacing"
DEFAULT_LIMIT = 0.20


def check_limit(cur: float, new: float, limit: float = DEFAULT_LIMIT, allow_large: bool = False,
                reason: str = "") -> tuple[bool, str]:
    """Pure check of one budget move. Returns (allowed, message)."""
    if cur <= 0:
        return False, "current budget is zero or missing; set it in the UI"
    if new <= 0:
        return False, "new budget must be above zero; pause the campaign in the UI instead"
    move = abs(new / cur - 1)
    if move <= limit + 1e-9:
        return True, ""
    if allow_large and reason.strip():
        return True, f"large change {move:.0%} allowed: {reason.strip()}"
    return False, f"{cur:.2f} -> {new:.2f} is a {move:.0%} change, above the {limit:.0%} limit. Pass --allow-large --reason \"<why>\" if approved."


def check_args(a) -> str | None:
    """Argument rules that need no API. Returns an error message or None."""
    if a.max_change > DEFAULT_LIMIT + 1e-9:
        return f"--max-change cannot exceed {DEFAULT_LIMIT:.0%}. Use --allow-large --reason for bigger moves."
    if a.allow_large and not a.reason.strip():
        return "--allow-large needs --reason \"<why>\" (it is printed and logged)."
    return None


def to_micros(x: float) -> int:
    return int(round(float(x) * 1_000_000 / 10_000) * 10_000)  # whole cents


def build_ops(client, rows):
    """CampaignBudgetOperation updates with a field mask on amount_micros only."""
    from google.api_core import protobuf_helpers  # type: ignore

    ops = []
    for r in rows:
        op = client.get_type("CampaignBudgetOperation")
        b = op.update
        b.resource_name = r["budget_resource_name"]
        b.amount_micros = to_micros(r["new_daily"])
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
    rn_amt = {v[0]: v[1] for v in live.values()}
    for r in rows:
        if not r.get("budget_resource_name"):
            first = r["campaigns"].split(";")[0].strip()
            key = live.get(first) or live.get("budget:" + r["budget_name"])
            if key:
                r["budget_resource_name"] = key[0]
        r["live_daily"] = rn_amt.get(r.get("budget_resource_name"))
    return rows


def read_back(cid, resource_names) -> dict:
    names = ", ".join(gads.gaql_str(rn) for rn in resource_names)
    rows = gads.query(cid, f"""
        SELECT campaign_budget.resource_name, campaign_budget.amount_micros
        FROM campaign_budget WHERE campaign_budget.resource_name IN ({names})""")
    return {r["campaign_budget.resource_name"]: round(gads.micros(r.get("campaign_budget.amount_micros")), 2) for r in rows}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--customer", "-c", required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--max-change", type=float, default=DEFAULT_LIMIT, help="Per-change limit (default and maximum 0.20).")
    ap.add_argument("--allow-large", action="store_true", help="Allow moves above the limit. Needs --reason.")
    ap.add_argument("--reason", default="", help="Why a large move was approved. Printed and logged.")
    ap.add_argument("--force", action="store_true", help="Apply even if the live budget differs from current_daily.")
    gads.add_write_args(ap)
    a = ap.parse_args()

    err = check_args(a)
    if err:
        gads.die(err)
    vo = gads.check_execute(a, a.file)  # dies before any API call if --execute lacks a valid token
    if a.reason:
        print(f"Reason given: {a.reason}")

    cid = gads.clean_cid(a.customer)
    rows = [r for r in csv.DictReader(open(a.file)) if r.get("new_daily")]
    if not rows:
        gads.die("No rows to apply.")
    rows = resolve(cid, rows)
    ok, large_notes = [], []
    for r in rows:
        cur, new = float(r["current_daily"]), float(r["new_daily"])
        if not r.get("budget_resource_name"):
            print(f"  SKIP {r['budget_name']}: budget not found in account")
            continue
        if r["live_daily"] is not None and abs(r["live_daily"] - cur) > max(0.01 * cur, 0.5) and not a.force:
            print(f"  SKIP {r['budget_name']}: live budget is {r['live_daily']:.2f}, analysis assumed {cur:.2f}. Re-run pacing or pass --force.")
            continue
        allowed, msg = check_limit(cur, new, a.max_change, a.allow_large, a.reason)
        if not allowed:
            print(f"  SKIP {r['budget_name']}: {msg}")
            continue
        if msg:
            large_notes.append(f"{r['budget_name']}: {msg}")
        shared = " (shared: " + r["campaigns"] + ")" if str(r.get("shared")).lower() == "true" else ""
        print(f"  {r['budget_name']}{shared}: {cur:.2f} -> {new:.2f}/day" + (f"  [{msg}]" if msg else ""))
        ok.append(r)
    if not ok:
        gads.die("Nothing to apply after safety checks.")

    client = gads.get_client()
    ops = build_ops(client, ok)
    req = {"customer_id": cid, "operations": ops, "partial_failure": True, "validate_only": vo}
    try:
        resp = client.get_service("CampaignBudgetService").mutate_campaign_budgets(request=req)
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

    expected = {r["budget_resource_name"]: round(to_micros(r["new_daily"]) / 1_000_000, 2) for r in ok}
    actual = read_back(cid, list(expected))
    bad = gads.verify_readback(expected, actual)
    for b in bad:
        print(f"  NOT LANDED {b}")
    log = gads.log_change(cid, SKILL, {
        "file": str(a.file), "token": a.confirm, "reason": a.reason, "large_changes": large_notes,
        "changes": [{"budget": r["budget_name"], "resource_name": r["budget_resource_name"], "campaigns": r["campaigns"],
                     "from": float(r["current_daily"]), "to": float(r["new_daily"])} for r in ok],
        "partial_failure": pf_msg, "readback_mismatches": bad})
    print(f"Applied {len(ops)} budget change(s); {len(ops) - len(bad)} confirmed by read-back. Logged to {log}")


if __name__ == "__main__":
    main()
