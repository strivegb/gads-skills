#!/usr/bin/env python3
"""
pace_budgets.py - month-to-date pacing, run-dry dates, and budget shifts.

Live:     $PY pace_budgets.py -c 123-456-7890
Offline:  python3 pace_budgets.py --csv campaign_by_day.csv --currency USD [--today 2026-11-12]

Offline export: Campaigns report segmented by Day, covering at least the
last 14 days and the whole current month to date. Columns: Day, Campaign,
Budget, Budget name (optional, identifies shared budgets), Cost, Conversions,
Conv. value, Search impr. share, Search lost IS (budget), Search lost IS (rank),
Bid strategy type (optional). Budget on each Day row drives the
overdelivery check; without it that check is skipped.

Outputs (gads-out/<cid>/<date>/):
  gads-budget-pacing.json / .md   summary + approval items
  pacing.csv                      one row per budget: MTD, run rate, projection, run-dry date
  budget_changes.csv              proposed daily budget changes (input to apply_budgets.py)
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gads  # noqa: E402
from gads import Approval, money, num, micros  # noqa: E402

SKILL = "gads-budget-pacing"
MAX_STEP = 0.20          # max change to a binding daily budget per adjustment (practitioner guidance)
MARGINAL = 1.30          # assume marginal CPA is 30% worse than average CPA
LIMITED_LB = 0.10        # search lost IS (budget) above this = budget-limited
UTIL_LIMITED = 0.95      # non-search proxy: spending >= 95% of daily budget
OVER_DAY = 1.20          # a day counts as overdelivery when spend > 1.2x that day's daily budget
OVER_REPEAT = 3          # this many overdelivery days in the last 14 = repeated
GOOGLE_DAY_MAX = 2.0     # Google may spend up to 2x the daily budget on one day
GOOGLE_MONTH_X = 30.4    # and bills at most 30.4x the average daily budget per month
VALUE_STRATS = {"MAXIMIZE_CONVERSION_VALUE", "TARGET_ROAS"}


def first_num(v) -> float:
    m = re.search(r"-?[\d,]*\.?\d+", str(v or ""))
    return num(m.group(0)) if m else 0.0


def acct_today(tz: str) -> dt.date:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(tz)).date()
    except Exception:
        return dt.date.today()


# ---------------------------------------------------------------- load


def load_live(cid: str, today: dt.date):
    month_start = today.replace(day=1)
    w_start = min(month_start, today - dt.timedelta(days=14))
    yday = today - dt.timedelta(days=1)
    camps = gads.query(cid, """
        SELECT campaign.id, campaign.name, campaign.advertising_channel_type, campaign.bidding_strategy_type,
               campaign.target_cpa.target_cpa_micros, campaign.maximize_conversions.target_cpa_micros,
               campaign.target_roas.target_roas, campaign.maximize_conversion_value.target_roas,
               campaign_budget.id, campaign_budget.resource_name, campaign_budget.name,
               campaign_budget.amount_micros, campaign_budget.period, campaign_budget.explicitly_shared,
               campaign_budget.total_amount_micros
        FROM campaign
        WHERE campaign.status = 'ENABLED' AND campaign.serving_status != 'ENDED'""")
    meta = {}
    for r in camps:
        tcpa = micros(r.get("campaign.target_cpa.target_cpa_micros")) or micros(r.get("campaign.maximize_conversions.target_cpa_micros"))
        troas = num(r.get("campaign.target_roas.target_roas")) or num(r.get("campaign.maximize_conversion_value.target_roas"))
        meta[str(r["campaign.id"])] = {
            "campaign_id": str(r["campaign.id"]), "campaign": r.get("campaign.name", ""),
            "channel": r.get("campaign.advertising_channel_type", ""), "strategy": r.get("campaign.bidding_strategy_type", ""),
            "tcpa": tcpa or None, "troas": troas or None,
            "budget_id": str(r.get("campaign_budget.id", "")), "budget_rn": r.get("campaign_budget.resource_name", ""),
            "budget_name": r.get("campaign_budget.name", ""), "daily": micros(r.get("campaign_budget.amount_micros")),
            "period": r.get("campaign_budget.period", "DAILY"), "shared": bool(r.get("campaign_budget.explicitly_shared")),
            "total": micros(r.get("campaign_budget.total_amount_micros")),
        }
    daily = gads.query(cid, f"""
        SELECT campaign.id, segments.date, metrics.cost_micros, metrics.conversions, metrics.conversions_value
        FROM campaign
        WHERE {gads.gaql_dates(w_start.isoformat(), yday.isoformat())} AND campaign.status = 'ENABLED'""")
    # campaign_budget.amount_micros is today's budget, not the budget on each past day.
    # The overdelivery check uses it for every day and says so (budget_source="current").
    days = [{"campaign_id": str(r["campaign.id"]), "date": r["segments.date"], "cost": micros(r.get("metrics.cost_micros")),
             "conv": num(r.get("metrics.conversions")), "value": num(r.get("metrics.conversions_value")),
             "budget": meta.get(str(r["campaign.id"]), {}).get("daily", 0.0)} for r in daily]
    s14, e14 = (today - dt.timedelta(days=14)).isoformat(), yday.isoformat()
    isr = gads.query(cid, f"""
        SELECT campaign.id, metrics.search_impression_share, metrics.search_budget_lost_impression_share,
               metrics.search_rank_lost_impression_share
        FROM campaign
        WHERE {gads.gaql_dates(s14, e14)} AND campaign.status = 'ENABLED'
          AND campaign.advertising_channel_type IN ('SEARCH', 'SHOPPING')""")
    for r in isr:
        m = meta.get(str(r["campaign.id"]))
        if m:
            m["is"] = num(r.get("metrics.search_impression_share")) or None
            m["lost_budget"] = num(r.get("metrics.search_budget_lost_impression_share"))
            m["lost_rank"] = num(r.get("metrics.search_rank_lost_impression_share"))
    sims = {}
    sim_rows = gads.query(cid, """
        SELECT campaign_simulation.campaign_id, campaign_simulation.budget_point_list.points
        FROM campaign_simulation WHERE campaign_simulation.type = 'BUDGET'""", soft=True)
    if not sim_rows:
        print("note: no budget simulations returned; using the marginal-CPA assumption instead.", file=sys.stderr)
    for r in sim_rows:
        pts = r.get("campaign_simulation.budget_point_list.points") or []
        sims[str(r["campaign_simulation.campaign_id"])] = sorted(
            [(micros(p.get("budget_amount_micros")), micros(p.get("cost_micros")), num(p.get("biddable_conversions")),
              num(p.get("biddable_conversions_value"))) for p in pts], key=lambda x: x[0])
    return list(meta.values()), days, sims, "current"


def load_csv(paths: list[str]):
    meta, days = {}, []
    seen_budget = False
    is_acc = defaultdict(lambda: {"w": 0.0, "is": 0.0, "lb": 0.0, "lr": 0.0})
    for p in paths:
        for r in gads.read_ui_csv(p):
            camp = gads.pick(r, "campaign")
            day = gads.pick(r, "day", "date")
            if not camp or not day:
                continue
            try:
                d = dt.date.fromisoformat(day)
            except ValueError:
                try:
                    d = dt.datetime.strptime(day, "%b %d, %Y").date()
                except ValueError:
                    continue
            bname = gads.pick(r, "budget_name", "shared_budget")
            m = meta.setdefault(camp, {
                "campaign_id": gads.pick(r, "campaign_id"), "campaign": camp,
                "channel": gads.pick(r, "campaign_type", default="SEARCH").upper(),
                "strategy": gads.pick(r, "bid_strategy_type", default="").upper().replace(" ", "_"),
                "tcpa": first_num(gads.pick(r, "target_cpa")) or None, "troas": None,
                "budget_id": bname or f"own:{camp}", "budget_rn": "", "budget_name": bname or camp,
                "daily": 0.0, "period": "DAILY", "shared": bool(bname), "total": 0.0})
            b = first_num(gads.pick(r, "budget"))
            if b:
                seen_budget = True
                m["daily"] = b  # last value wins = most recent budget
            cost = num(gads.pick(r, "cost"))
            days.append({"campaign_id": camp, "date": d.isoformat(), "cost": cost, "budget": b,
                         "conv": num(gads.pick(r, "conversions")), "value": num(gads.pick(r, "conv_value", "conversion_value"))})
            a = is_acc[camp]
            w = max(cost, 0.01)
            a["w"] += w
            a["is"] += gads.pct(gads.pick(r, "search_impr_share", default=0)) * w
            a["lb"] += gads.pct(gads.pick(r, "search_lost_is_budget", default=0)) * w
            a["lr"] += gads.pct(gads.pick(r, "search_lost_is_rank", default=0)) * w
    for camp, a in is_acc.items():
        if a["w"] and a["is"]:
            meta[camp]["is"] = a["is"] / a["w"]
            meta[camp]["lost_budget"] = a["lb"] / a["w"]
            meta[camp]["lost_rank"] = a["lr"] / a["w"]
    # shared budgets in offline mode: same budget name on 2+ campaigns
    counts = defaultdict(int)
    for m in meta.values():
        counts[m["budget_id"]] += 1
    for m in meta.values():
        m["shared"] = counts[m["budget_id"]] > 1
    for m in meta.values():
        m["campaign_id"] = m["campaign_id"] or m["campaign"]
    return list(meta.values()), days, {}, ("per_day" if seen_budget else None)


# ---------------------------------------------------------------- analysis


def build_entities(meta, days, sims, prof, today):
    month_start = today.replace(day=1)
    dim = calendar.monthrange(today.year, today.month)[1]
    remaining = dim - today.day + 1  # today counts as a remaining day
    by_c = defaultdict(list)
    for d in days:
        by_c[d["campaign_id"]].append(d)
    caps = {k.strip().lower(): v for k, v in (prof.get("campaign_budgets") or {}).items()}

    ents: dict[str, dict] = {}
    for m in meta:
        rows = by_c.get(m["campaign_id"], [])
        mtd = [r for r in rows if r["date"] >= month_start.isoformat()]
        d7 = [r for r in rows if r["date"] >= (today - dt.timedelta(days=7)).isoformat()]
        d14 = [r for r in rows if r["date"] >= (today - dt.timedelta(days=14)).isoformat()]
        e = ents.setdefault(m["budget_id"], {
            "budget_id": m["budget_id"], "budget_rn": m["budget_rn"], "name": m["budget_name"] if m["shared"] else m["campaign"],
            "shared": m["shared"], "daily": m["daily"], "period": m["period"], "total": m["total"], "campaigns": [],
            "mtd": 0.0, "c7": 0.0, "c14": 0.0, "conv14": 0.0, "val14": 0.0, "is_w": 0.0, "is": 0.0, "lb": 0.0, "lr": 0.0,
            "has_is": False, "tcpa": None, "troas": None, "value_mode": False, "cap": None, "sims": [],
            "day_spend": defaultdict(float), "day_budget": {}})
        e["campaigns"].append(m["campaign"])
        for r in rows:
            e["day_spend"][r["date"]] += r["cost"]
            if r.get("budget"):
                e["day_budget"][r["date"]] = max(e["day_budget"].get(r["date"], 0.0), r["budget"])
        e["mtd"] += sum(r["cost"] for r in mtd)
        c7 = sum(r["cost"] for r in d7)
        c14 = sum(r["cost"] for r in d14)
        e["c7"] += c7
        e["c14"] += c14
        e["conv14"] += sum(r["conv"] for r in d14)
        e["val14"] += sum(r["value"] for r in d14)
        if m.get("is"):
            w = max(c14, 0.01)
            e["is_w"] += w
            e["is"] += m["is"] * w
            e["lb"] += m.get("lost_budget", 0) * w
            e["lr"] += m.get("lost_rank", 0) * w
            e["has_is"] = True
        e["tcpa"] = e["tcpa"] or m.get("tcpa")
        e["troas"] = e["troas"] or m.get("troas")
        e["value_mode"] = e["value_mode"] or m.get("strategy") in VALUE_STRATS or bool(m.get("troas"))
        cap = caps.get(m["campaign"].strip().lower()) or caps.get(str(m["budget_name"]).strip().lower())
        if cap:
            e["cap"] = (e["cap"] or 0) + float(cap) if not m["shared"] else float(cap)
        if m["campaign_id"] in sims:
            e["sims"].append(sims[m["campaign_id"]])

    acct_cost14 = sum(e["c14"] for e in ents.values())
    acct_conv14 = sum(e["conv14"] for e in ents.values())
    acct_val14 = sum(e["val14"] for e in ents.values())
    t_cpa = prof.get("target_cpa") or (acct_cost14 / acct_conv14 if acct_conv14 else None)
    t_roas = prof.get("target_roas") or (acct_val14 / acct_cost14 if acct_cost14 and acct_val14 else None)

    for e in ents.values():
        if e["is_w"]:
            e["is"], e["lb"], e["lr"] = e["is"] / e["is_w"], e["lb"] / e["is_w"], e["lr"] / e["is_w"]
        rr7, rr14 = e["c7"] / 7, e["c14"] / 14
        e["rr"] = 0.6 * rr7 + 0.4 * rr14 if e["c14"] else rr7
        e["util"] = e["rr"] / e["daily"] if e["daily"] else 0
        e["remaining"] = remaining
        e["dim"] = dim
        e["projected"] = e["mtd"] + e["rr"] * remaining
        e["google_ceiling"] = e["daily"] * 30.4 if e["period"] == "DAILY" else None
        e["cpa"] = e["c14"] / e["conv14"] if e["conv14"] else None
        e["roas"] = e["val14"] / e["c14"] if e["c14"] and e["val14"] else None
        tgt_cpa = e["tcpa"] or t_cpa
        tgt_roas = e["troas"] or t_roas
        e["tgt_cpa"], e["tgt_roas"] = tgt_cpa, tgt_roas
        if e["value_mode"] and e["roas"] is not None and tgt_roas:
            ratio = e["roas"] / tgt_roas
            e["eff"] = "good" if ratio >= 1.0 else ("ok" if ratio >= 0.75 else "poor")
            e["eff_score"] = ratio
        elif e["cpa"] is not None and tgt_cpa:
            ratio = tgt_cpa / e["cpa"]
            e["eff"] = "good" if ratio >= 1.0 else ("ok" if ratio >= 0.77 else "poor")
            e["eff_score"] = ratio
        else:
            e["eff"] = "poor" if e["c14"] > (tgt_cpa or 0) * 2 and e["conv14"] == 0 else "unknown"
            e["eff_score"] = 0.0 if e["eff"] == "poor" else 0.5
        e["limited"] = (e["has_is"] and e["lb"] > LIMITED_LB) or (not e["has_is"] and e["util"] >= UTIL_LIMITED)
        e["dry_date"] = None
        if e["cap"]:
            e["rec_plan_daily"] = max(0.0, (e["cap"] - e["mtd"]) / remaining)
            if e["mtd"] >= e["cap"]:
                e["dry_date"] = today
            elif e["rr"] > 0 and e["projected"] > e["cap"]:
                k = (e["cap"] - e["mtd"]) / e["rr"]
                e["dry_date"] = today + dt.timedelta(days=int(k))
        else:
            e["rec_plan_daily"] = None
    return ents, t_cpa, t_roas, remaining, dim


def sim_extra_conv(e, new_daily) -> float | None:
    """Interpolate budget simulation points: extra conversions per day at new_daily."""
    if not e["sims"] or len(e["sims"]) != 1:
        return None
    pts = e["sims"][0]
    if len(pts) < 2:
        return None

    def at(b):
        for (b0, _, c0, _), (b1, _, c1, _) in zip(pts, pts[1:]):
            if b0 <= b <= b1:
                return c0 + (c1 - c0) * ((b - b0) / (b1 - b0) if b1 > b0 else 0)
        return pts[-1][2] if b > pts[-1][0] else pts[0][2]
    # simulation points are over the simulation window (typically 7 days)
    return (at(new_daily) - at(e["daily"])) / 7


def plan_shifts(ents, remaining):
    """Pair donors and receivers in $/day. Returns (transfers, receivers, donors)."""
    recv, donors = [], []
    for e in ents.values():
        if e["daily"] <= 0 or e["period"] != "DAILY":
            continue
        over_cap = e["cap"] and e["projected"] > e["cap"] * 1.02
        under_cap = e["cap"] and e["projected"] < e["cap"] * 0.95
        if e["eff"] == "good" and over_cap:
            # keeps current run rate instead of stopping on its plan; plan money is what moves
            e["need"] = e["rr"] - e["rec_plan_daily"]
            e["recv_kind"] = "plan"
            recv.append(e)
        elif e["eff"] == "good" and e["limited"] and not e["cap"]:
            share = max(e["is"], 0.05) if e["has_is"] else 0.8
            want = e["rr"] * (e["lb"] / share) if e["has_is"] else e["daily"] * MAX_STEP
            e["need"] = min(want, e["daily"] * MAX_STEP)
            e["recv_kind"] = "daily"
            recv.append(e)
        elif under_cap and not e["limited"]:
            e["give"] = (e["cap"] - e["projected"]) / remaining
            e["give_kind"] = "plan_slack"
            donors.append(e)
        elif not e["cap"] and not e["limited"] and e["util"] < 0.8:
            e["give"] = max(0.0, e["daily"] - e["rr"] * 1.15)
            e["give_kind"] = "headroom"
            donors.append(e)
        elif e["eff"] == "poor" and not e["limited"]:
            e["give"] = e["daily"] * MAX_STEP
            e["give_kind"] = "inefficient"
            donors.append(e)
    recv.sort(key=lambda e: -e["eff_score"])
    order = {"plan_slack": 0, "headroom": 1, "inefficient": 2}
    donors.sort(key=lambda e: (order[e["give_kind"]], e["eff_score"]))
    transfers = []
    di = 0
    left = {id(d): d["give"] for d in donors}
    for r in recv:
        need = r["need"]
        while need > 0.5 and di < len(donors):
            d = donors[di]
            amt = min(need, left[id(d)])
            if amt >= 1:
                transfers.append((d, r, amt))
                left[id(d)] -= amt
                need -= amt
            if left[id(d)] < 1:
                di += 1
        r["unfunded"] = max(0.0, need)
    return transfers, recv, donors


def overdelivery(ents, today, dim):
    """Days where spend beat the daily budget, and campaigns heading past Google's monthly ceiling.

    Returns one dict per budget worth flagging. Needs a daily budget per day (e["day_budget"]).
    """
    month_start = today.replace(day=1)
    w14 = {(today - dt.timedelta(days=i)).isoformat() for i in range(1, 15)}
    out = []
    for e in ents.values():
        if e["period"] != "DAILY" or e["daily"] <= 0 or not e["day_budget"]:
            continue
        over = sorted((d, sp, e["day_budget"][d]) for d, sp in e["day_spend"].items()
                      if d in w14 and e["day_budget"].get(d) and sp > e["day_budget"][d] * OVER_DAY)
        over_any = sum(1 for d, sp in e["day_spend"].items() if d in w14 and e["day_budget"].get(d) and sp > e["day_budget"][d])
        # Google's ceiling: 30.4x the average daily budget across the month.
        # Past days use the budget seen that day; the rest of the month uses today's budget.
        budgets = []
        for i in range(dim):
            d = (month_start + dt.timedelta(days=i)).isoformat()
            budgets.append(e["day_budget"].get(d, e["daily"]) if d < today.isoformat() else e["daily"])
        ceiling = GOOGLE_MONTH_X * sum(budgets) / dim
        peak = max(((sp / b, d, sp, b) for d, sp, b in over), default=None)
        heading_over = e["projected"] > ceiling * 1.03  # 3% slack: a 31-day month at exactly budget is 31x
        if len(over) >= OVER_REPEAT or heading_over:
            out.append({"e": e, "over": over, "over_any": over_any, "peak": peak, "ceiling": ceiling,
                        "heading_over": heading_over, "repeated": len(over) >= OVER_REPEAT})
    return out


def rnd(x: float) -> float:
    return round(x / 5) * 5 if x >= 50 else round(x)


# ---------------------------------------------------------------- output


def build_outputs(ents, transfers, recv, donors, prof, cid, cur, today, t_cpa, t_roas, remaining, dim, budget_source=None):
    month = today.strftime("%b")
    approvals, changes = [], {}

    def chg(e, new_daily, why):
        c = changes.setdefault(e["budget_id"], {"budget_id": e["budget_id"], "budget_resource_name": e["budget_rn"],
                                                 "budget_name": e["name"], "shared": e["shared"],
                                                 "campaigns": "; ".join(e["campaigns"]),
                                                 "current_daily": round(e["daily"], 2), "new_daily": round(e["daily"], 2), "why": ""})
        c["new_daily"] = round(new_daily, 2)
        c["why"] = (c["why"] + " | " if c["why"] else "") + why

    # 1) shift: donors -> receivers
    if transfers:
        by_d, by_r = defaultdict(float), defaultdict(float)
        for d, r, amt in transfers:
            by_d[d["budget_id"]] += amt
            by_r[r["budget_id"]] += amt
        total = sum(by_d.values())
        total_r = rnd(total)
        real_cut = sum(amt for bid, amt in by_d.items() if ents[bid]["give_kind"] == "inefficient")
        free = total - real_cut
        recv_names, give_names = [], []
        extra_conv = 0.0
        for bid, amt in by_r.items():
            r = ents[bid]
            if r["recv_kind"] == "daily":
                lim = f"lost {r['lb']:.0%} IS to budget" if r["has_is"] else f"spending {r['util']:.0%} of budget"
                chg(r, r["daily"] + amt, f"+{amt:.0f}/day: budget-limited ({lim}) at good efficiency")
            else:
                r["plan_add"] = amt * remaining  # daily budget unchanged; its monthly plan grows
            dd = f" (runs out of {month} plan {r['dry_date'].strftime('%b %d')})" if r.get("dry_date") else ""
            recv_names.append(f"{r['name']}{dd}")
            sc = sim_extra_conv(r, r["daily"] + amt)
            if sc is not None and r["recv_kind"] == "daily":
                extra_conv += sc * remaining
            elif r["tgt_cpa"] or r["cpa"]:
                base_cpa = r["cpa"] or r["tgt_cpa"]
                extra_conv += amt * remaining / (base_cpa * MARGINAL)
        for bid, amt in by_d.items():
            d = ents[bid]
            if d["give_kind"] == "plan_slack":
                # not budget-limited, so trimming to ~1.1x run rate costs no delivery
                new = min(d["daily"], max(d["rr"] * 1.1, d["daily"] - amt))
                chg(d, new, f"-{amt:.0f}/day of {month} plan it will not spend")
            elif d["give_kind"] == "headroom":
                chg(d, d["daily"] - amt, f"-{amt:.0f}/day unused headroom (spending {d['util']:.0%} of budget)")
            else:
                chg(d, d["daily"] - amt, f"-{amt:.0f}/day: CPA/ROAS well off target")
            give_names.append(d["name"])
        dry = [r for r in recv if r.get("dry_date") and r["budget_id"] in by_r]
        plan_only = all(ents[b]["recv_kind"] == "plan" for b in by_r)
        binding = sum(a for b, a in by_r.items() if ents[b]["recv_kind"] == "daily") + real_cut
        lead = (f"{len(dry)} campaign{'s' if len(dry) != 1 else ''} run out of {month} plan by "
                f"{max(r['dry_date'] for r in dry).strftime('%b %d')}. ") if dry else ""
        approvals.append(Approval(
            skill=SKILL, kind="budget_shift", entity="; ".join(recv_names),
            action=f"Shift ~{money(total_r, cur)}/day from {', '.join(give_names)} to {', '.join(recv_names)}",
            why=(f"{lead}"
                 + ("Funded from plan money the donors will not spend, so the month total stays flat. " if plan_only else
                    f"Total daily budget stays flat, but actual spend rises ~{money(sum(a for b, a in by_d.items() if ents[b]['give_kind'] == 'headroom'), cur)}/day because that donor budget was going unused. "
                    if free else "Total daily budget and spend stay flat. ")
                 + (f"Keeps ~{extra_conv:.0f} conversions that stop if they are held to plan" if plan_only
                    else f"Expected +{extra_conv:.0f} conversions over the rest of {month}")
                 + f" (marginal CPA assumed {MARGINAL:.1f}x average)."),
            impact_monthly=total * remaining, impact_label="protects", confidence="medium" if extra_conv else "low",
            risk="low" if binding <= sum(ents[b]["daily"] for b in by_r) * MAX_STEP + 1 else "medium",
            evidence={"shift_per_day": round(total, 2), "days_left": remaining, "free_headroom_per_day": round(free, 2),
                      "est_extra_conv": round(extra_conv, 1)},
            change={"file": "budget_changes.csv"}, files=["budget_changes.csv"]))

    # 2) pace fixes against plan caps (things the shift did not resolve)
    shifted = {r["budget_id"] for _, r, _ in transfers} | {d["budget_id"] for d, _, _ in transfers}
    for e in sorted(ents.values(), key=lambda e: -(e["projected"] - (e["cap"] or e["projected"]))):
        if not e["cap"] or e["budget_id"] in shifted:
            continue
        gap = e["projected"] - e["cap"]
        if abs(gap) < max(0.03 * e["cap"], e["daily"]):
            continue
        new = e["rec_plan_daily"]
        if gap > 0:
            dd = e["dry_date"].strftime("%b %d") if e["dry_date"] else "month end"
            chg(e, new, f"cut to land on {month} plan")
            approvals.append(Approval(
                skill=SKILL, kind="pace_cut", entity=e["name"],
                action=f"Lower '{e['name']}' daily budget {money(e['daily'], cur)} -> {money(new, cur)} to land on its {month} plan",
                why=f"At {money(e['rr'], cur)}/day it spends its {money(e['cap'], cur)} plan by {dd} and overshoots by {money(gap, cur)}. "
                    f"Efficiency is {e['eff']}, so no other budget was cut to fund it.",
                impact_monthly=gap, impact_label="saves", confidence="high", risk="low",
                evidence={"mtd": round(e["mtd"], 2), "run_rate": round(e["rr"], 2), "cap": e["cap"], "projected": round(e["projected"], 2)},
                change={"file": "budget_changes.csv"}, files=["budget_changes.csv"]))
        elif e["limited"]:
            step_new = min(new, e["daily"] * (1 + MAX_STEP))
            chg(e, step_new, f"raise toward {month} plan (underspending, budget-limited)")
            approvals.append(Approval(
                skill=SKILL, kind="pace_raise", entity=e["name"],
                action=f"Raise '{e['name']}' daily budget {money(e['daily'], cur)} -> {money(step_new, cur)} to use its {month} plan",
                why=f"It will underspend its {money(e['cap'], cur)} plan by {money(-gap, cur)} while losing {e['lb']:.0%} of impressions to budget. "
                    f"Step capped at {MAX_STEP:.0%}; re-check in 3 days.",
                impact_monthly=-gap, impact_label="gains", confidence="medium", risk="low",
                evidence={"mtd": round(e["mtd"], 2), "run_rate": round(e["rr"], 2), "cap": e["cap"], "lost_is_budget": round(e["lb"], 3)},
                change={"file": "budget_changes.csv"}, files=["budget_changes.csv"]))
        else:
            approvals.append(Approval(
                skill=SKILL, kind="underspend_flag", entity=e["name"],
                action=f"Review '{e['name']}': will underspend its {month} plan by {money(-gap, cur)}",
                why="It is not budget-limited, so a higher budget will not fix it. The limit is bids, targeting or demand. Move the plan money elsewhere or loosen targets.",
                impact_label="", confidence="high", risk="low",
                evidence={"projected": round(e["projected"], 2), "cap": e["cap"], "lost_is_rank": round(e["lr"], 3)}))

    # 3) account-level plan
    acct_cap = prof.get("monthly_budget")
    mtd = sum(e["mtd"] for e in ents.values())
    rr = sum(e["rr"] for e in ents.values())
    proj = mtd + rr * remaining
    acct_dry = None
    if acct_cap:
        if mtd >= acct_cap:
            acct_dry = today
        elif rr and proj > acct_cap:
            acct_dry = today + dt.timedelta(days=int((acct_cap - mtd) / rr))
        if abs(proj - acct_cap) > 0.03 * acct_cap:
            over = proj > acct_cap
            approvals.append(Approval(
                skill=SKILL, kind="account_pace", entity="account",
                action=(f"Account is on pace to {'overspend' if over else 'underspend'} the {month} budget by {money(abs(proj - acct_cap), cur)}"),
                why=(f"MTD {money(mtd, cur)}, run rate {money(rr, cur)}/day, projected {money(proj, cur)} vs {money(acct_cap, cur)}. "
                     + (f"Budget runs out on {acct_dry.strftime('%b %d')} at this rate. Trim {money((proj - acct_cap) / remaining, cur)}/day starting with the weakest campaigns."
                        if over else f"Room for {money((acct_cap - proj) / remaining, cur)}/day more; give it to budget-limited campaigns with good CPA.")),
                impact_monthly=abs(proj - acct_cap), impact_label="saves" if over else "", confidence="high", risk="low",
                evidence={"mtd": round(mtd, 2), "run_rate": round(rr, 2), "projected": round(proj, 2), "cap": acct_cap}))

    # 4) pause at cap: proposal only, never applied by a script
    month_end = today.replace(day=dim)
    funded = {r["budget_id"] for r in recv if r.get("unfunded", 0) < 1 and r["budget_id"] in {x["budget_id"] for _, x, _ in transfers}}
    pause_items = []
    for e in sorted(ents.values(), key=lambda e: e["dry_date"] or month_end):
        if not (e["cap"] and e["dry_date"] and e["dry_date"] < month_end and e["projected"] > e["cap"] * 1.02):
            continue
        if e["budget_id"] in funded:
            continue  # the shift gives it more plan, so it does not hit the cap
        stretch = max(0.0, (e["cap"] - e["mtd"]) / remaining)
        dd = e["dry_date"].strftime("%b %d")
        who = f"every campaign on shared budget '{e['name']}' ({'; '.join(e['campaigns'])})" if e["shared"] else f"'{e['name']}'"
        pause_items.append(Approval(
            skill=SKILL, kind="pause_at_cap", entity=e["name"],
            action=f"Pause {who} on {dd}, when it reaches its {money(e['cap'], cur)} {month} plan",
            why=(f"At {money(e['rr'], cur)}/day it spends its plan by {dd} and would overshoot by {money(e['projected'] - e['cap'], cur)} by month end. "
                 f"The alternative is a budget cut now: {money(e['daily'], cur)} -> {money(stretch, cur)}/day stretches the plan to {month_end.strftime('%b %d')}. "
                 "This is a proposal only; nothing pauses automatically. Set a reminder or an automated rule once approved."),
            impact_monthly=e["projected"] - e["cap"], impact_label="protects", confidence="medium", risk="medium",
            severity="high" if (e["dry_date"] - today).days <= 7 else "medium",
            evidence={"cap": e["cap"], "mtd": round(e["mtd"], 2), "run_rate": round(e["rr"], 2), "projected": round(e["projected"], 2),
                      "pause_on": e["dry_date"].isoformat(), "alt_daily_budget": round(stretch, 2)},
            change={"type": "pause_on_date", "budget": e["name"], "campaigns": e["campaigns"], "date": e["dry_date"].isoformat(),
                    "alternative": {"new_daily": round(stretch, 2)}}))
    if acct_cap and acct_dry and acct_dry < month_end and proj > acct_cap:
        weakest = [e["name"] for e in sorted(ents.values(), key=lambda e: e.get("eff_score", 0.5)) if e["rr"] > 0][:3]
        trim = (proj - acct_cap) / remaining
        pause_items.append(Approval(
            skill=SKILL, kind="pause_at_cap", entity="account",
            action=f"Pause the account's weakest campaigns on {acct_dry.strftime('%b %d')}, when spend reaches the {money(acct_cap, cur)} {month} budget",
            why=(f"Projected {money(proj, cur)} against a {money(acct_cap, cur)} monthly budget; the cap is reached on {acct_dry.strftime('%b %d')}. "
                 f"Pause in this order: {', '.join(weakest)}. The alternative is trimming {money(trim, cur)}/day now. "
                 "This is a proposal only; nothing pauses automatically."),
            impact_monthly=proj - acct_cap, impact_label="protects", confidence="medium", risk="medium",
            severity="high" if (acct_dry - today).days <= 7 else "medium",
            evidence={"cap": acct_cap, "mtd": round(mtd, 2), "run_rate": round(rr, 2), "projected": round(proj, 2),
                      "pause_on": acct_dry.isoformat(), "pause_order": weakest, "alt_trim_per_day": round(trim, 2)},
            change={"type": "pause_on_date", "budget": "account", "campaigns": weakest, "date": acct_dry.isoformat(),
                    "alternative": {"trim_per_day": round(trim, 2)}}))
    approvals += pause_items

    # 5) overdelivery
    over_flags = overdelivery(ents, today, dim) if budget_source else []
    for f in over_flags:
        e = f["e"]
        bits = []
        if f["repeated"]:
            pk = f["peak"]
            bits.append(f"Spent more than {OVER_DAY:.1f}x its daily budget on {len(f['over'])} of the last 14 days "
                        f"(peak {money(pk[2], cur)} on {pk[1]}, {pk[0]:.1f}x a {money(pk[3], cur)} budget).")
        if f["heading_over"]:
            bits.append(f"Projected {money(e['projected'], cur)} this month is above Google's ceiling of {money(f['ceiling'], cur)} "
                        f"({GOOGLE_MONTH_X}x the average daily budget). Google credits spend above that, so delivery is likely to slow late in the month.")
        bits.append(f"Google may spend up to {GOOGLE_DAY_MAX:.0f}x the daily budget on one day. If {money(e['daily'], cur)} is the most you want to pay on any day, "
                    "lower the daily budget; if the monthly total is what matters, set a plan cap in the profile instead.")
        if budget_source == "current":
            bits.append("Budgets for past days are today's budget, so a budget change in the last 14 days can cause a false flag.")
        approvals.append(Approval(
            skill=SKILL, kind="overdelivery", entity=e["name"],
            action=(f"Review overdelivery on '{e['name']}': above budget on {f['over_any']} of the last 14 days"
                    + (f", heading past Google's {money(f['ceiling'], cur)} monthly ceiling" if f["heading_over"] else "")),
            why=" ".join(bits), impact_label="", confidence="medium" if budget_source == "per_day" else "low", risk="low",
            severity="medium" if f["heading_over"] else "low",
            evidence={"daily_budget": round(e["daily"], 2), "days_over_budget_14d": f["over_any"],
                      f"days_over_{OVER_DAY}x_14d": len(f["over"]),
                      "over_days": [{"date": d, "spend": round(sp, 2), "budget": round(b, 2)} for d, sp, b in f["over"]][:10],
                      "google_month_ceiling": round(f["ceiling"], 2), "projected": round(e["projected"], 2),
                      "budget_source": budget_source}))

    # tables
    pace_rows = []
    for e in sorted(ents.values(), key=lambda e: -e["rr"]):
        pace_rows.append({
            "budget": e["name"], "shared": e["shared"], "campaigns": "; ".join(e["campaigns"]),
            "daily_budget": round(e["daily"], 2), "run_rate": round(e["rr"], 2), "utilization": round(e["util"], 2),
            "mtd": round(e["mtd"], 2), "projected": round(e["projected"], 2), "plan_cap": e["cap"] or "",
            "google_ceiling": round(e["google_ceiling"], 2) if e["google_ceiling"] else "",
            "runs_dry": e["dry_date"].isoformat() if e["dry_date"] else "",
            "lost_is_budget": round(e["lb"], 3) if e["has_is"] else "", "lost_is_rank": round(e["lr"], 3) if e["has_is"] else "",
            "cpa_14d": round(e["cpa"], 2) if e["cpa"] else "", "roas_14d": round(e["roas"], 2) if e["roas"] else "",
            "efficiency": e["eff"], "limited": e["limited"]})
    change_rows = [c for c in changes.values() if abs(c["new_daily"] - c["current_daily"]) >= 1]
    for c in change_rows:
        # apply_budgets.py refuses moves above 20% unless --allow-large --reason is given
        c["large"] = "yes" if c["current_daily"] and abs(c["new_daily"] / c["current_daily"] - 1) > MAX_STEP + 1e-9 else "no"

    dry_list = [e for e in ents.values() if e["dry_date"] and e["dry_date"] < today.replace(day=dim)
                and e["projected"] > e["cap"] * 1.02]
    shift_total = sum(a for _, _, a in transfers)
    has_caps = any(e["cap"] for e in ents.values())
    n_lim = sum(1 for e in ents.values() if e["limited"] and e["eff"] == "good")
    headline = ((f"{len(dry_list)} budget{'s' if len(dry_list) != 1 else ''} run dry before month end" if has_caps
                 else f"No plan caps set; {n_lim} budget-limited campaign{'s' if n_lim != 1 else ''} with good CPA/ROAS")
                + (f" (earliest {min(e['dry_date'] for e in dry_list).strftime('%b %d')})" if dry_list else "")
                + (f"; proposed ~{money(rnd(shift_total), cur)}/day shift" if transfers else "; no shift needed")
                + f"; account projected {money(proj, cur)}" + (f" vs {money(acct_cap, cur)} plan" if acct_cap else ""))

    md = [f"# Budget pacing · {gads.fmt_cid(cid) or 'offline export'} · {today.isoformat()}", "",
          f"**{headline}.**", "",
          f"- Month to date: {money(mtd, cur)} · run rate {money(rr, cur)}/day (60% last 7d, 40% last 14d) · {remaining} days left incl. today",
          f"- Targets used: CPA {money(t_cpa, cur) if t_cpa else 'n/a'} · ROAS {f'{t_roas:.2f}x' if t_roas else 'n/a'} (campaign targets override)",
          f"- Caps: {'profile plan caps' if any(e['cap'] for e in ents.values()) else 'no plan caps in profile; Google ceiling (30.4x daily) shown for reference'}", "",
          "## Pacing", "",
          gads.md_table([[r["budget"] + (" (shared)" if r["shared"] else ""), money(r["daily_budget"], cur), money(r["run_rate"], cur),
                          f"{r['utilization']:.0%}", money(r["mtd"], cur), money(r["projected"], cur),
                          money(r["plan_cap"], cur) if r["plan_cap"] else "-", r["runs_dry"] or "-",
                          f"{r['lost_is_budget']:.0%}" if r["lost_is_budget"] != "" else "-", r["efficiency"]] for r in pace_rows],
                        ["Budget", "Daily", "Run rate", "Util", "MTD", "Projected", "Plan", "Runs dry", "Lost IS budget", "Eff."]), ""]
    if transfers:
        md += ["## Proposed shift", "",
               gads.md_table([[d["name"], r["name"], money(a, cur) + "/day", d["give_kind"].replace("_", " ")] for d, r, a in transfers],
                             ["From", "To", "Amount", "Source"]), ""]
        plan_moves = [r for r in recv if r.get("plan_add")]
        if plan_moves:
            md += [f"Plan changes ({month}, daily budgets unchanged): " + ", ".join(
                f"{r['name']} {money(r['cap'], cur)} -> {money(r['cap'] + r['plan_add'], cur)}" for r in plan_moves), ""]
            for d_ in {id(d): d for d, _, _ in transfers if d["give_kind"] == "plan_slack"}.values():
                given = sum(a for dd, _, a in transfers if dd is d_) * remaining
                md[-2] += f", {d_['name']} {money(d_['cap'], cur)} -> {money(d_['cap'] - given, cur)}"
        unf = [r for r in recv if r.get("unfunded", 0) >= 1]
        if unf:
            md += ["Unfunded demand (no donor left): " + ", ".join(f"{r['name']} {money(r['unfunded'], cur)}/day" for r in unf), ""]
    if change_rows:
        md += ["## Daily budget changes", "",
               gads.md_table([[c["budget_name"], money(c["current_daily"], cur), money(c["new_daily"], cur),
                               f"{(c['new_daily'] / c['current_daily'] - 1):+.0%}" if c["current_daily"] else "-", c["why"]] for c in change_rows],
                             ["Budget", "Now", "New", "Change", "Why"]), ""]
    if pause_items:
        md += ["## Pause at cap (proposals)", "",
               gads.md_table([[a.entity, a.evidence["pause_on"], money(a.evidence["cap"], cur), money(a.evidence["projected"], cur)] for a in pause_items],
                             ["Budget", "Pause on", "Plan", "Projected"]), ""]
    md += ["## Overdelivery", ""]
    if not budget_source:
        md += ["Overdelivery check skipped: the export has no Budget column on its Day rows. Add Budget to the report to enable it.", ""]
    elif over_flags:
        md += [gads.md_table([[f["e"]["name"], money(f["e"]["daily"], cur), f["over_any"], len(f["over"]),
                               f"{f['peak'][0]:.1f}x" if f["peak"] else "-", money(f["ceiling"], cur), money(f["e"]["projected"], cur)] for f in over_flags],
                             ["Budget", "Daily", "Days over (14d)", f"Days > {OVER_DAY:.1f}x", "Peak", "Google ceiling", "Projected"]), ""]
    else:
        md += ["No budget overdelivered repeatedly or is heading past Google's monthly ceiling.", ""]
    if budget_source == "current":
        md += ["Live mode compares each day's spend with today's budget (the API returns the current amount only).", ""]
    md += ["## Caveats", "",
           "- Run rate excludes today (partial). Projection assumes the next days look like the last 7-14.",
           "- Google can spend up to 2x the daily budget on a single day and caps the month at 30.4x the average daily budget.",
           f"- Extra conversions use marginal CPA {MARGINAL:.1f}x the 14-day average unless a budget simulation was available.",
           f"- Daily changes to budget-limited campaigns are capped at {MAX_STEP:.0%} per step (practitioner guidance to avoid Smart Bidding volatility, not a Google rule).",
           "- Weekends, promos and month-end seasonality are not modelled. Re-run every 2-3 days."]

    summary = {"headline": headline, "mtd": round(mtd, 2), "run_rate": round(rr, 2), "projected": round(proj, 2),
               "account_cap": acct_cap, "account_runs_dry": acct_dry.isoformat() if acct_dry else None,
               "runs_dry": [{"budget": e["name"], "date": e["dry_date"].isoformat()} for e in dry_list],
               "shift_per_day": round(shift_total, 2), "changes": len(change_rows), "days_left": remaining,
               "overdelivery": [f["e"]["name"] for f in over_flags] if budget_source else "skipped: no per-day budget",
               "pause_at_cap": [{"budget": a.entity, "date": a.evidence["pause_on"]} for a in pause_items]}
    extra = {"pacing.csv": gads.to_csv(pace_rows), "budget_changes.csv": gads.to_csv(change_rows)}
    return approvals, summary, "\n".join(md) + "\n", extra


def main():
    ap = gads.common_args(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    ap.add_argument("--today", help="Pretend today is YYYY-MM-DD (testing / backfills).")
    args = ap.parse_args()

    if args.csv:
        cid = gads.clean_cid(args.customer) or "offline"
        cur = args.currency
        today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
        meta, days, sims, budget_source = load_csv(args.csv)
    else:
        if not args.customer:
            gads.die("--customer is required in live mode (or pass --csv).")
        cid = gads.clean_cid(args.customer)
        info = gads.account_info(cid)
        cur = info["currency"]
        today = dt.date.fromisoformat(args.today) if args.today else acct_today(info["time_zone"])
        meta, days, sims, budget_source = load_live(cid, today)
    if not meta:
        gads.die("No enabled campaigns with budgets found.")
    if today.day == 1:
        print("note: first day of the month; MTD is empty and projections lean entirely on last month's run rate.", file=sys.stderr)
    prof = gads.load_profile(cid)
    ents, t_cpa, t_roas, remaining, dim = build_entities(meta, days, sims, prof, today)
    transfers, recv, donors = plan_shifts(ents, remaining)
    if not budget_source:
        print("note: no Budget column on Day rows; overdelivery check skipped.", file=sys.stderr)
    approvals, summary, md, extra = build_outputs(ents, transfers, recv, donors, prof, cid, cur, today, t_cpa, t_roas, remaining, dim, budget_source)
    d = gads.write_results(SKILL, cid, approvals, summary, md, cur, extra)
    print(summary["headline"])
    print(f"-> {d}/{SKILL}.md  ·  queue: {d}/approvals.md")
    if args.json:
        import json
        print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
