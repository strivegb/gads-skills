#!/usr/bin/env bash
# Offline tests for gads-search-terms. Exits non-zero on the first failed assertion.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
S="$HERE/../scripts"
F="$HERE/fixtures"
export GADS_OUT="${GADS_OUT:-$(mktemp -d)}"
export GADS_PROFILE_DIR="${GADS_PROFILE_DIR:-$(mktemp -d)}"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok - $*"; }

python3 -m py_compile "$S/mine_search_terms.py" "$S/apply_negatives.py"

# 1. Full run: history (over-blocking) + daily (trending)
python3 "$S/mine_search_terms.py" --csv "$F/search_terms.csv" --keywords "$F/keywords.csv" \
  --history "$F/history.csv" --daily "$F/daily.csv" --currency USD >/dev/null
D="$(ls -d "$GADS_OUT"/offline/*/ | tail -1)"
D="${D%/}"
[ -s "$D/approvals.md" ] || fail "approvals.md not produced"; ok "approvals.md produced"
[ -s "$D/negatives.csv" ] || fail "negatives.csv not produced"

python3 - "$D" <<'PY' || exit 1
import csv, json, sys
d = sys.argv[1]
neg = list(csv.DictReader(open(f"{d}/negatives.csv")))
summ = json.load(open(f"{d}/gads-search-terms.json"))["summary"]
md = open(f"{d}/gads-search-terms.md").read()
queue = open(f"{d}/approvals.md").read()
def check(cond, msg):
    if not cond:
        print(f"FAIL: {msg}", file=sys.stderr); sys.exit(1)
    print(f"ok - {msg}")

texts = {(r["text"], r["level"]) for r in neg}
held = {h["negative"]: h["blocks"] for h in summ["held_back"]}
# over-blocking: broad 'free' would block 'free payroll software trial' (converted in history)
check(("free", "shared list") not in texts, "broad negative 'free' is not proposed")
check("free payroll software trial" in held.get("free", []), "'free' held back with the converting query it would block")
check("payroll tool for accountants" in held.get('"tool"', []), "phrase '\"tool\"' held back with its converting query")
check(("payroll software pdf", "campaign") not in texts and "[payroll software pdf]" in held, "exact negative held back by history")
check("variant" in {t for t, _ in texts}, "a safer theme negative replaces the held-back one")
check("Held back (would block a query that converted)" in md and "free payroll software trial" in md, "held-back table in report")
check("history.csv" in summ["history_window"] and "2025" in summ["history_window"], "history window reported")
# examples
check(all(r["blocks_example"] for r in neg), "every negative has blocks_example")
check(sum(1 for r in neg if r["spares_example"]) >= 3, "spares_example populated where a boundary exists")
row = next(r for r in neg if r["text"] == "payroll software course")
check(row["blocks_example"] == "payroll software course" and row["spares_example"] == "payroll software",
      "exact negative shows the query it blocks and the converting neighbour it spares")
for r in neg:
    check(r["spares_example"] == "" or r["spares_example"] != r["blocks_example"], f"spare differs from block for {r['keyword']}")
# trending
check("payroll software reddit" in summ["trending"], "trending term detected")
check("payroll calculator" not in summ["trending"], "steady zero-conversion term is not trending")
check("payroll software reddit" in md and "Trending terms watch list" in md, "trending watch list in report")
check("started spending fast" in queue, "material trending spend becomes an approval item")
PY

# 2. No history / no daily: checks degrade gracefully
rm -rf "$GADS_OUT/offline"
python3 "$S/mine_search_terms.py" --csv "$F/search_terms.csv" --keywords "$F/keywords.csv" --currency USD >/dev/null
D="$(ls -d "$GADS_OUT"/offline/*/ | tail -1)"; D="${D%/}"
grep -q "current window only" "$D/gads-search-terms.md" || fail "missing history note when --history is absent"
grep -q "skipped: no date-segmented data" "$D/gads-search-terms.md" || fail "trending not skipped gracefully"
grep -q "started spending fast" "$D/approvals.md" && fail "trending approval without daily data"
ok "runs without --history and --daily"

# 3. Write gate: --execute without a matching validation is refused before any API call
if python3 "$S/apply_negatives.py" -c 1234567890 --file "$D/negatives.csv" --execute --confirm deadbeef 2>"$GADS_OUT/gate.err"; then
  fail "apply_negatives.py wrote without validation"
fi
grep -q "Refusing to write" "$GADS_OUT/gate.err" || fail "write gate message missing"
ok "write gate refuses unvalidated --execute"

echo "all tests passed"
