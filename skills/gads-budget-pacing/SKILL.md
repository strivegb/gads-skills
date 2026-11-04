---
name: gads-budget-pacing
description: |
  Google Ads budget bot. Paces every budget against the month: month-to-date spend, weighted run rate, projected month end, and the date each campaign runs out of its monthly plan. Finds budget-limited campaigns with good CPA/ROAS and funds them from budget that is unused or underperforming, as a $/day shift that keeps the total flat. Writes the proposal into the shared approval queue; nothing changes until the user says yes.

  Triggers: budget pacing, pacing report, month-end budget, will we overspend, run out of budget, budget-limited, limited by budget, lost impression share budget, budget reallocation, shift budget, daily budget changes, monthly ad budget, spend forecast, Google Ads budget check.
---

# Google Ads budget bot

Goal: no campaign silently stops mid-month, no money sits unused in a weak campaign, and the account lands on its monthly number. Output is one shift proposal plus any pace fixes, in plain $/day.

Output lands in `gads-out/<customer_id>/<date>/` and merges into `approvals.md`, the queue shared by all `gads-*` skills.

## Setup (once per machine, once per account)

```bash
PY="$HOME/Library/Application Support/pipx/venvs/google-ads-mcp/bin/python"   # has google-ads
S="$HOME/.claude/skills/gads-budget-pacing/scripts"
"$PY" "$S/gads.py" accounts                     # fails fast if auth expired
"$PY" "$S/gads.py" profile-init -c 1234567890   # edit ~/.config/gads-skills/accounts/1234567890.json
```

Auth is Application Default Credentials with the adwords scope (developer tokens were sunset 2026-09-09). If auth has expired, give the user the `gcloud auth application-default login ...` command `gads.py` prints (suggest `! <command>`). Manager access needs `GOOGLE_ADS_LOGIN_CUSTOMER_ID`. Never route client accounts through treg.

**Profile fields that matter here.** Ask the user for them if empty; do not guess:
- `monthly_budget`: the account's monthly number.
- `campaign_budgets`: `{"Campaign or shared budget name": monthly_plan}`. Without these, "runs dry" has no meaning and the bot falls back to daily reallocation by lost impression share.
- `target_cpa` / `target_roas`: decide which campaigns deserve more money. Campaign-level Smart Bidding targets override them.

Offline mode: Campaigns report segmented by Day covering the last 14 days and the whole month to date, with Budget, Budget name, Cost, Conversions, Conv. value, Search impr. share, Search lost IS (budget), Search lost IS (rank).

## Workflow

1. **Run the pacer.**
   ```bash
   "$PY" "$S/pace_budgets.py" -c 1234567890
   python3 "$S/pace_budgets.py" --csv campaign_by_day.csv --currency USD    # offline
   ```
   Live mode uses the account's time zone for "today" and month bounds. `--today YYYY-MM-DD` replays a past date.

2. **Read `pacing.csv` before trusting the shift.** Check:
   - A run rate that is far from the daily budget usually means a recent budget change, a paused day, or a promo. If the last 7 days are not representative, say so and soften the proposal.
   - Shared budgets appear once, with all their campaigns. A shift touches every campaign on that budget.
   - Efficiency `unknown` means too few conversions in 14 days to judge. Those budgets never receive money.

3. **Present** `gads-budget-pacing.md` in 6-10 lines: MTD, run rate, projected vs plan, which budgets run dry and when, the proposed $/day shift and where it comes from, expected conversions. Point to `approvals.md`.

4. **On approval**, validate then write:
   ```bash
   "$PY" "$S/apply_budgets.py" -c 1234567890 --file <dir>/budget_changes.csv            # validate only
   "$PY" "$S/apply_budgets.py" -c 1234567890 --file <dir>/budget_changes.csv --execute  # write
   ```
   Remove rejected rows first. The script re-reads live budgets and skips any that changed since the analysis. Never pass `--execute` without an explicit yes in this conversation. Plan-only changes (a campaign keeps its daily budget but gets more of the month) need the user to update their plan sheet and the profile `campaign_budgets`; say so.

5. **Re-run in 2-3 days.** Pacing drifts; one check per month is not pacing.

## How it decides

| Step | Rule |
|---|---|
| Run rate | 0.6 x last-7-day average + 0.4 x last-14-day average, complete days only |
| Projection | MTD + run rate x days left (today included) |
| Runs dry | first date cumulative spend reaches the monthly plan; only counted if projection exceeds plan by 2%+ and the date is before month end |
| Budget-limited | Search lost IS (budget) > 10%; non-Search: spending >= 95% of daily budget |
| Efficiency | CPA vs target (good >= on target, poor < 0.77x) or ROAS vs target (poor < 0.75x); 14-day window |
| Receivers | good efficiency and (runs dry on plan, or budget-limited with no plan) |
| Donors, in order | 1. plan slack (will underspend its plan, not limited) 2. unused headroom (spending < 80% of daily) 3. poor efficiency (gives 20%) |
| Step size | budget-limited campaigns move at most 20% per change |
| Impact | budget simulation when the API returns one; otherwise marginal CPA = 1.3x average CPA |

Also flagged: campaigns that will overshoot their plan with weak efficiency (cut to land on plan), campaigns that will underspend a committed plan (raise if budget-limited; otherwise the limit is bids or demand, not budget), and the account against `monthly_budget`. Details and the reasoning behind each threshold: `references/budget-pacing-playbook.md`.

## Output contract

| File | Use |
|---|---|
| `gads-budget-pacing.md` | human summary with pacing table, shift, changes |
| `pacing.csv` | one row per budget: daily, run rate, utilization, MTD, projected, plan, runs_dry, lost IS, CPA/ROAS, efficiency |
| `budget_changes.csv` | budget_id, budget_resource_name, budget_name, shared, campaigns, current_daily, new_daily, why (input to `apply_budgets.py`) |
| `approvals.md` | merged queue across all gads-* skills |

## Rules

- Read-only by default. Writes only through `apply_budgets.py --execute` after a yes.
- Never move money into a campaign without conversion evidence, and never into one that is limited by rank rather than budget (more budget will not buy impressions it is losing on bids or quality).
- Be honest about what a shift does. Moving unused headroom into a limited campaign raises actual spend even though total daily budget is flat; the summary says so.
- Google can spend up to 2x a daily budget on one day and caps a month at 30.4x the average daily budget. Explain this when a single-day spike alarms the user.
- Plain language. No em dashes.
