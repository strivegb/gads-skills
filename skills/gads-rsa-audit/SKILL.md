---
name: gads-rsa-audit
description: |
  Google Ads ads bot. Audits every enabled responsive search ad (ad strength, headline and description count, pinning, near-duplicates, keyword coverage, Low-rated assets, editorial issues, disapproved and limited ads, ad groups with no RSA or only one), checks every final URL for broken pages and bad redirects, picks ad test winners with a significance test, writes a rewrite brief, drafts replacement copy, validates it, and outputs a paused Editor-ready file. Drops each finding into the shared approval queue. Nothing changes in the account until the user says yes.

  Triggers: RSA audit, ad strength, Poor ad strength, responsive search ads, ad copy audit, rewrite headlines, new headlines, Google Ads copy, low performing assets, asset performance labels, pinning, ad copy refresh, character limits, broken landing pages, link checker, 404 ads, disapproved ads, ad test winner, which ad is winning, ad groups without ads.
---

# Google Ads ads bot (RSA audit and rewrite)

Goal: find the responsive search ads that are holding back delivery, and hand back replacement copy that is on-brief, within limits and policy-clean, ready for one approval.

Output lands in `gads-out/<customer_id>/<date>/` and merges into `approvals.md`, the single queue shared by all `gads-*` skills.

## Setup (once per machine, once per account)

```bash
PY="$HOME/Library/Application Support/pipx/venvs/google-ads-mcp/bin/python"   # has google-ads
S="$HOME/.claude/skills/gads-rsa-audit/scripts"
"$PY" "$S/gads.py" accounts                     # fails fast if auth expired
"$PY" "$S/gads.py" profile-init -c 1234567890   # then edit ~/.config/gads-skills/accounts/1234567890.json
```

Auth is Application Default Credentials with the adwords scope (developer tokens were sunset 2026-09-09). If auth expired, ask the user to run the `gcloud auth application-default login ...` command `gads.py` prints (suggest `! <command>`). Manager access needs `GOOGLE_ADS_LOGIN_CUSTOMER_ID`. Never route client accounts through treg.

The profile matters for copy: `brand_terms` (exact brand spelling), `notes` (positioning, proof points, offers, words to avoid). Ask the user for proof points if the profile has none. Never invent numbers, ratings, customer counts or awards.

No API access? Offline mode takes UI exports: Ads report (Ads > download, include Ad ID, Ad type, Ad status, Status, Policy details, Ad strength, Headline 1-15 and positions, Description 1-4, Path 1-2, Final URL, Impr., Clicks, Cost, Conversions), optionally the asset report (Asset, Asset type, Campaign, Ad group, Performance, Impr.), the keywords report, and the ad groups report (Campaign, Ad group, Ad group status, Campaign type) for coverage gaps. Without an ad groups report, coverage uses the keywords report's ad groups; with neither, the "no RSA" check is skipped and the report says so.

## Workflow

1. **Run the audit.**
   ```bash
   "$PY" "$S/audit_rsas.py" -c 1234567890 --days 30 --check-links
   python3 "$S/audit_rsas.py" --csv ads.csv --assets assets.csv --keywords keywords.csv \
     --adgroups adgroups.csv --check-links --currency USD   # offline
   ```
   `--check-links` fetches each unique final URL once (stdlib, User-Agent `gads-skills-link-checker/1.0`, 10s timeout, 4 at a time, redirects followed and recorded, ValueTrack `{placeholders}` filled with test values). Standalone: `python3 "$S/check_links.py" --csv ads.csv` or `--urls <url> ...`. Add soft 404 phrases with `--fail-text "discontinued"`. Ad tests use `--min-test-impr 1000 --confidence 95 --min-winner-conv 30`.

2. **Read the brief.** `rsa_brief.json` lists every ad needing work (including disapproved ads and their policy topics), worst first: final URL, top keywords, assets to keep, assets to replace (Low, duplicate, editorial), slots to fill, and `recommend_unpin`. Broken landing pages come first in the queue: no copy helps an ad whose page is dead.

3. **Read the landing page** for each final URL (WebFetch). Copy must match what the page says and offers. If the page and the ad disagree, flag it rather than writing copy the page does not support.

4. **Draft the copy** into `drafts.csv` in the same output folder, one asset per row: `ad_id,kind,text,pin,replaces` (`kind` is headline, description, path1 or path2). Fill every slot in `headlines_needed` and `descriptions_needed`. Follow the copy brief below.

