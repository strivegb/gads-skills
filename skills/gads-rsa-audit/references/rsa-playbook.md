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
- If a slot must be pinned, pin 2-3 alternatives to it, not one.
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

## Ads per ad group

- 1 RSA: fine for low-volume ad groups, but no message test is running. Suggest a second RSA with a different lead angle.
- 2-3 RSAs: normal. More splits impressions and slows learning.

## Measuring a rewrite

- Wait about 2 weeks and a few thousand impressions per ad.
- Compare conversions per impression (CTR x CVR), not CTR alone. A curiosity headline can lift CTR and lower conversion rate.
- Keep the old ad paused, not removed, so the comparison stays available.
