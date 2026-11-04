#!/usr/bin/env bash
# Offline tests for gads-performance-report. Exits non-zero on the first failure.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
S="$here/../scripts"; F="$here/fixtures"
export GADS_OUT="${GADS_OUT:-$(mktemp -d)}"
export GADS_PROFILE_DIR="${GADS_PROFILE_DIR:-$(mktemp -d)}"
fail() { echo "FAIL: $*" >&2; exit 1; }
has() { grep -qF -- "$2" "$1" || fail "expected '$2' in $(basename "$1")"; }
hasnt() { if grep -qF -- "$2" "$1"; then fail "did not expect '$2' in $(basename "$1")"; fi; }
rm -f "$GADS_PROFILE_DIR/offline.json"

# 1. Nine weeks of daily data ending Mon 2026-11-02, today 2026-11-03, default lag 3, change history export.
python3 "$S/build_report.py" --csv "$F/campaigns_daily_9w.csv" --changes "$F/change_history.csv" \
  --currency USD --today 2026-11-03 > /dev/null
d=$(dirname "$(find "$GADS_OUT" -name gads-performance-report.json | head -1)")
[ -n "$d" ] || fail "no gads-performance-report.json written"
J="$d/gads-performance-report.json"; M="$d/gads-performance-report.md"; Q="$d/approvals.md"
python3 - "$J" <<'PY' || fail "json checks"
import json, sys
p = json.load(open(sys.argv[1])); s = p["summary"]; a = p["approvals"]
an = s["anomalies"]
# weekday anomaly on the last complete day (spend spike) and a conversion drop on a complete day
assert any(x["metric"] == "cost" and x["end"] == "2026-11-02" and x["window_days"] == 1 and x["z"] >= 2 for x in an), an
assert any(x["metric"] == "conv" and x["end"] == "2026-10-30" and x["window_days"] == 1 and x["z"] <= -2 for x in an), an
# conversion metrics never judged on days inside the lag window (Oct 31 to Nov 2 are low on purpose)
assert not any(x["metric"] in ("conv", "cpa", "value") and x["end"] > "2026-10-30" for x in an), an
# zero impressions: enabled campaign flagged as critical, paused campaign ignored
assert s["zero_impressions"] == ["Display - Remarketing"], s["zero_impressions"]
z = [x for x in a if x["kind"] == "zero_impressions"]
assert len(z) == 1 and z[0]["severity"] == "critical", z
assert "paused" in z[0]["why"].lower(), z[0]["why"]
# change history attached to the mover in the window; the Oct 10 budget change is outside it
pay = next(m for m in s["movers"] if m["campaign"] == "Search - Payroll")
assert any(c["what"] == "target CPA raised from $60 to $90" and c["date"] == "2026-10-28" for c in pay["changes"]), pay["changes"]
assert "target CPA raised from $60 to $90 on Oct 28 by a user" in pay["explain"], pay["explain"]
assert not any("Oct 10" in pay["explain"] or "budget" in c["what"] for c in pay["changes"]), pay
assert s["lag_days"] == 3 and s["lag_days_in_period"] == 3, s
assert any(x["kind"] == "anomaly" for x in a), "no anomaly approvals"
PY
has "$M" "## Stopped serving"
has "$M" "## Same-weekday check"
has "$M" "still collecting conversions"
has "$M" "On complete days only"
has "$M" "by auto-applied recommendations"
[ -s "$Q" ] || fail "approvals.md missing"
has "$Q" "Check why 'Display - Remarketing' stopped serving"
has "$Q" "critical"
hasnt "$Q" "Search - Old Promo' stopped"
# no user emails anywhere in the output
if grep -rqF "acme-example.com" "$d"; then fail "user email leaked into the report"; fi

# 2. Profile can allow names in change annotations
echo '{"show_change_users": true}' > "$GADS_PROFILE_DIR/offline.json"
python3 "$S/build_report.py" --csv "$F/campaigns_daily_9w.csv" --changes "$F/change_history.csv" \
  --currency USD --today 2026-11-03 > /dev/null
has "$M" "by maria@acme-example.com"
rm -f "$GADS_PROFILE_DIR/offline.json"

# 3. Two exports without a Day column: lag is stated, daily checks are skipped, nothing crashes
python3 "$S/build_report.py" --csv "$F/cur_titled.csv" "$F/prev_titled.csv" --currency USD > /dev/null
has "$M" "still collecting conversions"
has "$M" "need an export segmented by Day"

# 4. Short daily export (14 days): runs, says the weekday check needs more history
python3 "$S/build_report.py" --csv "$F/campaigns_by_day.csv" --currency USD --today 2026-11-03 > /dev/null
has "$M" "Same-weekday check skipped"
[ -s "$d/report.html" ] || fail "report.html missing"
echo "PASS gads-performance-report ($d)"
