#!/usr/bin/env python3
"""
audit_rsas.py - find weak responsive search ads and build a rewrite brief.

Live:     $PY audit_rsas.py -c 123-456-7890 --days 30
Offline:  python3 audit_rsas.py --csv ads.csv [--assets assets.csv] [--keywords keywords.csv] --currency USD

Outputs (gads-out/<cid>/<date>/):
  gads-rsa-audit.json / .md   summary + approval items
  rsa_findings.csv            one row per enabled RSA with every issue found
  rsa_brief.json              per-ad rewrite brief for Claude (keep / replace / slots to fill)
  rsa_assets.csv              asset-level labels and metrics
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gads  # noqa: E402
from gads import Approval, micros, money, num  # noqa: E402
from copyrules import check_text, key, similar, toks  # noqa: E402

SKILL = "gads-rsa-audit"
STRENGTH_RANK = {"POOR": 0, "AVERAGE": 1, "GOOD": 2, "EXCELLENT": 3}


# ---------------------------------------------------------------- load

def _pin(v) -> str:
    v = str(v or "").upper()
    if v in ("", "UNSPECIFIED", "UNKNOWN", "NONE", "-", "--"):
        return ""
    # UI exports say "1", "Headline 1" or "Position 1"; API says HEADLINE_1
    digits = "".join(ch for ch in v if ch.isdigit())
    if "DESCRIPTION" in v:
        return f"DESCRIPTION_{digits}"
    return f"HEADLINE_{digits}" if digits else v


def load_live(cid: str, start: str, end: str):
    w = gads.gaql_dates(start, end)
    base = """campaign.status = 'ENABLED' AND ad_group.status = 'ENABLED'
              AND ad_group_ad.status = 'ENABLED' AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'"""
    attrs = gads.query(cid, f"""
        SELECT campaign.id, campaign.name, ad_group.id, ad_group.name, ad_group_ad.ad.id,
               ad_group_ad.ad_strength, ad_group_ad.action_items,
               ad_group_ad.ad.responsive_search_ad.headlines,
               ad_group_ad.ad.responsive_search_ad.descriptions,
               ad_group_ad.ad.responsive_search_ad.path1, ad_group_ad.ad.responsive_search_ad.path2,
               ad_group_ad.ad.final_urls, ad_group_ad.policy_summary.approval_status,
               ad_group_ad.primary_status, ad_group_ad.primary_status_reasons
        FROM ad_group_ad WHERE {base}""")
    mets = {str(r.get("ad_group_ad.ad.id")): r for r in gads.query(cid, f"""
        SELECT ad_group_ad.ad.id, metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions
        FROM ad_group_ad WHERE {base} AND {w}""")}
    ads = []
    for r in attrs:
        aid = str(r.get("ad_group_ad.ad.id"))
        m = mets.get(aid, {})
        ads.append({
            "campaign": r.get("campaign.name", ""), "campaign_id": str(r.get("campaign.id", "")),
            "ad_group": r.get("ad_group.name", ""), "ad_group_id": str(r.get("ad_group.id", "")),
            "ad_id": aid, "strength": str(r.get("ad_group_ad.ad_strength", "")).upper(),
            "headlines": [{"text": h.get("text", ""), "pin": _pin(h.get("pinned_field"))}
                          for h in r.get("ad_group_ad.ad.responsive_search_ad.headlines") or []],
            "descriptions": [{"text": d.get("text", ""), "pin": _pin(d.get("pinned_field"))}
                             for d in r.get("ad_group_ad.ad.responsive_search_ad.descriptions") or []],
            "path1": r.get("ad_group_ad.ad.responsive_search_ad.path1", "") or "",
            "path2": r.get("ad_group_ad.ad.responsive_search_ad.path2", "") or "",
            "final_url": (r.get("ad_group_ad.ad.final_urls") or [""])[0],
            "approval": str(r.get("ad_group_ad.policy_summary.approval_status", "")).upper(),
            "primary_status": str(r.get("ad_group_ad.primary_status", "")).upper(),
            "status_reasons": r.get("ad_group_ad.primary_status_reasons") or [],
            "action_items": r.get("ad_group_ad.action_items") or [],
            "impr": num(m.get("metrics.impressions")), "clicks": num(m.get("metrics.clicks")),
            "cost": micros(m.get("metrics.cost_micros")), "conv": num(m.get("metrics.conversions")),
        })
    assets = []
    for r in gads.query(cid, f"""
        SELECT ad_group_ad_asset_view.ad_group_ad, ad_group_ad_asset_view.field_type,
               ad_group_ad_asset_view.performance_label, ad_group_ad_asset_view.pinned_field,
               asset.text_asset.text, metrics.impressions, metrics.clicks, metrics.conversions
        FROM ad_group_ad_asset_view
        WHERE {w} AND ad_group_ad_asset_view.enabled = TRUE
          AND ad_group_ad_asset_view.field_type IN ('HEADLINE', 'DESCRIPTION')"""):
        rn = str(r.get("ad_group_ad_asset_view.ad_group_ad", ""))
        assets.append({
            "ad_id": rn.split("~")[-1] if "~" in rn else "",
            "field": str(r.get("ad_group_ad_asset_view.field_type", "")).upper(),
            "text": r.get("asset.text_asset.text", ""),
            "label": str(r.get("ad_group_ad_asset_view.performance_label", "")).upper(),
            "impr": num(r.get("metrics.impressions")), "clicks": num(r.get("metrics.clicks")),
            "conv": num(r.get("metrics.conversions")),
        })
    kws = defaultdict(list)
    for r in gads.query(cid, f"""
        SELECT ad_group.id, ad_group_criterion.keyword.text, metrics.impressions
        FROM keyword_view
        WHERE {w} AND ad_group_criterion.status = 'ENABLED' AND ad_group_criterion.negative = FALSE
          AND campaign.status = 'ENABLED' AND ad_group.status = 'ENABLED'
        ORDER BY metrics.impressions DESC"""):
        kws[str(r.get("ad_group.id"))].append((r.get("ad_group_criterion.keyword.text", ""), num(r.get("metrics.impressions"))))
    return ads, assets, kws


