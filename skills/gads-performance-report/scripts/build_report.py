#!/usr/bin/env python3
"""
build_report.py - pull spend, CPA and ROAS, compare periods, write up the top movers.

Live:
  $PY build_report.py -c 123-456-7890                      # last 7 days vs prior 7
  $PY build_report.py -c 123-456-7890 --days 30            # last 30 vs prior 30
  $PY build_report.py -c 123-456-7890 --compare mtd        # month to date vs same days last month
  $PY build_report.py -c 123-456-7890 --compare yoy        # same window, 364 days earlier (weekday aligned)
Offline:
  python3 build_report.py --csv campaigns_by_day.csv --currency USD          # one export segmented by Day
  python3 build_report.py --csv current.csv previous.csv --currency USD      # two campaign exports

Also checks (both modes, given daily data):
  * same-weekday anomalies: last complete day and last 7 days vs the same
    weekdays over the previous --anomaly-weeks (default 8), z-score >= 2;
    conversion metrics skip the conversion lag window (--lag-days, default 3)
  * campaigns that went to zero impressions on the last full day
  * change history attached to each top mover (live: change_event, last 30
    days; offline: --changes <UI Change history export>)

Outputs (gads-out/<cid>/<date>/):
  gads-performance-report.json / .md   summary, movers, approval items
  report.html                          self-contained client-ready page
  campaigns.csv                        campaign table with deltas
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gads  # noqa: E402
from gads import Approval, micros, money, num, pct  # noqa: E402

SKILL = "gads-performance-report"
METRICS = ("impr", "clicks", "cost", "conv", "value")

# ---------------------------------------------------------------- periods


def ref_today(args) -> dt.date:
    """'Today' for windows and lag: --today, else the real date."""
    return dt.date.fromisoformat(args.today) if getattr(args, "today", None) else gads.today()


def periods(args) -> tuple[tuple[dt.date, dt.date], tuple[dt.date, dt.date]]:
    """Return (current, previous) inclusive date ranges."""
    yday = ref_today(args) - dt.timedelta(days=1)
    end = dt.date.fromisoformat(args.end) if args.end else yday
    if args.compare == "mtd":
        start = end.replace(day=1)
        prev_end_month = start - dt.timedelta(days=1)
        p_start = prev_end_month.replace(day=1)
        p_end = p_start + (end - start)
        if p_end > prev_end_month:  # e.g. Mar 31 vs Feb
            p_end = prev_end_month
        return (start, end), (p_start, p_end)
    if args.start:
        start = dt.date.fromisoformat(args.start)
    else:
        start = end - dt.timedelta(days=args.days - 1)
    n = (end - start).days + 1
    if args.compare == "yoy":
        return (start, end), (start - dt.timedelta(days=364), end - dt.timedelta(days=364))
    return (start, end), (start - dt.timedelta(days=n), start - dt.timedelta(days=1))


def fmt_range(r) -> str:
    a, b = r
    if a.year == b.year:
        return f"{a:%b %-d} to {b:%b %-d, %Y}"
    return f"{a:%b %-d, %Y} to {b:%b %-d, %Y}"


# ---------------------------------------------------------------- load

DATE_FORMATS = ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%A, %B %d, %Y", "%a, %b %d, %Y", "%d/%m/%Y", "%m/%d/%Y", "%d %b %Y")


def parse_date(s: str) -> dt.date | None:
    s = str(s).strip()
    for f in DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def row_from(campaign, channel, impr, clicks, cost, conv, value, is_, bl, rl, date=None, status="", budget=None, cid=""):
    return {"date": date, "campaign": campaign, "channel": channel, "status": status, "impr": impr, "clicks": clicks,
            "cost": cost, "conv": conv, "value": value, "is": is_, "budget_lost": bl, "rank_lost": rl,
            "budget": budget, "campaign_id": cid}


def load_live(cid: str, ranges) -> list[dict]:
    out = []
    for a, b in ranges:
        rows = gads.query(cid, f"""
            SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type,
                   segments.date, metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.conversions, metrics.conversions_value,
                   metrics.search_impression_share, metrics.search_budget_lost_impression_share,
                   metrics.search_rank_lost_impression_share
            FROM campaign
            WHERE {gads.gaql_dates(a.isoformat(), b.isoformat())} AND metrics.impressions > 0""")
        for r in rows:
            out.append(row_from(
                r.get("campaign.name", ""), r.get("campaign.advertising_channel_type", ""),
                num(r.get("metrics.impressions")), num(r.get("metrics.clicks")), micros(r.get("metrics.cost_micros")),
                num(r.get("metrics.conversions")), num(r.get("metrics.conversions_value")),
                num(r.get("metrics.search_impression_share")), num(r.get("metrics.search_budget_lost_impression_share")),
                num(r.get("metrics.search_rank_lost_impression_share")),
                date=dt.date.fromisoformat(r["segments.date"]), status=r.get("campaign.status", ""),
                cid=str(r.get("campaign.id", ""))))
    return out


def merge_ranges(ranges):
    """Merge overlapping or touching date ranges so no day is queried twice."""
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + dt.timedelta(days=1):
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def load_meta_live(cid: str) -> dict:
    """Current status, budget and end date per campaign (for the zero-impression check)."""
    meta = {}
    for r in gads.query(cid, """
        SELECT campaign.id, campaign.name, campaign.status, campaign.primary_status,
               campaign.primary_status_reasons, campaign.end_date_time, campaign.campaign_budget,
               campaign_budget.amount_micros
        FROM campaign WHERE campaign.status != 'REMOVED'"""):
        meta[r.get("campaign.name", "")] = {
            "id": str(r.get("campaign.id", "")), "status": r.get("campaign.status", ""),
            "primary_status": r.get("campaign.primary_status", ""),
            "reasons": r.get("campaign.primary_status_reasons") or [],
            "end": (r.get("campaign.end_date_time") or "")[:10],
            "budget_res": r.get("campaign.campaign_budget", ""),
            "budget": micros(r.get("campaign_budget.amount_micros"))}
    return meta


# Upper edge (days) of each conversion lag bucket.
LAG_BUCKET_DAYS = {
    "LESS_THAN_ONE_DAY": 1, "ONE_TO_TWO_DAYS": 2, "TWO_TO_THREE_DAYS": 3, "THREE_TO_FOUR_DAYS": 4,
    "FOUR_TO_FIVE_DAYS": 5, "FIVE_TO_SIX_DAYS": 6, "SIX_TO_SEVEN_DAYS": 7, "SEVEN_TO_EIGHT_DAYS": 8,
    "EIGHT_TO_NINE_DAYS": 9, "NINE_TO_TEN_DAYS": 10, "TEN_TO_ELEVEN_DAYS": 11, "ELEVEN_TO_TWELVE_DAYS": 12,
    "TWELVE_TO_THIRTEEN_DAYS": 13, "THIRTEEN_TO_FOURTEEN_DAYS": 14, "FOURTEEN_TO_TWENTY_ONE_DAYS": 21,
    "TWENTY_ONE_TO_THIRTY_DAYS": 30, "THIRTY_TO_FORTY_FIVE_DAYS": 45, "FORTY_FIVE_TO_SIXTY_DAYS": 60,
    "SIXTY_TO_NINETY_DAYS": 90,
}


def load_lag_live(cid: str, end: dt.date, share: float = 0.9) -> int | None:
    """Days by which ~90% of the account's conversions have arrived (90 days of lag buckets)."""
    s = end - dt.timedelta(days=89)
    b = defaultdict(float)
    for r in gads.query(cid, f"""
        SELECT segments.conversion_lag_bucket, metrics.conversions
        FROM customer WHERE {gads.gaql_dates(s.isoformat(), end.isoformat())}""", soft=True):
        b[r.get("segments.conversion_lag_bucket", "")] += num(r.get("metrics.conversions"))
    tot = sum(v for k, v in b.items() if k in LAG_BUCKET_DAYS)
    if tot < 20:
        return None
    cum = 0.0
    for name, days in sorted(LAG_BUCKET_DAYS.items(), key=lambda kv: kv[1]):
        cum += b.get(name, 0.0) / tot
        if cum >= share - 1e-9:
            return days
    return 90


