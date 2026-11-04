#!/usr/bin/env bash
# Offline tests for gads-tracking-audit. Exits non-zero on the first failure.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
S="$here/../scripts"; F="$here/fixtures"
export GADS_OUT="${GADS_OUT:-$(mktemp -d)}"
export GADS_PROFILE_DIR="${GADS_PROFILE_DIR:-$(mktemp -d)}"
fail() { echo "FAIL: $*" >&2; exit 1; }
has() { grep -qF -- "$2" "$1" || fail "expected '$2' in $(basename "$1")"; }
hasnt() { if grep -qF -- "$2" "$1"; then fail "did not expect '$2' in $(basename "$1")"; fi; }

# 1. Conversion audit, fixed dates: data ends 2026-11-02, today 2026-11-03, default lag 3.
python3 "$S/audit_conversions.py" --csv "$F/conversion_actions.csv" --daily "$F/conversions_by_action_day.csv" \
  --currency USD --today 2026-11-03 --json > "$GADS_OUT/audit.out"
d=$(dirname "$(find "$GADS_OUT" -name gads-tracking-audit.json | head -1)")
[ -n "$d" ] || fail "no gads-tracking-audit.json written"
J="$d/gads-tracking-audit.json"; M="$d/gads-tracking-audit.md"
python3 - "$J" <<'PY' || fail "approval checks"
import json, sys
a = json.load(open(sys.argv[1]))["approvals"]
silent = [x for x in a if x["kind"] == "silent_action"]
names = {x["entity"] for x in silent}
assert "Lead form submit" in names, f"high-volume silent action not flagged: {names}"
assert not any("Demo booked" in n for n in names), "low-volume action flagged on noise"
ev = next(x for x in silent if x["entity"] == "Lead form submit")["evidence"]
assert ev["p_zero"] < 0.01 and ev["lag_days"] == 3 and ev["excluded_days"] == 3, ev
assert ev["window"] == "2026-10-24 to 2026-10-30", ev["window"]
assert any(x["kind"] == "duplicate_conversion" for x in a), "duplicate pair missing"
PY
has "$M" "Lag (days)"
has "$M" "default 3 days"
has "$M" "Not flagged: 'Demo booked (calendar)'"
has "$M" "chance of zero at that rate"
[ -s "$d/approvals.md" ] || fail "approvals.md missing"
has "$d/approvals.md" "Investigate 'Lead form submit'"
hasnt "$d/approvals.md" "Investigate 'Demo booked"

# 2. --lag-days override is used and reported
python3 "$S/audit_conversions.py" --csv "$F/conversion_actions.csv" --daily "$F/conversions_by_action_day.csv" \
  --currency USD --today 2026-11-03 --lag-days 0 > /dev/null
has "$M" "set by --lag-days"
python3 - "$J" <<'PY' || fail "lag override"
import json, sys
s = json.load(open(sys.argv[1]))
ev = next(x for x in s["approvals"] if x["kind"] == "silent_action")["evidence"]
assert ev["window"] == "2026-10-27 to 2026-11-02" and ev["excluded_days"] == 0, ev
PY

# 3. Site tags: double gtag load and wrong account tag from the static fixture
python3 "$S/check_site_tags.py" --url "$F/dup_tags.html" --expect "$F/expected_tags.json" > /dev/null
T="$d/gads-tracking-tags.json"
[ -f "$T" ] || fail "gads-tracking-tags.json missing"
python3 - "$T" <<'PY' || fail "site tag checks"
import json, sys
kinds = {x["kind"] for x in json.load(open(sys.argv[1]))["approvals"]}
assert "duplicate_tag_load" in kinds, kinds
assert "wrong_account_tag" in kinds, kinds
PY
has "$d/approvals.md" "gads-tracking-tags"
echo "PASS gads-tracking-audit ($d)"
