---
name: gads-search-terms
description: |
  Google Ads search terms bot. Reads every search term in a window, flags junk (zero-conversion spend, job seekers, freebies, research, support/login, custom themes), builds a deduplicated negative keyword list with the right match type and level, harvests converting terms as new keywords, and drops everything into the shared approval queue. Nothing changes in the account until the user says yes.

  Triggers: search terms report, search term mining, negative keywords, negative keyword list, wasted spend, junk queries, irrelevant clicks, query mining, n-gram analysis, keyword harvesting, "what are we paying for", Google Ads waste audit.
---

# Google Ads search terms bot

Goal: find the spend that buys nothing and turn it into a clean, conflict-free negative list a human can approve in one pass. Secondary goal: find converting queries that deserve their own keyword.

Output lands in `gads-out/<customer_id>/<date>/` and merges into `approvals.md`, the single queue shared by all `gads-*` skills.

## Setup (once per machine, once per account)

```bash
PY="$HOME/Library/Application Support/pipx/venvs/google-ads-mcp/bin/python"   # has google-ads
S="$HOME/.claude/skills/gads-search-terms/scripts"
"$PY" "$S/gads.py" accounts                     # lists accessible customer IDs; fails fast if auth expired
"$PY" "$S/gads.py" profile-init -c 1234567890   # then edit ~/.config/gads-skills/accounts/1234567890.json
```

Auth is Application Default Credentials with the adwords scope (developer tokens were sunset 2026-09-09; access sits on the Cloud project). If `gads.py accounts` says auth expired, ask the user to run the `gcloud auth application-default login ...` command it prints (suggest `! <command>`). Access via a manager account needs `GOOGLE_ADS_LOGIN_CUSTOMER_ID`. Never route client accounts through treg.

**Fill the profile before the first run.** `brand_terms`, `competitor_terms` and `never_negate` decide what is protected. `target_cpa` sets the waste thresholds; without it the account's actual CPA is used. Ask the user for brand and competitor names if the profile is empty. Do not guess them.

No API access? Use offline mode with UI exports: Search terms report (Insights and reports > Search terms, all columns incl. Campaign, Ad group, Keyword, Cost, Conversions), optionally the Keywords report for conflict checks, the same Search terms report over the longest range available (ideally 12 months) for `--history`, and one segmented by Day (last 4-5 weeks) for `--daily`.

## Workflow

1. **Run the miner.**
   ```bash
   "$PY" "$S/mine_search_terms.py" -c 1234567890 --days 30          # add --pmax for Performance Max terms
   python3 "$S/mine_search_terms.py" --csv terms.csv --keywords keywords.csv \
     --history terms_12m.csv --daily terms_by_day.csv --currency USD   # offline
   ```
   Live mode also pulls 365 days of converting queries, 35 days of daily terms and PMax search categories. Offline without `--history` or `--daily`, those checks shrink or skip and the report says so.
   30 days for accounts spending above ~$10K/mo, 60-90 days for smaller ones so thresholds have enough clicks.

2. **Judge the ambiguous terms yourself.** Open `ambiguous.csv`. These are terms with real spend and no conversions that the rules could not call: competitor names not in the profile, research phrasing, bargain/DIY hints, odd geographies. Read the profile and, if unsure what the business sells, fetch the main landing page. For each row fill `label` (`negate` or `keep`), `match_type` (`EXACT` default, `PHRASE` when the junk part is a reusable phrase) and a short `reason`. Save as `llm_labels.csv` in the same folder. When unsure, `keep`. A wrong negative silently kills demand; a missed negative costs a few dollars.

3. **Re-run with labels** so the queue reflects your judgment:
   ```bash
   "$PY" "$S/mine_search_terms.py" -c 1234567890 --days 30 --labels gads-out/1234567890/<date>/llm_labels.csv
   ```

4. **Sanity check before presenting.** Read `negatives.csv` top to bottom. Kill any row that:
   - contains a product, category or use-case word the business sells (the script protects live keywords and converting terms; it cannot know products the account does not bid on yet)
   - is a single generic word as a phrase or broad negative unless it is unambiguous junk (`jobs`, `torrent`)
   - targets brand queries (brand is never negated here; brand cost questions go to `gads-bid-audit`)
   - has a `blocks_example` that looks like a buyer, or a `spares_example` that shows the boundary in the wrong place

   Read the "Held back" tables too. Those negatives would block a live keyword or a query that converted in the history window. Leave them out unless the user decides otherwise.

