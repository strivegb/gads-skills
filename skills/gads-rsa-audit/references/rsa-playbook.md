# RSA playbook

Reference for `gads-rsa-audit`. Load when explaining a flag, judging a borderline ad, or checking limits.

## Hard limits

| Element | Limit | Count |
|---|---|---|
| Headline | 30 characters | 3 minimum, 15 maximum |
| Description | 90 characters | 2 minimum, 4 maximum |
| Display path 1 and 2 | 15 characters each | optional |
| Enabled RSAs per ad group | 3 | |

Double-width characters (Chinese, Japanese, Korean, full-width forms) count as 2. `validate_copy.py` applies this.

Google shows up to 3 headlines and 2 descriptions per impression, in any order, and may show only 2 headlines. Every headline has to work alone and next to any other.

## Ad strength

Values: Poor, Average, Good, Excellent (plus Pending / Unrated while it is calculated).

What drives it: number of unique headlines and descriptions, how different they are from each other, presence of popular keywords from the ad group, and pinning (pinning lowers it). It does not measure CTR, conversion rate or CPA.

How to use it:
- Poor is worth fixing: it usually means too few assets or repeated ones, which limits the auctions the ad can enter.
- Average to Good is usually the gap between 8-10 and 13-15 distinct headlines.
- Do not chase Excellent at the cost of message quality or by stuffing keywords.
- A Good-strength ad that converts worse than an Average one in the same ad group is the one to change.

## Asset performance labels

Values: Best, Good, Low, Learning, Pending, Unrated. Labels compare an asset to the other assets of the same type in the same ad, and they need volume.

- Replace Low assets once they have about 1,000+ impressions (`--min-asset-impr`). Below that, the label is noise.
- Learning and Pending are not verdicts.
- Best assets are the best evidence of what this audience responds to. Write new variants on the same angle.
- Asset-level impressions, clicks and conversions are also reported; use them to sanity-check a label before acting.

## Pinning

- Every pinned headline reduces the combinations Google can test.
- Pin only for legal disclaimers, regulated claims or a brand requirement.
- If a slot must be pinned, pin 2-3 alternatives to it so Google can still rotate.
- 3+ pinned headlines is flagged; all headlines pinned turns the RSA into a static ad.

## Near-duplicates

Flagged when two headlines share the same words after removing case and punctuation, when one is the other plus a single filler word ("HR Software" vs "HR Software Online"), or when token overlap is 75%+. Near-duplicates waste slots and drag ad strength down.

## Keyword coverage

At least one, ideally two, headlines should contain the ad group's top keyword (by impressions) word for word or very close. This drives relevance in ad strength and quality score expected-CTR. The audit flags ads where none of the top 3 keywords appear in any headline.

## Editorial policy checked by validate_copy.py

| Check | Why |
|---|---|
| No `!` in headlines; max one `!` in descriptions | Google editorial policy |
| No repeated punctuation (`!!`, `...`, `??`) | Google editorial policy |
| No ALL CAPS words except recognised acronyms (SaaS, CRM, SOC, HR, AI...) | Google editorial policy |
| No emoji or decorative symbols | Google editorial policy |
| No phone numbers in ad text | use a call asset instead |
| No "click here" style CTA | Google editorial policy (gimmicky/unclear) |
| No double or trailing spaces | editorial |
| Paths without spaces or slashes | display URL format |

Not checked automatically (judge yourself): trademark use of competitor names, superlatives that need third-party proof ("No. 1", "best"), health, finance and other restricted claims, prices that differ from the landing page.

## Ads per ad group (coverage)

- 0 enabled RSAs, or only disapproved ones, in an enabled ad group of an enabled Search campaign: the keywords have no RSA to show. High severity; write one. Dynamic Search Ad groups are excluded.
- 1 RSA: fine for low-volume ad groups, but no message test is running. Suggest a second RSA with a different lead angle.
- 2-3 RSAs: normal. More splits impressions and slows learning.
- Live mode reads the ad group list from the API. Offline it needs the ad groups report, or falls back to the ad groups in the keywords report. Without either, only single-RSA ad groups are reported.

## Policy

- Disapproved (`policy_summary.approval_status` DISAPPROVED, or "Disapproved" in the export's Status column): the ad does not serve. Critical. The policy topic names the problem (trademarks, destination not working, editorial). Fix the asset or the page, then resubmit.
- Approved (limited): serves in fewer auctions, countries or devices. High. Rewrite when the restriction is avoidable (a trademark you do not hold, a restricted claim); leave it when the limit is structural (alcohol, healthcare).
- "Destination not working" disapprovals usually match a broken page in `rsa_links.csv`. Fix the page first.

## Landing pages

`check_links.py` fetches each unique final URL of an enabled ad once, the way Google's Link Checker script does.

| Problem | Meaning | Severity |
|---|---|---|
| broken | HTTP 4xx or 5xx, or more than 10 redirects | critical with spend, high without |
| unreachable | timeout, DNS failure or refused connection (retried once) | critical with spend, high without |
| soft_404 | HTTP 200, but the page says "page not found", "no longer available", "out of stock" and similar | critical with spend, high without |
| off_domain | a redirect ends on a different domain (www and subdomains count as the same site) | high: risks a destination mismatch disapproval |
| homepage | a deep URL redirects to the homepage, which usually means the page was removed | high |

- ValueTrack placeholders are filled before fetching: `{keyword}` becomes `test`, `{matchtype}` `e`, `{device}` `c`, `{ifnotmobile:x}` and `{ifsearch:x}` become `x`, and any other placeholder (including `{_custom}`) is removed.
- Soft 404 phrases are plain substring matches on the first 512 KB of the page. Add a site's own wording with `--fail-text`. A product page that mentions "out of stock" for one variant will match; check the page before pausing anything.
- Some sites block unknown bots or rate-limit. A 401, 403 or 429 is reported as "blocked": a medium, low-confidence item asking a person to open the page, never a critical broken-page item.
- The User-Agent is `gads-skills-link-checker/1.0`. Never add an email or account name.

## Ad testing

Within each ad group with 2+ enabled, non-disapproved RSAs, `audit_rsas.py` compares the leader against every other ad on two rates:

- CTR = clicks / impressions
- conversions per impression = conversions / impressions (CTR x conversion rate, the number that matters)

Method: two-proportion z-test, two-sided. z = (p1 - p2) / sqrt(p (1 - p) (1/n1 + 1/n2)), with p the pooled rate. Defaults: 1,000+ impressions per ad (`--min-test-impr`), 95% confidence (`--confidence`).

- A winner is reported only when the leader beats the other ad at the confidence level. Anything else reads "not enough data", including ad groups where fewer than two ads have the minimum impressions.
- A pause proposal needs a significant conversions-per-impression result and 30+ conversions on the winner (`--min-winner-conv`). A CTR win alone never triggers a pause: a curiosity headline can lift CTR and lower conversion rate.
- With 3 ads, the leader is tested twice, so the chance of a false winner rises a little. Treat a borderline p near 0.05 as "not enough data".
- After pausing a loser, write a new challenger so the ad group keeps two RSAs.

## Measuring a rewrite

- Wait about 2 weeks and a few thousand impressions per ad, then read `rsa_tests.csv`.
- Keep the old ad paused rather than removed so the comparison stays available.