def load_csv(paths, assets_csv, kw_csv):
    ads = []
    for p in paths:
        for i, r in enumerate(gads.read_ui_csv(p)):
            atype = str(gads.pick(r, "ad_type", default="responsive search ad")).lower()
            if "responsive search" not in atype:
                continue
            status = str(gads.pick(r, "ad_status", "status", default="enabled")).lower()
            if status and not status.startswith(("enabled", "eligible", "approved")):
                continue
            hs = [{"text": gads.pick(r, f"headline_{n}"), "pin": _pin(gads.pick(r, f"headline_{n}_position", f"headline_{n}_pinned"))}
                  for n in range(1, 16) if gads.pick(r, f"headline_{n}")]
            ds = [{"text": gads.pick(r, f"description_{n}"), "pin": _pin(gads.pick(r, f"description_{n}_position", f"description_{n}_pinned"))}
                  for n in range(1, 5) if gads.pick(r, f"description_{n}")]
            ds = [{**d, "pin": d["pin"].replace("HEADLINE_", "DESCRIPTION_") if d["pin"] else ""} for d in ds]
            ads.append({
                "campaign": gads.pick(r, "campaign"), "campaign_id": gads.pick(r, "campaign_id"),
                "ad_group": gads.pick(r, "ad_group"), "ad_group_id": gads.pick(r, "ad_group_id", default=gads.pick(r, "ad_group")),
                "ad_id": str(gads.pick(r, "ad_id", default=f"row{i + 1}")),
                "strength": str(gads.pick(r, "ad_strength")).upper().replace(" ", "_"),
                "headlines": hs, "descriptions": ds,
                "path1": gads.pick(r, "path_1", "path1"), "path2": gads.pick(r, "path_2", "path2"),
                "final_url": gads.pick(r, "final_url", "final_urls"),
                "approval": str(gads.pick(r, "approval_status", "policy_details", default="")).upper().replace(" ", "_"),
                "primary_status": str(gads.pick(r, "status", "ad_status", default="")).upper(),
                "status_reasons": [], "action_items": [],
                "impr": num(gads.pick(r, "impr", "impressions")), "clicks": num(gads.pick(r, "clicks")),
                "cost": num(gads.pick(r, "cost")), "conv": num(gads.pick(r, "conversions")),
            })
    assets = []
    if assets_csv:
        by_group = defaultdict(list)
        for a in ads:
            by_group[(a["campaign"], a["ad_group"])].append(a)
        for r in gads.read_ui_csv(assets_csv):
            text = gads.pick(r, "asset", "asset_text", "text")
            if not text:
                continue
            field = str(gads.pick(r, "asset_type", "field_type", "type")).upper()
            if "HEADLINE" not in field and "DESCRIPTION" not in field:
                continue
            group_ads = by_group.get((gads.pick(r, "campaign"), gads.pick(r, "ad_group")), [])
            ad_id = gads.pick(r, "ad_id")
            if not ad_id:  # attach to the ad in that ad group that carries this text
                for a in group_ads:
                    if any(key(x["text"]) == key(text) for x in a["headlines"] + a["descriptions"]):
                        ad_id = a["ad_id"]
                        break
            assets.append({"ad_id": str(ad_id), "field": "HEADLINE" if "HEADLINE" in field else "DESCRIPTION",
                           "text": text, "label": str(gads.pick(r, "performance", "performance_label", "asset_performance")).upper(),
                           "impr": num(gads.pick(r, "impr", "impressions")), "clicks": num(gads.pick(r, "clicks")),
                           "conv": num(gads.pick(r, "conversions"))})
    kws = defaultdict(list)
    if kw_csv:
        for r in gads.read_ui_csv(kw_csv):
            text = str(gads.pick(r, "keyword", "search_keyword")).strip('[]"+')
            if text:
                kws[gads.pick(r, "ad_group_id", default=gads.pick(r, "ad_group"))].append((text, num(gads.pick(r, "impr", "impressions"))))
        for k in kws:
            kws[k].sort(key=lambda x: -x[1])
    return ads, assets, kws