def _csv_rows(path, date=None):
    out = []
    for r in gads.read_ui_csv(path):
        camp = gads.pick(r, "campaign")
        if not camp or camp.lower().startswith("total"):
            continue
        d = parse_date(gads.pick(r, "day", "date")) if gads.pick(r, "day", "date") else date
        out.append(row_from(
            camp, gads.pick(r, "campaign_type", "advertising_channel_type"),
            num(gads.pick(r, "impr", "impressions")), num(gads.pick(r, "clicks")), num(gads.pick(r, "cost")),
            num(gads.pick(r, "conversions", "conv")), num(gads.pick(r, "conv_value", "conversion_value", "total_conv_value")),
            pct(gads.pick(r, "search_impr_share", "search_impression_share")),
            pct(gads.pick(r, "search_lost_is_budget", "search_lost_impr_share_budget")),
            pct(gads.pick(r, "search_lost_is_rank", "search_lost_impr_share_rank")),
            date=d, status=gads.pick(r, "campaign_status", "status"),
            budget=num(gads.pick(r, "budget", "daily_budget", "budget_amount")) if gads.pick(r, "budget", "daily_budget", "budget_amount") else None))
    return out


def _span(rows):
    ds = [r["date"] for r in rows if r["date"]]
    return (min(ds), max(ds)) if ds else None


def title_range(path):
    """UI exports carry the range in a title row: 'October 20, 2026 - October 26, 2026'."""
    try:
        head = Path(path).read_text(encoding="utf-8-sig", errors="ignore").splitlines()[:3]
    except OSError:
        return None
    for ln in head:
        m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})\s*[-–]\s*([A-Z][a-z]+ \d{1,2}, \d{4})", ln.replace('"', ""))
        if m:
            a, b = parse_date(m.group(1)), parse_date(m.group(2))
            if a and b:
                return (a, b)
    return None


def load_csv(paths, args):
    """Returns rows tagged 'cur'/'prev', plus the two ranges actually used."""
    if len(paths) >= 2:
        cur = _csv_rows(paths[0])
        prev = _csv_rows(paths[1])
        for r in cur:
            r["period"] = "cur"
        for r in prev:
            r["period"] = "prev"
        cr = title_range(paths[0]) or _span(cur)
        pr = title_range(paths[1]) or _span(prev)
        if not cr or not pr:
            cr, pr = periods(args)
            print(f"note: exports carry no date range; assuming {fmt_range(cr)} vs {fmt_range(pr)} from --days/--end.", file=sys.stderr)
        return cur + prev, cr, pr, []
    rows = _csv_rows(paths[0])
    dates = sorted({r["date"] for r in rows if r["date"]})
    if not dates:
        gads.die("The export has no Day column. Export campaigns segmented by Day, or pass two exports (current, previous).")
    if not args.end:
        args.end = dates[-1].isoformat()
    if not args.today:  # assume the export ran the day after its last day
        args.today = (dates[-1] + dt.timedelta(days=1)).isoformat()
    cr, pr = periods(args)
    for r in rows:
        r["period"] = "cur" if cr[0] <= r["date"] <= cr[1] else ("prev" if pr[0] <= r["date"] <= pr[1] else None)
    if not any(r["period"] == "prev" for r in rows):
        gads.die(f"The export does not cover the previous period ({fmt_range(pr)}). Export a longer date range.")
    return [r for r in rows if r["period"]], cr, pr, rows


# ---------------------------------------------------------------- math


def totals(rows) -> dict:
    t = {m: 0.0 for m in METRICS}
    elig = bl = rl = is_impr = 0.0
    for r in rows:
        for m in METRICS:
            t[m] += r[m]
        if r["is"] and r["is"] > 0:
            is_impr += r["impr"]
            e = r["impr"] / r["is"]
            elig += e
            bl += r["budget_lost"] * e
            rl += r["rank_lost"] * e
    t.update(derived(t))
    t["is"] = is_impr / elig if elig else None
    t["budget_lost"] = bl / elig if elig else None
    t["rank_lost"] = rl / elig if elig else None
    return t


def derived(t) -> dict:
    sd = gads.safe_div
    return {"ctr": sd(t["clicks"], t["impr"]), "cpc": sd(t["cost"], t["clicks"]), "cvr": sd(t["conv"], t["clicks"]),
            "cpa": sd(t["cost"], t["conv"]), "roas": sd(t["value"], t["cost"]), "aov": sd(t["value"], t["conv"])}


def chg(a, b):
    """Relative change b vs a (cur vs prev). None if not computable."""
    if a is None or b is None or a == 0:
        return None
    return b / a - 1


def log_share(parts: dict) -> dict:
    """Share of a multiplicative change explained by each factor (ratios cur/prev)."""
    logs = {k: math.log(v) for k, v in parts.items() if v and v > 0}
    tot = sum(abs(x) for x in logs.values())
    return {k: (x / tot if tot else 0) for k, x in logs.items()}


def decompose(p, c, goal):
    """Additive decomposition of the change in the primary outcome, plus efficiency drivers."""
    out = {}
    # conversions = clicks x CVR  (additive, exact)
    cvr0 = p["cvr"] or 0
    out["d_conv"] = c["conv"] - p["conv"]
    out["conv_volume_effect"] = (c["clicks"] - p["clicks"]) * cvr0
    out["conv_rate_effect"] = c["clicks"] * ((c["cvr"] or 0) - cvr0)
    # value = conv x AOV
    aov0 = p["aov"] or 0
    out["d_value"] = c["value"] - p["value"]
    out["value_conv_effect"] = (c["conv"] - p["conv"]) * aov0
    out["value_aov_effect"] = c["conv"] * ((c["aov"] or 0) - aov0)
    # clicks = impressions x CTR
    ctr0 = p["ctr"] or 0
    out["clicks_impr_effect"] = (c["impr"] - p["impr"]) * ctr0
    out["clicks_ctr_effect"] = c["impr"] * ((c["ctr"] or 0) - ctr0)
    # CPA = CPC / CVR ; ROAS = CVR x AOV / CPC  (log shares)
    if p["cpa"] and c["cpa"]:
        out["cpa_drivers"] = log_share({"CPC": c["cpc"] / p["cpc"], "CVR": p["cvr"] / c["cvr"]})
    if p["roas"] and c["roas"]:
        out["roas_drivers"] = log_share({"CVR": c["cvr"] / p["cvr"], "AOV": (c["aov"] or 1) / (p["aov"] or 1), "CPC": p["cpc"] / c["cpc"]})
    return out


def noise(a: float, b: float) -> float:
    """1.5 sd of the difference of two Poisson counts."""
    return 1.5 * math.sqrt(max(a + b, 1))


def pick_goal(args, prof, cur_t) -> str:
    if args.goal != "auto":
        return args.goal
    if prof.get("target_roas"):
        return "roas"
    if prof.get("target_cpa"):
        return "cpa"
    return "roas" if cur_t["value"] > cur_t["cost"] * 0.3 else "cpa"


# ---------------------------------------------------------------- analysis


