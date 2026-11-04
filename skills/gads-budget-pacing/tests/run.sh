#!/usr/bin/env bash
# Offline tests for gads-budget-pacing. No API access needed.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
S="$HERE/../scripts"
F="$HERE/fixtures"
export GADS_OUT="${GADS_OUT:-$(mktemp -d)}"
export GADS_PROFILE_DIR="${GADS_PROFILE_DIR:-$(mktemp -d)}"
PY="${PYTHON:-python3}"
fail=0
ok()   { echo "  ok   $1"; }
bad()  { echo "  FAIL $1"; fail=1; }
has()  { if grep -qF -- "$2" "$1"; then ok "$3"; else bad "$3 (missing: $2)"; fi; }
hasnt(){ if grep -qF -- "$2" "$1"; then bad "$3 (unexpected: $2)"; else ok "$3"; fi; }

echo "1. overdelivery + pause at cap"
cp "$F/profile_overdelivery.json" "$GADS_PROFILE_DIR/offline.json"
"$PY" "$S/pace_budgets.py" --csv "$F/overdelivery_by_day.csv" --currency USD --today 2026-11-20 >/dev/null
D="$(ls -d "$GADS_OUT"/offline/*/ | tail -1)"
MD="$D/gads-budget-pacing.md"; Q="$D/approvals.md"
[ -s "$Q" ] && ok "approvals.md written" || bad "approvals.md missing"
has "$Q" "Review overdelivery on 'Search - Demo Requests'" "overdelivery flagged for repeat overdeliverer"
has "$Q" "heading past Google's \$6,080 monthly ceiling" "30.4x ceiling projection"
hasnt "$Q" "overdelivery on 'Search - Brand'" "no overdelivery flag for on-budget campaign"
has "$Q" "Pause 'Search - Payroll' on Nov 24, when it reaches its \$7,000 Nov plan" "pause-at-cap for campaign plan"
has "$Q" "Pause the account's weakest campaigns on Nov 23" "pause-at-cap for account monthly budget"
has "$Q" "nothing pauses automatically" "pause is a proposal only"
grep -q ',yes' "$D/budget_changes.csv" && ok "budget_changes.csv marks >20% rows large=yes" || bad "large flag missing"

echo "2. no Budget column: overdelivery skipped with a note"
rm -rf "$GADS_OUT/offline"
"$PY" "$S/pace_budgets.py" --csv "$F/no_budget_column.csv" --currency USD --today 2026-11-20 >/dev/null 2>&1
D="$(ls -d "$GADS_OUT"/offline/*/ | tail -1)"
has "$D/gads-budget-pacing.md" "Overdelivery check skipped" "skip note present"
hasnt "$D/approvals.md" "Review overdelivery" "no overdelivery item without budgets"
[ -s "$D/approvals.md" ] && ok "approvals.md written" || bad "approvals.md missing"

echo "3. original fixture still runs"
rm -rf "$GADS_OUT/offline" "$GADS_PROFILE_DIR/offline.json"
"$PY" "$S/pace_budgets.py" --csv "$F/campaign_by_day.csv" --currency USD --today 2026-11-12 >/dev/null
D="$(ls -d "$GADS_OUT"/offline/*/ | tail -1)"
has "$D/gads-budget-pacing.md" "No budget overdelivered repeatedly" "normal 1x spend not flagged"

echo "4. apply_budgets.py write gate and limits (no API)"
PLAN="$GADS_OUT/plan/budget_changes.csv"; mkdir -p "$(dirname "$PLAN")"
printf 'budget_id,budget_resource_name,budget_name,shared,campaigns,current_daily,new_daily,why\n1,customers/1/campaignBudgets/1,B,False,C,100,110,test\n' > "$PLAN"
if out=$("$PY" "$S/apply_budgets.py" -c 1234567890 --file "$PLAN" --execute --confirm abc 2>&1); then bad "--execute without validation was accepted"
else echo "$out" | grep -q "Refusing to write" && ok "--execute refused without validation token" || bad "wrong refusal: $out"; fi
if out=$("$PY" "$S/apply_budgets.py" -c 1234567890 --file "$PLAN" --allow-large 2>&1); then bad "--allow-large without --reason accepted"
else echo "$out" | grep -q "needs --reason" && ok "--allow-large needs --reason" || bad "wrong error: $out"; fi
if out=$("$PY" "$S/apply_budgets.py" -c 1234567890 --file "$PLAN" --max-change 0.5 2>&1); then bad "--max-change 0.5 accepted"
else ok "--max-change cannot raise the limit"; fi
"$PY" - "$S" "$PLAN" >/dev/null 2>"$GADS_OUT/py.err" <<'PYEOF' && ok "check_limit and check_execute logic" || { bad "check_limit / check_execute logic"; cat "$GADS_OUT/py.err"; }
import sys, types
sys.path.insert(0, sys.argv[1])
import apply_budgets as ab, gads
plan = sys.argv[2]
assert ab.check_limit(100, 120)[0] is True
assert ab.check_limit(100, 121)[0] is False
assert ab.check_limit(100, 79)[0] is False
assert ab.check_limit(100, 150, allow_large=True, reason="")[0] is False
ok, msg = ab.check_limit(100, 150, allow_large=True, reason="client approved launch week")
assert ok and "client approved launch week" in msg
def refused(args):
    try:
        gads.check_execute(args, plan)
    except SystemExit:
        return True
    return False
A = lambda **k: types.SimpleNamespace(execute=k.get("execute", False), confirm=k.get("confirm", ""))
assert gads.check_execute(A(), plan) is True                      # no --execute = validate only
assert refused(A(execute=True, confirm="x"))                      # no validation recorded
tok = gads.record_validation(plan, 1)
assert refused(A(execute=True, confirm="wrong"))                  # wrong token
assert gads.check_execute(A(execute=True, confirm=tok), plan) is False
open(plan, "a").write("2,customers/1/campaignBudgets/2,B2,False,C2,100,90,edit\n")
assert refused(A(execute=True, confirm=tok))                      # file edited after validation
PYEOF

[ $fail -eq 0 ] && echo "PASS gads-budget-pacing" || echo "FAIL gads-budget-pacing"
exit $fail
