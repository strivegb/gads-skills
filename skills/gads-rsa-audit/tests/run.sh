#!/usr/bin/env bash
# Offline tests for gads-rsa-audit. No internet: landing pages come from a local http.server.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
S="$HERE/../scripts"
F="$HERE/fixtures"
export GADS_OUT="${GADS_OUT:-$(mktemp -d)}"
export GADS_PROFILE_DIR="${GADS_PROFILE_DIR:-$(mktemp -d)}"
TMP="$(mktemp -d)"
SERVER_PID=""
cleanup() {
  if [ -n "$SERVER_PID" ]; then kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true; fi
  rm -rf "$TMP"
}
trap cleanup EXIT

fails=0
ok()   { echo "ok   $1"; }
bad()  { echo "FAIL $1"; fails=$((fails + 1)); }
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
has()  { grep -qF -- "$2" "$1"; }
py()   { python3 -c "$1"; }
free_port() { python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1])"; }

# ---- local landing page server
PORT="$(free_port)"
python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$F/site" >/dev/null 2>&1 &
SERVER_PID=$!
BASE="http://127.0.0.1:$PORT"
for _ in $(seq 1 50); do
  python3 -c "import urllib.request; urllib.request.urlopen('$BASE/index.html', timeout=1)" 2>/dev/null && break
  sleep 0.1
done
sed "s#__BASE__#$BASE#g" "$F/ads_checks.csv" > "$TMP/ads_checks.csv"

# ---- 1. original fixture: audit + copy validation, drafts exported Paused
export GADS_OUT_ROOT="$GADS_OUT"
GADS_OUT="$GADS_OUT_ROOT/base" python3 "$S/audit_rsas.py" --csv "$F/ads.csv" --assets "$F/assets.csv" \
  --keywords "$F/keywords.csv" --currency USD > "$TMP/base.log"
BASE_DIR="$(dirname "$(find "$GADS_OUT_ROOT/base" -name rsa_brief.json | head -1)")"
check "base: approvals.md exists" test -s "$BASE_DIR/approvals.md"
check "base: Poor ad flagged" has "$BASE_DIR/gads-rsa-audit.md" "Payroll Core"
python3 "$S/validate_copy.py" --drafts "$F/drafts_good.csv" --brief "$BASE_DIR/rsa_brief.json" --out "$TMP/rsa_drafts.csv" > "$TMP/val.log"
check "drafts: good drafts pass" test -s "$TMP/rsa_drafts.csv"
check "drafts: every row Status Paused" py "
import csv; rows = list(csv.DictReader(open('$TMP/rsa_drafts.csv')))
assert rows and all(r['Status'] == 'Paused' for r in rows), [r['Status'] for r in rows]"
if python3 "$S/validate_copy.py" --drafts "$F/drafts_bad.csv" --brief "$BASE_DIR/rsa_brief.json" --out "$TMP/bad.csv" > "$TMP/bad.log"; then
  bad "drafts: bad drafts rejected"; else ok "drafts: bad drafts rejected"; fi

# ---- 2. new checks: links, policy, coverage, ad tests
GADS_OUT="$GADS_OUT_ROOT/checks" python3 "$S/audit_rsas.py" --csv "$TMP/ads_checks.csv" --adgroups "$F/adgroups.csv" \
  --keywords "$F/keywords_checks.csv" --check-links --currency USD > "$TMP/checks.log"
D="$(dirname "$(find "$GADS_OUT_ROOT/checks" -name rsa_brief.json | head -1)")"
J="$D/gads-rsa-audit.json"
check "checks: approvals.md exists" test -s "$D/approvals.md"
item() {  # item <kind> <severity> <text in action>
  python3 - "$J" "$@" <<'PY'
import json, sys
path, kind, sev, text = sys.argv[1:5]
a = json.load(open(path))["approvals"]
assert any(x["kind"] == kind and x["severity"] == sev and text in x["action"] for x in a), [(x["kind"], x["severity"], x["action"]) for x in a]
PY
}
check "links: 404 page with spend is critical"   item rsa_link critical "/gone.html (HTTP 404)"
check "links: soft 404 page is critical"          item rsa_link critical "/discontinued.html"
check "links: healthy pages not flagged"          py "
import csv; r = {x['url']: x for x in csv.DictReader(open('$D/rsa_links.csv'))}
assert r['$BASE/payroll.html']['problem'] == '' and r['$BASE/hr/']['problem'] == ''"
check "links: redirect chain recorded (301)"      py "
import csv; r = {x['url']: x for x in csv.DictReader(open('$D/rsa_links.csv'))}
assert r['$BASE/hr']['chain'].startswith('301 ') and r['$BASE/hr']['problem'] == ''"
check "links: ValueTrack replaced before fetch"   py "
import csv; r = [x for x in csv.DictReader(open('$D/rsa_links.csv')) if '{keyword}' in x['url']][0]
assert r['fetched_url'] == '$BASE/payroll.html?kw=test&mt=e' and r['status'] == '200', r"
check "links: no email in User-Agent"             py "
import sys; sys.path.insert(0, '$S'); import check_links as c
assert c.UA == 'gads-skills-link-checker/1.0' and '@' not in c.UA"
check "policy: disapproved ad is critical"        item rsa_policy critical "RSA 2004 in 'HR Software' (Trademarks in ad text)"
check "policy: limited ad is high"                item rsa_policy high "limited RSA 2005"
check "coverage: ad group with no RSA"            item rsa_coverage high "'HR Onboarding'"
check "coverage: ad group with only disapproved"  item rsa_coverage high "'Brand Core'"
check "coverage: single-RSA ad group listed"      has "$D/rsa_coverage.csv" "Payroll Small Biz,Payroll Small Biz,one_rsa"
check "coverage: paused and Display groups skipped" py "
t = open('$D/rsa_coverage.csv').read(); assert 'Old Group' not in t and 'All Visitors' not in t"
check "tests: significant loser gets pause item"  item rsa_test medium "Pause losing RSA 2002 in 'Payroll Core'"
check "tests: low volume says not enough data"    py "
import csv; r = [x for x in csv.DictReader(open('$D/rsa_tests.csv')) if x['ad_group'] == 'HR Payroll']
assert r and all(x['verdict'] == 'not enough data' for x in r)"
check "tests: no pause item without significance" py "
import json; a = json.load(open('$J'))['approvals']
assert not any(x['kind'] == 'rsa_test' and 'HR Payroll' in x['action'] for x in a)"
check "tests: winner below min conversions is held" bash -c "
GADS_OUT='$GADS_OUT_ROOT/held' python3 '$S/audit_rsas.py' --csv '$TMP/ads_checks.csv' --min-winner-conv 500 --currency USD >/dev/null
! grep -q '\"kind\": \"rsa_test\"' \$(find '$GADS_OUT_ROOT/held' -name gads-rsa-audit.json)"

