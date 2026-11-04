#!/usr/bin/env python3
"""
audit_rsas.py - find weak responsive search ads and build a rewrite brief.

Live:     $PY audit_rsas.py -c 123-456-7890 --days 30 [--check-links]
Offline:  python3 audit_rsas.py --csv ads.csv [--assets assets.csv] [--keywords keywords.csv]
                                [--adgroups adgroups.csv] [--check-links] --currency USD

Outputs (gads-out/<cid>/<date>/):
  gads-rsa-audit.json / .md   summary + approval items
  rsa_findings.csv            one row per enabled RSA with every issue found
  rsa_brief.json              per-ad rewrite brief for Claude (keep / replace / slots to fill)
  rsa_assets.csv              asset-level labels and metrics
  rsa_coverage.csv            ad groups with no RSA, no approved RSA, or a single RSA
  rsa_tests.csv               ad tests: CTR and conversions per impression, two-proportion z-test
  rsa_links.csv               landing page check (only with --check-links)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gads  # noqa: E402
from gads import Approval, micros, money, num  # noqa: E402
from copyrules import check_text, key, similar, toks  # noqa: E402
import check_links  # noqa: E402

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
               ad_group_ad.policy_summary.policy_topic_entries,
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
            "policy_topics": _topics(r.get("ad_group_ad.policy_summary.policy_topic_entries")),
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
    groups = [{"campaign": r.get("campaign.name", ""), "campaign_id": str(r.get("campaign.id", "")),
               "ad_group": r.get("ad_group.name", ""), "ad_group_id": str(r.get("ad_group.id", ""))}
              for r in gads.query(cid, """
        SELECT campaign.id, campaign.name, ad_group.id, ad_group.name
        FROM ad_group
        WHERE campaign.status = 'ENABLED' AND ad_group.status = 'ENABLED'
          AND campaign.advertising_channel_type = 'SEARCH' AND ad_group.type = 'SEARCH_STANDARD'""")]
    return ads, assets, kws, groups


def _topics(entries) -> list[str]:
    """policy_topic_entries -> ['TRADEMARKS_IN_AD_TEXT (LIMITED)', ...]."""
    out = []
    for e in entries or []:
        if isinstance(e, dict):
            t = e.get("topic") or ""
            kind = str(e.get("type") or e.get("type_") or "")
            out.append(f"{t} ({kind})" if kind and kind not in ("UNSPECIFIED", "UNKNOWN") else t)
    return [t for t in out if t]


def _approval(text: str) -> str:
    t = str(text or "").lower()
    if "disapproved" in t:
        return "DISAPPROVED"
    if "limited" in t:
        return "APPROVED_LIMITED"
    return "APPROVED" if "approved" in t else ""


_STATUS_WORDS = re.compile(r"^\s*(disapproved|approved \(limited\)|eligible \(limited\)|approved|eligible|limited)\s*[:-]?\s*", re.I)


def _csv_topics(text: str) -> list[str]:
    """'Disapproved: Trademarks in ad text; Destination not working' -> the two topics."""
    out = []
    for t in re.split(r"[;\n|]", str(text or "")):
        t = _STATUS_WORDS.sub("", t).strip()
        if t and t not in ("--", "-"):
            out.append(t)
    return out


INACTIVE = ("paused", "removed", "disabled", "ended")


def _active(*vals) -> bool:
    return not any(str(v or "").strip().lower().startswith(INACTIVE) for v in vals)


def load_csv(paths, assets_csv, kw_csv, adgroups_csv=None, all_types=False):
    """Returns ads, assets, keywords by ad group, and the ad group list for coverage
    (from --adgroups, else the keywords export, else None)."""
    ads = []
    for p in paths:
        for i, r in enumerate(gads.read_ui_csv(p)):
            atype = str(gads.pick(r, "ad_type", default="responsive search ad")).lower()
            if "responsive search" not in atype and not all_types:
                continue
            # "Ad state"/"Ad status" carry Enabled/Paused; "Status" carries the serving or policy state
            if not _active(gads.pick(r, "ad_state", "ad_status", "status"),
                           gads.pick(r, "ad_group_state", "ad_group_status"), gads.pick(r, "campaign_state", "campaign_status")):
                continue
            policy = gads.pick(r, "approval_status", "status", "policy_details")
            topics = gads.pick(r, "policy_details", "policy_topics", "policy")
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
                "approval": _approval(f"{policy} {topics}"),
                "policy_topics": _csv_topics(topics),
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
    groups = None
    src = adgroups_csv or kw_csv
    if src:
        groups, seen = [], set()
        for r in gads.read_ui_csv(src):
            ctype = str(gads.pick(r, "campaign_type", "advertising_channel_type")).lower()
            if (ctype and "search" not in ctype) or "dynamic" in str(gads.pick(r, "ad_group_type")).lower():
                continue  # Display, Shopping, PMax, and Dynamic Search Ad groups cannot hold RSAs
            state = gads.pick(r, "ad_group_state", "ad_group_status", "status") if adgroups_csv else gads.pick(r, "ad_group_state", "ad_group_status")
            if not _active(state, gads.pick(r, "campaign_state", "campaign_status")):
                continue
            g = {"campaign": gads.pick(r, "campaign"), "campaign_id": gads.pick(r, "campaign_id"),
                 "ad_group": gads.pick(r, "ad_group"), "ad_group_id": gads.pick(r, "ad_group_id", default=gads.pick(r, "ad_group"))}
            if g["ad_group"] and (g["campaign"], g["ad_group_id"]) not in seen:
                seen.add((g["campaign"], g["ad_group_id"]))
                groups.append(g)
    return ads, assets, kws, groups


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
        # Policy gets its own approval item (rsa_policy), so it is listed here without raising severity.
        topics = ", ".join(ad.get("policy_topics") or [])
        if ad["approval"] == "DISAPPROVED":
            issues.append("disapproved" + (f": {topics}" if topics else ""))
        elif "LIMITED" in ad["approval"] or "LIMITED" in ad["primary_status"]:
            issues.append("approved (limited)" + (f": {topics}" if topics else ""))

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


def coverage(ads, groups):
    """Ad groups with no enabled RSA, no approved RSA, or a single RSA.
    groups=None (no ad group list available) still reports single-RSA ad groups."""
    by = defaultdict(list)
    for a in ads:
        by[(a["campaign"], a["ad_group_id"])].append(a)
    known = {(g["campaign"], g["ad_group_id"]): g for g in (groups or [])}
    for (camp, agid), group_ads in by.items():
        known.setdefault((camp, agid), {"campaign": camp, "campaign_id": group_ads[0].get("campaign_id", ""),
                                        "ad_group": group_ads[0]["ad_group"], "ad_group_id": agid})
    rows = []
    for k, g in known.items():
        group_ads = by.get(k, [])
        serving = [a for a in group_ads if a["approval"] != "DISAPPROVED"]
        if not group_ads:
            gap, note = "no_rsa", "no enabled RSA; the ad group cannot show a responsive search ad"
        elif not serving:
            gap, note = "no_approved_rsa", "every enabled RSA is disapproved"
        elif len(serving) == 1:
            gap, note = "one_rsa", "one RSA, so no message test is running"
        else:
            continue
        rows.append({**g, "gap": gap, "rsas": len(group_ads), "note": note,
                     "cost": round(sum(a["cost"] for a in group_ads), 2)})
    order = {"no_rsa": 0, "no_approved_rsa": 1, "one_rsa": 2}
    return sorted(rows, key=lambda r: (order[r["gap"]], -r["cost"], r["campaign"], r["ad_group"]))


def ztest(x1: float, n1: float, x2: float, n2: float) -> tuple[float, float]:
    """Two-proportion z-test. Returns (z, two-sided p-value); z > 0 means rate 1 is higher."""
    if n1 <= 0 or n2 <= 0:
        return 0.0, 1.0
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0, 1.0
    z = (x1 / n1 - x2 / n2) / se
    return z, math.erfc(abs(z) / math.sqrt(2))


TEST_COLS = ("metric", "verdict", "leader", "other", "leader_rate", "other_rate", "z", "p", "leader_conv",
             "leader_impr", "other_impr", "other_conv", "other_cost", "note")


def ad_tests(ads, args):
    """Compare RSAs within each ad group on CTR and conversions per impression.
    A winner is reported only when the leader beats an ad at the chosen confidence."""
    conf = args.confidence / 100 if args.confidence > 1 else args.confidence
    alpha = 1 - conf
    by = defaultdict(list)
    for a in ads:
        if a["approval"] != "DISAPPROVED":
            by[(a["campaign"], a["ad_group_id"])].append(a)
    rows = []
    for (camp, agid), group in by.items():
        if len(group) < 2:
            continue
        eligible = [a for a in group if a["impr"] >= args.min_test_impr]
        base = {"campaign": camp, "ad_group": group[0]["ad_group"], "ad_group_id": agid, "ads": len(group),
                **{k: "" for k in TEST_COLS}}
        if len(eligible) < 2:
            rows.append({**base, "metric": "all", "verdict": "not enough data",
                         "note": f"{len(eligible)} of {len(group)} ads have {args.min_test_impr:,}+ impressions"})
            continue
        for metric, field in (("ctr", "clicks"), ("conv_per_impr", "conv")):
            rate = lambda a: a[field] / a["impr"]  # noqa: E731
            leader = max(eligible, key=rate)
            for other in eligible:
                if other is leader:
                    continue
                z, pv = ztest(leader[field], leader["impr"], other[field], other["impr"])
                sig = pv < alpha and z > 0
                rows.append({**base, "metric": metric, "verdict": "winner" if sig else "not enough data",
                             "leader": leader["ad_id"], "other": other["ad_id"],
                             "leader_rate": round(rate(leader), 5), "other_rate": round(rate(other), 5),
                             "z": round(z, 2), "p": round(pv, 4), "leader_conv": round(leader["conv"], 1),
                             "leader_impr": int(leader["impr"]), "other_impr": int(other["impr"]),
                             "other_conv": round(other["conv"], 1), "other_cost": round(other["cost"], 2),
                             "note": f"{conf:.0%} confidence" if sig else f"p={pv:.3f}, needs p<{alpha:.2f}"})
    return rows


# ---------------------------------------------------------------- output

def link_approvals(link_rows, cur, days):
    out = []
    for r in link_rows:
        if not r["problem"]:
            continue
        n, cost = r["ads"], r["cost"]
        where = r["ad_groups"].split(" | ")[0] + (f" and {len(r['ad_groups'].split(' | ')) - 1} more" if " | " in r["ad_groups"] else "")
        if r["problem"] in check_links.BROKEN:
            sev = "critical" if cost > 0 else "high"
            out.append(Approval(
                skill=SKILL, kind="rsa_link", entity=r["url"], severity=sev, risk="low", confidence="high",
                action=f"Fix the landing page {r['url']} ({r['detail']})",
                why=(f"{n} enabled ad{'s' * (n != 1)} in {where} {'send' if n != 1 else 'sends'} clicks here"
                     + (f" and spent {money(cost, cur)} in {days}d." if cost > 0 else ". No spend yet.")
                     + (" Every click lands on a page that says it is unavailable." if r["problem"] == "soft_404"
                        else " Every click lands on an error.")
                     + " Fix the page or point the ads at a working URL; pause them if neither can happen today."),
                impact_monthly=round(cost * 30 / max(days, 1), 2) if cost > 0 else None,
                impact_label="saves" if cost > 0 else "",
                evidence={"status": r["status"], "chain": r["chain"], "ad_ids": r["ad_ids"].split(), "cost": cost},
                change={"final_url": r["url"], "ad_ids": r["ad_ids"].split(), "fix": "repair page or update final URL"},
                files=["rsa_links.csv"]))
        elif r["problem"] in check_links.BLOCKED:
            out.append(Approval(
                skill=SKILL, kind="rsa_link", entity=r["url"], severity="medium", risk="low", confidence="low",
                action=f"Open {r['url']} in a browser and confirm it loads",
                why=(f"The site answered the link checker with {r['detail']}. Real visitors usually get the page, "
                     f"but {n} ad{'s' * (n != 1)} in {where} ({money(cost, cur)} in {days}d) depend on it, so check it by hand."),
                evidence={"status": r["status"], "chain": r["chain"], "ad_ids": r["ad_ids"].split(), "cost": cost},
                change={"final_url": r["url"], "fix": "manual check"},
                files=["rsa_links.csv"]))
        else:
            out.append(Approval(
                skill=SKILL, kind="rsa_link", entity=r["url"], severity="high", risk="low", confidence="medium",
                action=f"Point ads at the real page: {r['url']} {r['detail']}",
                why=(f"{n} ad{'s' * (n != 1)} in {where} ({money(cost, cur)} in {days}d) land on a different page from their final URL. "
                     + ("A different domain can trigger a destination mismatch disapproval." if r["problem"] == "off_domain"
                        else "A deep link that falls back to the homepage usually means the page was removed.")),
                evidence={"status": r["status"], "chain": r["chain"], "ad_ids": r["ad_ids"].split(), "cost": cost},
                change={"final_url": r["url"], "lands_on": r["final_url"], "ad_ids": r["ad_ids"].split()},
                files=["rsa_links.csv"]))
    return out


def build_outputs(results, assets, cid, cur, days, cov=None, cov_skipped=False, tests=None, links=None, args=None):
    cov, tests = cov or [], tests or []
    flagged = [r for r in results if r["severity"] >= 2]
    poor = [r for r in results if r["strength"] == "POOR"]
    avg = [r for r in results if r["strength"] == "AVERAGE"]
    disapproved = [r for r in results if r["approval"] == "DISAPPROVED"]
    limited = [r for r in results if r["approval"] != "DISAPPROVED"
               and ("LIMITED" in r["approval"] or "LIMITED" in r["primary_status"])]
    total_cost = sum(r["cost"] for r in results)
    flagged_cost = sum(r["cost"] for r in flagged)
    slots = sum(r["headlines_needed"] for r in flagged)
    by_ad = {r["ad_id"]: r for r in results}

    approvals = []
    for r in sorted(flagged, key=lambda x: (-x["severity"], -x["cost"])):
        approvals.append(Approval(
            skill=SKILL, kind="rsa_rewrite", entity=f"{r['campaign']} > {r['ad_group']} > {r['ad_id']}",
            severity="critical" if r["severity"] >= 3 else "high",
            action=f"Refresh RSA {r['ad_id']} in '{r['ad_group']}' ({r['strength'].title() or 'unrated'}): "
                   f"{r['headlines_needed']} new headline{'s' * (r['headlines_needed'] != 1)}, "
                   f"{r['descriptions_needed']} new description{'s' * (r['descriptions_needed'] != 1)}, replace {len(r['replace'])}",
            why="; ".join(r["issues"][:4]) + f". Ad spent {money(r['cost'], cur)} in {days}d.",
            impact_label="gains", confidence="medium" if r["strength"] in ("POOR", "AVERAGE") else "low",
            risk="low", evidence={"ad_id": r["ad_id"], "strength": r["strength"], "cost": round(r["cost"], 2),
                                  "impr": int(r["impr"]), "conv": round(r["conv"], 1)},
            change={"ad_id": r["ad_id"], "file": "rsa_drafts.csv"}, files=["rsa_brief.json", "rsa_drafts.csv"]))

    for r in disapproved + limited:
        dis = r["approval"] == "DISAPPROVED"
        topics = ", ".join(r.get("policy_topics") or []) or "see the policy details in Google Ads"
        approvals.append(Approval(
            skill=SKILL, kind="rsa_policy", entity=f"{r['campaign']} > {r['ad_group']} > {r['ad_id']}",
            severity="critical" if dis else "high", risk="low", confidence="high", impact_label="gains",
            action=(f"Fix disapproved RSA {r['ad_id']} in '{r['ad_group']}' ({topics})" if dis
                    else f"Review limited RSA {r['ad_id']} in '{r['ad_group']}' ({topics})"),
            why=("Disapproved ads do not serve. Rewrite or remove the asset the policy topic points to, then resubmit."
                 if dis else f"Approved (limited) ads serve in fewer auctions or locations. Ad spent {money(r['cost'], cur)} in {days}d. "
                             "Rewrite the asset if the restriction is avoidable."),
            evidence={"ad_id": r["ad_id"], "approval": r["approval"], "policy_topics": r.get("policy_topics") or [],
                      "cost": round(r["cost"], 2)},
            change={"ad_id": r["ad_id"], "file": "rsa_drafts.csv"}, files=["rsa_brief.json"]))

    for g in cov:
        if g["gap"] == "one_rsa":
            continue
        approvals.append(Approval(
            skill=SKILL, kind="rsa_coverage", entity=f"{g['campaign']} > {g['ad_group']}", severity="high",
            risk="low", confidence="high", impact_label="gains",
            action=f"Write an RSA for '{g['ad_group']}' ({g['campaign']})",
            why=f"Enabled ad group in an enabled Search campaign. Gap: {g['note']}.",
            evidence={"gap": g["gap"], "rsas": g["rsas"]},
            change={"campaign": g["campaign"], "ad_group": g["ad_group"], "file": "rsa_drafts.csv"},
            files=["rsa_coverage.csv", "rsa_drafts.csv"]))

    winners = [t for t in tests if t["metric"] == "conv_per_impr" and t["verdict"] == "winner"]
    pausable = [t for t in winners if t["leader_conv"] >= args.min_winner_conv] if args else []
    for t in pausable:
        extra = (t["leader_rate"] - t["other_rate"]) * t["other_impr"] * 30 / max(days, 1)
        approvals.append(Approval(
            skill=SKILL, kind="rsa_test", entity=f"{t['campaign']} > {t['ad_group']} > {t['other']}",
            severity="medium", risk="low", confidence="high", impact_label="gains",
            action=f"Pause losing RSA {t['other']} in '{t['ad_group']}' (ad {t['leader']} wins on conversions)",
            why=(f"Ad {t['leader']} converts {t['leader_rate'] * 1000:.2f} per 1,000 impressions against "
                 f"{t['other_rate'] * 1000:.2f} for ad {t['other']} ({t['note']}, z={t['z']}). "
                 f"Moving its impressions to the winner adds about {extra:.0f} conversions a month. "
                 "Then write a new challenger so the ad group keeps two RSAs."),
            evidence={k: t[k] for k in ("leader", "other", "leader_rate", "other_rate", "z", "p",
                                        "leader_conv", "leader_impr", "other_conv", "other_impr")},
            change={"ad_id": t["other"], "ad_group_id": t["ad_group_id"], "status": "PAUSED"},
            files=["rsa_tests.csv"]))

    link_rows = links or []
    for lr in link_rows:
        if lr["problem"]:
            for aid in lr["ad_ids"].split():
                if aid in by_ad:
                    by_ad[aid]["issues"].append(f"landing page {lr['problem']}: {lr['detail']}")
    approvals += link_approvals(link_rows, cur, days)
    broken = [lr for lr in link_rows if lr["problem"] in check_links.BROKEN]
    redirects = [lr for lr in link_rows if lr["problem"] in check_links.REDIRECT]

    headline = (f"Audited {len(results)} RSAs: {len(poor)} Poor, {len(avg)} Average, {len(flagged)} need a rewrite "
                f"({money(flagged_cost, cur)} of {money(total_cost, cur)} spend), {slots} headline slots to fill")
    extras = []
    if disapproved or limited:
        extras.append(f"{len(disapproved)} disapproved, {len(limited)} limited")
    if links is not None:
        extras.append(f"{len(broken)} broken landing page{'s' * (len(broken) != 1)}")
    no_rsa = [g for g in cov if g["gap"] != "one_rsa"]
    if no_rsa:
        extras.append(f"{len(no_rsa)} ad group{'s' * (len(no_rsa) != 1)} without a serving RSA")
    if winners:
        extras.append(f"{len(winners)} significant ad test result{'s' * (len(winners) != 1)}")
    if extras:
        headline += "; " + ", ".join(extras)

    md = [f"# RSA audit · {gads.fmt_cid(cid) or 'offline export'} · last {days} days", "", f"**{headline}.**", ""]
    if links is not None:
        md += ["## Landing pages", ""]
        bad = [lr for lr in link_rows if lr["problem"]]
        md.append(gads.md_table([[lr["problem"], lr["url"], lr["detail"], lr["ads"], money(lr["cost"], cur)] for lr in bad],
                                ["Problem", "Final URL", "Detail", "Ads", "Cost"]) if bad
                  else f"All {len(link_rows)} final URLs load.")
        md.append("")
    if disapproved or limited:
        md += ["## Policy", "", gads.md_table(
            [[r["ad_group"], r["ad_id"], "Disapproved" if r["approval"] == "DISAPPROVED" else "Limited",
              ", ".join(r.get("policy_topics") or []) or "-"] for r in disapproved + limited],
            ["Ad group", "Ad ID", "Status", "Policy topics"]), ""]
    md += ["## Ads to fix", ""]
    if flagged:
        md.append(gads.md_table([[r["campaign"], r["ad_group"], r["strength"].title(), money(r["cost"], cur),
                                  len({key(h['text']) for h in r['headlines']}), "; ".join(r["issues"][:3])]
                                 for r in sorted(flagged, key=lambda x: (-x["severity"], -x["cost"]))[:25]],
                                ["Campaign", "Ad group", "Strength", "Cost", "Headlines", "Top issues"]))
    else:
        md.append("_none_")
    md += ["", "## Coverage gaps", ""]
    if cov:
        md.append(gads.md_table([[g["campaign"], g["ad_group"], g["gap"].replace("_", " "), g["note"]] for g in cov[:30]],
                                ["Campaign", "Ad group", "Gap", "Note"]))
    else:
        md.append("_none_")
    if cov_skipped:
        md += ["", "Ad groups with no RSA were not checked: pass `--adgroups` (ad groups export) or `--keywords`."]
    if tests:
        md += ["", "## Ad tests", ""]
        conf_word = f"{args.confidence:g}%" if args else "95%"
        md.append(f"Two-proportion z-test at {conf_word} confidence, {args.min_test_impr:,}+ impressions per ad." if args else "")
        md.append("")
        md.append(gads.md_table([[t["ad_group"], t["metric"], t["leader"] or "-", t["other"] or "-",
                                  t["verdict"], t["note"]] for t in tests],
                                ["Ad group", "Metric", "Leader", "Compared with", "Verdict", "Note"]))
        held = [t for t in winners if t not in pausable]
        if held:
            md += ["", f"{len(held)} conversion winner(s) have fewer than {args.min_winner_conv:g} conversions, so no pause is proposed yet."]
    low_assets = [a for r in results for a in r["low_assets"]]
    if low_assets:
        md += ["", "## Low-rated assets to replace", "",
               gads.md_table([[a["field"].title(), a["text"], int(a["impr"])] for a in sorted(low_assets, key=lambda x: -x["impr"])[:20]],
                             ["Type", "Text", "Impr."])]
    minor = [r for r in results if r["severity"] == 1]
    if minor:
        md += ["", f"## Minor ({len(minor)} ads)", ""] + [f"- {r['ad_group']}: {'; '.join(r['issues'][:3])}" for r in minor[:15]]
    md += ["", "## Next step", "",
           "Claude drafts replacement copy from `rsa_brief.json`, then `validate_copy.py` checks limits and policy and writes `rsa_drafts.csv` with every ad set to Paused.",
           "", "## Caveats", "",
           "- Ad strength measures asset variety and keyword relevance. Treat it as a hygiene signal.",
           "- Asset performance labels need volume; 'Learning' and 'Pending' are not verdicts.",
           "- Do not judge a rewrite before ~2 weeks or a few thousand impressions."]

    in_brief = sorted({r["ad_id"]: r for r in flagged + disapproved}.values(), key=lambda x: (-x["severity"], -x["cost"]))
    brief = [{
        "ad_id": r["ad_id"], "campaign": r["campaign"], "campaign_id": r["campaign_id"], "ad_group": r["ad_group"],
        "ad_group_id": r["ad_group_id"], "strength": r["strength"], "final_url": r["final_url"],
        "path1": r["path1"], "path2": r["path2"], "top_keywords": r["top_keywords"],
        "keywords_covered": r["keywords_covered"], "issues": r["issues"],
        "policy_topics": r.get("policy_topics") or [],
        "keep_headlines": r["keep_headlines"], "keep_descriptions": r["keep_descriptions"],
        "replace": r["replace"], "headlines_needed": r["headlines_needed"], "descriptions_needed": r["descriptions_needed"],
        "editorial": r["editorial"],
        "recommend_unpin": any("pinned" in i for i in r["issues"]),
    } for r in in_brief]

    find_rows = [{"campaign": r["campaign"], "ad_group": r["ad_group"], "ad_id": r["ad_id"], "strength": r["strength"],
                  "severity": r["severity"], "approval": r["approval"], "headlines": len(r["headlines"]),
                  "descriptions": len(r["descriptions"]),
                  "pinned": sum(1 for h in r["headlines"] if h["pin"]), "impr": int(r["impr"]),
                  "cost": round(r["cost"], 2), "conv": round(r["conv"], 2), "issues": " | ".join(r["issues"])}
                 for r in sorted(results, key=lambda x: (-x["severity"], -x["cost"]))]
    extra = {"rsa_findings.csv": gads.to_csv(find_rows), "rsa_brief.json": json.dumps(brief, indent=2),
             "rsa_assets.csv": gads.to_csv(assets) if assets else "",
             "rsa_coverage.csv": gads.to_csv(cov) if cov else "",
             "rsa_tests.csv": gads.to_csv(tests) if tests else ""}
    if links is not None:
        extra["rsa_links.csv"] = gads.to_csv(link_rows)
    summary = {"headline": headline, "ads": len(results), "poor": len(poor), "average": len(avg),
               "flagged": len(flagged), "disapproved": len(disapproved), "limited": len(limited),
               "headline_slots": slots, "flagged_cost": round(flagged_cost, 2), "days": days,
               "coverage_gaps": len(no_rsa), "single_rsa_groups": sum(g["gap"] == "one_rsa" for g in cov),
               "test_winners": len(winners), "pause_proposals": len(pausable),
               "links_checked": len(link_rows) if links is not None else None,
               "broken_links": len(broken) if links is not None else None,
               "bad_redirects": len(redirects) if links is not None else None}
    return approvals, summary, "\n".join(md) + "\n", extra


def main():
    ap = gads.common_args(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    ap.add_argument("--assets", help="Offline: asset report export (Asset, Asset type, Performance, Impr.).")
    ap.add_argument("--keywords", help="Offline: keywords export (Keyword, Ad group, Impr.).")
    ap.add_argument("--adgroups", help="Offline: ad groups export (Campaign, Ad group, status) for coverage gaps.")
    ap.add_argument("--min-asset-impr", type=int, default=1000, help="Low-label assets need this many impressions to count.")
    ap.add_argument("--top-keywords", type=int, default=3)
    ap.add_argument("--target-headlines", type=int, default=15)
    ap.add_argument("--min-test-impr", type=int, default=1000, help="Ad tests: minimum impressions per ad.")
    ap.add_argument("--confidence", type=float, default=95, help="Ad tests: confidence level in percent (default 95).")
    ap.add_argument("--min-winner-conv", type=float, default=30, help="Ad tests: winner conversions needed before proposing a pause.")
    ap.add_argument("--check-links", action="store_true", help="Fetch every final URL and flag broken pages and bad redirects.")
    ap.add_argument("--link-timeout", type=float, default=10)
    ap.add_argument("--link-workers", type=int, default=4)
    ap.add_argument("--fail-text", action="append", default=[], help="Extra soft 404 phrase for --check-links (repeatable).")
    args = ap.parse_args()
    start, end = gads.resolve_window(args)
    days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1

    if args.csv:
        cid = gads.clean_cid(args.customer) or "offline"
        ads, assets, kws, groups = load_csv(args.csv, args.assets, args.keywords, args.adgroups)
        cur = args.currency
    else:
        if not args.customer:
            gads.die("--customer is required in live mode (or pass --csv).")
        cid = gads.clean_cid(args.customer)
        cur = gads.account_info(cid)["currency"]
        ads, assets, kws, groups = load_live(cid, start, end)
    if not ads and not groups:
        gads.die("No enabled responsive search ads found.")
    results = audit(ads, assets, kws, args)
    cov = coverage(ads, groups)
    tests = ad_tests(ads, args)
    links = None
    if args.check_links:
        usage = check_links.collect(ads)
        checked = check_links.check_urls(usage, args.link_timeout, args.link_workers,
                                         check_links.DEFAULT_FAIL_TEXT + args.fail_text)
        links = check_links.link_rows(checked, usage)
    approvals, summary, md, extra = build_outputs(results, assets, cid, cur, days, cov, groups is None, tests, links, args)
    d = gads.write_results(SKILL, cid, approvals, summary, md, cur, extra)
    print(summary["headline"])
    print(f"-> {d}/gads-rsa-audit.md  ·  brief: {d}/rsa_brief.json  ·  queue: {d}/approvals.md")
    if args.json:
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