5. **Validate. Iterate until it passes.**
   ```bash
   python3 "$S/validate_copy.py" --drafts <dir>/drafts.csv --brief <dir>/rsa_brief.json
   ```
   Errors exit 1 and write nothing. Fix and re-run. Warnings are judgment calls; resolve or explain them.

6. **Present** in 6-10 lines: broken pages and disapprovals first, then ads audited, how many Poor/Average, spend behind them, ad test winners, then per ad 3-4 of the strongest new headlines. Point to `approvals.md` and `rsa_drafts.csv`.

7. **On approval** the user imports `rsa_drafts.csv` in Google Ads Editor (Account > Import > From file). It creates a new RSA per row with **Status Paused**, so nothing serves until a human reviews and enables it. Once the new ad is enabled and approved by policy, pause the old ad (`Replaces ad ID` column) so performance history stays clean. Editing assets in place resets the ad's learning anyway; a fresh ad keeps the comparison honest.

## Copy brief (for step 4)

Write like a sharp colleague stating facts. Plain, specific, measured. No hype words (amazing, revolutionary, best-in-class, game-changing, unlock), no exclamation marks in headlines, no em dashes.

Headline mix for 15 slots (each must read well alone and in any order):

| Slots | Angle | Example (payroll SaaS) |
|---|---|---|
| 3-4 | Keyword: top keyword verbatim or close, at least 2 headlines | Payroll Software for Startups |
| 3 | Benefit or outcome | Run Payroll in Under 5 Minutes |
| 2-3 | Proof: real numbers, ratings, customers, certifications from the profile | Rated 4.8 on G2 by 900 Teams |
| 2 | Offer or risk reducer | Free 30-Day Trial, No Card |
| 1-2 | Feature that removes an objection | Automatic Tax Filing in 50 States |
| 1-2 | CTA | Book a 20-Minute Demo |
| 1 | Brand | Acme Payroll |

Descriptions (4): one benefit plus proof, one feature set, one objection handler, one offer plus CTA. Use 75-90 characters; short descriptions waste space.

Rules: vary length (some under 20 characters so they pair), avoid two headlines that say the same thing, keep each headline independent (no "and more" that depends on another), use Title Case consistently, mirror the landing page offer exactly, keep claims verifiable. Pin only for legal or brand requirements, and pin 2-3 options to a slot instead of one.

Display paths: 1-2 relevant words each, max 15 characters, no spaces (use hyphens).

## What the audit flags

| Flag | Severity |
|---|---|
| Landing page broken (4xx, 5xx, unreachable, soft 404) behind ads with spend; disapproved ad; ad strength Poor | critical |
| Broken page with no spend yet; redirect to another domain or the homepage; approved (limited); ad group with no enabled or no approved RSA; ad strength Average; under 10 unique headlines; no top keyword in any headline; 3+ pinned headlines | high |
| Ad test loser at the set confidence, winner with 30+ conversions (proposal: pause the loser) | medium, in queue |
| 10-12 headlines; under 4 descriptions; Low-rated assets with 1,000+ impressions; near-duplicates; editorial issues; missing path; 1 RSA per ad group | medium, report only |

Ad tests report a winner only when the two-proportion z-test clears the confidence level and both ads have the minimum impressions; anything else reads "not enough data". Details and reasoning: `references/rsa-playbook.md`.

## Output contract

| File | Use |
|---|---|
| `gads-rsa-audit.md` | human summary |
| `rsa_findings.csv` | every enabled RSA with all issues |
| `rsa_brief.json` | rewrite brief per flagged ad |
| `rsa_assets.csv` | asset labels and metrics |
| `rsa_links.csv` | every final URL: status, redirect chain, problem, ads and spend behind it |
| `rsa_coverage.csv` | ad groups with no RSA, no approved RSA, or one RSA |
| `rsa_tests.csv` | ad tests per ad group: CTR and conversions per impression, z, p, verdict |
| `drafts.csv` | your drafted copy (you write this) |
| `rsa_drafts.csv` | validated, Editor-ready import, every ad Paused |
| `approvals.md` | merged queue across all gads-* skills |

## Rules

- Read-only. This skill never writes to the account; changes go through Editor after a yes, and drafted ads arrive paused.
- The link checker only reads public pages with a generic User-Agent. Never put an email or account detail in it.
- Never invent proof points, prices or offers. If the profile and landing page do not provide them, use benefit and feature angles instead and tell the user what proof would help.
- Ad strength is a hygiene signal. Judge performance on CPA and conversions per impression. If an Average ad has the best CPA in its ad group, say so and propose adding a second RSA instead of replacing it.
- Call a test only from `rsa_tests.csv`. If it says "not enough data", say that and give the date or volume needed.
