#!/usr/bin/env bash
# Offline tests for gads-bid-audit. No API access needed.
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
cp "$F/profile.json" "$GADS_PROFILE_DIR/offline.json"
run() { "$PY" "$S/audit_bids.py" --csv "$F/campaigns_30d.csv" --recent "$F/campaigns_7d.csv" --prior "$F/campaigns_prior.csv" \
          --keywords "$F/keywords.csv" --currency USD --days 30 "$@" >/dev/null 2>&1; }

echo "1. Search Partners check with --network"
run --network "$F/campaigns_by_network.csv"
D="$(ls -d "$GADS_OUT"/offline/*/ | tail -1)"
Q="$D/approvals.md"
[ -s "$Q" ] && ok "approvals.md written" || bad "approvals.md missing"
has "$Q" "Turn off Search Partners on 'Search - Payroll Software'" "partners flagged where CPA is 2.2x Google Search"
has "$Q" "CPA \$137 on 400 clicks vs \$61.02 on Google Search" "why text carries partners vs search CPA"
hasnt "$Q" "Search Partners on 'Search - HR Software'" "partners at 1.2x CPA not flagged"
hasnt "$Q" "Search Partners on 'Brand - Acme'" "partners with 5 clicks not flagged"
grep -q '"field": "campaign.network_settings.target_search_network"' "$D/gads-bid-audit.json" && ok "change payload names target_search_network" || bad "change payload"
has "$Q" "Switch 'Brand - Acme' to Target Impression Share" "existing brand overbid check still fires"

echo "2. without --network the check is skipped with a note"
rm -rf "$GADS_OUT/offline"
run
D="$(ls -d "$GADS_OUT"/offline/*/ | tail -1)"
has "$D/gads-bid-audit.md" "Search Partners check skipped" "skip note present"
hasnt "$D/approvals.md" "Turn off Search Partners" "no partners item without network data"

echo "3. bid simulator preview (pure function, live data shape)"
"$PY" - "$S" >"$GADS_OUT/sim.out" 2>&1 <<'PYEOF' && ok "simulator preview interpolates current vs proposed" || { bad "simulator preview"; cat "$GADS_OUT/sim.out"; }
import sys
sys.path.insert(0, sys.argv[1])
import audit_bids as a
pts = [{"target_cpa_micros": "40000000", "cost_micros": "8000000000", "biddable_conversions": 190.0},
       {"target_cpa_micros": "50000000", "cost_micros": "9800000000", "biddable_conversions": 220.0}]
pv = a.sim_preview({"points": pts, "start": "2026-10-27", "end": "2026-11-02"}, "TARGET_CPA", 40, 46)
assert round(pv["proposed"]["cost"]) == 9080 and round(pv["proposed"]["conv"]) == 208, pv
assert pv["current"]["conv"] == 190
t = a.sim_text(pv, "TARGET_CPA", "USD")
assert "Google's simulator estimates" in t and "$9,080 cost, 208 conversions" in t, t
assert "simulator" in a.sim_evidence(pv)
roas = [{"target_roas": 3.0, "cost_micros": "6000000000", "biddable_conversions": 120, "biddable_conversions_value": 21000},
        {"target_roas": 4.0, "cost_micros": "4000000000", "biddable_conversions": 90, "biddable_conversions_value": 16000}]
pv = a.sim_preview({"points": roas}, "TARGET_ROAS", 4.0, 3.4)
assert round(pv["proposed"]["cost"]) == 5200, pv
# one point that does not reach the proposed target says nothing
assert a.sim_preview({"points": roas[:1]}, "TARGET_ROAS", 4.0, 3.4) is None
assert a.sim_preview(None, "TARGET_CPA", 40, 46) is None
PYEOF

echo "4. apply_targets.py write gate and limits (no API)"
PLAN="$GADS_OUT/plan/bid_changes.csv"; mkdir -p "$(dirname "$PLAN")"
printf 'campaign_id,campaign,strategy,metric,current,proposed,held\n111,C,TARGET_CPA,target_cpa,40,46,no\n' > "$PLAN"
if out=$("$PY" "$S/apply_targets.py" -c 1234567890 --file "$PLAN" --execute --confirm abc 2>&1); then bad "--execute without validation was accepted"
else echo "$out" | grep -q "Refusing to write" && ok "--execute refused without validation token" || bad "wrong refusal: $out"; fi
if out=$("$PY" "$S/apply_targets.py" -c 1234567890 --file "$PLAN" --allow-large 2>&1); then bad "--allow-large without --reason accepted"
else echo "$out" | grep -q "needs --reason" && ok "--allow-large needs --reason" || bad "wrong error: $out"; fi
if "$PY" "$S/apply_targets.py" -c 1234567890 --file "$PLAN" --max-step 0.5 >/dev/null 2>&1; then bad "--max-step 0.5 accepted"
else ok "--max-step cannot raise the limit"; fi
"$PY" - "$S" "$PLAN" >/dev/null 2>"$GADS_OUT/py.err" <<'PYEOF' && ok "check_limit and check_execute logic" || { bad "check_limit / check_execute logic"; cat "$GADS_OUT/py.err"; }
import sys, types
sys.path.insert(0, sys.argv[1])
import apply_targets as at, gads
plan = sys.argv[2]
assert at.check_limit(40, 48)[0] is True
assert at.check_limit(40, 48.5)[0] is False
assert at.check_limit(4.0, 3.0)[0] is False
assert at.check_limit(0, 45)[0] is True            # adding a target is not a % move
assert at.check_limit(40, 60, allow_large=True, reason="")[0] is False
ok, msg = at.check_limit(40, 60, allow_large=True, reason="margin approved by CFO")
assert ok and "margin approved by CFO" in msg
def refused(args):
    try:
        gads.check_execute(args, plan)
    except SystemExit:
        return True
    return False
A = lambda **k: types.SimpleNamespace(execute=k.get("execute", False), confirm=k.get("confirm", ""))
assert gads.check_execute(A(), plan) is True
assert refused(A(execute=True, confirm="x"))
tok = gads.record_validation(plan, 1)
assert refused(A(execute=True, confirm="wrong"))
assert gads.check_execute(A(execute=True, confirm=tok), plan) is False
open(plan, "a").write("222,D,TARGET_CPA,target_cpa,50,55,no\n")
assert refused(A(execute=True, confirm=tok))
PYEOF

[ $fail -eq 0 ] && echo "PASS gads-bid-audit" || echo "FAIL gads-bid-audit"
exit $fail
