#!/usr/bin/env python3
"""
mine_search_terms.py - read every search term, flag junk, build the negative list.

Live:     $PY mine_search_terms.py -c 123-456-7890 --days 30
Offline:  python3 mine_search_terms.py --csv search_terms.csv [--keywords keywords.csv] --currency USD
Re-run with Claude's judgments on ambiguous terms:
          ... --labels gads-out/<cid>/<date>/llm_labels.csv

Outputs (gads-out/<cid>/<date>/):
  gads-search-terms.json / .md   summary + approval items
  negatives.csv                  proposed negatives, Editor-ready syntax
  harvest.csv                    converting terms to add as keywords
  ambiguous.csv                  terms for Claude to judge (label + reason)
  ngrams.csv                     1-3 gram waste table
"""
from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gads  # noqa: E402
from gads import Approval, money, num, micros  # noqa: E402

SKILL = "gads-search-terms"

# ---------------------------------------------------------------- lexicon
# Themes that are almost never commercial intent for a paid advertiser.
# Each theme -> (patterns, default match type, note). Patterns are whole words.
JUNK_THEMES: dict[str, tuple[list[str], str, str]] = {
    "jobs": (["job", "jobs", "career", "careers", "vacancy", "vacancies",
              "internship", "intern", "resume", "cv", "recruiter", "glassdoor", "indeed", "naukri", "employment",
              "walk in interview", "fresher", "freshers"],
             "BROAD", "job seekers"),
    "free/piracy": (["free", "crack", "cracked", "torrent", "pirate", "keygen", "nulled", "apk", "mod apk"],
                    "BROAD", "freebie or piracy intent"),
    "education": (["course", "courses", "tutorial", "tutorials", "certification", "syllabus", "pdf", "ppt",
                   "thesis", "assignment", "notes", "exam", "udemy", "coursera", "youtube", "wikipedia",
                   "meaning", "definition", "example", "examples"],
                  "PHRASE", "research or learning intent"),
    "diy": (["diy", "homemade", "how to make", "how to build", "template", "sample", "open source", "github"],
            "PHRASE", "do-it-yourself intent"),
    "support/login": (["login", "log in", "sign in", "signin", "password", "customer care", "customer service",
                       "helpline", "phone number", "contact number", "toll free", "cancel", "cancellation",
                       "refund", "unsubscribe", "delete account", "complaint", "complaints"],
                      "PHRASE", "existing-customer navigation"),
    "adult": (["porn", "xxx", "sex", "nude", "escort"], "BROAD", "adult"),
    "bargain": (["cheap", "cheapest", "discount code", "coupon", "promo code", "second hand", "used"],
                "PHRASE", "bargain hunters (review for D2C)"),
}


# Themes that depend on the business (D2C vs B2B). Routed to Claude's review, never auto-negated.
REVIEW_THEMES = {"bargain", "diy"}


