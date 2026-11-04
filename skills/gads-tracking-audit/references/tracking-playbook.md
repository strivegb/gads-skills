# Conversion tracking playbook

Reference for `gads-tracking-audit`. Load when explaining a finding or choosing a fix.

## Primary vs secondary, and what "Conversions" means

- `primary_for_goal = true` puts the action in its goal; if that goal is used for account-default bidding (biddable), the action counts in the **Conversions** column and Smart Bidding optimizes toward it.
- Secondary actions show only in **All conv.** They are for observation.
- `include_in_conversions_metric` is the effective result. The audit treats an action as counted when it is enabled and primary or included.
- Campaign-specific goals can override account defaults. If a campaign uses a custom goal, an account-level fix may not change that campaign. Check Campaign settings > Goals.

## Category cheat sheet

| Category | Typical use | Counting | Primary? |
|---|---|---|---|
| Purchase | completed order | Every | yes |
| Submit lead form, Contact, Book appointment, Request quote, Sign-up | lead | One | yes |
| Qualified lead, Converted lead (offline import) | CRM stages | One | yes, ideally the bidding target once volume allows |
| Phone call lead | calls from ads or site | One | yes if calls matter |
| Add to cart, Begin checkout | funnel step | Every | no |
| Page view, Engagement, Outbound click | micro signal | One | no |

## Duplicate patterns and fixes

| Pattern | Symptom | Fix |
|---|---|---|
| Google Ads tag purchase + GA4 purchase import both primary | Conversions roughly 2x orders; daily counts move together | Keep one primary (usually the Ads tag), set the other secondary |
| gtag hard-coded + same tag in GTM | each page view sends two hits | remove one installation |
| Same conversion event fired twice on the thank-you page (reload, SPA route change) | conversions per order > 1 on "Every" | fire once per transaction; pass `transaction_id` so Google dedupes |
| Lead form tag + GA4 generate_lead import | lead counts doubled | keep one primary |
| Offline import + online tag for the same lead | both primary | online for volume (secondary) and offline qualified lead primary, or vice versa, never both |

Always pass `transaction_id` on purchase tags. Google dedupes conversions with the same ID for the same action.

## Silent or dropped actions: triage order

1. What changed on the site on the last conversion day? (release, form vendor change, checkout migration, consent banner change)
2. Does the thank-you URL still exist? Did it move to a modal or SPA route that never loads the tag?
3. GTM: was the conversion tag paused, its trigger edited, or a new container version published?
4. Consent: a new banner defaulting `ad_storage` to denied without Advanced consent mode drops tags entirely.
5. Is the action's status in Google Ads "No recent conversions" or "Unverified"?

While an action is silent, Smart Bidding sees fewer conversions and can lower bids sharply. Consider pausing target changes and flag it to the user before anything else.

## Values

- Purchase actions should pass the dynamic order value and currency. Always-default values make tROAS bidding blind.
- Lead actions can use a static value, ideally set from close rate x average deal value, and differentiated by lead type if several exist.

## Attribution

- Data-driven is the default and the only non-last-click model for new actions since 2023 (first click, linear, time decay and position-based were retired).
- Switching from last click to data-driven shifts credit from brand to generic campaigns. Warn the user that campaign-level conversion counts will move.

## Enhanced conversions and consent mode

- Enhanced conversions (web) send hashed first-party data (email, phone) with the conversion. Typical recovery is meaningful for accounts with consent or cookie loss. Requires accepting customer data terms.
- Enhanced conversions for leads lets offline lead imports match on hashed email instead of GCLID. Needed for reliable CRM qualified-lead imports.
- Consent mode v2 is required for EEA/UK traffic. Advanced mode (tags load, send cookieless pings when denied) allows conversion modeling; basic mode (tags blocked until consent) does not.
- Static HTML often hides consent setup inside a CMP script or GTM template; "no consent default in HTML" is a prompt to check, not a finding by itself.

## Tag anatomy (what the site check looks for)

- Google tag: `googletagmanager.com/gtag/js?id=AW-XXXXXXXXX` (or G-/GT- ID), then `gtag('config', 'AW-XXXXXXXXX')`.
- Conversion event: `gtag('event', 'conversion', {'send_to': 'AW-XXXXXXXXX/LabelAbC', 'value': 1.0, 'currency': 'USD', 'transaction_id': '...'})`.
- GTM: `googletagmanager.com/gtm.js?id=GTM-XXXX`. In the container, Ads conversion tags are `__awct` with `vtp_conversionId` and `vtp_conversionLabel`; the conversion linker is `__gclidw`.
- Network hits that prove firing: `googleadservices.com/pagead/conversion/<id>/?label=<label>`, `google.com/pagead/1p-conversion/...`, `google.com/ccm/collect`, GA4 `.../g/collect?...&en=purchase`.

## Data freshness

- Conversion delay: conversions are reported on the click date, so recent days keep filling in. The lag is account and action specific: web leads often land within a day, purchases with long consideration and offline imports take a week or more.
- How the audit measures it (live): `segments.conversion_lag_bucket` (days from click to conversion) with `metrics.all_conversions`, per action, over 90 days. The lag is the upper edge of the bucket where the running share reaches 90%. Under 20 conversions in 90 days keeps the default of 3 days. Skipped days are capped at 14 (`--max-lag`) so the check stays timely; within that, expected counts are also scaled by the completion curve.
- Offline: no lag data in UI exports, so the default is 3 days. Set `--lag-days` from the Conversions > action > "Days to conversion" segment if you know it.

## Silent vs quiet

- The silent check asks how likely zero is at the action's usual rate: P(0 in N days) = exp(-rate x N). At 0.4 a day over 7 days that is about 6%, which happens; at 4 a day it is under 0.01%, which does not. Only results below 1% (`--silent-p`) are flagged.
- An action reported as "not flagged" may still be broken. If it matters (a low-volume qualified lead, say), check it by hand in Tag Assistant after a test conversion.
- Modeled conversions (consent mode) are added with a delay.
- Offline imports arrive on the import schedule; a "silent" offline action may just be a missed upload.