def analyse(rows, cr, pr, prof, args):
    cur_rows = [r for r in rows if r.get("period") == "cur"]
    prev_rows = [r for r in rows if r.get("period") == "prev"]
    T, P = totals(cur_rows), totals(prev_rows)
    goal = pick_goal(args, prof, T)
    prim = "value" if goal == "roas" else "conv"

    by = defaultdict(lambda: {"cur": [], "prev": []})
    for r in rows:
        by[r["campaign"]][r["period"]].append(r)
    camps = []
    for name, d in by.items():
        c, p = totals(d["cur"]), totals(d["prev"])
        chan = next((r["channel"] for r in d["cur"] + d["prev"] if r["channel"]), "")
        camps.append({"campaign": name, "channel": chan, "cur": c, "prev": p, "dec": decompose(p, c, goal),
                      "d_cost": c["cost"] - p["cost"], "d_prim": c[prim] - p[prim]})

    gross_prim = sum(abs(x["d_prim"]) for x in camps) or 1
    gross_cost = sum(abs(x["d_cost"]) for x in camps) or 1
    aov = (P["aov"] or T["aov"] or 1) if prim == "value" else 1
    for x in camps:
        x["share_prim"] = abs(x["d_prim"]) / gross_prim
        x["share_cost"] = abs(x["d_cost"]) / gross_cost
        x["score"] = max(x["share_prim"], x["share_cost"])
        thr = noise(x["prev"]["conv"], x["cur"]["conv"]) * aov
        x["significant_prim"] = abs(x["d_prim"]) > thr
        x["material"] = x["score"] >= args.materiality and (x["significant_prim"] or x["share_cost"] >= args.materiality)
    movers = sorted([x for x in camps if x["material"]], key=lambda x: -x["score"])[: args.top]

    # conversion lag: days inside the lag window are still collecting conversions
    L = args.lag_days
    today = ref_today(args)
    n_cur = (cr[1] - cr[0]).days + 1
    k = sum(1 for i in range(n_cur) if (today - (cr[0] + dt.timedelta(days=i))).days <= L)
    adj = None
    dated = any(r["date"] for r in cur_rows)
    if dated and 0 < k < n_cur:
        cut_c, cut_p = cr[1] - dt.timedelta(days=k), pr[1] - dt.timedelta(days=k)
        adj = {"k": k, "cut_c": cut_c, "cut_p": cut_p,
               "T": totals([r for r in cur_rows if r["date"] and r["date"] <= cut_c]),
               "P": totals([r for r in prev_rows if r["date"] and r["date"] <= cut_p])}
    lag = None
    if cur_rows and cr:
        tail_start = cr[1] - dt.timedelta(days=max(L, 1) - 1)
        tail = totals([r for r in cur_rows if r["date"] and r["date"] >= tail_start])
        head = totals([r for r in cur_rows if r["date"] and r["date"] < tail_start])
        if tail["cvr"] is not None and head["cvr"] and tail["clicks"] >= 30:
            drop = 1 - tail["cvr"] / head["cvr"]
            if drop > 0.25:
                lag = {"tail_cvr": tail["cvr"], "head_cvr": head["cvr"], "drop": drop}
    lag_note = k > 0 or lag is not None

    # daily series for sparklines
    daily = defaultdict(lambda: {"cost": 0.0, "conv": 0.0, "value": 0.0})
    for r in rows:
        if r["date"]:
            for m in ("cost", "conv", "value"):
                daily[r["date"]][m] += r[m]
    series = [(d, daily[d]) for d in sorted(daily)]
    return {"T": T, "P": P, "goal": goal, "prim": prim, "camps": camps, "movers": movers, "lag": lag,
            "lag_note": lag_note, "lag_k": k, "lag_adj": adj, "series": series, "cr": cr, "pr": pr}


def action_for(x, goal, prof) -> tuple[str, str]:
    """(verb, sentence). Verbs: Start, Stop, Continue, Double down."""
    c, p, d = x["cur"], x["prev"], x["dec"]
    target_cpa = prof.get("target_cpa")
    target_roas = prof.get("target_roas")
    # With a target, judge against it. Without one, allow 15% drift from last period:
    # marginal growth is normally a little less efficient than the average.
    if goal == "roas":
        eff_ok = c["roas"] is not None and c["roas"] >= (target_roas or (p["roas"] or 0) * 0.85)
    else:
        eff_ok = c["cpa"] is not None and c["cpa"] <= (target_cpa or (p["cpa"] or 1e18) * 1.15)
    bl_up = (c["budget_lost"] or 0) - (p["budget_lost"] or 0)
    rl_up = (c["rank_lost"] or 0) - (p["rank_lost"] or 0)
    up = x["d_prim"] > 0
    vol_led = abs(d["conv_volume_effect"]) >= abs(d["conv_rate_effect"])
    if c["conv"] == 0 and p["conv"] >= 3 and c["cost"] > 0:
        return "Start", "Check conversion tracking on this campaign today (gads-tracking-audit). It spent with zero conversions after converting last period."
    if up and eff_ok and (c["budget_lost"] or 0) >= 0.10:
        return "Double down", f"Raise budget; it lost {c['budget_lost']:.0%} of impression share to budget while hitting the goal (gads-budget-pacing)."
    if up and eff_ok:
        return "Continue", "Leave it alone. Growth came at or better than goal efficiency."
    if up and not eff_ok:
        return "Stop", "Stop the extra spend from creeping further: tighten the target or cap budget until efficiency recovers (gads-bid-audit)."
    if not up and vol_led and bl_up > 0.05:
        return "Start", f"Restore budget or pacing; budget-lost impression share rose {bl_up:.0%} points (gads-budget-pacing)."
    if not up and vol_led and rl_up > 0.05:
        return "Start", f"Fix ad rank: rank-lost impression share rose {rl_up:.0%} points. Review bids and ad quality (gads-bid-audit, gads-rsa-audit)."
    if not up and not vol_led:
        return "Start", "Conversion rate fell, not traffic. Check landing page and tracking before touching bids (gads-tracking-audit)."
    if x["d_cost"] > 0 and x["d_prim"] <= 0:
        return "Stop", "Spend rose without more results. Mine search terms for waste (gads-search-terms) and hold budget."
    return "Continue", "Volume dipped with demand; no structural change found. Watch next period."


def explain(x, cur, goal) -> str:
    c, p, d = x["cur"], x["prev"], x["dec"]
    if goal == "roas":
        head = f"conv. value {money(p['value'], cur)} to {money(c['value'], cur)}, ROAS {fmt_x(p['roas'])} to {fmt_x(c['roas'])}"
    else:
        head = f"conversions {p['conv']:.0f} to {c['conv']:.0f}, CPA {money(p['cpa'], cur)} to {money(c['cpa'], cur)}"
    parts = [f"{x['campaign']}: {head}, spend {money(p['cost'], cur)} to {money(c['cost'], cur)}."]
    ve, re_ = d["conv_volume_effect"], d["conv_rate_effect"]
    parts.append(f"Conversion change splits into {ve:+.1f} from click volume and {re_:+.1f} from conversion rate "
                 f"(CVR {fmt_pct(p['cvr'])} to {fmt_pct(c['cvr'])}).")
    drv = d.get("roas_drivers" if goal == "roas" else "cpa_drivers")
    if drv:
        main = max(drv, key=lambda k: abs(drv[k]))
        parts.append(f"{'ROAS' if goal == 'roas' else 'CPA'} moved mostly on {main} ({abs(drv[main]):.0%} of the change).")
    if c["is"] is not None and p["is"] is not None:
        parts.append(f"Search IS {fmt_pct(p['is'])} to {fmt_pct(c['is'])}; lost to budget {fmt_pct(p['budget_lost'])} to {fmt_pct(c['budget_lost'])}, to rank {fmt_pct(p['rank_lost'])} to {fmt_pct(c['rank_lost'])}.")
    return " ".join(parts)


def fmt_pct(v):
    return "n/a" if v is None else f"{v:.1%}" if v < 0.1 else f"{v:.0%}"


def fmt_x(v):
    return "n/a" if v is None else f"{v:.2f}x"


def fmt_chg(v, inverse=False):
    if v is None:
        return "n/a"
    arrow = "▲" if v > 0 else ("▼" if v < 0 else "")
    return f"{arrow} {abs(v):.0%}" if abs(v) >= 0.01 else "flat"


# ---------------------------------------------------------------- same-weekday anomalies

ANOM_METRICS = [  # key, label, kind, bad direction ('up'|'down'|'both'), conversion metric?
    ("cost", "Spend", "money", "both", False), ("clicks", "Clicks", "num", "down", False),
    ("conv", "Conversions", "num", "down", True), ("cpa", "CPA", "money", "up", True),
    ("value", "Conv. value", "money", "down", True),
]
MIN_ABS = {"clicks": 20, "conv": 3}  # smallest absolute change worth a flag, for counts


def daily_totals(rows) -> dict:
    tot = defaultdict(lambda: {"cost": 0.0, "clicks": 0.0, "conv": 0.0, "value": 0.0, "impr": 0.0})
    for r in rows:
        if r["date"]:
            for m in ("cost", "clicks", "conv", "value", "impr"):
                tot[r["date"]][m] += r[m]
    return tot


def _agg(tot, days, key):
    if key == "cpa":
        cost = sum(tot[d]["cost"] for d in days if d in tot)
        conv = sum(tot[d]["conv"] for d in days if d in tot)
        return cost / conv if conv else None
    return sum(tot[d][key] for d in days if d in tot)


