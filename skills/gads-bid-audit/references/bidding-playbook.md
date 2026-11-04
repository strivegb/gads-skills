# Bidding playbook

Reference for `gads-bid-audit`. Load when choosing a target, explaining a recommendation, or handling an edge case.

## Strategy cheat sheet

| Strategy | Optimises for | Use when | Watch |
|---|---|---|---|
| Maximize conversions (no target) | most conversions for the budget | new campaign, building history | CPA drifts up as budget grows |
| Maximize conversions + target CPA | conversions at an average CPA | steady volume, known CPA goal | target too tight chokes volume |
| Maximize conversion value + target ROAS | value at an average ROAS | real, varied conversion values | needs reliable values; low volume makes it erratic |
| Target impression share | showing in a position | brand defence, very small budgets | always set a max CPC ceiling |
| Manual CPC | your bids | low volume, tight control | Enhanced CPC no longer adjusts Search bids; treat eCPC as manual |
| Maximize clicks | clicks | traffic tests only | not an outcome strategy |

## Volume thresholds (guidance, not policy)

Commonly cited: about 30 conversions in 30 days for target CPA, about 50 for target ROAS. Google's own help pages have changed wording over time. Check the current "About Target CPA bidding" and "About Target ROAS bidding" pages before quoting a number to a client. Below threshold: pool similar campaigns in a portfolio strategy, optimise to a higher-volume conversion action, or run Maximize conversions without a target.

## Reading target vs actual

| Pattern | Meaning | Move |
|---|---|---|
| Target CPA < 0.8x actual, lost IS (rank) high, budget not limiting | constrained, bidder cannot win auctions at that price | raise target up to 15% |
| Target CPA > 1.25x actual | loose, the target is not steering | lower toward actual, up to 15% |
| Actual above business target, target already at or under actual | the account cannot hit the goal at current volume | step toward business target, 15% per change; expect less volume |
| Lost IS (budget) high | budget, not bids, is the limit | send to `gads-budget-pacing`, do not touch the target |
| 7-day CPA much worse than 30-day | recent shock (season, tracking, competitor) | investigate before tightening |

ROAS is the mirror image: higher target = tighter.

## Change discipline

- One lever per campaign at a time: target, budget or conversion goal. Not two.
- Move targets in steps of 15-20% at most. Larger jumps behave like a new strategy and reset learning.
- Wait about two weeks, or three conversion-lag cycles for long sales cycles, before the next move.
- The skill reads `change_event` for the last 14 days and holds recommendations on campaigns that changed recently or show a LEARNING status.
- Portfolio strategies: a target change moves every campaign in the portfolio. Recommend it, but never apply it from a single-campaign view.

## Brand overbidding

Signs:
- Brand impression share already 85%+ (absolute top 75%+), yet brand CPC rose 25%+ against a baseline period.
- Brand on Maximize conversions or target CPA with no CPC ceiling. Smart Bidding sees brand clicks convert and bids up for clicks the account would mostly win anyway.
- Brand CPC above half of non-brand CPC (heuristic only; used when no baseline is available, flagged low confidence).

Fix: Target impression share, absolute top of page, 90-95%, with a max CPC ceiling around 1.2x the reference CPC. Check Auction insights first. A new competitor bidding on the brand is a real reason for higher CPCs, and cutting the ceiling then hands them the top slot.

Overspend estimate = (current brand CPC - reference CPC) x brand clicks. Reference CPC comes from the profile `brand_max_cpc`, else a baseline period (live: the 30 days ending 60 days before the window), else the heuristic.

## Brand mixed into non-brand

Brand keywords inside a non-brand campaign make its CPA look better than it is, so its target is set on the wrong number. When brand is 15%+ of a campaign's spend, the skill reports the true non-brand CPA and holds any target change until brand is split out and added as a negative there.

## Search Partners

Search Partners shows Search ads on non-Google sites. Quality varies by account, so the bot compares it with Google Search inside each Search campaign (live: `segments.ad_network_type`; offline: a Campaigns report segmented by Network (with search partners)).

It flags a campaign when partners have enough data (100+ clicks and 5%+ of the campaign's spend) and are materially worse: CPA at 1.5x Google Search or more, ROAS at 0.67x or less, or no conversions on spend worth two Google Search conversions. The saving is partners spend minus what its conversions would have cost on Google Search; the item also states the conversions given up, since Google Search will recover only part of them. The change is `campaign.network_settings.target_search_network = false`, made in Settings > Networks or Editor.

Check before turning it off: lead quality from partners (offline conversion imports may tell a different story), and whether the campaign is volume-starved, where partners can be worth a higher CPA.

## Simulators

Live runs read `campaign_simulation` (type TARGET_CPA with `target_cpa_point_list.points`, type TARGET_ROAS with `target_roas_point_list.points`), plus `start_date` and `end_date`. For each proposed target the bot interpolates cost, conversions and value at the current target and at the proposed one, and puts both in the why text and the item evidence. Outside the simulated range it uses the nearest point and says so; if the simulator does not reach the proposed target at all, it adds nothing. The query is soft: an error or a campaign without simulations just means no preview. Simulators are Google's model and the numbers are directional. Quote them as "Google's simulator estimates".

## Applying changes

`apply_targets.py` uses the shared write gate: validate (prints a token), then `--execute --confirm <token>`. Moves above 20% need `--allow-large --reason "<why>"`. After writing it reads targets back and logs every change with the reason.