# ---------------------------------------------------------------- analysis

def kw_in(text: str, kw: str) -> bool:
    kt = toks(kw)
    return bool(kt) and kt.issubset(toks(text))


def audit(ads, assets, kws, args):
    assets_by_ad = defaultdict(list)
    for a in assets:
        assets_by_ad[a["ad_id"]].append(a)
    per_group = defaultdict(list)
    for a in ads:
        per_group[(a["campaign"], a["ad_group_id"])].append(a)

    results = []
    for ad in ads:
        issues, sev = [], 0  # sev: 3 critical, 2 high, 1 medium
        def flag(level, msg):
            nonlocal sev
            issues.append(msg)
            sev = max(sev, level)

        st = ad["strength"]
        if st == "POOR":
            flag(3, "Ad strength Poor")
        elif st == "AVERAGE":
            flag(2, "Ad strength Average")

        uniq_h = {key(h["text"]) for h in ad["headlines"] if h["text"]}
        uniq_d = {key(d["text"]) for d in ad["descriptions"] if d["text"]}
        if len(uniq_h) < 10:
            flag(2, f"only {len(uniq_h)} unique headlines (aim 12-15)")
        elif len(uniq_h) < 13:
            flag(1, f"{len(uniq_h)} unique headlines (room for {15 - len(uniq_h)} more)")
        if len(uniq_d) < 4:
            flag(1 if len(uniq_d) >= 2 else 2, f"only {len(uniq_d)} descriptions (aim 4)")

        pinned_h = [h for h in ad["headlines"] if h["pin"]]
        pinned_d = [d for d in ad["descriptions"] if d["pin"]]
        pos_count = defaultdict(int)
        for h in pinned_h:
            pos_count[h["pin"]] += 1
        if len(pinned_h) >= 3 or (ad["headlines"] and len(pinned_h) == len(ad["headlines"])):
            flag(2, f"over-pinned: {len(pinned_h)} headlines pinned")
        elif any(c == 1 for c in pos_count.values()) and len(pinned_h) >= 2:
            flag(1, "single headline pinned per position on 2+ positions (pin 2-3 options per slot)")
        if len(pinned_d) >= 2:
            flag(1, f"{len(pinned_d)} descriptions pinned")

        dups = []
        hs = [h["text"] for h in ad["headlines"] if h["text"]]
        for i in range(len(hs)):
            for j in range(i + 1, len(hs)):
                if similar(hs[i], hs[j]):
                    dups.append((hs[i], hs[j]))
        if dups:
            flag(1, f"{len(dups)} near-duplicate headline pair(s)")

        top_kws = [k for k, _ in kws.get(ad["ad_group_id"], [])[: args.top_keywords]]
        covered = [k for k in top_kws if any(kw_in(h, k) for h in hs)]
        if top_kws and not covered:
            flag(2, f"no top keyword in any headline (top: {top_kws[0]!r})")

        low = [a for a in assets_by_ad.get(ad["ad_id"], []) if a["label"] == "LOW" and a["impr"] >= args.min_asset_impr]
        if low:
            flag(1, f"{len(low)} asset(s) rated Low with {args.min_asset_impr}+ impressions")

        text_errs = []
        for h in ad["headlines"]:
            text_errs += [f"H '{h['text']}': {e}" for e in check_text(h["text"], "headline") if not e.startswith("too long")]
        for d in ad["descriptions"]:
            text_errs += [f"D '{d['text'][:30]}...': {e}" for e in check_text(d["text"], "description") if not e.startswith("too long")]
        if text_errs:
            flag(1, f"{len(text_errs)} editorial issue(s)")

        if not ad["path1"]:
            flag(1, "no display path")
        appr = ad["approval"]
        if "DISAPPROVED" in appr:
            flag(3, "disapproved")
        elif "LIMITED" in appr or "LIMITED" in ad["primary_status"]:
            flag(2, "approved (limited)")

        n_in_group = len(per_group[(ad["campaign"], ad["ad_group_id"])])
        if n_in_group == 1:
            flag(1, "only RSA in its ad group (add a second to test messaging)")
        elif n_in_group > 3:
            flag(1, f"{n_in_group} RSAs in ad group (max 3 enabled; impressions split thin)")

        replace, seen = [], set()
        def rep(text, field, label, impr=0):
            if key(text) not in seen:
                seen.add(key(text))
                replace.append({"text": text, "field": field, "label": label, "impr": impr})
        for a in low:
            rep(a["text"], a["field"], "LOW", a["impr"])
        for x, y in dups:
            rep(y, "HEADLINE", "DUPLICATE")
        for h in ad["headlines"]:
            if [e for e in check_text(h["text"], "headline") if not e.startswith("too long")]:
                rep(h["text"], "HEADLINE", "EDITORIAL")
        for d in ad["descriptions"]:
            if [e for e in check_text(d["text"], "description") if not e.startswith("too long")]:
                rep(d["text"], "DESCRIPTION", "EDITORIAL")
        keep_h = [h for h in ad["headlines"] if not any(key(h["text"]) == key(r["text"]) for r in replace)]
        keep_d = [d for d in ad["descriptions"] if not any(key(d["text"]) == key(r["text"]) for r in replace)]
        results.append({**ad, "issues": issues, "severity": sev, "top_keywords": top_kws,
                        "keywords_covered": covered, "low_assets": low, "duplicates": dups,
                        "editorial": text_errs, "rsas_in_group": n_in_group,
                        "replace": replace, "keep_headlines": keep_h, "keep_descriptions": keep_d,
                        "headlines_needed": max(0, args.target_headlines - len({key(h['text']) for h in keep_h})),
                        "descriptions_needed": max(0, 4 - len({key(d['text']) for d in keep_d}))})
    return results


