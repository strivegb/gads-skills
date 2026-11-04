---
name: gads-rsa-audit
description: |
  Google Ads ads bot. Audits every enabled responsive search ad (ad strength, headline and description count, pinning, near-duplicates, keyword coverage, Low-rated assets, editorial and policy issues, ads per ad group), writes a rewrite brief, drafts replacement headlines and descriptions, validates them against character limits and editorial policy, and outputs an Editor-ready file. Drops each rewrite into the shared approval queue. Nothing changes in the account until the user says yes.

  Triggers: RSA audit, ad strength, Poor ad strength, responsive search ads, ad copy audit, rewrite headlines, new headlines, Google Ads copy, low performing assets, asset performance labels, pinning, ad copy refresh, character limits.
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

No API access? Offline mode takes UI exports: Ads report (Ads > download, include Ad ID, Ad type, Ad strength, Headline 1-15 and positions, Description 1-4, Path 1-2, Final URL, Impr., Clicks, Cost, Conversions), optionally the asset report (Asset, Asset type, Campaign, Ad group, Performance, Impr.) and the keywords report.

## Workflow

1. **Run the audit.**
   ```bash
   "$PY" "$S/audit_rsas.py" -c 1234567890 --days 30
   python3 "$S/audit_rsas.py" --csv ads.csv --assets assets.csv --keywords keywords.csv --currency USD   # offline
   ```

2. **Read the brief.** `rsa_brief.json` lists every ad needing work, worst first: final URL, top keywords, assets to keep, assets to replace (Low, duplicate, editorial), slots to fill, and `recommend_unpin`.

3. **Read the landing page** for each final URL (WebFetch). Copy must match what the page says and offers. If the page and the ad disagree, flag it rather than writing copy the page does not support.

4. **Draft the copy** into `drafts.csv` in the same output folder, one asset per row: `ad_id,kind,text,pin,replaces` (`kind` is headline, description, path1 or path2). Fill every slot in `headlines_needed` and `descriptions_needed`. Follow the copy brief below.

5. **Validate. Iterate until it passes.**
   ```bash
   python3 "$S/validate_copy.py" --drafts <dir>/drafts.csv --brief <dir>/rsa_brief.json
   ```
   Errors exit 1 and write nothing. Fix and re-run. Warnings are judgment calls; resolve or explain them.

6. **Present** in 6-10 lines: ads audited, how many Poor/Average, spend behind them, then per ad 3-4 of the strongest new headlines. Point to `approvals.md` and `rsa_drafts.csv`.

7. **On approval** the user imports `rsa_drafts.csv` in Google Ads Editor (Account > Import > From file). It creates a new RSA per row. Pause the old ad (`Replaces ad ID` column) once the new one is approved by policy, so performance history stays clean. Editing assets in place resets the ad's learning anyway; a fresh ad keeps the comparison honest.

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
| Disapproved | critical |
| Ad strength Poor | critical |
| Ad strength Average; under 10 unique headlines; no top keyword in any headline; 3+ pinned headlines; approved (limited) | high |
| 10-12 headlines; under 4 descriptions; Low-rated assets with 1,000+ impressions; near-duplicates; editorial issues; missing path; 1 RSA per ad group | medium |

High and critical go to the approval queue. Medium shows under "Minor" in the report. Details and reasoning: `references/rsa-playbook.md`.

## Output contract

| File | Use |
|---|---|
| `gads-rsa-audit.md` | human summary |
| `rsa_findings.csv` | every enabled RSA with all issues |
| `rsa_brief.json` | rewrite brief per flagged ad |
| `rsa_assets.csv` | asset labels and metrics |
| `drafts.csv` | your drafted copy (you write this) |
| `rsa_drafts.csv` | validated, Editor-ready import |
| `approvals.md` | merged queue across all gads-* skills |

## Rules

- Read-only. This skill never writes to the account; changes go through Editor after a yes.
- Never invent proof points, prices or offers. If the profile and landing page do not provide them, use benefit and feature angles instead and tell the user what proof would help.
- Ad strength is a hygiene signal, not a performance metric. If an Average ad has the best CPA in its ad group, say so and propose adding a second RSA instead of replacing it.
- Do not call a rewrite a win or loss before about 2 weeks and a few thousand impressions.