def norm(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w\s&+\-.']", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def toks(s: str) -> list[str]:
    return norm(s).split()


def contains_phrase(text_toks: list[str], phrase_toks: list[str]) -> bool:
    n = len(phrase_toks)
    return any(text_toks[i:i + n] == phrase_toks for i in range(len(text_toks) - n + 1)) if n else False


def neg_blocks(neg_text: str, neg_type: str, query: str) -> bool:
    """Would negative (text, type) block this query? Negatives ignore close variants."""
    q, n = toks(query), toks(neg_text)
    t = neg_type.upper()
    if t == "EXACT":
        return q == n
    if t == "PHRASE":
        return contains_phrase(q, n)
    return set(n).issubset(set(q))  # BROAD


def syntax(text: str, mtype: str) -> str:
    return {"EXACT": f"[{text}]", "PHRASE": f'"{text}"'}.get(mtype.upper(), text)


def theme_hit(term: str, extra: list[str]) -> tuple[str, str, str] | None:
    """Return (theme, matched_word, match_type) if the term hits the lexicon."""
    tt = toks(term)
    for theme, (pats, mtype, _) in list(JUNK_THEMES.items()) + [("custom", (extra, "PHRASE", "profile"))]:
        for p in pats:
            pt = toks(p)
            if pt and contains_phrase(tt, pt):
                return theme, p, mtype
    return None


def p_zero(clicks: float, cvr: float) -> float:
    """P(0 conversions in `clicks` clicks | true CVR = account CVR)."""
    if cvr <= 0 or clicks <= 0:
        return 1.0
    return math.exp(clicks * math.log(max(1e-9, 1 - cvr)))


# ---------------------------------------------------------------- load


def load_live(cid: str, start: str, end: str, pmax: bool) -> tuple[list[dict], list[dict], list[dict]]:
    w = gads.gaql_dates(start, end)
    rows = gads.query(cid, f"""
        SELECT campaign.id, campaign.name, campaign.advertising_channel_type,
               ad_group.id, ad_group.name,
               search_term_view.search_term, search_term_view.status,
               segments.keyword.info.text, segments.keyword.info.match_type,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.conversions_value
        FROM search_term_view
        WHERE {w} AND metrics.impressions > 0""")
    terms = [{
        "campaign_id": r.get("campaign.id", ""), "campaign": r.get("campaign.name", ""),
        "ad_group_id": r.get("ad_group.id", ""), "ad_group": r.get("ad_group.name", ""),
        "term": r.get("search_term_view.search_term", ""),
        "status": r.get("search_term_view.status", ""),
        "keyword": r.get("segments.keyword.info.text", ""),
        "kw_match": r.get("segments.keyword.info.match_type", ""),
        "impr": num(r.get("metrics.impressions")), "clicks": num(r.get("metrics.clicks")),
        "cost": micros(r.get("metrics.cost_micros")), "conv": num(r.get("metrics.conversions")),
        "value": num(r.get("metrics.conversions_value")),
    } for r in rows]

    if pmax:
        try:
            prow = gads.query(cid, f"""
                SELECT campaign.id, campaign.name, campaign_search_term_view.search_term,
                       metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions,
                       metrics.conversions_value
                FROM campaign_search_term_view
                WHERE {w} AND campaign.advertising_channel_type = 'PERFORMANCE_MAX'""")
            for r in prow:
                terms.append({
                    "campaign_id": r.get("campaign.id", ""), "campaign": r.get("campaign.name", ""),
                    "ad_group_id": "", "ad_group": "(PMax)",
                    "term": r.get("campaign_search_term_view.search_term", ""), "status": "NONE",
                    "keyword": "", "kw_match": "", "impr": num(r.get("metrics.impressions")),
                    "clicks": num(r.get("metrics.clicks")), "cost": micros(r.get("metrics.cost_micros")),
                    "conv": num(r.get("metrics.conversions")), "value": num(r.get("metrics.conversions_value")),
                })
        except SystemExit:
            print("note: PMax search terms not available for this account/API version; skipped.", file=sys.stderr)

    kws = gads.query(cid, """
        SELECT campaign.id, campaign.name, ad_group.id, ad_group.name,
               ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type,
               ad_group_criterion.negative, ad_group_criterion.status
        FROM keyword_view
        WHERE ad_group_criterion.status != 'REMOVED' AND campaign.status != 'REMOVED'""")
    positives = [{"campaign_id": r.get("campaign.id"), "campaign": r.get("campaign.name"),
                  "ad_group": r.get("ad_group.name"), "text": r.get("ad_group_criterion.keyword.text", ""),
                  "match": r.get("ad_group_criterion.keyword.match_type", "")}
                 for r in kws if not r.get("ad_group_criterion.negative")]

    negs: list[dict] = []
    for r in gads.query(cid, """
        SELECT campaign.id, campaign.name, campaign_criterion.keyword.text,
               campaign_criterion.keyword.match_type
        FROM campaign_criterion
        WHERE campaign_criterion.negative = TRUE AND campaign_criterion.type = 'KEYWORD'"""):
        negs.append({"scope": "campaign", "campaign_id": r.get("campaign.id"), "campaign": r.get("campaign.name"),
                     "text": r.get("campaign_criterion.keyword.text", ""),
                     "match": r.get("campaign_criterion.keyword.match_type", "")})
    for r in gads.query(cid, """
        SELECT campaign.id, campaign.name, ad_group.name, ad_group_criterion.keyword.text,
               ad_group_criterion.keyword.match_type
        FROM ad_group_criterion
        WHERE ad_group_criterion.negative = TRUE AND ad_group_criterion.type = 'KEYWORD'
          AND ad_group_criterion.status != 'REMOVED'"""):
        negs.append({"scope": "ad_group", "campaign_id": r.get("campaign.id"), "campaign": r.get("campaign.name"),
                     "text": r.get("ad_group_criterion.keyword.text", ""),
                     "match": r.get("ad_group_criterion.keyword.match_type", "")})
    # shared negative lists and which campaigns use them
    attached = defaultdict(list)
    for r in gads.query(cid, """
        SELECT campaign.id, campaign.name, shared_set.id, shared_set.name
        FROM campaign_shared_set
        WHERE shared_set.type = 'NEGATIVE_KEYWORDS' AND campaign_shared_set.status = 'ENABLED'"""):
        attached[str(r.get("shared_set.id"))].append((r.get("campaign.id"), r.get("campaign.name")))
    for r in gads.query(cid, """
        SELECT shared_set.id, shared_set.name, shared_criterion.keyword.text,
               shared_criterion.keyword.match_type
        FROM shared_criterion
        WHERE shared_set.type = 'NEGATIVE_KEYWORDS' AND shared_set.status = 'ENABLED'"""):
        for cid_, cname in attached.get(str(r.get("shared_set.id")), []):
            negs.append({"scope": f"list:{r.get('shared_set.name')}", "campaign_id": cid_, "campaign": cname,
                         "text": r.get("shared_criterion.keyword.text", ""),
                         "match": r.get("shared_criterion.keyword.match_type", "")})
    return terms, positives, negs


def load_csv(paths: list[str], kw_csv: str | None) -> tuple[list[dict], list[dict], list[dict]]:
    terms = []
    for p in paths:
        for r in gads.read_ui_csv(p):
            term = gads.pick(r, "search_term", "search_terms", "query")
            if not term:
                continue
            terms.append({
                "campaign_id": gads.pick(r, "campaign_id"), "campaign": gads.pick(r, "campaign"),
                "ad_group_id": gads.pick(r, "ad_group_id"), "ad_group": gads.pick(r, "ad_group"),
                "term": term, "status": gads.pick(r, "added/excluded", "added_excluded", default="NONE"),
                "keyword": gads.pick(r, "keyword"), "kw_match": gads.pick(r, "match_type", "keyword_match_type"),
                "impr": num(gads.pick(r, "impr", "impressions")), "clicks": num(gads.pick(r, "clicks")),
                "cost": num(gads.pick(r, "cost")), "conv": num(gads.pick(r, "conversions", "conv")),
                "value": num(gads.pick(r, "conv_value", "conversion_value", "conv_value_/_cost", default=0)),
            })
    positives, negs = [], []
    if kw_csv:
        for r in gads.read_ui_csv(kw_csv):
            text = gads.pick(r, "keyword", "search_keyword")
            mt = gads.pick(r, "match_type", "keyword_match_type", "criterion_type")
            if not text:
                continue
            text = text.strip('[]"+')
            row = {"campaign_id": gads.pick(r, "campaign_id"), "campaign": gads.pick(r, "campaign"),
                   "ad_group": gads.pick(r, "ad_group"), "text": text, "match": mt.upper().replace("NEGATIVE ", "")}
            (negs if "negative" in str(mt).lower() else positives).append(row)
    return terms, positives, negs


# ---------------------------------------------------------------- analysis


def aggregate(terms: list[dict]) -> list[dict]:
    """Collapse keyword-level rows to one row per campaign x ad group x term."""
    agg: dict[tuple, dict] = {}
    for t in terms:
        k = (t["campaign"], t["ad_group"], norm(t["term"]))
        a = agg.setdefault(k, {**t, "impr": 0, "clicks": 0, "cost": 0, "conv": 0, "value": 0, "keywords": set()})
        for m in ("impr", "clicks", "cost", "conv", "value"):
            a[m] += t[m]
        if t.get("keyword"):
            a["keywords"].add(f"{t['keyword']} ({str(t.get('kw_match', '')).lower()})")
    for a in agg.values():
        a["keywords"] = "; ".join(sorted(a["keywords"]))
    return list(agg.values())


def ngram_table(rows: list[dict], max_n: int = 3) -> list[dict]:
    g: dict[str, dict] = {}
    for r in rows:
        tt = toks(r["term"])
        seen = set()
        for n in range(1, max_n + 1):
            for i in range(len(tt) - n + 1):
                gram = " ".join(tt[i:i + n])
                if gram in seen:
                    continue
                seen.add(gram)
                x = g.setdefault(gram, {"ngram": gram, "n": n, "terms": 0, "clicks": 0, "cost": 0.0, "conv": 0.0, "value": 0.0})
                x["terms"] += 1
                x["clicks"] += r["clicks"]
                x["cost"] += r["cost"]
                x["conv"] += r["conv"]
                x["value"] += r["value"]
    return sorted(g.values(), key=lambda x: -x["cost"])


STOP = {"a", "an", "the", "for", "of", "in", "on", "to", "and", "or", "with", "near", "me", "best", "top",
        "is", "are", "what", "vs", "by", "my", "at", "from", "how", "online", "new", "india", "usa"}


def protected(text: str, protect_toks: list[list[str]]) -> bool:
    tt = toks(text)
    return any(contains_phrase(tt, p) or contains_phrase(p, tt) for p in protect_toks if p)


def analyse(rows, positives, negs, prof, args, currency):
    days = args.window_days
    scale = 30.0 / days
    tot_cost = sum(r["cost"] for r in rows)
    tot_clicks = sum(r["clicks"] for r in rows)
    tot_conv = sum(r["conv"] for r in rows)
    cvr = (tot_conv / tot_clicks) if tot_clicks else 0
    acct_cpa = (tot_cost / tot_conv) if tot_conv else None
    target_cpa = args.target_cpa or prof.get("target_cpa") or acct_cpa or (tot_cost / max(1, len(rows)))

    brand = [toks(b) for b in prof.get("brand_terms", [])]
    never = [toks(b) for b in prof.get("never_negate", [])]
    comp = [toks(b) for b in prof.get("competitor_terms", [])]
    converting = [r for r in rows if r["conv"] > 0]
    protect = brand + never + [toks(p["text"]) for p in positives] + [toks(r["term"]) for r in converting]

    labels = {}
    if args.labels:
        with open(args.labels) as f:
            for r in csv.DictReader(f):
                labels[norm(r.get("term", ""))] = r

    def already_blocked(r):
        for n in negs:
            if n.get("campaign") and r["campaign"] and n["campaign"] != r["campaign"]:
                continue
            if neg_blocks(n["text"], n["match"], r["term"]):
                return True
        return False

    flagged, ambiguous, harvest, competitor = [], [], [], []
    for r in rows:
        if str(r.get("status", "")).upper() in ("EXCLUDED", "ADDED_EXCLUDED") or already_blocked(r):
            continue
        tt = toks(r["term"])
        is_brand = any(contains_phrase(tt, b) for b in brand if b)
        is_comp = any(contains_phrase(tt, c) for c in comp if c)
        pz = p_zero(r["clicks"], cvr)
        lab = labels.get(norm(r["term"]))
        reason, conf, mtype = None, "medium", "EXACT"

        if r["conv"] > 0:
            cpa = r["cost"] / r["conv"]
            is_kw = any(norm(p["text"]) == norm(r["term"]) for p in positives)
            if not is_kw and r["conv"] >= args.harvest_min_conv and cpa <= target_cpa * 1.1 and not is_brand:
                harvest.append({**r, "cpa": cpa})
            continue
        if is_brand:
            continue  # brand traffic is handled by the bids skill, never negated here
        if is_comp:
            competitor.append(r)
            continue
        if lab:
            if lab.get("label", "").lower() in ("negate", "junk", "irrelevant"):
                reason, conf = f"judged irrelevant: {lab.get('reason', '')}".strip(), "high"
                mtype = lab.get("match_type", "EXACT").upper() or "EXACT"
            else:
                continue
        else:
            hit = theme_hit(r["term"], prof.get("irrelevant_themes", []))
            if hit and r["cost"] > 0 and hit[0] in REVIEW_THEMES:
                ambiguous.append({**r, "hint": f"{hit[0]} ('{hit[1]}')"})
                continue
            elif hit and r["cost"] > 0:
                theme, word, _ = hit
                reason, conf = f"{theme} intent ('{word}')", "high"
            elif r["cost"] >= target_cpa * args.cpa_multiple and pz < 0.10:
                reason, conf = f"spent {money(r['cost'], currency)} ({r['cost'] / target_cpa:.1f}x target CPA) with 0 conversions", "high"
            elif r["cost"] >= target_cpa * 0.5 and pz < 0.20:
                reason, conf = f"{int(r['clicks'])} clicks, 0 conversions; at account CVR {cvr:.1%} that is {1 - pz:.0%} unlikely to be chance", "medium"
            elif r["cost"] >= target_cpa * 0.25 or r["clicks"] >= 5:
                ambiguous.append(r)
                continue
            else:
                continue
        if protected(r["term"], never + brand):
            continue
        flagged.append({**r, "reason": reason, "confidence": conf, "neg_text": norm(r["term"]), "neg_match": mtype,
                        "level": "campaign", "p_zero": round(pz, 3)})

    # n-gram theme negatives: one phrase negative instead of many exacts.
    # Greedy set cover by spend, so two grams that hit the same terms
    # ("tool" and "variant" in "payroll tool variant 12") yield one negative.
    grams = ngram_table(rows)
    row_toks = [toks(r["term"]) for r in rows]
    covered_idx: set[int] = set()
    theme_negs = []
    for g in grams:
        if g["conv"] > 0 or g["terms"] < args.ngram_min_terms or g["cost"] < target_cpa * args.cpa_multiple:
            continue
        gt = g["ngram"].split()
        if all(w in STOP for w in gt) or len(g["ngram"]) < 3:
            continue
        if protected(g["ngram"], protect) or p_zero(g["clicks"], cvr) > 0.10:
            continue
        hits = [i for i, tt in enumerate(row_toks) if contains_phrase(tt, gt)]
        new = [i for i in hits if i not in covered_idx]
        new_cost = sum(rows[i]["cost"] for i in new)
        if len(new) < args.ngram_min_terms or new_cost < target_cpa * args.cpa_multiple:
            continue
        covered_idx.update(hits)
        theme_negs.append({"neg_text": g["ngram"], "neg_match": "PHRASE", "level": "shared", "cost": g["cost"],
                           "clicks": g["clicks"], "terms": g["terms"],
                           "reason": f"appears in {g['terms']} terms, {money(g['cost'], currency)} spend, 0 conversions"})
        if len(theme_negs) >= args.max_ngrams:
            break

    # lexicon themes -> shared list broad/phrase negatives for the matched words
    lex = defaultdict(lambda: {"cost": 0.0, "terms": 0})
    for f_ in flagged:
        m = re.match(r"^(\S+?) intent \('(.+)'\)$", f_["reason"])
        if m and m.group(1) in JUNK_THEMES:
            k = (m.group(2), JUNK_THEMES[m.group(1)][1])
            lex[k]["cost"] += f_["cost"]
            lex[k]["terms"] += 1
    for (word, mtype), v in lex.items():
        if v["terms"] >= 2 and not protected(word, protect) and not any(t["neg_text"] == word for t in theme_negs):
            theme_negs.append({"neg_text": word, "neg_match": mtype, "level": "shared", "cost": v["cost"],
                               "clicks": 0, "terms": v["terms"], "reason": f"junk theme word, {v['terms']} terms"})

    # drop exact negatives already covered by a theme negative
    covered = lambda f_: any(neg_blocks(t["neg_text"], t["neg_match"], f_["term"]) for t in theme_negs)  # noqa: E731
    exact_negs = [f_ for f_ in flagged if not covered(f_)]

    # conflicts: proposed negative would block a live keyword
    conflicts = []
    for n in exact_negs + theme_negs:
        for p in positives:
            if neg_blocks(n["neg_text"], n["neg_match"], p["text"]):
                conflicts.append((n["neg_text"], p["text"]))
                n["conflict"] = p["text"]
                break
    exact_negs = [n for n in exact_negs if not n.get("conflict")]
    theme_negs = [n for n in theme_negs if not n.get("conflict")]

    def blocked(r):
        if any(neg_blocks(t["neg_text"], t["neg_match"], r["term"]) for t in theme_negs):
            return True
        return any(n["campaign"] == r["campaign"] and neg_blocks(n["neg_text"], n["neg_match"], r["term"]) for n in exact_negs)
    blocked_rows = [r for r in rows if r["conv"] == 0 and blocked(r)]
    waste = sum(r["cost"] for r in blocked_rows)
    flagged_n = len(blocked_rows)
    return {
        "rows": rows, "flagged": flagged, "exact_negs": exact_negs, "theme_negs": theme_negs,
        "ambiguous": sorted(ambiguous, key=lambda r: -r["cost"]), "harvest": sorted(harvest, key=lambda r: -r["conv"]),
        "competitor": competitor, "grams": grams, "conflicts": conflicts,
        "stats": {"terms": len(rows), "cost": tot_cost, "clicks": tot_clicks, "conv": tot_conv, "cvr": cvr,
                  "acct_cpa": acct_cpa, "target_cpa": target_cpa, "waste": waste, "waste_monthly": waste * scale,
                  "flagged": flagged_n, "days": days},
    }


# ---------------------------------------------------------------- output


def build_outputs(res, cid, currency, args):
    s = res["stats"]
    cur = currency
    neg_rows = []
    for n in res["theme_negs"]:
        neg_rows.append({"level": "shared list", "campaign": "", "campaign_id": "", "keyword": syntax(n["neg_text"], n["neg_match"]),
                         "text": n["neg_text"], "match_type": n["neg_match"], "reason": n["reason"],
                         "cost": round(n["cost"], 2), "clicks": int(n["clicks"]), "conv": 0, "terms_covered": n["terms"]})
    for n in sorted(res["exact_negs"], key=lambda x: -x["cost"]):
        neg_rows.append({"level": "campaign", "campaign": n["campaign"], "campaign_id": n.get("campaign_id", ""),
                         "keyword": syntax(n["neg_text"], n["neg_match"]), "text": n["neg_text"], "match_type": n["neg_match"],
                         "reason": n["reason"], "cost": round(n["cost"], 2), "clicks": int(n["clicks"]), "conv": 0, "terms_covered": 1})
    cols = ["level", "campaign", "campaign_id", "keyword", "text", "match_type", "reason", "cost", "clicks", "conv", "terms_covered"]
    harvest_rows = [{"campaign": h["campaign"], "ad_group": h["ad_group"], "keyword": f"[{norm(h['term'])}]",
                     "conv": round(h["conv"], 2), "cost": round(h["cost"], 2), "cpa": round(h["cpa"], 2),
                     "triggered_by": h.get("keywords", "")} for h in res["harvest"]]
    amb_rows = [{"term": a["term"], "campaign": a["campaign"], "ad_group": a["ad_group"], "clicks": int(a["clicks"]),
                 "cost": round(a["cost"], 2), "triggered_by": a.get("keywords", ""), "hint": a.get("hint", ""), "label": "", "match_type": "", "reason": ""}
                for a in res["ambiguous"][: args.max_ambiguous]]
    gram_rows = [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in g.items()} for g in res["grams"][:300]]

    approvals = []
    shared_cost = sum(n["cost"] for n in res["theme_negs"])
    if res["theme_negs"]:
        approvals.append(Approval(
            skill=SKILL, kind="negative_list",
            action=f"Add {len(res['theme_negs'])} theme negatives to an account-level shared negative list",
            why=f"Themes like {', '.join(repr(n['neg_text']) for n in res['theme_negs'][:4])} spent {money(shared_cost, cur)} in {s['days']} days with zero conversions.",
            entity="shared negative list", impact_monthly=shared_cost * 30 / s["days"], impact_label="saves",
            confidence="medium" if any(n["neg_match"] == "PHRASE" and " " not in n["neg_text"] and n["reason"].startswith("appears") for n in res["theme_negs"]) else "high",
            risk="medium" if any(n["neg_match"] == "BROAD" for n in res["theme_negs"]) else "low",
            evidence={"negatives": len(res["theme_negs"]), "cost": round(shared_cost, 2)},
            change={"file": "negatives.csv", "level": "shared list"}, files=["negatives.csv"]))
    by_camp = defaultdict(list)
    for n in res["exact_negs"]:
        by_camp[n["campaign"]].append(n)
    for camp, items in sorted(by_camp.items(), key=lambda kv: -sum(i["cost"] for i in kv[1])):
        c = sum(i["cost"] for i in items)
        if c <= 0:
            continue
        conf = "high" if all(i["confidence"] == "high" for i in items) else "medium"
        approvals.append(Approval(
            skill=SKILL, kind="negative_keywords", entity=camp,
            action=f"Add {len(items)} exact negatives to '{camp}'",
            why=f"{len(items)} search terms spent {money(c, cur)} with zero conversions. Top: {items[0]['term']!r} ({money(items[0]['cost'], cur)}).",
            impact_monthly=c * 30 / s["days"], impact_label="saves", confidence=conf, risk="low",
            evidence={"terms": len(items), "cost": round(c, 2)}, change={"file": "negatives.csv", "campaign": camp},
            files=["negatives.csv"]))
    if res["harvest"]:
        hv = res["harvest"]
        approvals.append(Approval(
            skill=SKILL, kind="keyword_harvest", entity="keywords",
            action=f"Add {len(hv)} converting search terms as exact-match keywords",
            why=f"They drove {sum(h['conv'] for h in hv):.0f} conversions at or under target CPA but are not keywords yet, so bids and ads cannot be tuned for them.",
            impact_label="gains", confidence="medium", risk="low",
            evidence={"terms": len(hv), "conv": round(sum(h['conv'] for h in hv), 1)},
            change={"file": "harvest.csv"}, files=["harvest.csv"]))
    if res["competitor"]:
        cc = sum(r["cost"] for r in res["competitor"])
        approvals.append(Approval(
            skill=SKILL, kind="competitor_review", entity="competitor terms",
            action=f"Decide on {len(res['competitor'])} non-converting competitor terms ({money(cc, cur)})",
            why="Competitor traffic is a strategy call, not junk. Either move it to a dedicated competitor campaign with its own CPA target, or negate it.",
            impact_monthly=cc * 30 / s["days"], impact_label="saves", confidence="low", risk="medium",
            evidence={"terms": len(res["competitor"]), "cost": round(cc, 2)}))

    headline = (f"Read {s['terms']:,} search terms, flagged {s['flagged']:,} as junk "
                f"({money(s['waste'], cur)} in {s['days']}d, ~{money(s['waste_monthly'], cur)}/mo), "
                f"built {len(neg_rows)} negatives")
    md = [f"# Search terms · {gads.fmt_cid(cid) or 'offline export'} · last {s['days']} days", "",
          f"**{headline}.**", "",
          f"- Spend in search terms report: {money(s['cost'], cur)} · {int(s['clicks']):,} clicks · {s['conv']:.1f} conv · CVR {s['cvr']:.2%}",
          f"- CPA used for thresholds: {money(s['target_cpa'], cur)} ({'profile/flag target' if (args.target_cpa) else 'account actual or profile'})",
          f"- Converting terms to harvest: {len(res['harvest'])} · ambiguous for review: {len(res['ambiguous'])} · competitor terms: {len(res['competitor'])}",
          f"- Negatives dropped because they would block a live keyword: {len(res['conflicts'])}", "",
          "## Theme negatives (shared list)", "",
          gads.md_table([[syntax(n['neg_text'], n['neg_match']), money(n['cost'], cur), n['terms'], n['reason']] for n in res['theme_negs'][:30]],
                        ["Negative", "Cost", "Terms", "Why"]) if res['theme_negs'] else "_none_", "",
          "## Top exact negatives", "",
          gads.md_table([[n['term'], n['campaign'], money(n['cost'], cur), int(n['clicks']), n['reason']] for n in sorted(res['exact_negs'], key=lambda x: -x['cost'])[:25]],
                        ["Search term", "Campaign", "Cost", "Clicks", "Why"]) if res['exact_negs'] else "_none_", ""]
    if res["harvest"]:
        md += ["## Harvest as keywords", "", gads.md_table([[h['term'], h['campaign'], f"{h['conv']:.1f}", money(h['cpa'], cur)] for h in res['harvest'][:15]], ["Term", "Campaign", "Conv", "CPA"]), ""]
    if res["conflicts"]:
        md += ["## Held back (would block a live keyword)", ""] + [f"- `{a}` would block keyword `{b}`" for a, b in res["conflicts"][:20]] + [""]
    md += ["## Caveats", "",
           "- Google hides low-volume search terms; the report covers part of spend, not all of it.",
           "- Savings assume blocked spend is not simply re-spent on similar junk. Re-check in 14 days.",
           "- Negatives do not match close variants. Plurals and synonyms need their own entry (misspellings are covered)."]

    extra = {"negatives.csv": gads.to_csv(neg_rows, cols), "harvest.csv": gads.to_csv(harvest_rows),
             "ambiguous.csv": gads.to_csv(amb_rows), "ngrams.csv": gads.to_csv(gram_rows)}
    summary = {"headline": headline, **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in s.items()},
               "negatives": len(neg_rows), "harvest": len(res["harvest"]), "ambiguous": len(amb_rows)}
    return approvals, summary, "\n".join(md) + "\n", extra