# ---------------------------------------------------------------- output

def build_outputs(results, assets, cid, cur, days):
    flagged = [r for r in results if r["severity"] >= 2]
    poor = [r for r in results if r["strength"] == "POOR"]
    avg = [r for r in results if r["strength"] == "AVERAGE"]
    disapproved = [r for r in results if "disapproved" in r["issues"]]
    total_cost = sum(r["cost"] for r in results)
    flagged_cost = sum(r["cost"] for r in flagged)
    slots = sum(r["headlines_needed"] for r in flagged)

    approvals = []
    for r in sorted(flagged, key=lambda x: (-x["severity"], -x["cost"])):
        approvals.append(Approval(
            skill=SKILL, kind="rsa_rewrite", entity=f"{r['campaign']} > {r['ad_group']}",
            action=f"Refresh RSA in '{r['ad_group']}' ({r['strength'].title() or 'unrated'}): "
                   f"{r['headlines_needed']} new headline{'s' * (r['headlines_needed'] != 1)}, "
                   f"{r['descriptions_needed']} new description{'s' * (r['descriptions_needed'] != 1)}, replace {len(r['replace'])}",
            why="; ".join(r["issues"][:4]) + f". Ad spent {money(r['cost'], cur)} in {days}d.",
            impact_label="gains", confidence="medium" if r["strength"] in ("POOR", "AVERAGE") else "low",
            risk="low", evidence={"ad_id": r["ad_id"], "strength": r["strength"], "cost": round(r["cost"], 2),
                                  "impr": int(r["impr"]), "conv": round(r["conv"], 1)},
            change={"ad_id": r["ad_id"], "file": "rsa_drafts.csv"}, files=["rsa_brief.json", "rsa_drafts.csv"]))
    if disapproved:
        approvals.insert(0, Approval(
            skill=SKILL, kind="rsa_policy", entity="disapproved ads", risk="low", confidence="high",
            action=f"Fix {len(disapproved)} disapproved RSA(s)",
            why="Disapproved ads do not serve. Read the policy topic in Google Ads, rewrite the offending asset, resubmit.",
            impact_label="gains",
            evidence={"ad_ids": [r["ad_id"] for r in disapproved]}))

    headline = (f"Audited {len(results)} RSAs: {len(poor)} Poor, {len(avg)} Average, {len(flagged)} need a rewrite "
                f"({money(flagged_cost, cur)} of {money(total_cost, cur)} spend), {slots} headline slots to fill")

    md = [f"# RSA audit · {gads.fmt_cid(cid) or 'offline export'} · last {days} days", "", f"**{headline}.**", "",
          "## Ads to fix", ""]
    if flagged:
        md.append(gads.md_table([[r["campaign"], r["ad_group"], r["strength"].title(), money(r["cost"], cur),
                                  len({key(h['text']) for h in r['headlines']}), "; ".join(r["issues"][:3])]
                                 for r in sorted(flagged, key=lambda x: (-x["severity"], -x["cost"]))[:25]],
                                ["Campaign", "Ad group", "Strength", "Cost", "Headlines", "Top issues"]))
    else:
        md.append("_none_")
    low_assets = [a for r in results for a in r["low_assets"]]
    if low_assets:
        md += ["", "## Low-rated assets to replace", "",
               gads.md_table([[a["field"].title(), a["text"], int(a["impr"])] for a in sorted(low_assets, key=lambda x: -x["impr"])[:20]],
                             ["Type", "Text", "Impr."])]
    minor = [r for r in results if r["severity"] == 1]
    if minor:
        md += ["", f"## Minor ({len(minor)} ads)", ""] + [f"- {r['ad_group']}: {'; '.join(r['issues'][:3])}" for r in minor[:15]]
    md += ["", "## Next step", "",
           "Claude drafts replacement copy from `rsa_brief.json`, then `validate_copy.py` checks limits and policy and writes `rsa_drafts.csv`.",
           "", "## Caveats", "",
           "- Ad strength measures asset variety and keyword relevance, not performance. Use it as a hygiene signal.",
           "- Asset performance labels need volume; 'Learning' and 'Pending' are not verdicts.",
           "- Do not judge a rewrite before ~2 weeks or a few thousand impressions."]

    brief = [{
        "ad_id": r["ad_id"], "campaign": r["campaign"], "campaign_id": r["campaign_id"], "ad_group": r["ad_group"],
        "ad_group_id": r["ad_group_id"], "strength": r["strength"], "final_url": r["final_url"],
        "path1": r["path1"], "path2": r["path2"], "top_keywords": r["top_keywords"],
        "keywords_covered": r["keywords_covered"], "issues": r["issues"],
        "keep_headlines": r["keep_headlines"], "keep_descriptions": r["keep_descriptions"],
        "replace": r["replace"], "headlines_needed": r["headlines_needed"], "descriptions_needed": r["descriptions_needed"],
        "editorial": r["editorial"],
        "recommend_unpin": any("pinned" in i for i in r["issues"]),
    } for r in sorted(flagged, key=lambda x: (-x["severity"], -x["cost"]))]

    find_rows = [{"campaign": r["campaign"], "ad_group": r["ad_group"], "ad_id": r["ad_id"], "strength": r["strength"],
                  "severity": r["severity"], "headlines": len(r["headlines"]), "descriptions": len(r["descriptions"]),
                  "pinned": sum(1 for h in r["headlines"] if h["pin"]), "impr": int(r["impr"]),
                  "cost": round(r["cost"], 2), "conv": round(r["conv"], 2), "issues": " | ".join(r["issues"])}
                 for r in sorted(results, key=lambda x: (-x["severity"], -x["cost"]))]
    extra = {"rsa_findings.csv": gads.to_csv(find_rows), "rsa_brief.json": json.dumps(brief, indent=2),
             "rsa_assets.csv": gads.to_csv(assets) if assets else ""}
    summary = {"headline": headline, "ads": len(results), "poor": len(poor), "average": len(avg),
               "flagged": len(flagged), "disapproved": len(disapproved), "headline_slots": slots,
               "flagged_cost": round(flagged_cost, 2), "days": days}
    return approvals, summary, "\n".join(md) + "\n", extra