def weekday_anomalies(rows, today: dt.date, lag: int, args) -> dict:
    """Compare the last complete day and last 7 days with the same weekdays over the previous N weeks.

    Conversion metrics anchor on the last day outside the lag window, since
    conversions are reported on click date and recent days are still filling in.
    """
    tot = daily_totals(rows)
    if not tot:
        return {"items": [], "note": "No daily data, so the same-weekday check was skipped."}
    first, last = min(tot), max(tot)
    for d in [first + dt.timedelta(i) for i in range((last - first).days + 1)]:
        tot[d]  # absent days are zero days (the API omits rows with no impressions)
    conv_anchor = min(last, today - dt.timedelta(days=lag + 1))
    items, used = [], 0
    for key, label, kind, bad_dir, is_conv in ANOM_METRICS:
        anchor = conv_anchor if is_conv else last
        for w in (1, 7):
            cur_days = [anchor - dt.timedelta(days=i) for i in range(w)]
            base = []
            for k in range(1, args.anomaly_weeks + 1):
                ds = [d - dt.timedelta(weeks=k) for d in cur_days]
                if min(ds) < first:
                    break
                v = _agg(tot, ds, key)
                if v is not None:
                    base.append(v)
            x = _agg(tot, cur_days, key)
            if x is None or len(base) < 4 or not any(base):
                continue
            used = max(used, len(base))
            mean = sum(base) / len(base)
            sd = math.sqrt(sum((b - mean) ** 2 for b in base) / (len(base) - 1))
            floor = 0.05 * abs(mean)
            if key in MIN_ABS:
                floor = max(floor, math.sqrt(max(mean, 1)))  # Poisson floor for counts
            sd = max(sd, floor, 1e-9)
            z = (x - mean) / sd
            rel = (x - mean) / mean if mean else None
            if abs(z) < args.anomaly_z or rel is None or abs(rel) < args.anomaly_min_change or abs(x - mean) < MIN_ABS.get(key, 0):
                continue
            bad = bad_dir == "both" or (bad_dir == "up") == (x > mean)
            when = (f"on {anchor:%a %b} {anchor.day}" if w == 1 else f"in the 7 days to {anchor:%b} {anchor.day}")
            items.append({"metric": key, "label": label, "kind": kind, "window_days": w, "when": when,
                          "start": cur_days[-1], "end": anchor, "value": x, "mean": mean, "sd": sd, "z": z, "rel": rel,
                          "bad": bad, "weeks": len(base), "lag_shifted": is_conv and anchor < last})
    note = (f"Same-weekday check: last complete day {last:%b} {last.day} and the 7 days to it, vs the same weekdays over the previous "
            f"{used or args.anomaly_weeks} weeks (flag at |z| >= {args.anomaly_z:g} and a change of {args.anomaly_min_change:.0%} or more).")
    if conv_anchor < last:
        note += f" Conversions, CPA and value use {conv_anchor:%b} {conv_anchor.day} as the last day, because the last {lag} days are still collecting conversions."
    if not used:
        note = f"Same-weekday check skipped: it needs daily data covering at least 5 weeks (got {(last - first).days + 1} days)."
    return {"items": items, "note": note}


def fmt_val(v, kind, cur):
    return money(v, cur) if kind == "money" else (f"{v:,.0f}" if v >= 100 else f"{v:,.1f}")


# ---------------------------------------------------------------- zero impressions


def zero_impressions(rows, meta, prof, last: dt.date, args) -> list[dict]:
    """Enabled campaigns with budget and zero impressions in the last --zero-days, after serving before."""
    zero_days = {last - dt.timedelta(days=i) for i in range(args.zero_days)}
    before = {last - dt.timedelta(days=args.zero_days + i) for i in range(14)}
    by = defaultdict(lambda: {"zero": 0.0, "before": 0.0, "days_before": 0, "status": "", "budget": None, "id": ""})
    for r in sorted((r for r in rows if r["date"]), key=lambda r: r["date"]):
        b = by[r["campaign"]]
        if r["date"] in zero_days:
            b["zero"] += r["impr"]
        elif r["date"] in before:
            b["before"] += r["impr"]
            b["days_before"] += 1 if r["impr"] > 0 else 0
        b["status"] = r.get("status") or b["status"]
        b["budget"] = r.get("budget") if r.get("budget") is not None else b["budget"]
        b["id"] = r.get("campaign_id") or b["id"]
    out = []
    budgets = prof.get("campaign_budgets") or {}
    for name, b in by.items():
        if b["before"] <= 0 or b["zero"] > 0:
            continue
        m = meta.get(name, {})
        status = str(m.get("status") or b["status"] or "").upper()
        budget = m.get("budget") if m.get("budget") is not None else b["budget"]
        if budget is None and name in budgets:
            budget = budgets[name]
        if status and status != "ENABLED":
            continue
        if budget is not None and budget <= 0:
            continue
        if m.get("end") and m["end"] < last.isoformat():
            continue
        out.append({"campaign": name, "impr_per_day": b["before"] / 14, "days_before": b["days_before"],
                    "status_known": bool(status), "budget": budget, "primary_status": m.get("primary_status", ""),
                    "reasons": m.get("reasons") or [], "id": m.get("id") or b["id"]})
    return out


# ---------------------------------------------------------------- change history

CLIENT_LABEL = {
    "GOOGLE_ADS_WEB_CLIENT": "a user in the Google Ads UI", "GOOGLE_ADS_EDITOR": "a user in Google Ads Editor",
    "GOOGLE_ADS_MOBILE_APP": "a user in the Google Ads app", "GOOGLE_ADS_API": "an API tool",
    "GOOGLE_ADS_SCRIPTS": "a Google Ads script", "GOOGLE_ADS_AUTOMATED_RULE": "an automated rule",
    "GOOGLE_ADS_BULK_UPLOAD": "a bulk upload", "GOOGLE_ADS_RECOMMENDATIONS": "auto-applied recommendations",
    "GOOGLE_ADS_RECOMMENDATIONS_SUBSCRIPTION": "auto-applied recommendations",
    "SEARCH_ADS_360_SYNC": "Search Ads 360", "SEARCH_ADS_360_POST": "Search Ads 360", "INTERNAL_TOOL": "Google",
}
FIELD_LABEL = {"target_cpa_micros": "target CPA", "target_roas": "target ROAS", "amount_micros": "daily budget",
               "cpc_bid_micros": "max CPC", "status": "status", "bidding_strategy_type": "bid strategy",
               "cpc_bid_ceiling_micros": "max CPC limit", "final_urls": "final URL"}
RES_LABEL = {"CAMPAIGN": "campaign", "CAMPAIGN_BUDGET": "budget", "AD_GROUP": "ad group", "AD_GROUP_CRITERION": "keyword or targeting",
             "CAMPAIGN_CRITERION": "campaign targeting", "AD_GROUP_AD": "ad", "AD": "ad", "AD_GROUP_BID_MODIFIER": "bid adjustment"}
# changes most likely to explain a move come first
PRIORITY = ("target", "budget", "bid", "status", "paused", "removed", "strategy")


