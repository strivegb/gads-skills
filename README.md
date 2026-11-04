# gads-skills

Six Claude Code skills that run a Google Ads account the way a careful PPC manager would: each one audits one area, and all of them write into a single approval list. Nothing changes in the account until a person says yes.

| Skill | What it does |
|---|---|
| `gads-search-terms` | Finds search terms that spend without converting, builds a deduplicated negative keyword list, checks each negative against a year of converting queries, harvests winners as keywords, and flags trending junk. |
| `gads-budget-pacing` | Projects month-end spend per campaign, flags overdelivery and campaigns that will hit their cap early, and proposes budget shifts toward campaigns limited by budget with good CPA or ROAS. |
| `gads-rsa-audit` | Audits responsive search ads (ad strength, asset counts, pinning, duplicates, policy status), checks every landing page, tests ads against each other for significance, and drafts paused replacement copy. |
| `gads-bid-audit` | Checks every bid strategy against reality: targets vs actuals, conversion volume, brand overbidding, brand mixed into non-brand, Search Partners drag, with simulator previews of proposed targets. |
| `gads-tracking-audit` | Audits conversion actions (duplicates, counting type, micro-conversions as primary, silent actions adjusted for the account's own conversion lag) and checks the site for the right tags. |
| `gads-performance-report` | Period-over-period report with top movers, a same-weekday anomaly check, zero-impression alerts and change history tied to each mover. Writes markdown and a self-contained HTML report. |

Every run writes to `gads-out/<customer_id>/<date>/` and rebuilds `approvals.md`, ranked by severity and then by monthly dollar impact.

## Install

```bash
git clone <this repo> ~/gads-skills
~/gads-skills/bin/install        # copies the skills into ~/.claude/skills
```

Then ask Claude for any of the jobs above, for example "run the search terms bot on 123-456-7890".

## Data access

- **Live:** Google Ads API v25 with the `google-ads` Python library and Application Default Credentials. Developer tokens were sunset on 2026-09-09, so access belongs to the Cloud project. See `shared/SHARED_RUNTIME.md`.
- **Offline:** Google Ads UI CSV exports. Pure standard library Python.

## Safety

- Read-only by default.
- Writes go through `apply_*.py`: validate first (prints a token), then `--execute --confirm <token>` after approval. Editing the plan file after validation invalidates the token.
- Budget and bid target changes above 20% need `--allow-large --reason`.
- After every write the script reads the account back, reports anything that did not land, and appends to `changes.log.jsonl`.
- New ads are exported paused.

## Develop

```bash
bin/test          # offline tests for every skill + shared runtime sync check
bin/check-gaql    # every GAQL field exists in the installed API version
bin/sync-shared   # copy shared/gads.py into each skill after editing it
bin/install       # install into ~/.claude/skills
```

`shared/gads.py` is the single source of the runtime. Each skill carries an identical copy so it stays self-contained.

## Status

Tested offline against fixtures. Live API mode has not been run against a real account yet. Fixtures contain the problems on purpose, so tests prove detection works; they do not prove accuracy on real accounts.
