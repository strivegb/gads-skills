# Budget pacing playbook

Reference for `gads-budget-pacing`. Load when explaining a recommendation or handling an unusual account.

## How Google spends a daily budget

- A campaign can spend up to 2x its average daily budget on a given day to catch traffic swings.
- Over a calendar month it will not charge more than 30.4x the average daily budget (Google Ads Help, "About average daily budgets", answer 2375423). Overdelivery beyond that is credited.
- Mid-month budget changes: see Google's "About spending limits" article for how the monthly limit adjusts; do not promise a specific clawback.
- Campaign total budgets (`period` not DAILY, `total_amount_micros`) spend toward a fixed total over the campaign dates. The pacer reports them but does not shift money into or out of them.

## Overdelivery

A day above the daily budget is normal; Google averages spend over the month. The bot flags a budget when it is a pattern worth a decision:

- **Repeated:** 3 or more of the last 14 days above 1.2x that day's daily budget. The 1.2x floor ignores routine small overages.
- **Heading past the ceiling:** projected month spend above 30.4x the average daily budget (with 3% slack so a 31-day month at exactly budget is not flagged). Google credits spend above the ceiling, so delivery tends to slow late in the month.

The fix depends on what the owner cares about. If the daily number is the most they want to pay on any day, lower it. If the monthly total matters, add a plan cap to the profile and let pacing manage it.

Data: offline exports carry Budget per Day row, so each day is compared with the budget that applied. Live mode reads `campaign_budget.amount_micros`, which is today's value; a budget change in the last 14 days can cause a false flag, and confidence is set to low.

## Pause at cap

When a campaign with a plan cap, or the account against `monthly_budget`, is projected to pass its cap before month end, the bot proposes a pause on the projected date. The same item gives the alternative: a daily budget cut now that stretches the remaining plan to month end. For the account, it lists the campaigns to pause first (weakest CPA/ROAS first). Campaigns that a budget shift already funds are skipped. This is a proposal only; no script pauses anything. Once approved, the user pauses on the date or sets an automated rule.

## Plan caps vs Google's ceiling

Google only knows daily budgets. "Runs dry" is a business concept: the campaign has a monthly allocation (in the profile `campaign_budgets`) and at the current rate it uses it up before month end. At that point the owner either pauses it or overspends the plan. The bot's job is to see that 1-2 weeks early and move plan money from campaigns that will not use theirs.

Without plan caps, the bot cannot say "runs dry". It switches to daily reallocation: lost impression share to budget tells which campaigns would spend more if allowed.

## Reading lost impression share

| Search lost IS (budget) | Search lost IS (rank) | Meaning | Lever |
|---|---|---|---|
| > 10% | low | money is the constraint | more budget, if CPA/ROAS holds |
| low | high | bids, Quality Score or targets are the constraint | bids, ads, landing page; budget does nothing |
| high | high | both | fix rank first, then budget |
| low | low | saturated | more budget buys little; expand targeting |

Impression share metrics exist only for Search and Shopping. For PMax and Demand Gen the bot uses utilization (run rate / daily budget >= 95%) as the budget-limited proxy, which is weaker evidence.

## Applying changes

`apply_budgets.py` uses the shared write gate: validate (prints a token), then `--execute --confirm <token>`. Any move above 20% is refused unless `--allow-large --reason "<why>"` is given; the pacer marks those rows `large=yes`. Donor cuts of unused headroom often exceed 20%; they carry no delivery risk, but they still need the explicit reason so the log shows who approved what. After writing, the script reads the budgets back and logs every change.

## Why a 20% step

Smart Bidding recalibrates when budgets change sharply. Practitioners commonly keep budget moves on budget-limited campaigns within 15-20% per change, a few days apart. Google does not publish a hard threshold, so the bot treats 20% as guidance and states it that way. Cutting an unused budget (a donor spending well under its daily) has no delivery effect and is not capped.

## Marginal CPA

The next dollar into a campaign buys conversions at a worse rate than the average dollar: the campaign is already winning the cheapest auctions. The bot assumes marginal CPA = 1.3x the 14-day average unless the API returns a budget simulation (`campaign_simulation` with `type = BUDGET`), in which case it interpolates conversions between simulation points. Treat any conversion estimate as directional.

## Donor priority

1. **Plan slack.** A capped campaign projected under its plan and not budget-limited. Its leftover plan money moves at no cost; trimming its daily budget to ~1.1x run rate changes nothing in delivery.
2. **Unused headroom.** No plan cap, spending under 80% of its daily budget. Freeing it lets receivers spend more, so actual account spend rises.
3. **Poor efficiency.** CPA well over target or ROAS well under, not budget-limited. Gives 20% of its daily. This is the only donor whose cut reduces conversions; say so.

Brand campaigns often look like headroom donors (cheap, rarely limited). That is fine, but keep enough headroom for demand spikes such as launches or PR days.

## Situations that need a human call

- **First 3 days of a month:** MTD is thin and projection leans on last month. Present as indicative only.
- **Promos, launches, seasonality:** a 7-day run rate during a sale will over-project. Ask about the calendar before cutting.
- **Recent budget change:** run rate lags the new budget for several days. Check change history before calling a campaign an underspender.
- **Conversion lag:** B2B lead-to-SQL or long purchase cycles make 14-day CPA look worse than it is. Use the profile target rather than the trailing number for those accounts, or lengthen the window.
- **Portfolio bid strategies and shared budgets:** money moved into a shared budget is split by Google across its campaigns, not by you.

## Cadence

- Every 2-3 days for accounts with monthly plans.
- Daily in the last week of the month or during promotions.
- After any manual budget change, the next day.