def main():
    ap = gads.common_args(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    ap.add_argument("--assets", help="Offline: asset report export (Asset, Asset type, Performance, Impr.).")
    ap.add_argument("--keywords", help="Offline: keywords export (Keyword, Ad group, Impr.).")
    ap.add_argument("--min-asset-impr", type=int, default=1000, help="Low-label assets need this many impressions to count.")
    ap.add_argument("--top-keywords", type=int, default=3)
    ap.add_argument("--target-headlines", type=int, default=15)
    args = ap.parse_args()
    start, end = gads.resolve_window(args)
    days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1

    if args.csv:
        cid = gads.clean_cid(args.customer) or "offline"
        ads, assets, kws = load_csv(args.csv, args.assets, args.keywords)
        cur = args.currency
    else:
        if not args.customer:
            gads.die("--customer is required in live mode (or pass --csv).")
        cid = gads.clean_cid(args.customer)
        cur = gads.account_info(cid)["currency"]
        ads, assets, kws = load_live(cid, start, end)
    if not ads:
        gads.die("No enabled responsive search ads found.")
    results = audit(ads, assets, kws, args)
    approvals, summary, md, extra = build_outputs(results, assets, cid, cur, days)
    d = gads.write_results(SKILL, cid, approvals, summary, md, cur, extra)
    print(summary["headline"])
    print(f"-> {d}/gads-rsa-audit.md  ·  brief: {d}/rsa_brief.json  ·  queue: {d}/approvals.md")
    if args.json:
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