5. **Present** `gads-search-terms.md` in 6-10 lines: terms read, junk flagged, spend at stake per month, the top 3 themes, negatives held back (and the history window used), trending terms worth watching. Point to `approvals.md`.

6. **On approval**, either hand over `negatives.csv` for Google Ads Editor (the `keyword` column is already in `[exact]` / `"phrase"` / broad syntax) or apply via API. Remove rejected rows first, then validate, then execute with the token:
   ```bash
   A=("$S/apply_negatives.py" -c 1234567890 --file <dir>/negatives.csv --shared-list "Negatives - Junk themes")
   "$PY" "${A[@]}"                                   # validate only; prints a token
   "$PY" "${A[@]}" --execute --confirm <token>       # write, then read back and log
   ```
   Any edit to the CSV after validation changes the token and the script refuses until you validate again. `--attach-all-search` attaches the shared list to every enabled Search campaign. Never pass `--execute` without an explicit yes for those items in this conversation. Report any read-back mismatches to the user.

## How the miner decides

| Signal | Rule | Confidence |
|---|---|---|
| Junk theme lexicon | jobs, free/piracy, education/research, support/login, adult, plus profile `irrelevant_themes` | high |
| Zero-conversion spend | cost >= 1x target CPA **and** P(0 conv by chance at account CVR) < 10% | high |
| Click volume | cost >= 0.5x CPA and P(0 conv) < 20% | medium |
| Bargain, DIY, unknown | routed to `ambiguous.csv` for your judgment | n/a |
| Converting term with no keyword yet | conv >= 2 and CPA <= 1.1x target | harvest |
| Over-blocking | proposed negative would block a query that converted in the history window | held back, listed |
| Trending | 0 conv in last 7 days, daily cost >= 2x the earlier rate, cost >= 0.25x CPA | watch list; approval if 7-day cost >= 1x CPA |
| PMax category growing | 100+ impr, >= 1.5x prior impr, 0 conv (live only) | report only |

P(0 conv) = (1 - account CVR)^clicks. It stops the bot from punishing a term for 6 clicks of bad luck.

Then: n-gram roll-up (1-3 grams) with greedy set cover, so one phrase negative replaces dozens of exacts and two grams never cover the same terms. Lexicon words seen in 2+ terms go to the shared list. Every candidate is checked against live keywords, converting terms, brand and `never_negate`, then against converting queries from the history window (365 days live, `--history` offline). A negative that would block any of them is held back and listed; for themes the next safe gram takes its place.

Match type and level rules, the new checks in detail, close-variant gotchas, limits and PMax caveats: `references/negative-keyword-playbook.md`.

## Output contract

| File | Use |
|---|---|
| `gads-search-terms.md` | human summary |
| `gads-search-terms.md` sections | theme and exact negatives, held back (keyword conflicts, converting queries), trending watch list, PMax categories |
| `negatives.csv` | level, campaign, campaign_id, keyword (Editor syntax), text, match_type, reason, cost, clicks, conv, terms_covered, blocks_example, spares_example |
| `harvest.csv` | converting terms to add as `[exact]` keywords |
| `ambiguous.csv` | your review queue |
| `ngrams.csv` | full 1-3 gram cost table, useful for theme spotting |
| `approvals.md` | merged queue across all gads-* skills |
| `negatives.csv.validated.json` | written by a passing validate run; holds the token |
| `gads-out/<cid>/changes.log.jsonl` | one line per executed write, with read-back mismatches |

## Rules

- Read-only by default. Writes only through `apply_negatives.py --execute --confirm <token>` after a validate run and a yes.
- Report savings as "spend at stake". They are an upper bound, because blocked budget often moves to the next query.
- Hidden terms: Google omits low-volume queries, so the report never covers 100% of spend. Say so if coverage looks low (compare report cost to campaign cost).
- Never negate brand, never negate a term with conversions, never invent business context.
- Plain language in the summary. No em dashes.
