---
name: gads-tracking-audit
description: |
  Google Ads tracking bot. Audits every conversion action (duplicates like GA4 purchase plus Google Ads tag purchase both primary, wrong counting type, actions that went silent or dropped, micro-conversions counted as primary, static purchase values, last-click attribution, enhanced conversions off) and checks the website for the tags it should carry (gtag, GTM containers, AW IDs, conversion labels, double loads, consent mode). Findings go into the shared approval queue; nothing changes in the account.

  Triggers: conversion tracking audit, duplicate conversions, double counting, conversion tag not firing, purchase tag, GA4 import, primary vs secondary conversions, conversions dropped, tracking broken, enhanced conversions, consent mode, Tag Assistant, "are our conversions real".
---

# Google Ads tracking bot

Goal: make sure the number smart bidding optimizes toward is real. Every other gads-* skill trusts `metrics.conversions`; if it is double counted or silent, their recommendations are wrong. Run this first on a new account and weekly after.

Output lands in `gads-out/<customer_id>/<date>/` and merges into `approvals.md`, the queue shared by all `gads-*` skills.

## Setup (once per machine, once per account)

```bash
PY="$HOME/Library/Application Support/pipx/venvs/google-ads-mcp/bin/python"   # has google-ads
S="$HOME/.claude/skills/gads-tracking-audit/scripts"
"$PY" "$S/gads.py" accounts                     # fails fast if auth expired
"$PY" "$S/gads.py" profile-init -c 1234567890   # then edit ~/.config/gads-skills/accounts/1234567890.json
```

Auth is Application Default Credentials with the adwords scope (developer tokens were sunset 2026-09-09). If `gads.py accounts` says auth expired, ask the user to run the printed `gcloud auth application-default login ...` command (suggest `! <command>`). Manager access needs `GOOGLE_ADS_LOGIN_CUSTOMER_ID`. Never route client accounts through treg.

In the profile, set `site_urls` to the pages that matter: home, main landing page, and the thank-you / order confirmation URL. Set `primary_conversions` to the actions the business actually cares about (ask the user; do not guess).

No API access? Offline mode with two UI exports:
- Goals > Conversions > Summary, table view, download (columns: Conversion action, Conversion source, Category, Action optimization, Count, Click-through conv. window, Attribution model, Value, Status).
- A campaign or account report segmented by Day and Conversion action, with Conversions and All conv.

## Workflow

1. **Audit the conversion actions.**
   ```bash
   "$PY" "$S/audit_conversions.py" -c 1234567890                 # default 56 days
   python3 "$S/audit_conversions.py" --csv conversion_actions.csv --daily conv_by_action_day.csv --currency USD
   ```
   56 days gives a 4-week baseline before the 7-day silent/drop check. Use `--recent-days 3` for high-volume accounts during an incident.

2. **Check the site.**
   ```bash
   python3 "$S/check_site_tags.py" -c 1234567890 --expect gads-out/1234567890/<date>/expected_tags.json
   python3 "$S/check_site_tags.py" --url https://site.com/thank-you --render     # if playwright is installed
   ```
   Static mode reads the HTML and the public GTM container (`gtm.js`), so it sees AW IDs, Ads conversion tags and GA4 IDs configured inside GTM. `--render` loads the page in headless Chromium and records the real requests to `googleadservices.com/pagead/conversion`, `google.com/ccm/collect` and GA4 `g/collect`, which proves firing and catches double fires. Render mode needs `pip install playwright && playwright install chromium`; ask before installing.

3. **Confirm firing when the script cannot.** A purchase or lead tag fires only after a real conversion, and the scripts cannot buy or submit. When the thank-you page needs a real submission, or render mode is unavailable, walk the user through Tag Assistant:
   1. Open https://tagassistant.google.com, click Add domain, enter the site URL, Connect.
   2. In the connected tab, complete a test conversion (test order with a coupon, or a test lead with a clearly fake name).
   3. Back in Tag Assistant, open the confirmation page's event list. Look for the Google Ads tag with event "conversion" and check the Conversion ID and Label match the action from step 1.
   4. Count the conversion hits on that one page. More than one per label is a double fire.
   5. In Google Ads > Goals > Conversions, the action's status should move to "Recording conversions" within a few hours. Diagnostics tab shows enhanced conversions coverage.
   Ask the user to paste what they see; record it in the summary as "confirmed via Tag Assistant".

4. **Interpret before presenting.**
   - A "duplicate" pair is a strong signal, not proof. Confirm with the user which source is the system of record. Rule of thumb: keep the Google Ads tag (or the offline/CRM import) as primary for bidding; GA4 imports as secondary for reporting. Exception: if the Google Ads tag is broken and GA4 is clean, keep GA4.
   - A "silent" action right after a site release is almost always a broken tag or a changed form. Ask what shipped on the last conversion date.
   - Do not recommend bid or budget changes from other gads-* skills while a high-severity tracking issue is open. Say that explicitly in the summary.

5. **Present** in 6-10 lines: health score, the duplicate or silent finding first (with its count), what share of reported conversions is suspect, site tag status, and what was confirmed vs only inferred. Point to `approvals.md`.

6. **On approval**, changes are made by the user in Google Ads (Goals > Conversions > the action > Edit settings) or in GTM. Give exact click paths. This skill does not mutate conversion settings via API: a wrong primary/secondary switch changes what every Smart Bidding campaign optimizes toward, and that deserves a human click.

## What the audit checks

| Check | Rule | Severity |
|---|---|---|
| Duplicate conversion | two counted actions, same category; score from daily correlation (r >= 0.8), count ratio (>= 0.7), name similarity, mixed source (tag vs GA4 import) | high |
| Silent primary | averaged >= 0.3/day over the prior 28 days, active on 40%+ of days, zero in the last 7 | high |
| Sharp drop | last 7 days < 40% of expected and Poisson p < 0.01 | medium |
| Micro-conversion primary | page view, engagement, add to cart, begin checkout, outbound click, default category counted in Conversions | high if >= 20% of conversions |
| Counting type | purchase counted One; lead counted Every | medium |
| Static value | purchase action always uses the default value | medium |
| Attribution | counted action not on data-driven | low |
| Enhanced conversions | lead actions primary but EC for leads off; customer data terms not accepted | medium / low |
| Removed still firing | removed or hidden action still records | low |
| No primary | nothing counted in Conversions | high |

Site checks: duplicate gtag.js loads or config calls for the same ID, the same GTM container loaded twice, the same `send_to` label fired twice in source, Ads ID on the page different from the account's tag (wrong account), no consent mode default, conversion labels expected vs found, and in render mode, repeated conversion requests per label.

Deeper rules, category cheat sheet, GA4 vs Ads tag guidance and consent mode notes: `references/tracking-playbook.md`.

## Output contract

| File | Use |
|---|---|
| `gads-tracking-audit.md` | human summary with health score and actions table |
| `conversion_actions.csv` | normalized settings for every action |
| `daily_by_action.csv` | date x action matrix of all conversions |
| `expected_tags.json` | AW IDs and labels for `check_site_tags.py --expect` |
| `gads-tracking-tags.md` / `site_tags.json` | site tag findings per URL |
| `approvals.md` | merged queue across all gads-* skills |

## Rules

- Read-only. No API writes from this skill.
- Say what was confirmed (render hits, Tag Assistant) and what was inferred (correlation, static HTML). Never claim a tag fires from static HTML alone.
- The last 3 days of conversions are incomplete (conversion delay, modeled conversions). The silent check uses a 7-day window for this reason.
- Fetch pages with the script's generic user agent. Never send the user's email or credentials to a site.
- Plain language in the summary. No em dashes.
