# gads shared runtime

Every `gads-*` skill ships an identical `scripts/gads.py`. It keeps each skill self-contained while giving all six the same auth, data loading and approval queue.

## The six skills

| Skill | Bot | Main script |
|---|---|---|
| gads-search-terms | search terms bot | `mine_search_terms.py` |
| gads-budget-pacing | budget bot | `pace_budgets.py` |
| gads-rsa-audit | ads bot | `audit_rsas.py` |
| gads-bid-audit | bids bot | `audit_bids.py` |
| gads-tracking-audit | tracking bot | `audit_conversions.py`, `check_site_tags.py` |
| gads-performance-report | report bot | `build_report.py` |

Run any subset. Each writes `gads-out/<customer_id>/<YYYY-MM-DD>/<skill>.json` and `.md`, then rebuilds `approvals.md` in that folder from every skill that ran that day. That file is the single approval list. Suggested overnight order: tracking first (bad conversion data poisons every other bot), then search terms, budget, bids, ads, report last (it rolls up the queue).

## Python

- Live API mode needs the `google-ads` library: `PY="$HOME/Library/Application Support/pipx/venvs/google-ads-mcp/bin/python"`.
- Offline CSV mode is pure stdlib: any `python3`.

## Auth

Developer tokens were sunset on 2026-09-09; API access now belongs to the Google Cloud project that owns the OAuth client. The runtime uses Application Default Credentials:

```bash
gcloud auth application-default login \
  --client-id-file="$HOME/.config/google-ads/client_secret.json" \
  --scopes=https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform
export GOOGLE_ADS_LOGIN_CUSTOMER_ID=<manager id>   # only when access runs through an MCC
```

Alternative: `GOOGLE_ADS_CONFIGURATION_FILE_PATH=/path/google-ads.yaml`. Client ad accounts are always accessed directly, never through treg.

## Helpers

```bash
"$PY" gads.py accounts                 # accessible customer IDs
"$PY" gads.py whoami -c 1234567890     # name, currency, time zone, optimization score
"$PY" gads.py profile-init -c 1234567890
"$PY" gads.py profile-show -c 1234567890
python3 gads.py rebuild-queue --dir gads-out/1234567890/2026-11-03
```

## Account profile

`~/.config/gads-skills/accounts/<customer_id>.json` (offline runs use `offline.json`). Fields: `brand_terms`, `competitor_terms`, `never_negate`, `irrelevant_themes`, `target_cpa`, `target_roas`, `monthly_budget`, `campaign_budgets`, `primary_conversions`, `site_urls`, `notes`. Skills read it; CLI flags override it. Never fill it with guesses. Ask the user.

## Approval item schema

`skill, kind, action, why, entity, impact_monthly, impact_label (saves|protects|gains|fixes measurement), confidence, risk, evidence, change, files, severity (critical|high|medium|low), id, status`. The queue sorts by severity, then monthly impact. IDs are stable hashes, so the same finding on the next run keeps its ID.

## Safety

- Every skill is read-only by default.
- Apply scripts validate with `validate_only=True` unless `--execute` is passed, and Claude passes `--execute` only after the user approves those specific items in the conversation.
- Editing `gads.py`: change it in one skill, then copy it to all six so they stay identical (`RUNTIME_VERSION` must match).
