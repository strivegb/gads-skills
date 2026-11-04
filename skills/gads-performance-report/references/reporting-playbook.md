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
- The script flags lag when the period ends within 3 days of today, or when the last 3 days convert 25%+ below the rest of the period.
- When flagged, compare spend and clicks confidently and conversions cautiously. Re-run once the lag window has passed before calling a trend.
- Accounts importing offline conversions: compare periods that are at least the conversion window old.

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