def main():
    ap = gads.common_args(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    ap.add_argument("--keywords", help="Offline: keywords export (positives + negatives) for conflict checks.")
    ap.add_argument("--labels", help="CSV with term,label,match_type,reason from Claude's review of ambiguous.csv.")
    ap.add_argument("--target-cpa", type=float, help="Override CPA used for thresholds.")
    ap.add_argument("--cpa-multiple", type=float, default=1.0, help="Zero-conv spend threshold as a multiple of CPA (default 1.0).")
    ap.add_argument("--harvest-min-conv", type=float, default=2.0)
    ap.add_argument("--ngram-min-terms", type=int, default=3)
    ap.add_argument("--max-ngrams", type=int, default=40)
    ap.add_argument("--max-ambiguous", type=int, default=150)
    ap.add_argument("--pmax", action="store_true", help="Also pull Performance Max search terms (campaign level).")
    args = ap.parse_args()

    start, end = gads.resolve_window(args)
    import datetime as dt
    args.window_days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1

    if args.csv:
        cid = gads.clean_cid(args.customer) or "offline"
        terms, positives, negs = load_csv(args.csv, args.keywords)
        currency = args.currency
    else:
        if not args.customer:
            gads.die("--customer is required in live mode (or pass --csv).")
        cid = gads.clean_cid(args.customer)
        currency = gads.account_info(cid)["currency"]
        terms, positives, negs = load_live(cid, start, end, args.pmax)
    if not terms:
        gads.die("No search terms in the window. Check the date range or the export.")
    prof = gads.load_profile(cid)
    rows = aggregate(terms)
    res = analyse(rows, positives, negs, prof, args, currency)
    approvals, summary, md, extra = build_outputs(res, cid, currency, args)
    d = gads.write_results(SKILL, cid, approvals, summary, md, currency, extra)
    print(summary["headline"])
    print(f"-> {d}/gads-search-terms.md  ·  queue: {d}/approvals.md")
    if args.json:
        import json
        print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
