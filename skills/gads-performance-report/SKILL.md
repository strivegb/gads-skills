---
name: gads-performance-report
description: |
  Google Ads report bot. Pulls spend, conversions, CPA, value and ROAS for the current period against the previous one (week over week, 30 vs 30, month to date or year over year), finds the top 3 campaigns that actually moved the account, explains why each moved (click volume vs conversion rate, CPC vs CVR, budget vs rank impression share) and ends every insight with a start / stop / continue / double down action. Writes a markdown summary and a self-contained HTML report, and rolls up how many gads-* approvals are waiting.

  Triggers: Google Ads report, weekly Google Ads report, performance report, what changed in Google Ads, top movers, why did CPA go up, ROAS dropped, week over week, month to date, client report, Google Ads summary, spend CPA ROAS.
---

# Google Ads report bot

Goal: tell a busy marketer in under a minute what changed, why, and what to do about it. Describing numbers is not the job; deciding is.

Output lands in `gads-out/<customer_id>/<date>/` next to the other `gads-*` results, so the report can say how many decisions are already waiting in `approvals.md`. Run it last in an overnight sequence.

## Setup (once per machine, once per account)

```bash
PY="$HOME/Library/Application Support/pipx/venvs/google-ads-mcp/bin/python"   # has google-ads
S="$HOME/.claude/skills/gads-performance-report/scripts"
"$PY" "$S/gads.py" accounts                     # lists accessible customer IDs; fails fast if auth expired
"$PY" "$S/gads.py" profile-init -c 1234567890   # then edit ~/.config/gads-skills/accounts/1234567890.json
```

Auth is Application Default Credentials with the adwords scope (developer tokens were sunset 2026-09-09; access sits on the Cloud project). If `gads.py accounts` says auth expired, ask the user to run the `gcloud auth application-default login ...` command it prints (suggest `! <command>`). Access via a manager account needs `GOOGLE_ADS_LOGIN_CUSTOMER_ID`. Never route client accounts through treg.

**Profile fields that matter here:** `target_cpa` or `target_roas` (the goal the actions are judged against; without one the report compares to last period with a 15% tolerance) and `name`.

No API access? Offline mode takes UI exports from Campaigns with columns Campaign, Campaign type, Impr., Clicks, Cost, Conversions, Conv. value, Search impr. share, Search lost IS (budget), Search lost IS (rank). Either one export segmented by **Day** covering both periods, or two exports (current first, previous second). Title rows carry the date range and are read automatically.

## Workflow

1. **Run the other gads-* skills first** if this is an overnight or weekly sweep, so the queue count is complete.

2. **Run the report.**
   ```bash
   "$PY" "$S/build_report.py" -c 1234567890                     # last 7 days vs prior 7
   "$PY" "$S/build_report.py" -c 1234567890 --days 30           # 30 vs 30
   "$PY" "$S/build_report.py" -c 1234567890 --compare mtd       # month to date vs same days last month
   "$PY" "$S/build_report.py" -c 1234567890 --compare yoy       # weekday-aligned year over year
   python3 "$S/build_report.py" --csv campaigns_by_day.csv --currency USD          # offline
   ```
   `--goal cpa|roas` overrides the auto choice (ROAS when a ROAS target is set or value is a real share of spend). `--top 5` writes up more movers.

3. **Read the movers before presenting.** The script finds and decomposes them; you check the story holds:
   - A conversion-rate collapse with steady clicks is usually tracking or a landing page, not demand. Cross-check with `gads-tracking-audit` before advising bid changes.
   - Budget-lost impression share rising alongside good efficiency is a budget problem (`gads-budget-pacing`). Rank-lost rising is bids or ad quality (`gads-bid-audit`, `gads-rsa-audit`).
   - If the change log explains a move (new campaign, target change, paused ad group), say so. Pull it with `gads.query` on `change_event` when the user asks why.
   - Seasonality, promotions and launches are the user's context. Ask if a move looks calendar driven.

4. **Present** in this shape, no more than 10 lines: one headline sentence, the 3 movers with their action verb, the queue count, one caveat if conversion lag applies. Link `report.html` for the full page. Publish it as an Artifact if the user wants to share it with a client or team.

## How movers are chosen

| Step | Rule |
|---|---|
| Primary outcome | conversions (CPA goal) or conversion value (ROAS goal) |
| Ranking | share of the account's gross movement in the primary outcome or in spend, whichever is larger |
| Materiality | at least 15% of gross movement (`--materiality`) **and** larger than 1.5 standard deviations of Poisson noise on conversions |
| Decomposition | Δconv = Δclicks × CVR(prev) + clicks(cur) × ΔCVR. CPA and ROAS changes split by log share into CPC, CVR and AOV |
| Context | search impression share, lost to budget, lost to rank, prev vs cur |

Action rules (judged against the profile target, or last period ±15%):

| Pattern | Verb |
|---|---|
| Spent with zero conversions after converting last period | Start: check tracking |
| Growing at goal efficiency and losing 10%+ IS to budget | Double down: more budget |
| Growing at goal efficiency | Continue |
| Growing below goal efficiency | Stop: tighten target or cap budget |
| Falling on volume, budget-lost IS up | Start: restore budget or pacing |
| Falling on volume, rank-lost IS up | Start: fix bids and ad rank |
| Falling on conversion rate | Start: check landing page and tracking |
| More spend, no more results | Stop: mine search terms, hold budget |

Approvals from this skill are "investigate" items only (conversion-rate collapse on a mover, account-wide drop with no single cause). The report never proposes account changes itself; the other gads-* skills do.

Metric definitions, comparison windows, conversion lag and how to write the narrative: `references/reporting-playbook.md`.

## Output contract

| File | Use |
|---|---|
| `gads-performance-report.md` | KPI table, movers with actions, campaign table, queue count, notes |
| `report.html` | self-contained client-ready page (light and dark, phone friendly, no external assets) |
| `campaigns.csv` | campaign metrics for both periods plus decomposition columns |
| `gads-performance-report.json` | summary and investigate items, merged into `approvals.md` |

## Rules

- Read-only. This skill never changes the account.
- Rank by contribution, never by % change. A campaign going from 1 to 3 conversions is +200% and irrelevant.
- Say when the current period is still collecting delayed conversions. Do not call a drop on the last 1-3 days a trend.
- Optimization score is Google's recommendation adoption score. Report it, never present it as performance.
- Every insight ends with start, stop, continue or double down, tied to the goal. No insight without an action.
- Plain language. No em dashes. Never invent benchmarks; compare to the account's own history and targets.