# ---- 3. coverage fallbacks
GADS_OUT="$GADS_OUT_ROOT/kw" python3 "$S/audit_rsas.py" --csv "$TMP/ads_checks.csv" --keywords "$F/keywords_checks.csv" --currency USD > /dev/null
check "coverage: keywords export fallback finds no-RSA group" has "$(find "$GADS_OUT_ROOT/kw" -name rsa_coverage.csv)" "HR Onboarding"
check "coverage: keywords fallback skips paused group" py "assert 'Old Group' not in open('$(find "$GADS_OUT_ROOT/kw" -name rsa_coverage.csv)').read()"
GADS_OUT="$GADS_OUT_ROOT/none" python3 "$S/audit_rsas.py" --csv "$TMP/ads_checks.csv" --currency USD > /dev/null
check "coverage: skipped gracefully without ad group list" has "$(find "$GADS_OUT_ROOT/none" -name gads-rsa-audit.md)" "were not checked"

# ---- 4. standalone link checker: file://, unreachable host, redirect rules
CLOSED="$(free_port)"
python3 "$S/check_links.py" --urls "file://$F/site/payroll.html" "file://$F/site/missing.html" \
  "http://127.0.0.1:$CLOSED/x" "$BASE/discontinued.html" --out "$TMP/links.csv" --timeout 3 > "$TMP/links.log"
check "standalone: file:// page ok"        py "
import csv; r = {x['url']: x for x in csv.DictReader(open('$TMP/links.csv'))}
assert r['file://$F/site/payroll.html']['problem'] == ''"
check "standalone: missing file is broken" py "
import csv; r = {x['url']: x for x in csv.DictReader(open('$TMP/links.csv'))}
assert r['file://$F/site/missing.html']['problem'] == 'broken'"
check "standalone: refused connection is unreachable" py "
import csv; r = {x['url']: x for x in csv.DictReader(open('$TMP/links.csv'))}
assert r['http://127.0.0.1:$CLOSED/x']['problem'] == 'unreachable'"
check "standalone: --csv reads final URLs" bash -c "python3 '$S/check_links.py' --csv '$TMP/ads_checks.csv' --out '$TMP/l2.csv' >/dev/null && grep -q gone.html '$TMP/l2.csv'"
check "rules: off-domain and homepage redirects" py "
import sys; sys.path.insert(0, '$S'); import check_links as c
r = lambda u, f: {'status': 200, 'final_url': f, 'chain': [(301, u), (200, f)], 'body': 'ok', 'error': ''}
assert c.assess('https://acme.com/a', r('https://acme.com/a', 'https://other.com/a'))[0] == 'off_domain'
assert c.assess('https://acme.com/a', r('https://acme.com/a', 'https://shop.acme.com/a'))[0] == ''
assert c.assess('https://acme.com/p/x', r('https://acme.com/p/x', 'https://acme.com/'))[0] == 'homepage'
assert c.assess('https://acme.co.uk/a', r('https://acme.co.uk/a', 'https://www.acme.co.uk/b'))[0] == ''
assert c.clean_url('https://a.com/?k={keyword}&d={ifnotmobile:desk}{ifmobile:mob}&x={_custom}') == 'https://a.com/?k=test&d=desk&x='"
check "rules: z-test" py "
import sys; sys.path.insert(0, '$S'); from audit_rsas import ztest
z, p = ztest(80, 20000, 30, 20000); assert z > 4 and p < 0.001
z, p = ztest(10, 1000, 9, 1000); assert p > 0.5"
check "rules: 403/429 are blocked, 404 is broken" py "
import sys; sys.path.insert(0, '$S'); import check_links as c
assert c.assess('https://a.com/x', {'status': 403, 'chain': [], 'body': ''})[0] == 'blocked'
assert c.assess('https://a.com/x', {'status': 429, 'chain': [], 'body': ''})[0] == 'blocked'
assert c.assess('https://a.com/x', {'status': 404, 'chain': [], 'body': ''})[0] == 'broken'"

echo
if [ "$fails" -gt 0 ]; then echo "$fails check(s) failed"; exit 1; fi
echo "all checks passed"
