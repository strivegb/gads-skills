#!/usr/bin/env python3
"""
audit_conversions.py - is the conversion data that bidding learns from true?

Checks every conversion action for: duplicate counting (e.g. GA4-imported
purchase + Google Ads tag purchase both primary), wrong counting type,
actions that went silent or dropped sharply, micro-conversions inflating
the Conversions column, static purchase values, non data-driven
attribution, enhanced conversions off, and removed/hidden actions that
still record.

Live:     $PY audit_conversions.py -c 123-456-7890 --days 56
Offline:  python3 audit_conversions.py --csv conversion_actions.csv --daily conversions_by_action_day.csv

Outputs (gads-out/<cid>/<date>/):
  gads-tracking-audit.json / .md   summary + approval items
  conversion_actions.csv           normalized settings table
  daily_by_action.csv              date x action matrix (all_conversions)
  expected_tags.json               AW IDs + send_to labels for check_site_tags.py --expect
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gads  # noqa: E402
from gads import Approval, money, num  # noqa: E402

SKILL = "gads-tracking-audit"

# Categories that measure intent, not business outcomes. Primary = inflation.
MICRO = {"PAGE_VIEW", "ENGAGEMENT", "ADD_TO_CART", "BEGIN_CHECKOUT", "OUTBOUND_CLICK", "GET_DIRECTIONS", "DEFAULT"}
# One per click is right for leads; many per click is right for sales.
LEAD_LIKE = {"LEAD", "SUBMIT_LEAD_FORM", "SIGNUP", "BOOK_APPOINTMENT", "REQUEST_QUOTE", "CONTACT",
             "PHONE_CALL_LEAD", "QUALIFIED_LEAD", "CONVERTED_LEAD", "IMPORTED_LEAD", "DOWNLOAD"}
SALE_LIKE = {"PURCHASE", "SUBSCRIBE_PAID", "STORE_SALE"}
DDA = {"GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN", "DATA_DRIVEN"}

# UI label -> API enum, for offline exports
UI_CATEGORY = {
    "purchase": "PURCHASE", "purchases": "PURCHASE", "submit lead form": "SUBMIT_LEAD_FORM", "submit lead forms": "SUBMIT_LEAD_FORM",
    "lead": "LEAD", "sign-up": "SIGNUP", "sign-ups": "SIGNUP", "signup": "SIGNUP", "page view": "PAGE_VIEW", "page views": "PAGE_VIEW",
    "add to cart": "ADD_TO_CART", "begin checkout": "BEGIN_CHECKOUT", "contact": "CONTACT", "contacts": "CONTACT",
    "book appointment": "BOOK_APPOINTMENT", "request quote": "REQUEST_QUOTE", "phone call lead": "PHONE_CALL_LEAD",
    "phone call leads": "PHONE_CALL_LEAD", "qualified lead": "QUALIFIED_LEAD", "converted lead": "CONVERTED_LEAD",
    "engagement": "ENGAGEMENT", "outbound click": "OUTBOUND_CLICK", "subscribe": "SUBSCRIBE_PAID", "download": "DOWNLOAD",
    "other": "DEFAULT", "default": "DEFAULT", "imported lead": "IMPORTED_LEAD", "get directions": "GET_DIRECTIONS",
}
UI_ATTR = {"data-driven": "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN", "last click": "GOOGLE_ADS_LAST_CLICK"}


def enumish(s: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(s or "").upper()).strip("_")


# ---------------------------------------------------------------- load


def load_live(cid: str, start: str, end: str) -> tuple[list[dict], dict, dict, list[dict]]:
    acts = gads.query(cid, """
        SELECT conversion_action.id, conversion_action.name, conversion_action.status,
               conversion_action.type, conversion_action.category, conversion_action.origin,
               conversion_action.primary_for_goal, conversion_action.counting_type,
               conversion_action.include_in_conversions_metric,
               conversion_action.attribution_model_settings.attribution_model,
               conversion_action.click_through_lookback_window_days,
               conversion_action.value_settings.default_value,
               conversion_action.value_settings.always_use_default_value,
               conversion_action.tag_snippets, conversion_action.owner_customer
        FROM conversion_action""")
    actions = []
    for r in acts:
        actions.append({
            "id": str(r.get("conversion_action.id", "")), "name": r.get("conversion_action.name", ""),
            "status": r.get("conversion_action.status", ""), "type": r.get("conversion_action.type", ""),
            "category": r.get("conversion_action.category", ""), "origin": r.get("conversion_action.origin", ""),
            "primary": bool(r.get("conversion_action.primary_for_goal")),
            "included": bool(r.get("conversion_action.include_in_conversions_metric")),
            "counting": r.get("conversion_action.counting_type", ""),
            "attribution": r.get("conversion_action.attribution_model_settings.attribution_model", ""),
            "lookback": num(r.get("conversion_action.click_through_lookback_window_days")),
            "default_value": num(r.get("conversion_action.value_settings.default_value")),
            "always_default": bool(r.get("conversion_action.value_settings.always_use_default_value")),
            "snippets": r.get("conversion_action.tag_snippets") or [],
            "owner": r.get("conversion_action.owner_customer", ""),
        })
    daily = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0]))  # name -> date -> [conv, all_conv, value]
    for r in gads.query(cid, f"""
        SELECT segments.date, segments.conversion_action_name, segments.conversion_action,
               metrics.conversions, metrics.all_conversions, metrics.all_conversions_value
        FROM customer WHERE {gads.gaql_dates(start, end)}"""):
        d = daily[r.get("segments.conversion_action_name", "")][r.get("segments.date", "")]
        d[0] += num(r.get("metrics.conversions"))
        d[1] += num(r.get("metrics.all_conversions"))
        d[2] += num(r.get("metrics.all_conversions_value"))
    goals = gads.query(cid, """
        SELECT customer_conversion_goal.category, customer_conversion_goal.origin,
               customer_conversion_goal.biddable
        FROM customer_conversion_goal""")
    cust = gads.query(cid, """
        SELECT customer.conversion_tracking_setting.enhanced_conversions_for_leads_enabled,
               customer.conversion_tracking_setting.accepted_customer_data_terms,
               customer.conversion_tracking_setting.conversion_tracking_status,
               customer.conversion_tracking_setting.conversion_tracking_id,
               customer.conversion_tracking_setting.google_ads_conversion_customer
        FROM customer LIMIT 1""")
    c = cust[0] if cust else {}
    setting = {
        "ec_leads": bool(c.get("customer.conversion_tracking_setting.enhanced_conversions_for_leads_enabled")),
        "accepted_terms": bool(c.get("customer.conversion_tracking_setting.accepted_customer_data_terms")),
        "status": c.get("customer.conversion_tracking_setting.conversion_tracking_status", ""),
        "tracking_id": str(c.get("customer.conversion_tracking_setting.conversion_tracking_id", "")),
        "conversion_customer": c.get("customer.conversion_tracking_setting.google_ads_conversion_customer", ""),
        "known": True,
    }
    return actions, daily, setting, goals


def load_csv(paths: list[str], daily_path: str | None) -> tuple[list[dict], dict, dict, list[dict]]:
    actions = []
    for p in paths:
        for r in gads.read_ui_csv(p):
            name = gads.pick(r, "conversion_action", "conversion_action_name", "name")
            if not name:
                continue
            opt = str(gads.pick(r, "action_optimization", "optimization", "primary_for_goal")).lower()
            primary = opt.startswith("primary") or opt in ("true", "yes")
            cat_raw = str(gads.pick(r, "category", "conversion_category", "goal_category", "goal", "conversion_goal")).lower()
            cat = UI_CATEGORY.get(cat_raw.split("(")[0].strip(), enumish(cat_raw))
            count = str(gads.pick(r, "count", "counting_type")).lower()
            counting = "MANY_PER_CLICK" if count.startswith(("every", "many")) else ("ONE_PER_CLICK" if count.startswith("one") else enumish(count))
            src = str(gads.pick(r, "source", "conversion_source", "type")).lower()
            typ = ("GOOGLE_ANALYTICS_4_PURCHASE" if "analytics" in src and cat == "PURCHASE" else
                   "GOOGLE_ANALYTICS_4_CUSTOM" if "analytics" in src else
                   "UPLOAD_CLICKS" if "import" in src or "upload" in src else
                   "WEBPAGE" if "website" in src or "web" in src else enumish(src))
            attr_raw = str(gads.pick(r, "attribution_model", "attribution")).lower()
            status_raw = str(gads.pick(r, "status", "tracking_status")).lower()
            status = "REMOVED" if "removed" in status_raw else "HIDDEN" if "hidden" in status_raw else "ENABLED"
            val = str(gads.pick(r, "value", "conversion_value", "value_settings")).lower()
            actions.append({
                "id": str(gads.pick(r, "conversion_action_id", "id")), "name": name, "status": status,
                "ui_status": status_raw, "type": typ, "category": cat,
                "origin": "GOOGLE_HOSTED" if "analytics" in src else ("WEBSITE" if "web" in src else enumish(src)),
                "primary": primary, "included": primary, "counting": counting,
                "attribution": UI_ATTR.get(attr_raw, enumish(attr_raw)),
                "lookback": num(gads.pick(r, "click_through_conv_window", "click_through_window", "conversion_window")),
                "default_value": num(re.sub(r"[^0-9.]", "", val) or 0),
                "always_default": ("same value" in val) or ("use the same" in val),
                "snippets": [], "owner": "",
            })
    daily = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0]))
    if daily_path:
        for r in gads.read_ui_csv(daily_path):
            name = gads.pick(r, "conversion_action", "conversion_action_name")
            day = gads.pick(r, "day", "date")
            if not name or not day:
                continue
            try:
                day = dt.date.fromisoformat(day[:10]).isoformat()
            except ValueError:
                day = dt.datetime.strptime(day, "%b %d, %Y").date().isoformat()
            d = daily[name][day]
            d[0] += num(gads.pick(r, "conversions", "conv"))
            d[1] += num(gads.pick(r, "all_conv", "all_conversions", "conversions"))
            d[2] += num(gads.pick(r, "all_conv_value", "conv_value", default=0))
    setting = {"known": False}
    return actions, daily, setting, []


# ---------------------------------------------------------------- checks


def series(daily_for: dict, days: list[str], idx: int = 1) -> list[float]:
    return [daily_for.get(d, [0, 0, 0])[idx] for d in days]


def corr(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 7:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va == 0 or vb == 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


def name_sim(a: str, b: str) -> float:
    strip = lambda s: re.sub(r"\b(ga4|ga|google ads|gads|analytics|tag|web|website|import(ed)?|conversion|\(.*?\)|-|_)\b", " ", s.lower())  # noqa: E731
    return difflib.SequenceMatcher(None, " ".join(strip(a).split()), " ".join(strip(b).split())).ratio()


def poisson_cdf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0
    term, total = math.exp(-lam), math.exp(-lam)
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return min(1.0, total)


def analyse(actions, daily, setting, goals, start, end, args, currency):
    d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    days = [(d0 + dt.timedelta(i)).isoformat() for i in range((d1 - d0).days + 1)]
    by_name = {a["name"]: a for a in actions}
    for a in actions:
        s = series(daily.get(a["name"], {}), days)
        a["all_conv_window"] = sum(s)
        a["conv_window"] = sum(series(daily.get(a["name"], {}), days, 0))
        a["value_window"] = sum(series(daily.get(a["name"], {}), days, 2))
    counted = [a for a in actions if a["status"] == "ENABLED" and (a["included"] or a["primary"])]
    total_conv = sum(a["all_conv_window"] for a in counted) or 0
    findings: list[Approval] = []
    issues: list[dict] = []

    def add(kind, sev, action, why, entity, ev, impact_label="fixes measurement", conf="high", risk="low", change=None):
        issues.append({"kind": kind, "severity": sev, "entity": entity, "why": why})
        findings.append(Approval(skill=SKILL, kind=kind, action=action, why=why, entity=entity,
                                 impact_label=impact_label, confidence=conf, risk=risk, evidence=ev, change=change or {},
                                 severity={"high": "critical", "medium": "high"}.get(sev, "low")))

    # 1. duplicates: two counted actions, same category, same event
    seen = set()
    for i, a in enumerate(counted):
        for b in counted[i + 1:]:
            if a["category"] != b["category"] or a["category"] in MICRO:
                continue
            key = tuple(sorted((a["name"], b["name"])))
            if key in seen:
                continue
            sa, sb = series(daily.get(a["name"], {}), days), series(daily.get(b["name"], {}), days)
            ta, tb = sum(sa), sum(sb)
            r = corr(sa, sb)
            ratio = (min(ta, tb) / max(ta, tb)) if max(ta, tb) else 0
            sim = name_sim(a["name"], b["name"])
            mixed_source = ("GOOGLE_ANALYTICS" in a["type"]) != ("GOOGLE_ANALYTICS" in b["type"])
            score = 0
            score += 2 if (r is not None and r >= 0.8) else (1 if (r is not None and r >= 0.6) else 0)
            score += 2 if ratio >= 0.7 else (1 if ratio >= 0.5 else 0)
            score += 1 if sim >= 0.6 else 0
            score += 1 if mixed_source else 0
            if score >= 4 or (mixed_source and a["category"] in SALE_LIKE | LEAD_LIKE and score >= 2):
                seen.add(key)
                keep, drop = (a, b) if ("GOOGLE_ANALYTICS" not in a["type"]) else (b, a)
                over = min(ta, tb)
                conf = "high" if score >= 5 else "medium"
                moves = f"daily counts move together (r={r:.2f}, ratio {ratio:.2f})" if r is not None else f"totals are close (ratio {ratio:.2f})"
                why = (f"'{a['name']}' and '{b['name']}' are both counted in Conversions for {a['category'].lower()} and {moves}. "
                       f"About {over:.0f} of {ta + tb:.0f} conversions in the window are likely double counted, so bidding chases inflated numbers.")
                add("duplicate_conversion", "high",
                    f"Set '{drop['name']}' to secondary; keep '{keep['name']}' as the primary {a['category'].lower()} action",
                    why, f"{a['name']} | {b['name']}",
                    {"corr": None if r is None else round(r, 3), "ratio": round(ratio, 3), "name_similarity": round(sim, 2),
                     "a_total": round(ta, 1), "b_total": round(tb, 1), "mixed_source": mixed_source},
                    conf=conf, risk="medium",
                    change={"conversion_action": drop["name"], "id": drop["id"], "set": {"primary_for_goal": False}})
                drop["_dup"] = True

    # 2. counting type
    for a in counted:
        if a["category"] in SALE_LIKE and a["counting"] == "ONE_PER_CLICK":
            add("counting_type", "medium", f"Change '{a['name']}' counting to Every (many per click)",
                "Purchases are counted One per click, so a customer who buys twice after one click is recorded once. Revenue and ROAS are understated.",
                a["name"], {"category": a["category"], "counting": a["counting"]}, conf="high", risk="low",
                change={"id": a["id"], "set": {"counting_type": "MANY_PER_CLICK"}})
        if a["category"] in LEAD_LIKE and a["counting"] == "MANY_PER_CLICK":
            add("counting_type", "medium", f"Change '{a['name']}' counting to One per click",
                "Leads are counted Every, so a person who submits twice (or a form that fires twice) counts as two leads. CPA looks better than it is.",
                a["name"], {"category": a["category"], "counting": a["counting"]}, conf="high", risk="low",
                change={"id": a["id"], "set": {"counting_type": "ONE_PER_CLICK"}})

    # 3. silent or sharply dropped actions
    recent_n = args.recent_days
    for a in counted:
        s = series(daily.get(a["name"], {}), days)
        if len(s) < recent_n + 14:
            continue
        base, recent = s[-(recent_n + 28):-recent_n] or s[:-recent_n], s[-recent_n:]
        base_mean = sum(base) / len(base) if base else 0
        expected = base_mean * recent_n
        got = sum(recent)
        active_days = sum(1 for x in base if x > 0)
        if base_mean >= 0.3 and got == 0 and active_days >= len(base) * 0.4:
            last = next((days[-recent_n - 1 - i] for i, x in enumerate(reversed(base)) if x > 0), "?")
            add("silent_action", "high", f"Investigate '{a['name']}': zero conversions in the last {recent_n} days",
                f"It averaged {base_mean:.1f}/day over the prior {len(base)} days and recorded nothing since {last}. Expected ~{expected:.0f}. A broken tag or form change is the usual cause; smart bidding is now optimizing toward nothing.",
                a["name"], {"baseline_per_day": round(base_mean, 2), "recent": got, "expected": round(expected, 1), "last_conversion_day": last},
                conf="high", risk="low")
        elif expected >= 10 and got < 0.4 * expected and poisson_cdf(int(got), expected) < 0.01:
            add("conversion_drop", "medium", f"Check '{a['name']}': down {1 - got / expected:.0%} vs its 4-week run rate",
                f"{got:.0f} conversions in the last {recent_n} days vs ~{expected:.0f} expected. That is unlikely to be noise (p<0.01). Check the tag, the form, and site changes before touching bids.",
                a["name"], {"baseline_per_day": round(base_mean, 2), "recent": got, "expected": round(expected, 1)},
                conf="medium", risk="low")

    # 4. micro-conversions counted as primary
    for a in counted:
        if a["category"] in MICRO:
            share = a["all_conv_window"] / total_conv if total_conv else 0
            add("micro_primary", "high" if share >= 0.2 else "medium",
                f"Set '{a['name']}' ({a['category'].lower().replace('_', ' ')}) to secondary",
                f"It is counted in Conversions and is {share:.0%} of them. Bidding treats it like a sale or lead and buys cheap clicks that only produce this event.",
                a["name"], {"category": a["category"], "share_of_conversions": round(share, 3), "count": round(a['all_conv_window'], 1)},
                conf="high" if a["category"] in {"PAGE_VIEW", "ENGAGEMENT", "OUTBOUND_CLICK"} else "medium", risk="medium",
                change={"id": a["id"], "set": {"primary_for_goal": False}})

    # 5. static purchase values
    for a in counted:
        if a["category"] in SALE_LIKE and a["always_default"]:
            add("static_value", "medium", f"Pass the real order value on '{a['name']}'",
                f"Every purchase is recorded at the default value ({money(a['default_value'], currency)}), so ROAS and value bidding see no difference between a small and a large order.",
                a["name"], {"default_value": a["default_value"]}, conf="high", risk="low")

    # 6. attribution
    non_dda = [a for a in counted if a["attribution"] and a["attribution"] not in DDA]
    if non_dda:
        add("attribution", "low", f"Move {len(non_dda)} primary action(s) to data-driven attribution",
            "Last click gives all credit to the final search, usually brand. Data-driven spreads credit to the generic queries that started the journey, which changes what bidding values. "
            "Expect conversion counts per campaign to shift; announce the change before switching.",
            ", ".join(a["name"] for a in non_dda[:5]), {"actions": [a["name"] for a in non_dda]}, conf="medium", risk="low")

    # 7. enhanced conversions / customer data terms
    if setting.get("known"):
        has_lead = any(a["category"] in LEAD_LIKE for a in counted)
        if has_lead and not setting.get("ec_leads"):
            add("enhanced_conversions", "medium", "Turn on enhanced conversions for leads",
                "Lead actions are primary but enhanced conversions for leads is off. Hashed email/phone recovers conversions lost to cookie and consent gaps and enables offline qualified-lead imports.",
                "account", {"ec_leads": False}, impact_label="gains", conf="medium", risk="low")
        if not setting.get("accepted_terms"):
            add("customer_data_terms", "low", "Accept the customer data terms in Conversions settings",
                "Required before enhanced conversions or customer data uploads can work.", "account",
                {"accepted_customer_data_terms": False}, conf="high", risk="low")

    # 8. removed/hidden still recording, or recording names with no action
    for a in actions:
        if a["status"] in ("REMOVED", "HIDDEN") and a["all_conv_window"] > 0:
            add("removed_recording", "low", f"Remove the tag for '{a['name']}' or re-enable the action",
                f"The action is {a['status'].lower()} but recorded {a['all_conv_window']:.0f} conversions in the window, so its tag is still on the site firing for nothing.",
                a["name"], {"status": a["status"], "all_conv": a["all_conv_window"]}, conf="medium", risk="low")
    orphan = [n for n in daily if n and n not in by_name and sum(series(daily[n], days)) > 0]
    for n in orphan[:5]:
        issues.append({"kind": "unknown_action", "severity": "low", "entity": n, "why": "records conversions but is not in the actions list"})

    # 9. no primary action at all, or primary in a non-biddable goal
    if not counted:
        add("no_primary", "high", "Set at least one real business outcome as a primary conversion",
            "No enabled action is counted in Conversions. Smart bidding has nothing to optimize toward.",
            "account", {}, conf="high", risk="low")
    biddable = {(g.get("customer_conversion_goal.category"), g.get("customer_conversion_goal.origin")): g.get("customer_conversion_goal.biddable") for g in goals}
    for a in actions:
        if a["status"] == "ENABLED" and a["primary"] and goals and biddable.get((a["category"], a["origin"])) is False:
            issues.append({"kind": "non_biddable_goal", "severity": "low", "entity": a["name"],
                           "why": "primary, but its goal is not used for account-default bidding"})

    # tags we expect to find on site
    expected = {"aw_ids": set(), "labels": set()}
    for a in counted:
        for sn in a.get("snippets") or []:
            blob = json.dumps(sn)
            expected["aw_ids"].update(re.findall(r"AW-\d+", blob))
            expected["labels"].update(re.findall(r"AW-\d+/[\w-]+", blob))
    if setting.get("tracking_id") and setting["tracking_id"] not in ("", "0"):
        expected["aw_ids"].add(f"AW-{setting['tracking_id']}")

    score = 100
    for it in issues:
        score -= {"high": 20, "medium": 8, "low": 3}[it["severity"]]
    score = max(0, score)
    return {"actions": actions, "counted": counted, "findings": findings, "issues": issues, "days": days,
            "total_conv": total_conv, "score": score,
            "expected": {k: sorted(v) for k, v in expected.items()}}


# ---------------------------------------------------------------- output


def build_outputs(res, cid, currency, setting):
    cnt = defaultdict(int)
    for it in res["issues"]:
        cnt[it["severity"]] += 1
    dup = [f for f in res["findings"] if f.kind == "duplicate_conversion"]
    silent = [f for f in res["findings"] if f.kind == "silent_action"]
    bits = [f"{len(res['actions'])} conversion actions, {len(res['counted'])} counted in Conversions"]
    if dup:
        bits.append(f"{len(dup)} duplicate pair(s)")
    if silent:
        bits.append(f"{len(silent)} silent primary action(s)")
    headline = f"Tracking health {res['score']}/100: " + ", ".join(bits)
    md = [f"# Conversion tracking · {gads.fmt_cid(cid) or 'offline export'} · {res['days'][0]} to {res['days'][-1]}", "",
          f"**{headline}.**", "",
          f"- Issues: {cnt['high']} high, {cnt['medium']} medium, {cnt['low']} low",
          f"- Counted conversions in window: {res['total_conv']:.0f}"]
    if setting.get("known"):
        md.append(f"- Enhanced conversions for leads: {'on' if setting.get('ec_leads') else 'off'} · customer data terms: {'accepted' if setting.get('accepted_terms') else 'not accepted'} · account tracking status: {setting.get('status', '')}")
    md += ["", "## Findings", ""]
    order = {"high": 0, "medium": 1, "low": 2}
    for it in sorted(res["issues"], key=lambda x: order[x["severity"]]):
        md.append(f"- **{it['severity'].upper()}** · {it['entity']}: {it['why']}")
    if not res["issues"]:
        md.append("_No issues found._")
    md += ["", "## Conversion actions", "",
           gads.md_table([[a["name"], a["category"], a["type"], "yes" if (a["included"] or a["primary"]) else "no",
                           a["counting"].replace("_PER_CLICK", "").lower(), a["attribution"].replace("GOOGLE_SEARCH_ATTRIBUTION_", "").replace("GOOGLE_ADS_", "").lower(),
                           f"{a['all_conv_window']:.0f}", a["status"].lower()] for a in sorted(res["actions"], key=lambda x: -x["all_conv_window"])],
                         ["Action", "Category", "Source", "Counted", "Count", "Attribution", "All conv", "Status"]), "",
           "## What this audit cannot see", "",
           "- Whether a tag fires on the real thank-you page. Run `check_site_tags.py` on that URL, or walk through Tag Assistant.",
           "- Offline/CRM imports quality beyond counts. Compare imported qualified leads to CRM totals by week.",
           "- Consent mode impact. Modeled conversions appear with a delay of a few days; do not read the last 3 days as final."]
    rows = [{k: (v if not isinstance(v, (list, dict)) else json.dumps(v)) for k, v in a.items() if not k.startswith("_")} for a in res["actions"]]
    summary = {"headline": headline, "score": res["score"], "actions": len(res["actions"]), "counted": len(res["counted"]),
               "issues_high": cnt["high"], "issues_medium": cnt["medium"], "issues_low": cnt["low"],
               "expected_tags": res["expected"], "total_conv": round(res["total_conv"], 1)}
    extra = {"conversion_actions.csv": gads.to_csv(rows), "expected_tags.json": json.dumps(res["expected"], indent=2)}
    return res["findings"], summary, "\n".join(md) + "\n", extra


def daily_matrix_csv(daily, actions, days) -> str:
    names = [a["name"] for a in actions] + [n for n in daily if n not in {a["name"] for a in actions}]
    out = [{"date": d, **{n: round(daily.get(n, {}).get(d, [0, 0, 0])[1], 2) for n in names}} for d in days]
    return gads.to_csv(out, ["date"] + names)


def main():
    ap = gads.common_args(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    ap.add_argument("--daily", help="Offline: report with Day, Conversion action, Conversions, All conv. columns.")
    ap.add_argument("--recent-days", type=int, default=7, help="Window for silent/drop checks (default 7).")
    ap.set_defaults(days=56)
    args = ap.parse_args()
    start, end = gads.resolve_window(args)

    if args.csv:
        cid = gads.clean_cid(args.customer) or "offline"
        actions, daily, setting, goals = load_csv(args.csv, args.daily)
        currency = args.currency
        if args.daily and not args.start:
            all_days = sorted({d for v in daily.values() for d in v})
            if all_days:
                start, end = all_days[0], all_days[-1]
    else:
        if not args.customer:
            gads.die("--customer is required in live mode (or pass --csv).")
        cid = gads.clean_cid(args.customer)
        currency = gads.account_info(cid)["currency"]
        actions, daily, setting, goals = load_live(cid, start, end)
    if not actions:
        gads.die("No conversion actions found.")
    res = analyse(actions, daily, setting, goals, start, end, args, currency)
    approvals, summary, md, extra = build_outputs(res, cid, currency, setting)
    extra["daily_by_action.csv"] = daily_matrix_csv(daily, actions, res["days"])
    d = gads.write_results(SKILL, cid, approvals, summary, md, currency, extra)
    print(summary["headline"])
    print(f"-> {d}/{SKILL}.md  ·  queue: {d}/approvals.md")
    if args.json:
        print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
