#!/usr/bin/env python3
"""
audit_bids.py - bid strategy audit: targets vs actuals, brand overbidding, learning.

Live:     $PY audit_bids.py -c 123-456-7890 [--days 30]
Offline:  python3 audit_bids.py --csv campaigns_30d.csv [--recent campaigns_7d.csv]
              [--prior campaigns_prior.csv] [--keywords keywords.csv]
              [--network campaigns_by_network.csv] --currency USD

--network: Campaigns report segmented by "Network (with search partners)"
(Campaign, Network (with search partners), Cost, Clicks, Conversions, Conv. value).
Without it the Search Partners check is skipped offline.

Outputs (gads-out/<cid>/<date>/):
  gads-bid-audit.json / .md    summary + approval items
  bid_changes.csv              proposed target changes (input for apply_targets.py)
  bid_campaigns.csv            per-campaign diagnostic table
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gads  # noqa: E402
from gads import Approval, micros, money, num, pct  # noqa: E402

SKILL = "gads-bid-audit"

# Practitioner guidance, not Google policy. Google's Target CPA / Target ROAS
# help pages are the authority; check them before quoting numbers to a client.
MIN_CONV_TCPA = 30
MIN_CONV_TROAS = 50
STEP = 0.15            # max target move per change
LOOSE = 1.25           # target this far above actual CPA = loose
TIGHT = 0.80           # target this far below actual CPA = constrained
BRAND_IS_HIGH = 0.85   # brand search IS at or above this = already dominant
BRAND_CPC_DRIFT = 1.25 # brand CPC up 25%+ vs baseline = overbid signal
CHANGE_QUIET_DAYS = 14
PARTNER_MIN_CLICKS = 100   # partners need this many clicks in the window to judge
PARTNER_MIN_SHARE = 0.05   # and at least this share of the campaign's spend
PARTNER_CPA_WORSE = 1.5    # partners CPA this many times Google Search CPA = materially worse
PARTNER_ROAS_WORSE = 0.67  # partners ROAS below this share of Google Search ROAS = materially worse

CPA_TYPES = {"TARGET_CPA", "MAXIMIZE_CONVERSIONS"}
ROAS_TYPES = {"TARGET_ROAS", "MAXIMIZE_CONVERSION_VALUE"}
MANUAL_TYPES = {"MANUAL_CPC", "ENHANCED_CPC"}


def norm_strategy(s: str) -> str:
    """Map UI labels ('Target CPA', 'Maximize conversions', 'Manual CPC') to API enum names."""
    s = (s or "").strip()
    if s.isupper():
        return s
    t = s.lower()
    table = [
        ("target impression share", "TARGET_IMPRESSION_SHARE"), ("target cpa", "TARGET_CPA"),
        ("target roas", "TARGET_ROAS"), ("maximize conversion value", "MAXIMIZE_CONVERSION_VALUE"),
        ("maximise conversion value", "MAXIMIZE_CONVERSION_VALUE"), ("maximize conversions", "MAXIMIZE_CONVERSIONS"),
        ("maximise conversions", "MAXIMIZE_CONVERSIONS"), ("enhanced cpc", "ENHANCED_CPC"),
        ("manual cpc", "MANUAL_CPC"), ("maximize clicks", "TARGET_SPEND"), ("maximise clicks", "TARGET_SPEND"),
    ]
    for k, v in table:
        if k in t:
            return v
    return s.upper().replace(" ", "_")


def roas_val(v) -> float:
    """Target ROAS: API gives 3.5; UI gives '350%' or '350.00%'."""
    if v in (None, "", "--"):
        return 0.0
    x = num(v)
    return x / 100 if (isinstance(v, str) and "%" in v) or x > 20 else x


def ispct(v):
    """Impression share or None when the platform reports no value (PMax, '--')."""
    if v is None or str(v).strip() in ("", "--", "-"):
        return None
    return pct(v)


def fmt_pct(v) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def nice(st: str) -> str:
    return st.replace("_", " ").lower().replace("cpc", "CPC").replace("cpa", "CPA").replace("roas", "ROAS")


def strip_brand(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower())).strip()


def is_brand_text(text: str, brand: list[str]) -> bool:
    t = f" {strip_brand(text)} "
    return any(f" {strip_brand(b)} " in t for b in brand if b)


# ---------------------------------------------------------------- load


def camp_metrics_live(cid: str, start: str, end: str) -> dict[str, dict]:
    rows = gads.query(cid, f"""
        SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type,
               campaign.bidding_strategy_type, campaign.bidding_strategy,
               campaign.bidding_strategy_system_status,
               campaign.target_cpa.target_cpa_micros, campaign.maximize_conversions.target_cpa_micros,
               campaign.target_roas.target_roas, campaign.maximize_conversion_value.target_roas,
               campaign.target_impression_share.location,
               campaign.target_impression_share.location_fraction_micros,
               campaign.target_impression_share.cpc_bid_ceiling_micros,
               metrics.cost_micros, metrics.clicks, metrics.impressions, metrics.conversions,
               metrics.conversions_value, metrics.average_cpc, metrics.search_impression_share,
               metrics.search_absolute_top_impression_share, metrics.search_budget_lost_impression_share,
               metrics.search_rank_lost_impression_share
        FROM campaign
        WHERE {gads.gaql_dates(start, end)} AND campaign.status != 'REMOVED'""")
    out = {}
    for r in rows:
        cid_ = str(r.get("campaign.id"))
        st = r.get("campaign.bidding_strategy_type", "")
        tcpa = micros(r.get("campaign.target_cpa.target_cpa_micros")) or micros(r.get("campaign.maximize_conversions.target_cpa_micros"))
        troas = num(r.get("campaign.target_roas.target_roas")) or num(r.get("campaign.maximize_conversion_value.target_roas"))
        out[cid_] = {
            "campaign_id": cid_, "campaign": r.get("campaign.name", ""), "status": r.get("campaign.status", ""),
            "channel": r.get("campaign.advertising_channel_type", ""), "strategy": st,
            "portfolio": r.get("campaign.bidding_strategy", "") or "", "system_status": r.get("campaign.bidding_strategy_system_status", ""),
            "target_cpa": tcpa, "target_roas": troas,
            "tis_location": r.get("campaign.target_impression_share.location", ""),
            "tis_fraction": micros(r.get("campaign.target_impression_share.location_fraction_micros")),
            "tis_ceiling": micros(r.get("campaign.target_impression_share.cpc_bid_ceiling_micros")),
            "cost": micros(r.get("metrics.cost_micros")), "clicks": num(r.get("metrics.clicks")),
            "impr": num(r.get("metrics.impressions")), "conv": num(r.get("metrics.conversions")),
            "value": num(r.get("metrics.conversions_value")), "cpc": micros(r.get("metrics.average_cpc")),
            "is": ispct(r.get("metrics.search_impression_share")), "abs_top_is": ispct(r.get("metrics.search_absolute_top_impression_share")),
            "lost_budget": ispct(r.get("metrics.search_budget_lost_impression_share")),
            "lost_rank": ispct(r.get("metrics.search_rank_lost_impression_share")),
        }
    return out


def portfolio_targets_live(cid: str) -> dict[str, dict]:
    try:
        rows = gads.query(cid, """
            SELECT accessible_bidding_strategy.id, accessible_bidding_strategy.name,
                   accessible_bidding_strategy.type,
                   accessible_bidding_strategy.target_cpa.target_cpa_micros,
                   accessible_bidding_strategy.maximize_conversions.target_cpa_micros,
                   accessible_bidding_strategy.target_roas.target_roas,
                   accessible_bidding_strategy.maximize_conversion_value.target_roas
            FROM accessible_bidding_strategy""")
    except SystemExit:
        return {}
    out = {}
    for r in rows:
        out[str(r.get("accessible_bidding_strategy.id"))] = {
            "name": r.get("accessible_bidding_strategy.name", ""), "type": r.get("accessible_bidding_strategy.type", ""),
            "target_cpa": micros(r.get("accessible_bidding_strategy.target_cpa.target_cpa_micros"))
            or micros(r.get("accessible_bidding_strategy.maximize_conversions.target_cpa_micros")),
            "target_roas": num(r.get("accessible_bidding_strategy.target_roas.target_roas"))
            or num(r.get("accessible_bidding_strategy.maximize_conversion_value.target_roas")),
        }
    return out


def changes_live(cid: str) -> dict[str, list[str]]:
    """Bid/budget-relevant changes per campaign id in the last CHANGE_QUIET_DAYS."""
    since = (gads.today() - dt.timedelta(days=CHANGE_QUIET_DAYS)).isoformat()
    until = (gads.today() + dt.timedelta(days=1)).isoformat()
    try:
        rows = gads.query(cid, f"""
            SELECT change_event.change_date_time, change_event.change_resource_type,
                   change_event.campaign, change_event.changed_fields,
                   change_event.resource_change_operation
            FROM change_event
            WHERE change_event.change_date_time >= '{since}' AND change_event.change_date_time <= '{until}'
            ORDER BY change_event.change_date_time DESC
            LIMIT 5000""")
    except SystemExit:
        print("note: change history unavailable; learning-period guard uses system status only.", file=sys.stderr)
        return {}
    out = defaultdict(list)
    for r in rows:
        camp = str(r.get("change_event.campaign", "")).split("/")[-1]
        fields = r.get("change_event.changed_fields", "")
        if isinstance(fields, dict):
            fields = ",".join(fields.get("paths", []))
        elif isinstance(fields, list):
            fields = ",".join(fields)
        fields = str(fields)
        rtype = str(r.get("change_event.change_resource_type", ""))
        if rtype in ("CAMPAIGN", "CAMPAIGN_BUDGET", "BIDDING_STRATEGY") and re.search(
                r"bidding|target_cpa|target_roas|amount_micros|target_impression|manual_cpc|maximize", fields + rtype.lower()):
            out[camp].append(f"{str(r.get('change_event.change_date_time', ''))[:10]} {rtype.lower()}: {fields[:80]}")
    return out


SIM_FIELDS = {"TARGET_CPA": ("target_cpa_point_list", "target_cpa_micros", True),
              "TARGET_ROAS": ("target_roas_point_list", "target_roas", False)}


def simulations_live(cid: str) -> dict[str, dict]:
    """Bid simulator points per campaign id: {cid: {"TARGET_CPA": {"points", "start", "end"}}}.

    Soft: an error or an account without simulations just means no preview.
    """
    out: dict[str, dict] = {}
    for kind, (field, _, _) in SIM_FIELDS.items():
        rows = gads.query(cid, f"""
            SELECT campaign_simulation.campaign_id, campaign_simulation.type,
                   campaign_simulation.start_date, campaign_simulation.end_date,
                   campaign_simulation.{field}.points
            FROM campaign_simulation
            WHERE campaign_simulation.type = '{kind}'""", soft=True)
        for r in rows:
            pts = r.get(f"campaign_simulation.{field}.points") or []
            out.setdefault(str(r.get("campaign_simulation.campaign_id")), {})[kind] = {
                "points": pts, "start": r.get("campaign_simulation.start_date", ""), "end": r.get("campaign_simulation.end_date", "")}
    return out


def network_live(cid: str, start: str, end: str) -> dict[str, dict]:
    rows = gads.query(cid, f"""
        SELECT campaign.id, campaign.name, campaign.network_settings.target_search_network,
               segments.ad_network_type, metrics.cost_micros, metrics.clicks,
               metrics.conversions, metrics.conversions_value
        FROM campaign
        WHERE {gads.gaql_dates(start, end)} AND campaign.status != 'REMOVED'
          AND campaign.advertising_channel_type = 'SEARCH'
          AND segments.ad_network_type IN ('SEARCH', 'SEARCH_PARTNERS')""", soft=True)
    out: dict[str, dict] = {}
    for r in rows:
        net = "partners" if r.get("segments.ad_network_type") == "SEARCH_PARTNERS" else "search"
        n = out.setdefault(r.get("campaign.name", ""), new_net(str(r.get("campaign.id", ""))))
        n["partners_on"] = bool(r.get("campaign.network_settings.target_search_network"))
        add_net(n[net], micros(r.get("metrics.cost_micros")), num(r.get("metrics.clicks")),
                num(r.get("metrics.conversions")), num(r.get("metrics.conversions_value")))
    return out


def new_net(campaign_id: str = "") -> dict:
    z = lambda: {"cost": 0.0, "clicks": 0.0, "conv": 0.0, "value": 0.0}  # noqa: E731
    return {"campaign_id": campaign_id, "partners_on": None, "search": z(), "partners": z()}


def add_net(a: dict, cost: float, clicks: float, conv: float, value: float) -> None:
    a["cost"] += cost
    a["clicks"] += clicks
    a["conv"] += conv
    a["value"] += value


def network_csv(path: str) -> dict[str, dict]:
    """Campaigns report segmented by 'Network (with search partners)'."""
    out: dict[str, dict] = {}
    for r in gads.read_ui_csv(path):
        name = gads.pick(r, "campaign")
        netv = str(gads.pick(r, "network_with_search_partners", "network", "ad_network_type")).lower()
        if not name or not netv:
            continue
        if "partner" in netv:
            net = "partners"
        elif netv in ("google search", "search", "google_search") or netv.startswith("google search"):
            net = "search"
        else:
            continue  # display, youtube, cross-network
        n = out.setdefault(name, new_net(gads.pick(r, "campaign_id")))
        add_net(n[net], num(gads.pick(r, "cost")), num(gads.pick(r, "clicks")),
                num(gads.pick(r, "conversions", "conv")), num(gads.pick(r, "conv_value", "conversion_value")))
    for n in out.values():
        n["partners_on"] = n["partners"]["clicks"] > 0 or n["partners"]["cost"] > 0
    return out


def brand_keywords_live(cid: str, start: str, end: str) -> list[dict]:
    rows = gads.query(cid, f"""
        SELECT campaign.id, campaign.name, ad_group_criterion.keyword.text,
               ad_group_criterion.keyword.match_type,
               metrics.cost_micros, metrics.clicks, metrics.conversions, metrics.conversions_value
        FROM keyword_view
        WHERE {gads.gaql_dates(start, end)} AND ad_group_criterion.negative = FALSE
          AND ad_group_criterion.status != 'REMOVED'""")
    return [{"campaign_id": str(r.get("campaign.id")), "campaign": r.get("campaign.name", ""),
             "text": r.get("ad_group_criterion.keyword.text", ""), "cost": micros(r.get("metrics.cost_micros")),
             "clicks": num(r.get("metrics.clicks")), "conv": num(r.get("metrics.conversions")),
             "value": num(r.get("metrics.conversions_value"))} for r in rows]


def camp_csv(path: str) -> dict[str, dict]:
    out = {}
    for r in gads.read_ui_csv(path):
        name = gads.pick(r, "campaign")
        if not name or name.lower().startswith("total"):
            continue
        key = gads.pick(r, "campaign_id", default=name)
        st = norm_strategy(gads.pick(r, "bid_strategy_type", "campaign_bid_strategy_type", "bidding_strategy_type"))
        out[str(key)] = {
            "campaign_id": gads.pick(r, "campaign_id"), "campaign": name,
            "status": gads.pick(r, "campaign_status", "status"), "channel": gads.pick(r, "campaign_type", default="SEARCH").upper(),
            "strategy": st, "portfolio": gads.pick(r, "bid_strategy", "portfolio_bid_strategy"),
            "system_status": gads.pick(r, "bid_strategy_status", "bidding_strategy_status"),
            "target_cpa": num(gads.pick(r, "target_cpa", "max_cpa")), "target_roas": roas_val(gads.pick(r, "target_roas")),
            "tis_location": "", "tis_fraction": pct(gads.pick(r, "target_impression_share", default=0)), "tis_ceiling": 0,
            "cost": num(gads.pick(r, "cost")), "clicks": num(gads.pick(r, "clicks")), "impr": num(gads.pick(r, "impr", "impressions")),
            "conv": num(gads.pick(r, "conversions", "conv")), "value": num(gads.pick(r, "conv_value", "conversion_value")),
            "cpc": num(gads.pick(r, "avg_cpc")), "is": ispct(gads.pick(r, "search_impr_share", "search_impression_share")),
            "abs_top_is": ispct(gads.pick(r, "search_abs_top_is", "search_abs_top_impr_share", "search_absolute_top_is")),
            "lost_budget": ispct(gads.pick(r, "search_lost_is_budget")), "lost_rank": ispct(gads.pick(r, "search_lost_is_rank")),
        }
    return out


def kw_csv(path: str) -> list[dict]:
    out = []
    for r in gads.read_ui_csv(path):
        text = gads.pick(r, "keyword", "search_keyword")
        if not text:
            continue
        out.append({"campaign_id": gads.pick(r, "campaign_id"), "campaign": gads.pick(r, "campaign"),
                    "text": text.strip('[]"+'), "cost": num(gads.pick(r, "cost")), "clicks": num(gads.pick(r, "clicks")),
                    "conv": num(gads.pick(r, "conversions", "conv")), "value": num(gads.pick(r, "conv_value"))})
    return out


def by_name(d: dict[str, dict]) -> dict[str, dict]:
    return {v["campaign"]: v for v in d.values()}


# ---------------------------------------------------------------- analysis


def sim_at(points: list[dict], key: str, scale_micros: bool, want: float) -> dict | None:
    """Simulator cost/conversions/value at a target, interpolated between points.

    Outside the simulated range it returns the nearest point and marks it.
    """
    pts = []
    for p in points:
        v = micros(p.get(key)) if scale_micros else num(p.get(key))
        if v:
            pts.append((v, micros(p.get("cost_micros")), num(p.get("biddable_conversions")), num(p.get("biddable_conversions_value"))))
    if not pts or not want:
        return None
    pts.sort()
    if want <= pts[0][0] or want >= pts[-1][0]:
        t, c, cv, val = pts[0] if want <= pts[0][0] else pts[-1]
        exact = abs(t - want) <= 0.02 * want
        return {"target": t, "cost": c, "conv": cv, "value": val, "nearest": not exact}
    for (t0, c0, v0, x0), (t1, c1, v1, x1) in zip(pts, pts[1:]):
        if t0 <= want <= t1:
            f = (want - t0) / (t1 - t0) if t1 > t0 else 0.0
            return {"target": want, "cost": c0 + (c1 - c0) * f, "conv": v0 + (v1 - v0) * f,
                    "value": x0 + (x1 - x0) * f, "nearest": False}
    return None


def sim_preview(sim: dict | None, kind: str, current: float, proposed: float) -> dict | None:
    """Simulator at the current and the proposed target. None when there is nothing usable."""
    if not sim or not sim.get("points"):
        return None
    _, key, scale = SIM_FIELDS[kind]
    at_new = sim_at(sim["points"], key, scale, proposed)
    if not at_new:
        return None
    at_cur = sim_at(sim["points"], key, scale, current) if current else None
    if at_new["nearest"] and (at_cur is None or at_cur["target"] == at_new["target"]):
        return None  # the simulator does not reach the proposed target, so it says nothing useful
    return {"current": at_cur, "proposed": at_new, "start": sim.get("start", ""), "end": sim.get("end", "")}


def sim_text(pv: dict, kind: str, cur: str) -> str:
    def f(label, x):
        t = money(x["target"], cur) if kind == "TARGET_CPA" else f"{x['target']:.0%}"
        out = f"{label} ({t}{', nearest simulated point' if x['nearest'] else ''}): {money(x['cost'], cur)} cost, {x['conv']:.0f} conversions"
        return out + (f" worth {money(x['value'], cur)}" if kind == "TARGET_ROAS" and x["value"] else "")
    period = f" ({pv['start']} to {pv['end']})" if pv.get("start") else ""
    parts = ([f("current", pv["current"])] if pv["current"] else []) + [f("proposed", pv["proposed"])]
    return f" Google's simulator estimates{period}: " + "; ".join(parts) + "."


def sim_evidence(pv: dict | None) -> dict:
    if not pv:
        return {}
    r = lambda x: None if not x else {k: (round(v, 2) if isinstance(v, float) else v) for k, v in x.items()}  # noqa: E731
    return {"simulator": {"period": f"{pv['start']}..{pv['end']}", "current": r(pv["current"]), "proposed": r(pv["proposed"])}}


def partners_check(net: dict[str, dict], camps: dict[str, dict], cur: str, days: int) -> list[Approval]:
    """Search Partners vs Google Search, per Search campaign."""
    scale = 30.0 / days
    by = by_name(camps)
    out = []
    for name, n in sorted(net.items()):
        s, p = n["search"], n["partners"]
        if n["partners_on"] is False or p["clicks"] < PARTNER_MIN_CLICKS:
            continue
        total = s["cost"] + p["cost"]
        if not total or p["cost"] < PARTNER_MIN_SHARE * total:
            continue
        c = by.get(name, {})
        value_mode = c.get("strategy") in ROAS_TYPES or (s["value"] > 0 and not c)
        if value_mode and s["value"] and s["cost"]:
            s_roas, p_roas = s["value"] / s["cost"], p["value"] / p["cost"]
            if p_roas > s_roas * PARTNER_ROAS_WORSE:
                continue
            excess = p["cost"] - p["value"] / s_roas
            gap = f"ROAS {p_roas:.0%} on {p['clicks']:.0f} clicks vs {s_roas:.0%} on Google Search"
            ev = {"search_roas": round(s_roas, 3), "partners_roas": round(p_roas, 3)}
        elif s["conv"]:
            s_cpa = s["cost"] / s["conv"]
            p_cpa = p["cost"] / p["conv"] if p["conv"] else None
            if p_cpa is not None and p_cpa < s_cpa * PARTNER_CPA_WORSE:
                continue
            if p_cpa is None and p["cost"] < 2 * s_cpa:
                continue
            excess = p["cost"] - p["conv"] * s_cpa
            gap = (f"CPA {money(p_cpa, cur)} on {p['clicks']:.0f} clicks vs {money(s_cpa, cur)} on Google Search ({p_cpa / s_cpa:.1f}x)"
                   if p_cpa else f"{money(p['cost'], cur)} and {p['clicks']:.0f} clicks with no conversions, vs {money(s_cpa, cur)} CPA on Google Search")
            ev = {"search_cpa": round(s_cpa, 2), "partners_cpa": round(p_cpa, 2) if p_cpa else None}
        else:
            continue
        if excess <= 0:
            continue
        out.append(Approval(
            skill=SKILL, kind="search_partners_off", entity=name,
            action=f"Turn off Search Partners on '{name}'",
            why=(f"Over {days} days Search Partners ran {gap}. Partners took {p['cost'] / total:.0%} of spend for "
                 f"{(p['conv'] / (s['conv'] + p['conv'])) if (s['conv'] + p['conv']) else 0:.0%} of conversions. "
                 f"Turning it off saves about {money(excess * scale, cur)}/month over what those conversions would cost on Google Search, "
                 f"and gives up about {p['conv'] * scale:.0f} conversions/month that Google Search may partly recover. Change it in Settings > Networks."),
            impact_monthly=excess * scale, impact_label="saves", confidence="high" if p["clicks"] >= 3 * PARTNER_MIN_CLICKS else "medium",
            risk="low", severity="medium",
            evidence={"days": days, "search": {k: round(v, 2) for k, v in s.items()}, "partners": {k: round(v, 2) for k, v in p.items()}, **ev},
            change={"campaign_id": n.get("campaign_id") or c.get("campaign_id"), "field": "campaign.network_settings.target_search_network",
                    "value": False}))
    return out


def analyse(camps, recent, prior, kws, portfolios, changes, sims, prof, cur, days):
    brand = [b for b in prof.get("brand_terms", []) if b]
    biz_cpa = prof.get("target_cpa")
    biz_roas = prof.get("target_roas")
    scale = 30.0 / days
    recent_n, prior_n = by_name(recent), by_name(prior)

    # brand classification per campaign
    kw_by_camp = defaultdict(lambda: {"cost": 0.0, "clicks": 0.0, "conv": 0.0, "value": 0.0,
                                      "b_cost": 0.0, "b_clicks": 0.0, "b_conv": 0.0, "b_value": 0.0, "b_kws": set()})
    for k in kws:
        a = kw_by_camp[k["campaign"]]
        for m in ("cost", "clicks", "conv", "value"):
            a[m] += k[m]
        if is_brand_text(k["text"], brand):
            for m in ("cost", "clicks", "conv", "value"):
                a["b_" + m] += k[m]
            a["b_kws"].add(k["text"])

    for c in camps.values():
        kc = kw_by_camp.get(c["campaign"])
        share = (kc["b_cost"] / kc["cost"]) if kc and kc["cost"] else None
        name_brand = bool(re.search(r"\bbrand(ed)?\b", c["campaign"], re.I)) or is_brand_text(c["campaign"], brand)
        c["brand_share"] = share
        c["is_brand"] = (share is not None and share >= 0.6) or (share is None and name_brand) or (name_brand and (share or 0) >= 0.3)
        # portfolio targets
        pid = str(c.get("portfolio", "")).split("/")[-1]
        if pid and pid in portfolios:
            p = portfolios[pid]
            c["portfolio_name"] = p["name"]
            c["target_cpa"] = c["target_cpa"] or p["target_cpa"]
            c["target_roas"] = c["target_roas"] or p["target_roas"]
            if not c["strategy"] or c["strategy"] == "UNSPECIFIED":
                c["strategy"] = p["type"]
        else:
            c["portfolio_name"] = c.get("portfolio", "") if c.get("portfolio") and "/" not in str(c.get("portfolio")) else ""
        c["cpa"] = gads.safe_div(c["cost"], c["conv"])
        c["roas"] = gads.safe_div(c["value"], c["cost"])
        r7 = recent_n.get(c["campaign"])
        c["cpa_7d"] = gads.safe_div(r7["cost"], r7["conv"]) if r7 else None
        c["roas_7d"] = gads.safe_div(r7["value"], r7["cost"]) if r7 else None
        c["is_known"] = c["lost_rank"] is not None
        for k in ("is", "abs_top_is", "lost_budget", "lost_rank"):
            c[k + "_v"] = c[k] or 0.0
        c["recent_changes"] = changes.get(str(c.get("campaign_id")), [])
        c["learning"] = "LEARNING" in str(c.get("system_status", "")).upper()

    non_brand = [c for c in camps.values() if not c["is_brand"] and c["clicks"]]
    nb_cpc = gads.safe_div(sum(c["cost"] for c in non_brand), sum(c["clicks"] for c in non_brand))

    approvals: list[Approval] = []
    changes_rows: list[dict] = []
    notes: list[str] = []
    brand_overspend = 0.0

    def held(c) -> str | None:
        if c["learning"]:
            return f"bid strategy is in learning ({c['system_status']})"
        if c["recent_changes"]:
            return f"{len(c['recent_changes'])} bid/budget change(s) in the last {CHANGE_QUIET_DAYS} days ({c['recent_changes'][0]})"
        return None

    for c in sorted(camps.values(), key=lambda x: -x["cost"]):
        if c["cost"] <= 0 or str(c.get("status", "")).upper() in ("PAUSED", "REMOVED"):
            continue
        st = c["strategy"]
        name = c["campaign"]
        hold = held(c)

        # ---------- brand overbidding
        if c["is_brand"]:
            base = prior_n.get(name)
            base_cpc = base["cpc"] if base and base.get("cpc") else (gads.safe_div(base["cost"], base["clicks"]) if base else None)
            reasonable = prof.get("brand_max_cpc") or base_cpc
            dominant = c["is_v"] >= BRAND_IS_HIGH or c["abs_top_is_v"] >= 0.75
            basis, conf = "", "medium"
            if reasonable and c["cpc"] > reasonable * BRAND_CPC_DRIFT and dominant:
                basis = (f"brand CPC {money(c['cpc'], cur)} vs {money(reasonable, cur)} "
                         f"{'profile cap' if prof.get('brand_max_cpc') else 'in the baseline period'}")
                conf = "high" if c["is_v"] >= 0.9 else "medium"
            elif not reasonable and nb_cpc and dominant and c["cpc"] > 0.5 * nb_cpc:
                reasonable = 0.35 * nb_cpc
                basis = (f"brand CPC {money(c['cpc'], cur)} is {c['cpc'] / nb_cpc:.0%} of non-brand CPC {money(nb_cpc, cur)}; "
                         f"no baseline period supplied, so the reference CPC ({money(reasonable, cur)}) is a heuristic")
                conf = "low"
            if basis:
                over = max(0.0, (c["cpc"] - reasonable) * c["clicks"])
                brand_overspend += over
                ceiling = round(reasonable * 1.2, 2)
                smart = st in CPA_TYPES | ROAS_TYPES | {"TARGET_SPEND"}
                action = (f"Switch '{name}' to Target Impression Share, absolute top 90%, max CPC {money(ceiling, cur)}"
                          if st != "TARGET_IMPRESSION_SHARE" else
                          f"Lower the max CPC cap on '{name}' to {money(ceiling, cur)}")
                approvals.append(Approval(
                    skill=SKILL, kind="brand_overbid", entity=name, action=action,
                    why=(f"Brand already holds {fmt_pct(c['is'])} impression share ({fmt_pct(c['abs_top_is'])} absolute top) yet pays {basis}. "
                         f"Estimated overbid: {money(over, cur)} in {days} days."
                         + (" Smart Bidding on brand bids up for clicks you would mostly get anyway." if smart else "")),
                    impact_monthly=over * scale, impact_label="saves", confidence=conf, risk="medium" if hold else "low",
                    evidence={"cpc": round(c["cpc"], 2), "reference_cpc": round(reasonable, 2), "clicks": int(c["clicks"]),
                              "impression_share": c["is"], "abs_top_is": c["abs_top_is"], "strategy": st},
                    change={"campaign_id": c.get("campaign_id"), "strategy": "TARGET_IMPRESSION_SHARE",
                            "location": "ABSOLUTE_TOP_OF_PAGE", "fraction": 0.9, "cpc_ceiling": ceiling}))
                c["finding"] = f"brand overbid ~{money(over, cur)}"
            continue

        # ---------- brand leaking into non-brand
        if c.get("brand_share") and c["brand_share"] >= 0.15:
            kc = kw_by_camp[name]
            nb_cpa = gads.safe_div(kc["cost"] - kc["b_cost"], kc["conv"] - kc["b_conv"])
            approvals.append(Approval(
                skill=SKILL, kind="brand_mixing", entity=name,
                action=f"Move {len(kc['b_kws'])} brand keywords out of '{name}' and add brand as a negative there",
                why=(f"Brand keywords are {c['brand_share']:.0%} of this campaign's spend and "
                     f"{gads.safe_div(kc['b_conv'], kc['conv']) or 0:.0%} of its conversions. Blended CPA {money(c['cpa'], cur)} "
                     f"hides a non-brand CPA of {money(nb_cpa, cur)}, so its bid target is set on the wrong number."),
                impact_label="fixes measurement", confidence="high", risk="medium",
                evidence={"brand_cost_share": round(c["brand_share"], 3), "blended_cpa": round(c["cpa"] or 0, 2),
                          "non_brand_cpa": round(nb_cpa or 0, 2), "brand_keywords": sorted(kc["b_kws"])[:10]}))
            notes.append(f"'{name}': no target change proposed while brand is mixed in. Its blended CPA is not the number to bid on; split brand out, then re-run.")
            continue

        # ---------- manual CPC with enough data
        if st in MANUAL_TYPES:
            if c["conv"] * scale >= MIN_CONV_TCPA and c["cpa"]:
                tgt = round(c["cpa"] * 1.05, 2)
                approvals.append(Approval(
                    skill=SKILL, kind="strategy_switch", entity=name,
                    action=f"Move '{name}' from {nice(st)} to Maximize conversions with a {money(tgt, cur)} target CPA",
                    why=(f"{c['conv']:.0f} conversions in {days} days is enough data for Smart Bidding. Start the target at actual CPA "
                         f"({money(c['cpa'], cur)}) plus 5% so volume holds through learning, then tighten in 15% steps."
                         + (" Enhanced CPC no longer adjusts Search bids, so this campaign is effectively manual." if st == "ENHANCED_CPC" else "")),
                    impact_label="gains", confidence="medium", risk="medium",
                    evidence={"conv": round(c["conv"], 1), "cpa": round(c["cpa"], 2), "strategy": st},
                    change={"campaign_id": c.get("campaign_id"), "strategy": "MAXIMIZE_CONVERSIONS", "target_cpa": tgt}))
            continue

        # ---------- target CPA family
        if st in CPA_TYPES:
            conv_m = c["conv"] * scale
            if conv_m < MIN_CONV_TCPA and c["target_cpa"]:
                notes.append(f"'{name}': {conv_m:.0f} conv/month on a target CPA, under the ~{MIN_CONV_TCPA} commonly cited. "
                             "Consider a portfolio strategy across similar campaigns or a higher-volume conversion action.")
            if not c["cpa"]:
                continue
            tgt, act = c["target_cpa"], c["cpa"]
            new, why, kind = None, "", ""
            if tgt and tgt < act * TIGHT and (not c["is_known"] or c["lost_rank_v"] >= 0.3 or c["lost_budget_v"] < 0.05):
                new = min(act, tgt * (1 + STEP))
                kind = "loosen_target"
                why = (f"Target {money(tgt, cur)} is {1 - tgt / act:.0%} below actual CPA {money(act, cur)}, so the bidder is constrained"
                       + (f": {c['lost_rank_v']:.0%} of impressions lost to rank, {c['lost_budget_v']:.0%} to budget." if c["is_known"] else ".")
                       + f" Raising the target {STEP:.0%} buys back volume.")
            elif tgt and tgt > act * LOOSE:
                new = max(act * 1.05, tgt * (1 - STEP))
                kind = "tighten_target"
                why = (f"Target {money(tgt, cur)} sits {tgt / act - 1:.0%} above actual CPA {money(act, cur)}, so it is not steering bids. "
                       f"Bring it toward actual in one {STEP:.0%} step.")
            elif biz_cpa and act > biz_cpa * 1.1 and (not tgt or tgt > biz_cpa):
                start_from = tgt or act
                new = max(biz_cpa, start_from * (1 - STEP))
                kind = "tighten_target"
                why = (f"Actual CPA {money(act, cur)} is {act / biz_cpa - 1:.0%} over the business target {money(biz_cpa, cur)}. "
                       f"Step the bid target from {money(start_from, cur)} to {money(new, cur)} (max {STEP:.0%}); "
                       "jumping straight to the business target usually chokes volume.")
            if new:
                new = round(new, 2)
                pv = sim_preview(sims.get(str(c.get("campaign_id")), {}).get("TARGET_CPA"), "TARGET_CPA", tgt or 0, new)
                if pv:
                    why += sim_text(pv, "TARGET_CPA", cur)
                if c["cpa_7d"] and c["cpa_7d"] > act * 1.3:
                    why += f" Last 7 days CPA is {money(c['cpa_7d'], cur)}, worse than the 30-day figure; watch before tightening further."
                if hold:
                    why += f" Hold until quiet: {hold}."
                gain = None
                if kind == "tighten_target" and (tgt or act) and c["conv"]:
                    gain = max(0.0, (act - new) * c["conv"]) * scale * 0.5  # half-credit: volume usually dips
                approvals.append(Approval(
                    skill=SKILL, kind=kind, entity=name,
                    action=(f"{'Hold, then set' if hold else 'Set'} target CPA on '{name}' from {money(tgt, cur)} to {money(new, cur)}" if tgt
                            else f"{'Hold, then add' if hold else 'Add'} a {money(new, cur)} target CPA to '{name}'"),
                    why=why, impact_monthly=gain, impact_label="saves" if gain else "gains",
                    confidence="low" if conv_m < MIN_CONV_TCPA else ("medium" if hold else "high"),
                    risk="high" if hold else "medium",
                    change={"campaign_id": c.get("campaign_id"), "metric": "target_cpa", "proposed": new},
                    evidence={"target_cpa": round(tgt or 0, 2), "cpa_30d": round(act, 2), "cpa_7d": round(c["cpa_7d"] or 0, 2),
                              "conv_30d": round(c["conv"] * scale, 1), "lost_is_rank": c["lost_rank"],
                              "lost_is_budget": c["lost_budget"], "portfolio": c.get("portfolio_name", ""), **sim_evidence(pv)}))
                if not c.get("portfolio_name") and c.get("campaign_id"):
                    changes_rows.append({"campaign_id": c["campaign_id"], "campaign": name, "strategy": st, "metric": "target_cpa",
                                         "current": round(tgt or 0, 2), "proposed": new, "held": "yes" if hold else "no"})
                elif c.get("portfolio_name"):
                    notes.append(f"'{name}' uses portfolio '{c['portfolio_name']}'; change the target on the portfolio, which affects every campaign in it.")
            continue

        # ---------- target ROAS family
        if st in ROAS_TYPES:
            conv_m = c["conv"] * scale
            if conv_m < MIN_CONV_TROAS and c["target_roas"]:
                notes.append(f"'{name}': {conv_m:.0f} conv/month on target ROAS, under the ~{MIN_CONV_TROAS} commonly cited.")
            if not c["roas"]:
                continue
            tgt, act = c["target_roas"], c["roas"]
            new, why, kind = None, "", ""
            if tgt and tgt > act / TIGHT and (not c["is_known"] or c["lost_rank_v"] >= 0.3 or c["lost_budget_v"] < 0.05):
                new = max(act, tgt * (1 - STEP))
                kind = "loosen_target"
                why = (f"Target ROAS {tgt:.0%} is well above actual {act:.0%}, so the bidder is constrained"
                       + (f" ({c['lost_rank_v']:.0%} IS lost to rank)." if c["is_known"] else " (no impression share reported for this campaign type).")
                       + f" Lower the target {STEP:.0%} to recover volume, or keep it if margin requires {tgt:.0%}.")
            elif tgt and tgt < act / LOOSE:
                new = min(act * 0.95, tgt * (1 + STEP))
                kind = "tighten_target"
                why = f"Target ROAS {tgt:.0%} is far below actual {act:.0%}, so it is not steering bids. Raise it one {STEP:.0%} step."
            elif biz_roas and act < biz_roas * 0.9 and (not tgt or tgt < biz_roas):
                start_from = tgt or act
                new = min(biz_roas, start_from * (1 + STEP))
                kind = "tighten_target"
                why = f"Actual ROAS {act:.0%} is under the business target {biz_roas:.0%}. Step the bid target to {new:.0%} (max {STEP:.0%})."
            if new:
                new = round(new, 2)
                pv = sim_preview(sims.get(str(c.get("campaign_id")), {}).get("TARGET_ROAS"), "TARGET_ROAS", tgt or 0, new)
                if pv:
                    why += sim_text(pv, "TARGET_ROAS", cur)
                if hold:
                    why += f" Hold until quiet: {hold}."
                approvals.append(Approval(
                    skill=SKILL, kind=kind, entity=name,
                    action=f"{'Hold, then set' if hold else 'Set'} target ROAS on '{name}' from {tgt:.0%} to {new:.0%}" if tgt else f"Add a {new:.0%} target ROAS to '{name}'",
                    why=why, impact_label="gains", confidence="low" if conv_m < MIN_CONV_TROAS else "medium",
                    risk="high" if hold else "medium",
                    evidence={"target_roas": tgt, "roas_30d": round(act, 3), "roas_7d": round(c["roas_7d"] or 0, 3),
                              "conv_30d": round(conv_m, 1), "lost_is_rank": c["lost_rank"], **sim_evidence(pv)}))
                if not c.get("portfolio_name") and c.get("campaign_id"):
                    changes_rows.append({"campaign_id": c["campaign_id"], "campaign": name, "strategy": st, "metric": "target_roas",
                                         "current": tgt, "proposed": new, "held": "yes" if hold else "no"})
            continue

        if st == "TARGET_SPEND" and c["conv"] * scale >= MIN_CONV_TCPA:
            notes.append(f"'{name}' runs Maximize clicks with {c['conv'] * scale:.0f} conv/month; it is optimising for clicks, not outcomes.")

    table = []
    for c in sorted(camps.values(), key=lambda x: -x["cost"]):
        if c["cost"] <= 0:
            continue
        table.append({"campaign": c["campaign"], "brand": "yes" if c["is_brand"] else "", "strategy": c["strategy"],
                      "portfolio": c.get("portfolio_name", ""), "target_cpa": round(c["target_cpa"] or 0, 2),
                      "target_roas": round(c["target_roas"] or 0, 3), "cost": round(c["cost"], 2), "conv": round(c["conv"], 1),
                      "cpa": round(c["cpa"] or 0, 2), "cpa_7d": round(c["cpa_7d"] or 0, 2), "roas": round(c["roas"] or 0, 3),
                      "cpc": round(c["cpc"], 2), "is": c["is"], "abs_top_is": c["abs_top_is"],
                      "lost_budget": c["lost_budget"], "lost_rank": c["lost_rank"],
                      "learning": "yes" if c["learning"] else "", "recent_changes": len(c["recent_changes"])})
    return approvals, changes_rows, notes, table, brand_overspend


# ---------------------------------------------------------------- main


def main():
    ap = gads.common_args(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    ap.add_argument("--recent", help="Offline: campaign report for the last 7 days.")
    ap.add_argument("--prior", help="Offline: campaign report for a baseline period (e.g. 61-90 days ago) for brand CPC drift.")
    ap.add_argument("--keywords", help="Offline: keyword report (Keyword, Campaign, Cost, Clicks, Conversions) for brand detection.")
    ap.add_argument("--network", help="Offline: campaign report segmented by Network (with search partners) for the Search Partners check.")
    ap.add_argument("--brand", help="Comma-separated brand terms (adds to the profile).")
    ap.add_argument("--target-cpa", type=float, help="Business target CPA (overrides profile).")
    ap.add_argument("--target-roas", type=float, help="Business target ROAS as a ratio, e.g. 4 for 400%% (overrides profile).")
    args = ap.parse_args()

    start, end = gads.resolve_window(args)
    days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1

    if args.csv:
        cid = gads.clean_cid(args.customer)
        cur = args.currency
        camps = {}
        for p in args.csv:
            camps.update(camp_csv(p))
        recent = camp_csv(args.recent) if args.recent else {}
        prior = camp_csv(args.prior) if args.prior else {}
        kws = kw_csv(args.keywords) if args.keywords else []
        net = network_csv(args.network) if args.network else None
        portfolios, changes, sims = {}, {}, {}
    else:
        if not args.customer:
            gads.die("--customer is required in live mode (or pass --csv).")
        cid = gads.clean_cid(args.customer)
        cur = gads.account_info(cid)["currency"]
        camps = camp_metrics_live(cid, start, end)
        r_start, r_end = gads.date_window(7)
        recent = camp_metrics_live(cid, r_start, r_end)
        p_end = (dt.date.fromisoformat(start) - dt.timedelta(days=31)).isoformat()
        p_start = (dt.date.fromisoformat(p_end) - dt.timedelta(days=29)).isoformat()
        prior = camp_metrics_live(cid, p_start, p_end)
        kws = brand_keywords_live(cid, start, end)
        portfolios = portfolio_targets_live(cid)
        changes = changes_live(cid)
        sims = simulations_live(cid)
        net = network_live(cid, start, end)
    if not camps:
        gads.die("No campaigns with data in the window.")

    prof = gads.load_profile(cid)
    if args.brand:
        prof["brand_terms"] = list(prof.get("brand_terms", [])) + [b.strip() for b in args.brand.split(",") if b.strip()]
    if args.target_cpa:
        prof["target_cpa"] = args.target_cpa
    if args.target_roas:
        prof["target_roas"] = args.target_roas
    if not prof.get("brand_terms"):
        print("warning: no brand_terms in profile or --brand; brand campaigns are detected by name only.", file=sys.stderr)

    approvals, change_rows, notes, table, overspend = analyse(camps, recent, prior, kws, portfolios, changes, sims, prof, cur, days)
    partner_items = partners_check(net, camps, cur, days) if net is not None else []
    approvals += partner_items

    n_target = sum(1 for a in approvals if a.kind in ("loosen_target", "tighten_target"))
    n_partners = len(partner_items)
    tc = [a for a in approvals if a.kind == "tighten_target" and "target CPA" in a.action]
    headline_bits = []
    if overspend:
        headline_bits.append(f"~{money(overspend, cur)} of brand overbidding in {days}d")
    if tc:
        headline_bits.append(f"{money(tc[0].change.get('proposed'), cur)} target CPA suggested for '{tc[0].entity}'")
    if n_partners:
        headline_bits.append(f"Search Partners underperforming on {n_partners} campaign(s)")
    headline_bits.append(f"{n_target} target change(s), {len(approvals)} items")
    headline = "; ".join(headline_bits)

    md = [f"# Bid audit · {gads.fmt_cid(cid) or 'offline export'} · last {days} days", "", f"**{headline}.**", ""]
    md += ["## Campaigns", "", gads.md_table(
        [[t["campaign"], t["brand"], nice(t["strategy"]), money(t["target_cpa"], cur) if t["target_cpa"] else
          (f"{t['target_roas']:.0%}" if t["target_roas"] else ""), money(t["cost"], cur), t["conv"],
          money(t["cpa"], cur) if t["cpa"] else "", fmt_pct(t["is"]), fmt_pct(t["lost_rank"]), fmt_pct(t["lost_budget"]),
          "learning" if t["learning"] else ("changed" if t["recent_changes"] else "")] for t in table[:25]],
        ["Campaign", "Brand", "Strategy", "Target", "Cost", "Conv", "CPA", "IS", "Lost rank", "Lost budget", "State"]), ""]
    if approvals:
        md += ["## Recommendations", ""] + [f"- **{a.action}.** {a.why}" for a in approvals] + [""]
    md += ["## Search Partners", ""]
    if net is None:
        md += ["Search Partners check skipped: pass --network with a Campaigns report segmented by Network (with search partners).", ""]
    elif partner_items:
        md += [gads.md_table([[a.entity, money(a.evidence["partners"]["cost"], cur), int(a.evidence["partners"]["clicks"]),
                               a.evidence["partners"]["conv"], money(a.evidence.get("search_cpa"), cur) if a.evidence.get("search_cpa") else f"{a.evidence.get('search_roas', 0):.0%}",
                               (money(a.evidence["partners_cpa"], cur) if a.evidence.get("partners_cpa") else "no conv") if "search_cpa" in a.evidence
                               else f"{a.evidence.get('partners_roas', 0):.0%}", money(a.impact_monthly, cur) + "/mo"] for a in partner_items],
                             ["Campaign", "Partners cost", "Clicks", "Conv", "Google Search CPA/ROAS", "Partners CPA/ROAS", "Saves"]), ""]
    else:
        md += [f"No Search campaign has Search Partners materially worse than Google Search with {PARTNER_MIN_CLICKS}+ partner clicks.", ""]
    if not sims and not args.csv:
        md += ["Bid simulator: no TARGET_CPA or TARGET_ROAS simulations returned, so target changes carry no simulator preview.", ""]
    if notes:
        md += ["## Notes", ""] + [f"- {n}" for n in notes] + [""]
    md += ["## Caveats", "",
           f"- Conversion thresholds (~{MIN_CONV_TCPA}/month for target CPA, ~{MIN_CONV_TROAS} for target ROAS) and the {STEP:.0%} step size are practitioner guidance. Check Google's current Target CPA and Target ROAS help pages before quoting them.",
           "- Target changes restart learning. Change one lever per campaign, then wait about two weeks (or three conversion cycles) before judging.",
           "- Brand overbid estimates assume you keep the same clicks at the reference CPC. Confirm with an auction insights check for new brand bidders before cutting.",
           "- CPA uses primary conversions only. If tracking is wrong (see gads-tracking-audit), fix it before acting on targets."]

    extra = {"bid_campaigns.csv": gads.to_csv(table)}
    if change_rows:
        extra["bid_changes.csv"] = gads.to_csv(change_rows)
        for a in approvals:
            if any(r["campaign"] == a.entity for r in change_rows):
                a.files = ["bid_changes.csv"]
    summary = {"headline": headline, "campaigns": len(table), "brand_overspend": round(overspend, 2),
               "target_changes": n_target, "items": len(approvals), "days": days,
               "search_partners": [a.entity for a in partner_items] if net is not None else "skipped: no network data"}
    d = gads.write_results(SKILL, cid, approvals, summary, "\n".join(md) + "\n", cur, extra)
    print(headline)
    print(f"-> {d}/gads-bid-audit.md  ·  queue: {d}/approvals.md")
    if args.json:
        print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