def snake(s: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


def who_label(user: str, client: str, prof) -> str:
    """Never print a person's email or name unless the profile allows it."""
    if prof.get("show_change_users") and user:
        return user
    if client and client.upper() in CLIENT_LABEL:
        return CLIENT_LABEL[client.upper()]
    u = (user or "").lower() + " " + (client or "").lower()
    if "recommend" in u or "auto-apply" in u or "auto apply" in u:
        return "auto-applied recommendations"
    if "rule" in u:
        return "an automated rule"
    if "script" in u:
        return "a Google Ads script"
    if "editor" in u:
        return "a user in Google Ads Editor"
    if "api" in u.split() or "api" in (client or "").lower():
        return "an API tool"
    return "a user"


def _num_text(v: str) -> float | None:
    t = re.sub(r"[^0-9.\-]", "", str(v))
    try:
        return float(t) if t not in ("", "-", ".") else None
    except ValueError:
        return None


def describe_from_to(label: str, old: str, new: str) -> str:
    a, b = _num_text(old), _num_text(new)
    if a is not None and b is not None and a != b:
        return f"{label} {'raised' if b > a else 'lowered'} from {old} to {new}"
    return f"{label} changed from {old} to {new}"


def _short(v: str) -> str:
    """'$60.00' -> '$60'."""
    return re.sub(r"(\d)\.00\b", r"\1", str(v).strip())


def describe_ui_change(text: str) -> str:
    first = str(text).strip().splitlines()[0] if str(text).strip() else ""
    m = re.match(r"^(.*?)\s+(?:changed\s+)?from\s+(.+?)\s+to\s+(.+?)\.?$", first, re.I)
    if m:
        label = m.group(1).strip()
        label = label[0].lower() + label[1:] if label[:3] != label[:3].upper() else label
        label = re.sub(r"^target cpa", "target CPA", label, flags=re.I)
        label = re.sub(r"^target roas", "target ROAS", label, flags=re.I)
        return describe_from_to(label, _short(m.group(2)), _short(m.group(3)))
    return first[0].lower() + first[1:] if first else "a change"


def load_changes_csv(path: str, prof) -> list[dict]:
    out = []
    for r in gads.read_ui_csv(path):
        raw = gads.pick(r, "date_time", "date_and_time", "date", "time", "change_date_time")
        m = re.search(r"\d{4}-\d{2}-\d{2}", str(raw)) or re.search(r"[A-Z][a-z]{2,8} \d{1,2}, \d{4}", str(raw))
        d = parse_date(m.group(0)) if m else None
        if not d:
            continue
        out.append({"date": d, "campaign": gads.pick(r, "campaign", "campaign_name"),
                    "what": describe_ui_change(gads.pick(r, "changes", "change", "description")),
                    "who": who_label(gads.pick(r, "user", "changed_by"), gads.pick(r, "tool", "client", "client_type", "source"), prof)})
    return out


def load_changes_live(cid: str, start: dt.date, end: dt.date, meta: dict, prof, cur: str = "") -> tuple[list[dict], str]:
    """change_event must be filtered to the last 30 days and needs LIMIT <= 10000."""
    floor = gads.today() - dt.timedelta(days=29)
    note = ""
    if start < floor:
        note = f"Change history only covers {floor:%b} {floor.day} onward (the API keeps 30 days)."
        start = floor
    if start > end:
        return [], note
    rows = gads.query(cid, f"""
        SELECT change_event.change_date_time, change_event.change_resource_type, change_event.change_resource_name,
               change_event.resource_change_operation, change_event.user_email, change_event.client_type,
               change_event.changed_fields, change_event.campaign, change_event.old_resource, change_event.new_resource
        FROM change_event
        WHERE change_event.change_date_time >= '{start.isoformat()}' AND change_event.change_date_time <= '{(end + dt.timedelta(days=1)).isoformat()}'
        ORDER BY change_event.change_date_time DESC
        LIMIT 10000""", soft=True)
    by_id = {m["id"]: n for n, m in meta.items()}
    by_budget = defaultdict(list)
    for n, m in meta.items():
        if m.get("budget_res"):
            by_budget[m["budget_res"]].append(n)
    out = []
    for r in rows:
        d = parse_date(str(r.get("change_event.change_date_time", ""))[:10])
        if not d:
            continue
        rt = r.get("change_event.change_resource_type", "")
        op = r.get("change_event.resource_change_operation", "")
        camp_res = r.get("change_event.campaign", "") or ""
        names = [by_id.get(camp_res.split("/")[-1], "")] if camp_res else []
        if rt == "CAMPAIGN_BUDGET":
            names = by_budget.get(r.get("change_event.change_resource_name", ""), names)
        what = describe_event(r, rt, op, cur)
        who = who_label(r.get("change_event.user_email", ""), r.get("change_event.client_type", ""), prof)
        for n in names or [""]:
            out.append({"date": d, "campaign": n, "what": what, "who": who})
    return out, note


def describe_event(r: dict, rt: str, op: str, cur: str = "") -> str:
    res = RES_LABEL.get(rt, rt.lower().replace("_", " ") or "item")
    if op == "CREATE":
        return f"{res} added"
    if op == "REMOVE":
        return f"{res} removed"
    key = rt.lower()
    fields = [snake(f.strip()).replace("._", ".") for f in str(r.get("change_event.changed_fields", "")).split(",") if f.strip()]
    bits = []
    for f in fields[:3]:
        old = r.get(f"change_event.old_resource.{key}.{f}")
        new = r.get(f"change_event.new_resource.{key}.{f}")
        leaf = f.split(".")[-1]
        label = FIELD_LABEL.get(leaf, leaf.replace("_micros", "").replace("_", " "))
        if rt != "CAMPAIGN" and label == "status":
            label = f"{res} status"
        if old is None and new is None:
            bits.append(f"{label} changed")
            continue
        if leaf.endswith("_micros"):
            old, new = money(micros(old), cur), money(micros(new), cur)
        bits.append(describe_from_to(label, _short(str(old).lower() if isinstance(old, str) and old.isupper() else old),
                                     _short(str(new).lower() if isinstance(new, str) and new.isupper() else new)))
    return "; ".join(bits) or f"{res} edited"


def changes_for(changes, campaign, start, end, limit=3) -> list[dict]:
    hit = [c for c in changes if c["campaign"] == campaign and start <= c["date"] <= end]
    rank = lambda c: next((i for i, k in enumerate(PRIORITY) if k in c["what"].lower()), len(PRIORITY))  # noqa: E731
    return sorted(hit, key=lambda c: (rank(c), -c["date"].toordinal()))[:limit]


def change_text(chs) -> str:
    return "; ".join(f"{c['what']} on {c['date']:%b} {c['date'].day} by {c['who']}" for c in chs)


# ---------------------------------------------------------------- output

KPIS = [  # key, label, kind, better ('up'|'down'|None)
    ("cost", "Spend", "money", None), ("conv", "Conversions", "num", "up"), ("cpa", "CPA", "money", "down"),
    ("value", "Conv. value", "money", "up"), ("roas", "ROAS", "x", "up"), ("clicks", "Clicks", "num", "up"),
    ("cpc", "CPC", "money", "down"), ("cvr", "Conv. rate", "pct", "up"), ("ctr", "CTR", "pct", "up"),
    ("impr", "Impressions", "num", None),
]


def fmt_kpi(v, kind, cur):
    if v is None:
        return "n/a"
    return {"money": money(v, cur), "num": f"{v:,.0f}" if v >= 100 else f"{v:,.1f}", "pct": fmt_pct(v), "x": fmt_x(v)}[kind]


def queue_count(d: Path) -> tuple[int, list[str]]:
    n, skills = 0, []
    for f in d.glob("gads-*.json"):
        if f.stem == SKILL:
            continue
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        k = len([a for a in data.get("approvals", []) if a.get("status", "pending") == "pending"])
        n += k
        skills.append(f"{data.get('skill', f.stem)} ({k})")
    return n, skills


def spark(values, w=220, h=40) -> str:
    if not values or max(values) == 0:
        return ""
    mx = max(values)
    n = len(values)
    pts = " ".join(f"{(i / max(1, n - 1)) * w:.1f},{h - 2 - (v / mx) * (h - 6):.1f}" for i, v in enumerate(values))
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" aria-label="trend">'
            f'<polyline fill="none" stroke="currentColor" stroke-width="1.6" points="{pts}"/></svg>')


def split_bar(a: float, b: float, la: str, lb: str) -> str:
    tot = abs(a) + abs(b)
    if tot == 0:
        return ""
    wa = abs(a) / tot * 100
    return (f'<div class="split" role="img" aria-label="{html.escape(la)} {a:+.1f}, {html.escape(lb)} {b:+.1f}">'
            f'<span class="seg a" style="width:{wa:.1f}%"></span><span class="seg b" style="width:{100 - wa:.1f}%"></span></div>'
            f'<div class="split-legend"><span><i class="sw a"></i>{html.escape(la)} {a:+.1f}</span>'
            f'<span><i class="sw b"></i>{html.escape(lb)} {b:+.1f}</span></div>')


def alert_lines(res, cur) -> tuple[list[str], list[str]]:
    """(zero-impression lines, anomaly lines) as plain sentences."""
    zl = [f"{z['campaign']}: {z.get('why', '')}" for z in res.get("zero") or []]
    al = []
    for a in res.get("anoms") or []:
        tag = "" if a["bad"] else " (favourable)"
        al.append(f"{a['label']} {a['when']}: {fmt_val(a['value'], a['kind'], cur)} vs usual {fmt_val(a['mean'], a['kind'], cur)}, "
                  f"{'+' if a['rel'] > 0 else '-'}{abs(a['rel']):.0%}, z = {a['z']:+.1f}{tag}")
    return zl, al


def alert_md(res, cur, args) -> list[str]:
    zl, al = alert_lines(res, cur)
    out = []
    if zl:
        out += ["## Stopped serving", ""] + [f"- **CRITICAL** · {x}" for x in zl] + [""]
    out += ["## Same-weekday check", ""]
    out += [f"- {x}" for x in al] if al else ["Nothing outside the normal range for these weekdays."]
    return out + [""]


