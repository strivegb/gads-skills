# Negative keyword playbook

Reference for `gads-search-terms`. Load when choosing match types, levels, or explaining a decision.

## How negatives match (different from positive keywords)

| Negative type | Blocks the query when | Example negative | Blocks | Does not block |
|---|---|---|---|---|
| Broad | every word of the negative appears, any order | `free software` | "software for free download" | "free" alone, "freeware" |
| Phrase | the words appear in that order, extra words allowed around | `"payroll jobs"` | "remote payroll jobs" | "jobs in payroll" |
| Exact | the query is exactly the negative, nothing extra | `[payroll calculator]` | "payroll calculator" | "free payroll calculator" |

Negatives **do not expand to close variants**. `jobs` does not block `job`; `crack` does not block `cracked`. Add singular/plural forms and synonyms explicitly. Google says negatives do account for casing and misspellings automatically.

Symbols (Google help, answer/2453972): `&`, accent marks and `*` are supported; periods are ignored; `+` is usually ignored; some other symbols return an error. Searches longer than 16 words can slip past a negative.

## Choosing the match type

- **Exact** for a single bad query whose words are otherwise good ("payroll software course" when "payroll software" sells). Default for zero-conversion spend.
- **Phrase** for a junk phrase that recurs with variations ("how to make", "source code", "customer care").
- **Broad (single word)** only for words that are junk in every context for this business: jobs, career, torrent, crack, porn. Never broad-negate a word that could appear in a buyer query ("free" is dangerous for SaaS with free trials; "cheap" is dangerous for D2C).

## Choosing the level

| Level | Use for | Notes |
|---|---|---|
| Account-level shared list | junk that is junk everywhere (jobs, piracy, adult) | Attach to every Search campaign. One list per theme makes reviews easy. |
| Campaign | junk relative to this campaign's intent; cross-campaign sculpting | e.g. negate brand terms in non-brand campaigns |
| Ad group | routing queries to the right ad group inside a campaign | sculpting, not waste control |

Cross-campaign sculpting (stopping two campaigns competing for the same query) is a structure decision; suggest it, never auto-apply it.

## Never negate

- Brand terms, including brand + modifier ("acme login" is a support query; send it to a non-ad path or let it be, but do not negate the brand itself in the brand campaign).
- Any term with conversions in the lookback, unless CPA is far above target and the user agrees.
- Anything in the profile `never_negate`.
- Competitor terms without a strategy call. Options: dedicated competitor campaign with its own target, or negate. That is the user's decision.

## Thresholds and statistics

- Account CVR = conversions / clicks across the search terms report.
- P(zero conversions in n clicks if the term converted at account CVR) = (1 - CVR)^n.
  - CVR 4%: 20 clicks -> 44% (not evidence), 57 clicks -> 10%, 75 clicks -> 5%.
  - CVR 2%: 114 clicks -> 10%.
- The miner requires cost >= 1x target CPA and P < 10% for a high-confidence call. This prevents negating low-volume terms on bad luck. Low-volume B2B accounts need longer windows (60-90 days).
- Conversions here are the account's primary conversions (`metrics.conversions`). If primary conversions are mis-set (see `gads-tracking-audit`), fix tracking first; mining on bad conversion data negates good traffic.

## Account limits (Google help, Nov 2026)

- 20 shared negative keyword lists per account.
- 5,000 keywords per shared list (some advertisers report 10,000; plan for 5,000).
- 10,000 negative keywords per campaign.

## Performance Max and AI Max

- PMax search terms are campaign-level only (`campaign_search_term_view`) and often grouped into categories (`campaign_search_term_insight`). Treat them as directional.
- PMax negatives: Google's help says account-level negative keywords apply to all Search and Shopping inventory, including Performance Max (answer/15726455). Campaign-level PMax negatives exist (third-party reports put the cap at 10,000 per campaign). Whether account-level negatives cover AI Max was not confirmed as of Nov 2026; check the UI. Brand exclusions (brand lists) are the right tool for keeping PMax off brand queries.
- Broad match and AI Max inflate the volume of loosely related queries. Expect a larger junk share and review weekly.

## Cadence

- Weekly for accounts > $20K/mo or using broad match / AI Max.
- Every two weeks otherwise.
- After any major match type change or new campaign launch, run at day 7.

## Reading the result honestly

- "Spend at stake" is past cost on queries the new negatives would block. Future savings are lower when the budget re-spends on adjacent queries. Re-run 14 days later and compare cost per conversion, not just junk spend.
- Hidden terms: Google hides search terms without enough volume. If report cost is well under campaign cost, say what share of spend the analysis covers.
