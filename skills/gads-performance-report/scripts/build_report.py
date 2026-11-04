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


def periods(args) -> tuple[tuple[dt.date, dt.date], tuple[dt.date, dt.date]]:
    """Return (current, previous) inclusive date ranges."""
    yday = gads.today() - dt.timedelta(days=1)
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


def row_from(campaign, channel, impr, clicks, cost, conv, value, is_, bl, rl, date=None, status=""):
    return {"date": date, "campaign": campaign, "channel": channel, "status": status, "impr": impr, "clicks": clicks,
            "cost": cost, "conv": conv, "value": value, "is": is_, "budget_lost": bl, "rank_lost": rl}


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
                date=dt.date.fromisoformat(r["segments.date"]), status=r.get("campaign.status", "")))
    return out


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
            date=d, status=gads.pick(r, "campaign_status", "status")))
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
        return cur + prev, cr, pr
    rows = _csv_rows(paths[0])
    dates = sorted({r["date"] for r in rows if r["date"]})
    if not dates:
        gads.die("The export has no Day column. Export campaigns segmented by Day, or pass two exports (current, previous).")
    if not args.end:
        args.end = dates[-1].isoformat()
    cr, pr = periods(args)
    for r in rows:
        r["period"] = "cur" if cr[0] <= r["date"] <= cr[1] else ("prev" if pr[0] <= r["date"] <= pr[1] else None)
    if not any(r["period"] == "prev" for r in rows):
        gads.die(f"The export does not cover the previous period ({fmt_range(pr)}). Export a longer date range.")
    return [r for r in rows if r["period"]], cr, pr


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

    # conversion lag: last 3 days of current period vs the rest
    lag = None
    if cur_rows and cr:
        tail_start = cr[1] - dt.timedelta(days=2)
        tail = totals([r for r in cur_rows if r["date"] and r["date"] >= tail_start])
        head = totals([r for r in cur_rows if r["date"] and r["date"] < tail_start])
        if tail["cvr"] is not None and head["cvr"] and tail["clicks"] >= 30:
            drop = 1 - tail["cvr"] / head["cvr"]
            if drop > 0.25:
                lag = {"tail_cvr": tail["cvr"], "head_cvr": head["cvr"], "drop": drop}
    days_since_end = (gads.today() - cr[1]).days if cr else 99
    lag_note = days_since_end <= 3 or lag is not None

    # daily series for sparklines
    daily = defaultdict(lambda: {"cost": 0.0, "conv": 0.0, "value": 0.0})
    for r in rows:
        if r["date"]:
            for m in ("cost", "conv", "value"):
                daily[r["date"]][m] += r[m]
    series = [(d, daily[d]) for d in sorted(daily)]
    return {"T": T, "P": P, "goal": goal, "prim": prim, "camps": camps, "movers": movers, "lag": lag,
            "lag_note": lag_note, "series": series, "cr": cr, "pr": pr}


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
.queue{{padding:12px 16px;border:1px dashed var(--muted);border-radius:8px}}ul.notes{{padding-left:18px;color:var(--muted);font-size:13px}}
footer{{margin-top:36px;font-size:12px;color:var(--muted)}}
</style></head><body><main>
<h1>Google Ads performance</h1>
<p class="sub">{e(info.get("name") or gads.fmt_cid(cid) or "Offline export")} · {e(fmt_range(res["cr"]))} vs {e(fmt_range(res["pr"]))} · goal: {"ROAS" if goal == "roas" else "CPA"}</p>
<p class="headline">{e(res["headline"])}</p>
<section class="grid">{"".join(tiles)}</section>
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
    for x in res["movers"]:
        x["action"] = action_for(x, goal, prof)
        x["explain"] = explain(x, cur, goal)
    notes = []
    if res["lag_note"]:
        if res["lag"]:
            notes.append(f"Conversion lag: the last 3 days convert at {fmt_pct(res['lag']['tail_cvr'])} vs {fmt_pct(res['lag']['head_cvr'])} earlier in the period. "
                         "Late conversions will still land, so the current period understates results.")
        else:
            notes.append("The period ends within 3 days of today. Conversions reported by click date can still rise as delayed conversions arrive.")
    if args.compare == "yoy":
        notes.append("Year over year compares weekday-aligned dates 364 days apart.")
    if args.compare == "mtd":
        notes.append("Month to date is compared with the same number of days last month.")
    notes.append("Movers are ranked by contribution to the account change, not by % change; small campaigns with big % swings are excluded as noise.")
    res["notes"] = notes

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
                         for k, lab, kind, _ in KPIS], ["Metric", "Previous", "Current", "Change"]), "",
          "## Top movers", ""]
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
                           "d_cost": round(x["d_cost"], 2)} for x in res["movers"]],
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
    args = ap.parse_args()

    if args.csv:
        cid = gads.clean_cid(args.customer) or "offline"
        rows, cr, pr = load_csv(args.csv, args)
        cur = args.currency
        info = {"name": "", "optimization_score": (args.opt_score / 100 if args.opt_score and args.opt_score > 1 else args.opt_score)}
    else:
        if not args.customer:
            gads.die("--customer is required in live mode (or pass --csv).")
        cid = gads.clean_cid(args.customer)
        info = gads.account_info(cid)
        cur = info["currency"]
        cr, pr = periods(args)
        rows = load_live(cid, [pr, cr])
        for r in rows:
            r["period"] = "cur" if cr[0] <= r["date"] <= cr[1] else "prev"
    if not any(r.get("period") == "cur" for r in rows):
        gads.die("No data in the current period.")
    prof = gads.load_profile(cid)
    res = analyse(rows, cr, pr, prof, args)
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
