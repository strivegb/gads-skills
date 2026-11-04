---
name: gads-bid-audit
description: |
  Google Ads bids bot. Audits every campaign's bid strategy against reality: target CPA/ROAS vs actual, conversion volume vs Smart Bidding needs, constrained vs loose targets, learning status and recent changes, brand overbidding (paying more for clicks you already win), and brand mixed into non-brand campaigns. Proposes stepped target changes into the shared approval queue. Nothing changes in the account until the user says yes.

  Triggers: bid strategy audit, target CPA, tCPA, target ROAS, tROAS, smart bidding, overbidding, brand CPC, brand bidding, brand campaign costs, bidding too high, CPA too high, learning period, impression share lost to rank, Google Ads bids.
---

# Google Ads bids bot

Goal: make every campaign bid on the right number. Find money wasted on brand clicks the account already wins, targets that strangle volume, targets that steer nothing, and campaigns ready for Smart Bidding. Propose changes in steps the bidder can absorb.

Output lands in `gads-out/<customer_id>/<date>/` and merges into `approvals.md`, the single queue shared by all `gads-*` skills.

## Setup (once per machine, once per account)

```bash
PY="$HOME/Library/Application Support/pipx/venvs/google-ads-mcp/bin/python"   # has google-ads
S="$HOME/.claude/skills/gads-bid-audit/scripts"
"$PY" "$S/gads.py" accounts                     # lists accessible customer IDs; fails fast if auth expired
"$PY" "$S/gads.py" profile-init -c 1234567890   # then edit ~/.config/gads-skills/accounts/1234567890.json
```

Auth is Application Default Credentials with the adwords scope (developer tokens were sunset 2026-09-09; access sits on the Cloud project). If `gads.py accounts` says auth expired, ask the user to run the `gcloud auth application-default login ...` command it prints (suggest `! <command>`). Access via a manager account needs `GOOGLE_ADS_LOGIN_CUSTOMER_ID`. Never route client accounts through treg.

**Fill the profile before the first run.** `brand_terms` drives brand detection; without it brand campaigns are found by name only and brand mixing is missed. `target_cpa` / `target_roas` are the business goals the bot steps toward. Optional `brand_max_cpc` sets the reference brand CPC. Ask the user for these if empty. Do not guess.

No API access? Offline mode takes UI exports: a Campaigns report for the window (columns: Campaign, Campaign type, Bid strategy type, Bid strategy, Target CPA, Target ROAS, Cost, Clicks, Impr., Conversions, Conv. value, Avg. CPC, Search impr. share, Search abs. top IS, Search lost IS (budget), Search lost IS (rank)), the same report for the last 7 days, the same report for a baseline period 60-90 days back, a Keywords report (Keyword, Campaign, Cost, Clicks, Conversions), and optionally a Campaigns report segmented by Network (with search partners) for the Search Partners check (skipped without it).

## Workflow

1. **Run the audit.**
   ```bash
   "$PY" "$S/audit_bids.py" -c 1234567890 --days 30
   python3 "$S/audit_bids.py" --csv camp_30d.csv --recent camp_7d.csv --prior camp_prior.csv \
       --keywords keywords.csv --network camp_by_network.csv \
       --brand "acme,acme payroll" --target-cpa 45 --currency USD    # offline
   ```
   Live mode pulls 30-day and 7-day campaign metrics, a baseline period, keyword-level brand split, a Google Search vs Search Partners split, portfolio targets, `change_event` for the last 14 days and bid simulators.

2. **Check context before presenting.** For each recommendation ask:
   - Brand overbid: is there a new competitor on the brand (Auction insights)? If yes, lower confidence and say so.
   - Tighten target: did tracking change recently (`gads-tracking-audit`)? A CPA jump from a broken tag is not a bidding problem.
   - Loosen target: does the user's margin allow a higher CPA? The business target in the profile wins over the bot.
   - Lost IS (budget) high: route to `gads-budget-pacing`, not bids.

3. **Present** `gads-bid-audit.md` in 6-10 lines: brand overbid amount, the target changes with old to new numbers, campaigns held for learning or recent changes, and notes on low-volume Smart Bidding. Point to `approvals.md`.

4. **On approval**, validate, then write with the token:
   ```bash
   "$PY" "$S/apply_targets.py" -c 1234567890 --file <dir>/bid_changes.csv                          # validate, prints token
   "$PY" "$S/apply_targets.py" -c 1234567890 --file <dir>/bid_changes.csv --execute --confirm <token>
   ```
   Remove rejected rows before validating; editing the file afterwards means validating again. The script only touches campaign-level target CPA / target ROAS and skips portfolios and held rows. Moves above 20% are refused unless the user approved them and you add `--allow-large --reason "<their reason>"` (printed and logged). After writing it reads the targets back, prints any that did not land, and logs to `gads-out/<cid>/changes.log.jsonl`. Strategy switches and Search Partners changes are made in the UI (Settings > Networks) or Editor so the approver sees every setting. Never pass `--execute` without an explicit yes in this conversation.

## How the audit decides

| Check | Rule | Output |
|---|---|---|
| Brand overbid | brand IS >= 85% or abs top >= 75%, and CPC >= 1.25x reference CPC | switch to Target IS, abs top 90%, CPC cap 1.2x reference; overspend estimate |
| Brand mixing | brand keywords >= 15% of a non-brand campaign's spend | split brand out; true non-brand CPA; target change held |
| Constrained target | target CPA < 0.8x actual and rank-limited (or no IS data) | raise target 15% |
| Loose target | target CPA > 1.25x actual | lower toward actual, max 15% |
| Over business goal | actual CPA > 1.1x profile target | step 15% toward the goal, never all the way in one move |
| Manual CPC with volume | >= 30 conv/month | Maximize conversions at actual CPA + 5% |
| Low volume | < 30 conv/month on tCPA, < 50 on tROAS | note: pool, change goal, or drop the target |
| Learning / recent change | LEARNING status or bid/budget change in 14 days | recommendation marked "Hold, then set", risk high |
| Search Partners | Search campaign, partners >= 100 clicks and >= 5% of spend, partners CPA >= 1.5x Google Search CPA (or ROAS <= 0.67x, or no conversions on 2x+ Google Search CPA of spend) | turn off `target_search_network`; monthly saving and conversions given up |
| Simulator preview (live) | `campaign_simulation` TARGET_CPA / TARGET_ROAS points exist | predicted cost and conversions at the current and proposed target, in the why text and evidence; skipped quietly when absent |

ROAS rules mirror CPA. Thresholds and step size are practitioner guidance; the rationale and the brand method are in `references/bidding-playbook.md`.

## Output contract

| File | Use |
|---|---|
| `gads-bid-audit.md` | human summary with campaign table |
| `bid_campaigns.csv` | per-campaign diagnostics (strategy, targets, CPA 30d/7d, IS, lost IS, learning, recent changes) |
| `bid_changes.csv` | target changes for `apply_targets.py` (live runs only; needs campaign IDs) |
| `approvals.md` | merged queue across all gads-* skills |

## Rules

- Read-only by default. Writes only through `apply_targets.py --execute --confirm <token>` after a yes.
- One lever per campaign per cycle. Never stack a target change on a fresh budget change.
- Say "Google's simulator estimates" for simulator numbers; never present them as a promise.
- Brand overbid is an estimate against a reference CPC. Name the reference.
- Plain language in the summary. No em dashes.