def render(res, info, cur, qn, qskills, cid, approvals):
    T, P, goal = res["T"], res["P"], res["goal"]
    e = html.escape
    kpis = [k for k in KPIS if not (goal == "cpa" and k[0] in ("value", "roas") and T["value"] == 0)]
    series = res["series"]
    cur_days = [v for d, v in series if res["cr"][0] <= d <= res["cr"][1]]
    tiles = []
    for key, label, kind, better in kpis:
        c = chg(P.get(key), T.get(key))
        tone = ""
        if c is not None and better and abs(c) >= 0.01:
            good = (c > 0) == (better == "up")
            tone = "good" if good else "bad"
        sp = ""
        if key in ("cost", "conv", "value") and cur_days:
            sp = spark([v[key] for _, v in series])
        tiles.append(f'<div class="tile"><div class="lab">{e(label)}</div><div class="val">{e(fmt_kpi(T.get(key), kind, cur))}</div>'
                     f'<div class="chg {tone}">{e(fmt_chg(c))} <span class="prev">vs {e(fmt_kpi(P.get(key), kind, cur))}</span></div>{sp}</div>')
    mv = []
    for i, x in enumerate(res["movers"], 1):
        d = x["dec"]
        mv.append(f'<article class="mover"><div class="mhead"><span class="rank">{i}</span><h3>{e(x["campaign"])}</h3>'
                  f'<span class="verb">{e(x["action"][0])}</span></div><p>{e(x["explain"])}</p>'
                  f'{split_bar(d["conv_volume_effect"], d["conv_rate_effect"], "click volume", "conv. rate")}'
                  f'<p class="act"><b>{e(x["action"][0])}:</b> {e(x["action"][1])}</p></article>')
    if not mv:
        mv.append('<p class="muted">No campaign moved enough to separate from normal variation. Continue as planned.</p>')
    rows = []
    for x in sorted(res["camps"], key=lambda x: -x["cur"]["cost"]):
        c, p = x["cur"], x["prev"]
        rows.append("<tr>" + "".join(f"<td>{e(str(v))}</td>" for v in [
            x["campaign"], money(c["cost"], cur), fmt_chg(chg(p["cost"], c["cost"])), f"{c['conv']:.1f}",
            fmt_chg(chg(p["conv"], c["conv"])), money(c["cpa"], cur), fmt_x(c["roas"]) if c["value"] else "n/a",
            fmt_pct(c["is"]), fmt_pct(c["budget_lost"]), fmt_pct(c["rank_lost"])]) + "</tr>")
    notes = res["notes"]
    zl, al = alert_lines(res, cur)
    alerts_html = ""
    lag_html = f'<p class="muted">{e(res["lag_line"])}</p>' if res.get("lag_line") else ""
    if zl:
        alerts_html += '<h2>Stopped serving</h2><ul class="alerts">' + "".join(f"<li><b>Critical:</b> {e(x)}</li>" for x in zl) + "</ul>"
    if al:
        alerts_html += '<h2>Same-weekday check</h2><ul class="alerts">' + "".join(f"<li>{e(x)}</li>" for x in al) + "</ul>"
    opt = info.get("optimization_score")
    opt_txt = f"{float(opt):.0%}" if opt not in (None, "") else "n/a"
    q = (f'<b>{qn} items waiting for approval</b> from {e(", ".join(qskills))}. See approvals.md.' if qn
         else "No other gads-* approvals waiting today.")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Google Ads Performance</title>
