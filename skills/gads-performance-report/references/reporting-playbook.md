# Reporting playbook

Reference for `gads-performance-report`. Load when explaining a number, choosing a window, or writing the narrative.

## Metric definitions (as the report computes them)

| Metric | Formula | Note |
|---|---|---|
| Spend | sum of cost | account currency |
| CTR | clicks / impressions | mixed across networks; compare like with like |
| CPC | cost / clicks | |
| Conv. rate (CVR) | conversions / clicks | uses `metrics.conversions` (primary actions only) |
| CPA | cost / conversions | |
| Conv. value | sum of `metrics.conversions_value` | primary actions only |
| ROAS | conv. value / cost | shown as 4.20x |
| AOV | conv. value / conversions | value per conversion |
| Search IS | impressions / eligible impressions | Search and Shopping only; aggregated weighted by eligible impressions |
| Lost IS (budget / rank) | share of eligible impressions lost | budget = ran out of money, rank = bid or quality too low |

Conversions are attributed to the click date. A conversion that happens on Wednesday for a Monday click is reported on Monday.

## Decomposition

Conversions = clicks × CVR. The change splits exactly into:

- volume effect = (clicks now − clicks before) × CVR before
- rate effect = clicks now × (CVR now − CVR before)

CPA = CPC / CVR and ROAS = CVR × AOV / CPC are multiplicative, so their changes split by log share. "CPA moved mostly on CPC (70%)" means auction cost explained 70% of the log change, conversion rate 30%.

| Driver | Usual causes | Next step |
|---|---|---|
| Click volume, budget-lost IS up | budget cap, pacing, shared budget drained by another campaign | gads-budget-pacing |
| Click volume, rank-lost IS up | target too tight, competitor pressure, ad quality drop | gads-bid-audit, gads-rsa-audit, auction insights |
| Click volume, IS flat | demand moved (seasonality, news, weekday mix) | context from the user |
| CVR | tracking change, landing page or site issue, offer change, lower-intent queries | gads-tracking-audit, gads-search-terms |
| CPC | auction pressure, bid strategy change, broader match | auction insights, gads-bid-audit |
| AOV | product mix, pricing, promo | the user's commerce data |

## Choosing the comparison

| Ask | Use | Why |
|---|---|---|
| Weekly check-in | `--days 7` (default) | full weeks keep weekday mix equal |
| Monthly client report | `--days 30` or `--compare mtd` | MTD compares the same number of days |
| Seasonal business | `--compare yoy` | 364 days back keeps weekdays aligned |
| After a big change | `--start` on the change date, `--end` | isolates before vs after |

Avoid comparing windows with a different weekday mix (Mon-Thu vs Fri-Sun) for B2B; weekends convert differently.

## Conversion lag

- Most lead forms report within a day; purchases and offline imports (CRM, calls) can lag 7-30 days.
- Lag days: live, the 90% point of `metrics.conversions` by `segments.conversion_lag_bucket` over 90 days (capped at 14); offline `--lag-days`, default 3. Days younger than the lag are "still collecting conversions".
- When the current period includes such days, the report adds a complete-days-only line: the same number of days dropped from the end of both periods, so weekdays stay aligned. Without a Day column it says the comparison is provisional.
- The script also flags lag when the last lag days convert 25%+ below the rest of the period.
- When flagged, compare spend and clicks confidently and conversions cautiously. Re-run once the lag window has passed before calling a trend.
- Accounts importing offline conversions: compare periods that are at least the conversion window old.

## Same-weekday baseline

- Weekday mix drives most day-to-day movement, so a Monday is compared only with earlier Mondays. Default history is 8 weeks; at least 4 are needed.
- z = (value - mean) / standard deviation of those weeks. The deviation has a floor (5% of the mean, and the square root of the mean for clicks and conversions) so a very steady history does not turn small moves into alerts.
- Flags need |z| >= 2 and a change of 20% or more. CPA uses cost over conversions per window and skips weeks with zero conversions.
- Conversions, CPA and value anchor on the last day outside the lag window. Spend and clicks use the last full day.
- Holidays and promotions break the weekday pattern. Ask before treating a holiday week as an anomaly.

## Stopped serving

- An enabled campaign with budget that had impressions in the previous 14 days and none on the last full day. Live mode reads status, primary status reasons, budget and end date; offline needs Campaign status and Budget columns, else it asks the user to confirm the campaign is enabled.
- Usual causes, in order: ads or assets disapproved, ad groups or keywords paused, billing or payment hold, an end date passed, a targeting or bid change that removed eligibility. Check change history first.

## Change history

- Live: `change_event` with a `change_event.change_date_time` filter inside the last 30 days and `LIMIT` 10000 or less. Fields used: change_date_time, change_resource_type, change_resource_name, resource_change_operation, client_type, changed_fields, campaign, old_resource, new_resource. Budget changes map to every campaign sharing that budget.
- Offline: Tools > Change history > download. Rows are matched to movers by campaign name and date.
- Attached text reads "target CPA lowered from $60 to $45 on Oct 28 by a user in the Google Ads UI". Bid, budget and status changes are listed first, up to 3 per mover.
- Emails and names stay out of the report. Set `"show_change_users": true` in the profile only when the reader should see them.

## Materiality and noise

- Conversions behave roughly like Poisson counts. The standard deviation of the difference between two periods is about √(a + b). Going from 20 to 26 conversions (√46 ≈ 6.8) is noise; 20 to 40 is not.
- Movers must account for at least 15% of the account's gross movement. This keeps a tiny campaign's +300% out of the headline.
- Say "within normal variation" plainly when nothing qualifies. A quiet week is a valid finding.

## Optimization score

Google's optimization score estimates how well the account follows Google's recommendations. It moves when you accept or dismiss recommendations, not when results improve. Report it because clients ask, label it honestly, and never set it as a goal.

## Writing the narrative

- Headline: spend, primary outcome, efficiency, the names of the top movers. One sentence.
- Each mover: what moved (before to after), why (the driver), the action verb and the concrete next step. Three sentences.
- End with the queue: "14 items are waiting for your yes" makes the report the start of the day, not a document to file.
- Numbers: currency rounded to whole units above 100, one decimal for conversions under 100, percentages with no decimals unless under 10%.
- Never: "performance was mixed", "it is important to note", benchmark claims without a source, em dashes.