<style>
:root{{--bg:#ffffff;--fg:#111111;--muted:#666666;--line:#e3e3e3;--soft:#f5f5f5;--a:#111111;--b:#a8a8a8}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#111111;--fg:#f2f2f2;--muted:#9a9a9a;--line:#2c2c2c;--soft:#1b1b1b;--a:#f2f2f2;--b:#5e5e5e}}}}
:root[data-theme="dark"]{{--bg:#111111;--fg:#f2f2f2;--muted:#9a9a9a;--line:#2c2c2c;--soft:#1b1b1b;--a:#f2f2f2;--b:#5e5e5e}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}}
main{{max-width:1040px;margin:0 auto;padding:28px 16px 48px}}h1{{font-size:24px;margin:0 0 4px;letter-spacing:-.01em}}h2{{font-size:17px;margin:32px 0 12px}}h3{{font-size:15px;margin:0;flex:1}}
.muted,.sub{{color:var(--muted)}}.sub{{margin:0 0 20px}}
.headline{{font-size:17px;padding:14px 16px;background:var(--soft);border-left:3px solid var(--fg);margin:0 0 20px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}}
.tile{{border:1px solid var(--line);border-radius:8px;padding:12px}}.lab{{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}}
.val{{font-size:22px;font-weight:600;margin:2px 0}}.chg{{font-size:13px}}.chg.good{{font-weight:600}}.chg.bad{{font-weight:600;text-decoration:underline;text-underline-offset:3px}}.prev{{color:var(--muted);font-weight:400}}
.spark{{width:100%;height:28px;margin-top:6px;color:var(--muted)}}
.mover{{border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin-bottom:12px}}.mhead{{display:flex;align-items:center;gap:10px;margin-bottom:6px}}
.rank{{width:24px;height:24px;border-radius:50%;background:var(--fg);color:var(--bg);display:grid;place-items:center;font-size:13px;font-weight:600;flex:none}}
.verb{{font-size:12px;border:1px solid var(--fg);border-radius:99px;padding:2px 10px;white-space:nowrap}}
.split{{display:flex;height:10px;border-radius:5px;overflow:hidden;background:var(--soft);margin:8px 0 4px}}.seg.a{{background:var(--a)}}.seg.b{{background:var(--b)}}
.split-legend{{display:flex;gap:16px;font-size:12px;color:var(--muted);flex-wrap:wrap}}.sw{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}}.sw.a{{background:var(--a)}}.sw.b{{background:var(--b)}}
.act{{margin:10px 0 0}}.tw{{overflow-x:auto;border:1px solid var(--line);border-radius:8px}}
table{{border-collapse:collapse;width:100%;font-size:13px;min-width:720px}}th,td{{padding:8px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}}
th:first-child,td:first-child{{text-align:left;white-space:normal}}th{{font-weight:600;color:var(--muted);background:var(--soft)}}tr:last-child td{{border-bottom:0}}
.alerts{{padding-left:18px}}.alerts li{{margin:4px 0}}.queue{{padding:12px 16px;border:1px dashed var(--muted);border-radius:8px}}ul.notes{{padding-left:18px;color:var(--muted);font-size:13px}}
footer{{margin-top:36px;font-size:12px;color:var(--muted)}}
</style></head><body><main>
<h1>Google Ads performance</h1>
<p class="sub">{e(info.get("name") or gads.fmt_cid(cid) or "Offline export")} · {e(fmt_range(res["cr"]))} vs {e(fmt_range(res["pr"]))} · goal: {"ROAS" if goal == "roas" else "CPA"}</p>
<p class="headline">{e(res["headline"])}</p>
<section class="grid">{"".join(tiles)}</section>
{lag_html}{alerts_html}
<h2>Top movers</h2>{"".join(mv)}
<h2>Campaigns</h2><div class="tw"><table><thead><tr><th>Campaign</th><th>Spend</th><th>Δ</th><th>Conv.</th><th>Δ</th><th>CPA</th><th>ROAS</th><th>Search IS</th><th>Lost (budget)</th><th>Lost (rank)</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
<h2>Waiting on you</h2><p class="queue">{q}</p>
<h2>Notes</h2><ul class="notes">{"".join(f"<li>{e(n)}</li>" for n in notes)}
<li>Optimization score {e(opt_txt)}. This is Google's measure of how many of its recommendations are adopted, not a performance KPI.</li></ul>
<footer>Generated {e(dt.datetime.now().strftime("%Y-%m-%d %H:%M"))} · gads-performance-report</footer>
</main></body></html>
"""


def build(res, info, cur, cid, prof, args, qn, qskills):
    T, P, goal = res["T"], res["P"], res["goal"]
    changes = res.get("changes") or []
    for x in res["movers"]:
        x["action"] = action_for(x, goal, prof)
        x["explain"] = explain(x, cur, goal)
        x["changes"] = changes_for(changes, x["campaign"], res["pr"][0], res["cr"][1])
        if x["changes"]:
            x["explain"] += " Changes in the window: " + change_text(x["changes"]) + "."
    notes = []
    L, k, adj = args.lag_days, res["lag_k"], res["lag_adj"]
    lag_line = ""
    if adj:
        Ta, Pa = adj["T"], adj["P"]
        eff_a = (f"ROAS {fmt_x(Pa['roas'])} to {fmt_x(Ta['roas'])}" if goal == "roas" else f"CPA {money(Pa['cpa'], cur)} to {money(Ta['cpa'], cur)}")
        lag_line = (f"The last {k} day(s) of the current period are still collecting conversions (lag {L} days, {args._lag_source}). "
                    f"On complete days only ({fmt_range((res['cr'][0], adj['cut_c']))} vs {fmt_range((res['pr'][0], adj['cut_p']))}): "
                    f"conversions {Pa['conv']:,.1f} to {Ta['conv']:,.1f} ({fmt_chg(chg(Pa['conv'], Ta['conv']))}), {eff_a}.")
    elif k:
        n_cur = (res["cr"][1] - res["cr"][0]).days + 1
        lead = (f"Every day in the current period is inside the {L}-day conversion lag window" if k >= n_cur else
                f"The last {k} day(s) of the current period are still collecting conversions (lag {L} days) and the export has no Day column to leave them out")
        lag_line = (f"{lead} ({args._lag_source}). Conversion, CPA and ROAS comparisons are provisional; "
                    "compare spend and clicks with confidence and re-run once the window has passed.")
    if res["lag"]:
        notes.append(f"Conversion lag: the last {max(L, 1)} days convert at {fmt_pct(res['lag']['tail_cvr'])} vs {fmt_pct(res['lag']['head_cvr'])} earlier in the period. "
                     "Late conversions will still land, so the current period understates results.")
    if res.get("anom_note"):
        notes.append(res["anom_note"])
    if res.get("changes_note"):
        notes.append(res["changes_note"])
    if args.compare == "yoy":
        notes.append("Year over year compares weekday-aligned dates 364 days apart.")
    if args.compare == "mtd":
        notes.append("Month to date is compared with the same number of days last month.")
    notes.append("Movers are ranked by contribution to the account change, not by % change; small campaigns with big % swings are excluded as noise.")
    res["notes"] = notes
    res["lag_line"] = lag_line

    prim_label = "conv. value" if goal == "roas" else "conversions"
    c_prim, p_prim = (T["value"], P["value"]) if goal == "roas" else (T["conv"], P["conv"])
    eff = f"ROAS {fmt_x(P['roas'])} to {fmt_x(T['roas'])}" if goal == "roas" else f"CPA {money(P['cpa'], cur)} to {money(T['cpa'], cur)}"
    top = ", ".join(x["campaign"] for x in res["movers"][:3]) or "no material movers"
    headline = (f"Spend {money(T['cost'], cur)} ({fmt_chg(chg(P['cost'], T['cost']))}), {prim_label} "
                f"{(money(c_prim, cur) if goal == 'roas' else f'{c_prim:,.0f}')} ({fmt_chg(chg(p_prim, c_prim))}), {eff}. Top movers: {top}")
    res["headline"] = headline

    approvals = []
    for x in res["movers"]:
        c, p = x["cur"], x["prev"]
        cvr_drop = chg(p["cvr"], c["cvr"])
        if (c["conv"] == 0 and p["conv"] >= 3) or (cvr_drop is not None and cvr_drop < -0.30 and abs(x["dec"]["conv_rate_effect"]) > abs(x["dec"]["conv_volume_effect"])):
            approvals.append(Approval(
                skill=SKILL, kind="investigate", entity=x["campaign"],
                action=f"Investigate the conversion-rate drop on '{x['campaign']}'",
                why=f"CVR {fmt_pct(p['cvr'])} to {fmt_pct(c['cvr'])} while clicks held up; this pattern usually means a tracking or landing page break, not demand.",
                impact_monthly=abs(x["dec"]["conv_rate_effect"]) * ((p["aov"] or P["aov"] or 0) if goal == "roas" else (P["cpa"] or 0))
                * 30 / max(1, (res["cr"][1] - res["cr"][0]).days + 1),
                impact_label="at risk", confidence="medium", risk="low",
                evidence={"cvr_prev": p["cvr"], "cvr_cur": c["cvr"], "clicks_cur": c["clicks"]}))
    for z in res.get("zero") or []:
        chs = changes_for(changes, z["campaign"], res["cr"][1] - dt.timedelta(days=13 + args.zero_days), res["cr"][1])
        last = res["cr"][1]
        when = f"{last:%b} {last.day}" if args.zero_days == 1 else f"the last {args.zero_days} days to {last:%b} {last.day}"
        why = (f"It averaged {z['impr_per_day']:,.0f} impressions a day over the previous 14 days and served nothing on {when}"
               + (f", with a daily budget of {money(z['budget'], cur)}" if z["budget"] else "") + ". ")
        if z["primary_status"]:
            why += f"Google reports status {z['primary_status'].lower().replace('_', ' ')}" + (f" ({', '.join(str(r).lower().replace('_', ' ') for r in z['reasons'][:3])})" if z["reasons"] else "") + ". "
        if chs:
            why += "Recent changes: " + change_text(chs) + ". "
        else:
            why += "Usual causes: disapproved ads, paused ad groups, billing, an end date, or a targeting change. "
        if not z["status_known"]:
            why += "The export has no status column; confirm the campaign is enabled."
        z["why"] = why.strip()
        approvals.append(Approval(
            skill=SKILL, kind="zero_impressions", entity=z["campaign"], severity="critical",
            action=f"Check why '{z['campaign']}' stopped serving: zero impressions on {when}",
            why=z["why"], impact_label="protects", confidence="high", risk="low",
            evidence={"impr_per_day_before": round(z["impr_per_day"], 1), "days_with_impr_before": z["days_before"],
                      "budget": z["budget"], "primary_status": z["primary_status"], "changes": len(chs)}))
    groups = defaultdict(list)  # one approval per window, listing every metric that broke
    for a in res.get("anoms") or []:
        if a["bad"]:
            groups[(a["end"], a["window_days"])].append(a)
    for (end, w), items in sorted(groups.items()):
        a0 = items[0]
        base = f"a normal {end:%A}" if w == 1 else f"the same 7 days in the previous {max(a['weeks'] for a in items)} weeks"
        lab = lambda a: a["label"] if a["label"].isupper() else a["label"].lower()  # noqa: E731
        moves = ", ".join(f"{lab(a)} {'up' if a['value'] > a['mean'] else 'down'} {abs(a['rel']):.0%}" for a in items)
        why = "; ".join(f"{a['label']} {fmt_val(a['value'], a['kind'], cur)} vs a usual {fmt_val(a['mean'], a['kind'], cur)} (z = {a['z']:+.1f})"
                        for a in items) + "."
        if any(a["lag_shifted"] for a in items):
            why += f" Days still collecting conversions are left out (lag {L} days)."
        chs = [c for c in changes if a0["start"] <= c["date"] <= end][:2]
        if chs:
            why += " Changes in that window: " + change_text(chs) + "."
        approvals.append(Approval(
            skill=SKILL, kind="anomaly", entity=f"account {end} {w}d",
            severity="high" if any(abs(a["z"]) >= 3 for a in items) else "medium",
            action=f"Investigate {a0['when'].split(' ', 1)[1] if w == 1 else 'the 7 days to ' + f'{end:%b} {end.day}'}: {moves} vs {base}",
            why=why, impact_label="at risk", confidence="medium", risk="low",
            evidence={a["metric"]: {"value": round(a["value"], 2), "mean": round(a["mean"], 2), "z": round(a["z"], 2)} for a in items}
            | {"start": str(a0["start"]), "end": str(end), "weeks": max(a["weeks"] for a in items)}))
    acct_drop = chg(p_prim, c_prim)
    if acct_drop is not None and acct_drop < -0.25 and not res["movers"]:
        approvals.append(Approval(
            skill=SKILL, kind="investigate", entity="account",
            action=f"Investigate the account-wide {abs(acct_drop):.0%} drop in {prim_label}",
            why="The drop is spread across campaigns rather than one mover, which points to tracking, seasonality or a site change.",
            impact_label="at risk", confidence="low", risk="low"))

    # markdown
    md = [f"# Performance · {info.get('name') or gads.fmt_cid(cid) or 'offline export'}",
          f"{fmt_range(res['cr'])} vs {fmt_range(res['pr'])} · goal {'ROAS' if goal == 'roas' else 'CPA'}", "",
          f"**{headline}.**", "",
          gads.md_table([[lab, fmt_kpi(P.get(k), kind, cur), fmt_kpi(T.get(k), kind, cur), fmt_chg(chg(P.get(k), T.get(k)))]
                         for k, lab, kind, _ in KPIS], ["Metric", "Previous", "Current", "Change"]), ""]
    if lag_line:
        md += [f"_{lag_line}_", ""]
    md += alert_md(res, cur, args)
    md += ["## Top movers", ""]
    if res["movers"]:
        for i, x in enumerate(res["movers"], 1):
            md += [f"{i}. {x['explain']}", f"   **{x['action'][0]}:** {x['action'][1]}", ""]
    else:
        md += ["No campaign moved enough to separate from normal variation. **Continue** as planned.", ""]
    md += ["## Campaigns", "", gads.md_table(
        [[x["campaign"], money(x["cur"]["cost"], cur), fmt_chg(chg(x["prev"]["cost"], x["cur"]["cost"])), f"{x['cur']['conv']:.1f}",
          fmt_chg(chg(x["prev"]["conv"], x["cur"]["conv"])), money(x["cur"]["cpa"], cur), fmt_x(x["cur"]["roas"]) if x["cur"]["value"] else "n/a",
          fmt_pct(x["cur"]["is"]), fmt_pct(x["cur"]["budget_lost"])] for x in sorted(res["camps"], key=lambda x: -x["cur"]["cost"])],
        ["Campaign", "Spend", "Δ", "Conv", "Δ", "CPA", "ROAS", "Search IS", "Lost (budget)"]), "",
          "## Waiting on you", "",
          (f"{qn} items waiting for approval from {', '.join(qskills)}. See approvals.md." if qn else "No other gads-* approvals waiting today."), "",
          "## Notes", ""] + [f"- {n}" for n in notes]
    opt = info.get("optimization_score")
    md.append(f"- Optimization score {float(opt):.0%}. Google's measure of recommendation adoption, not a performance KPI." if opt not in (None, "") else "- Optimization score not available.")

    camp_rows = [{"campaign": x["campaign"], "channel": x["channel"],
                  **{f"{m}_cur": round(x["cur"][m], 2) for m in METRICS}, **{f"{m}_prev": round(x["prev"][m], 2) for m in METRICS},
                  "cpa_cur": round(x["cur"]["cpa"], 2) if x["cur"]["cpa"] else "", "cpa_prev": round(x["prev"]["cpa"], 2) if x["prev"]["cpa"] else "",
                  "roas_cur": round(x["cur"]["roas"], 3) if x["cur"]["roas"] else "", "roas_prev": round(x["prev"]["roas"], 3) if x["prev"]["roas"] else "",
                  "conv_volume_effect": round(x["dec"]["conv_volume_effect"], 2), "conv_rate_effect": round(x["dec"]["conv_rate_effect"], 2),
                  "mover": x in res["movers"]} for x in res["camps"]]
    html_doc = render(res, info, cur, qn, qskills, cid, approvals)
    summary = {"headline": headline, "goal": goal, "period": [str(res["cr"][0]), str(res["cr"][1])],
               "previous": [str(res["pr"][0]), str(res["pr"][1])],
               "spend": round(T["cost"], 2), "spend_prev": round(P["cost"], 2), "conv": round(T["conv"], 2), "conv_prev": round(P["conv"], 2),
               "cpa": T["cpa"] and round(T["cpa"], 2), "roas": T["roas"] and round(T["roas"], 3),
               "movers": [{"campaign": x["campaign"], "action": x["action"][0], "d_conv": round(x["dec"]["d_conv"], 2),
                           "d_cost": round(x["d_cost"], 2), "explain": x["explain"],
                           "changes": [{"date": str(c["date"]), "what": c["what"], "who": c["who"]} for c in x["changes"]]}
                          for x in res["movers"]],
               "lag_days": args.lag_days, "lag_source": args._lag_source, "lag_days_in_period": res["lag_k"],
               "zero_impressions": [z["campaign"] for z in res.get("zero") or []],
               "anomalies": [{"metric": a["metric"], "end": str(a["end"]), "window_days": a["window_days"], "z": round(a["z"], 2),
                              "value": round(a["value"], 2), "mean": round(a["mean"], 2), "bad": a["bad"]} for a in res.get("anoms") or []],
               "queue_waiting": qn, "optimization_score": info.get("optimization_score")}
    return approvals, summary, "\n".join(md) + "\n", {"report.html": html_doc, "campaigns.csv": gads.to_csv(camp_rows)}


def main():
    ap = gads.common_args(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    ap.set_defaults(days=7)
    ap.add_argument("--compare", choices=["prev", "mtd", "yoy"], default="prev", help="Comparison period (default: previous equal period).")
    ap.add_argument("--goal", choices=["auto", "cpa", "roas"], default="auto")
    ap.add_argument("--top", type=int, default=3, help="Number of movers to write up (default 3).")
    ap.add_argument("--materiality", type=float, default=0.15, help="Min share of gross account movement for a mover (default 0.15).")
    ap.add_argument("--opt-score", type=float, help="Offline: optimization score (0-1 or 0-100) to show.")
    ap.add_argument("--today", help="Treat this date (YYYY-MM-DD) as today. Offline default: the day after the export's last day.")
    ap.add_argument("--lag-days", type=int, help="Conversion lag in days. Live default: estimated from conversion lag buckets; offline default 3.")
    ap.add_argument("--anomaly-weeks", type=int, default=8, help="Same-weekday history in weeks (default 8).")
    ap.add_argument("--anomaly-z", type=float, default=2.0, help="Flag when |z| reaches this (default 2).")
    ap.add_argument("--anomaly-min-change", type=float, default=0.2, help="And the change is at least this share of normal (default 0.2).")
    ap.add_argument("--zero-days", type=int, default=1, help="Zero-impression check: days with no impressions (default 1, the last full day).")
    ap.add_argument("--changes", help="Offline: Google Ads Change history export (CSV) to annotate movers.")
    args = ap.parse_args()

    meta, changes, changes_note = {}, [], ""
    if args.csv:
        cid = gads.clean_cid(args.customer) or "offline"
        prof = gads.load_profile(cid)
        rows, cr, pr, all_rows = load_csv(args.csv, args)
        if not args.today:
            args.today = (cr[1] + dt.timedelta(days=1)).isoformat()
        cur = args.currency
        info = {"name": "", "optimization_score": (args.opt_score / 100 if args.opt_score and args.opt_score > 1 else args.opt_score)}
        if args.changes:
            changes = load_changes_csv(args.changes, prof)
            changes_note = f"Change history: {len(changes)} change(s) read from the export."
        args._lag_source = "set by --lag-days" if args.lag_days is not None else "default for offline exports"
    else:
        if not args.customer:
            gads.die("--customer is required in live mode (or pass --csv).")
        cid = gads.clean_cid(args.customer)
        prof = gads.load_profile(cid)
        info = gads.account_info(cid)
        cur = info["currency"]
        cr, pr = periods(args)
        hist = (cr[1] - dt.timedelta(days=7 * (args.anomaly_weeks + 1) - 1), cr[1])
        all_rows = load_live(cid, merge_ranges([pr, cr, hist]))
        for r in all_rows:
            r["period"] = "cur" if cr[0] <= r["date"] <= cr[1] else ("prev" if pr[0] <= r["date"] <= pr[1] else None)
        rows = [r for r in all_rows if r["period"]]
        meta = load_meta_live(cid)
        changes, changes_note = load_changes_live(cid, pr[0], cr[1], meta, prof, cur)
        changes_note = (changes_note + " " if changes_note else "") + f"Change history: {len(changes)} change(s) from change_event."
        args._lag_source = "set by --lag-days"
        if args.lag_days is None:
            est = load_lag_live(cid, cr[1])
            args._lag_source = "default, too little lag data" if est is None else "estimated from 90 days of conversion lag buckets"
            if est is not None and est > 14:
                args._lag_source += f"; estimate was {est} days, capped at 14"
            args.lag_days = min(est, 14) if est is not None else None
    if args.lag_days is None:
        args.lag_days = 3
    if not any(r.get("period") == "cur" for r in rows):
        gads.die("No data in the current period.")
    res = analyse(rows, cr, pr, prof, args)
    res["changes"], res["changes_note"] = changes, changes_note
    if any(r["date"] for r in all_rows):
        an = weekday_anomalies(all_rows, ref_today(args), args.lag_days, args)
        res["anoms"], res["anom_note"] = an["items"], an["note"]
        res["zero"] = zero_impressions(all_rows, meta, prof, cr[1], args)
    else:
        res["anoms"], res["zero"] = [], []
        res["anom_note"] = "Same-weekday and zero-impression checks need an export segmented by Day; skipped."

    d = gads.out_dir(cid)
    qn, qskills = queue_count(d)
    approvals, summary, md, extra = build(res, info, cur, cid, prof, args, qn, qskills)
    d = gads.write_results(SKILL, cid, approvals, summary, md, cur, extra)
    print(summary["headline"])
    print(f"-> {d}/report.html  ·  {d}/gads-performance-report.md  ·  queue: {d}/approvals.md")
    if args.json:
        print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
