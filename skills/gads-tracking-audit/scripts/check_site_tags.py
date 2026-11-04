#!/usr/bin/env python3
"""
check_site_tags.py - what Google tags does the site actually load?

Static mode (default, stdlib only): fetch each URL, find gtag.js loads,
gtag('config') calls, GTM containers, GA4 IDs, send_to conversion labels,
consent mode defaults, and duplicate loads of the same ID. GTM containers
are fetched too (gtm.js is public) so AW conversion IDs/labels configured
inside GTM are visible.

Render mode (--render, needs `pip install playwright && playwright install chromium`):
load the page headless and record the actual requests to Google Ads and
GA4 endpoints, so you see what fires, how often, and with which label.

  python3 check_site_tags.py --url https://example.com/ --url https://example.com/thank-you
  python3 check_site_tags.py -c 1234567890                     # URLs from profile site_urls
  python3 check_site_tags.py --url ./page.html --expect gads-out/<cid>/<date>/expected_tags.json
  python3 check_site_tags.py --url https://example.com/thank-you --render

Honest limit: a purchase or lead tag fires on the confirmation page. This
script can load that URL, but it cannot place an order or submit a form.
A thank-you page that only renders after a real submission needs Tag
Assistant (see SKILL.md).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gads  # noqa: E402
from gads import Approval  # noqa: E402

SKILL = "gads-tracking-tags"
UA = "Mozilla/5.0 (compatible; gads-tracking-audit/1.0)"

RX = {
    "gtag_js": re.compile(r"googletagmanager\.com/gtag/js\?id=((?:AW|G|GT|DC)-[A-Z0-9]+)", re.I),
    "gtm_js": re.compile(r"googletagmanager\.com/gtm\.js\?id=(GTM-[A-Z0-9]+)", re.I),
    "gtm_inline": re.compile(r"['\"](GTM-[A-Z0-9]{4,})['\"]"),
    "config": re.compile(r"gtag\(\s*['\"]config['\"]\s*,\s*['\"]((?:AW|G|GT|DC)-[A-Z0-9]+)['\"]", re.I),
    "send_to": re.compile(r"send_to['\"]?\s*:\s*['\"](AW-\d+/[\w-]+)['\"]"),
    "conv_event": re.compile(r"gtag\(\s*['\"]event['\"]\s*,\s*['\"]conversion['\"]"),
    "consent_default": re.compile(r"gtag\(\s*['\"]consent['\"]\s*,\s*['\"]default['\"]"),
    "consent_update": re.compile(r"gtag\(\s*['\"]consent['\"]\s*,\s*['\"]update['\"]"),
    "ec_user_data": re.compile(r"gtag\(\s*['\"]set['\"]\s*,\s*['\"]user_data['\"]|allow_enhanced_conversions", re.I),
    "ga4_id": re.compile(r"['\"](G-[A-Z0-9]{6,})['\"]"),
    "aw_id": re.compile(r"['\"](AW-\d{6,})['\"]"),
}
# inside a GTM container (gtm.js)
GTM_RX = {
    "aw_conv": re.compile(r'"vtp_conversionId"\s*:\s*"(\d+)"'),
    "aw_label": re.compile(r'"vtp_conversionLabel"\s*:\s*"([\w-]+)"'),
    "ga4": re.compile(r'"(G-[A-Z0-9]{6,})"'),
    "aw": re.compile(r'"(AW-\d{6,})"'),
    "consent": re.compile(r"consent", re.I),
    "awct": re.compile(r'"function"\s*:\s*"__awct"'),       # Google Ads conversion tag
    "paused_awct": re.compile(r'"vtp_originalTagType"\s*:\s*"awct"'),
}
ENDPOINTS = {
    "ads_conversion": re.compile(r"googleadservices\.com/pagead/conversion/|google\.com/pagead/1p-conversion/|googleads\.g\.doubleclick\.net/pagead/viewthroughconversion/"),
    "ccm": re.compile(r"google\.com/ccm/collect|google\.[a-z.]+/ccm/"),
    "gtm_load": re.compile(r"googletagmanager\.com/(gtm|gtag)/js"),
    "ga4_collect": re.compile(r"google-analytics\.com/g/collect|analytics\.google\.com/g/collect"),
}


def fetch(url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (final_url, text). Accepts http(s), file:// and local paths."""
    if url.startswith("file://"):
        p = url[7:]
        return url, Path(p).read_text(errors="replace")
    if not re.match(r"^https?://", url):
        p = Path(url).expanduser()
        if p.exists():
            return p.resolve().as_uri(), p.read_text(errors="replace")
        url = "https://" + url
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return r.geturl(), r.read().decode(r.headers.get_content_charset() or "utf-8", errors="replace")


def scan_html(html: str) -> dict:
    f = {k: rx.findall(html) for k, rx in RX.items()}
    gtm = sorted(set(f["gtm_js"]) | set(f["gtm_inline"]))
    return {
        "gtag_loads": Counter(i.upper() for i in f["gtag_js"]),
        "configs": Counter(i.upper() for i in f["config"]),
        "gtm_loads": Counter(i.upper() for i in f["gtm_js"]),
        "gtm_ids": gtm,
        "send_to": Counter(f["send_to"]),
        "conversion_events": len(f["conv_event"]),
        "consent_default": len(f["consent_default"]),
        "consent_update": len(f["consent_update"]),
        "ec_user_data": len(f["ec_user_data"]) > 0,
        "aw_ids": sorted({i.upper() for i in f["aw_id"]} | {i.upper() for i in f["config"] if i.upper().startswith("AW-")} | {i.upper() for i in f["gtag_js"] if i.upper().startswith("AW-")}),
        "ga4_ids": sorted({i.upper() for i in f["ga4_id"]}),
    }


def scan_gtm(container: str) -> dict:
    try:
        _, js = fetch(f"https://www.googletagmanager.com/gtm.js?id={container}")
    except Exception as e:  # network, 404
        return {"error": str(e)}
    ids = GTM_RX["aw_conv"].findall(js)
    labels = GTM_RX["aw_label"].findall(js)
    return {
        "aw_ids": sorted({f"AW-{i}" for i in ids} | set(GTM_RX["aw"].findall(js))),
        "labels": sorted(set(labels)),
        "ga4_ids": sorted(set(GTM_RX["ga4"].findall(js))),
        "mentions_consent": bool(GTM_RX["consent"].search(js)),
        "ads_conversion_tags": len(GTM_RX["awct"].findall(js)),
        "paused_ads_conversion_tags": len(GTM_RX["paused_awct"].findall(js)),
    }


def render(url: str, wait_ms: int) -> dict:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return {"error": "playwright not installed (pip install playwright && playwright install chromium)"}
    hits: list[dict] = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(user_agent=UA)
        pg.on("request", lambda r: hits.append({"url": r.url}))
        try:
            pg.goto(url, wait_until="networkidle", timeout=45000)
            pg.wait_for_timeout(wait_ms)
        except Exception as e:
            b.close()
            return {"error": str(e)}
        b.close()
    out = {k: [] for k in ENDPOINTS}
    for h in hits:
        for k, rx in ENDPOINTS.items():
            if rx.search(h["url"]):
                out[k].append(h["url"])
    labels = Counter()
    for u in out["ads_conversion"]:
        m = re.search(r"/(?:conversion|1p-conversion|viewthroughconversion)/(\d+)/?\?.*?label=([\w-]+)", u)
        if m:
            labels[f"AW-{m.group(1)}/{m.group(2)}"] += 1
    ga4_events = Counter(m.group(1) for u in out["ga4_collect"] for m in [re.search(r"[?&]en=([\w-]+)", u)] if m)
    return {"counts": {k: len(v) for k, v in out.items()}, "conversion_labels": dict(labels), "ga4_events": dict(ga4_events)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--customer", "-c", help="Customer ID (reads site_urls from profile; names the output folder).")
    ap.add_argument("--url", action="append", default=[], help="Page URL or local HTML path. Repeatable.")
    ap.add_argument("--expect", help="expected_tags.json from audit_conversions.py, to compare IDs and labels.")
    ap.add_argument("--render", action="store_true", help="Headless browser: record real tag requests (needs playwright).")
    ap.add_argument("--wait-ms", type=int, default=4000)
    ap.add_argument("--no-gtm", action="store_true", help="Do not fetch GTM containers.")
    a = ap.parse_args()

    cid = gads.clean_cid(a.customer) or "offline"
    urls = list(a.url)
    if a.customer and not urls:
        urls = gads.load_profile(cid).get("site_urls", [])
    if not urls:
        gads.die("Pass --url (repeatable) or set site_urls in the account profile.")
    expected = json.loads(Path(a.expect).read_text()) if a.expect else None

    pages, approvals, notes = [], [], []
    gtm_cache: dict[str, dict] = {}
    for u in urls:
        try:
            final, html = fetch(u)
        except Exception as e:
            pages.append({"url": u, "error": str(e)})
            notes.append(f"{u}: could not fetch ({e})")
            continue
        s = scan_html(html)
        page = {"url": final, **{k: (dict(v) if isinstance(v, Counter) else v) for k, v in s.items()}}
        if not a.no_gtm:
            for g in s["gtm_ids"]:
                if g not in gtm_cache:
                    gtm_cache[g] = scan_gtm(g)
            page["gtm"] = {g: gtm_cache[g] for g in s["gtm_ids"]}
        if a.render and final.startswith("http"):
            page["render"] = render(final, a.wait_ms)
        pages.append(page)

        # findings per page
        dup_loads = {k: v for k, v in s["gtag_loads"].items() if v > 1}
        dup_cfg = {k: v for k, v in s["configs"].items() if v > 1}
        dup_gtm = {k: v for k, v in s["gtm_loads"].items() if v > 1}
        dup_send = {k: v for k, v in s["send_to"].items() if v > 1}
        if dup_loads or dup_cfg or dup_gtm:
            approvals.append(Approval(
                skill=SKILL, kind="duplicate_tag_load", entity=final, severity="critical",
                action=f"Remove the duplicate Google tag load on {final}",
                why="The same tag is installed more than once (" + ", ".join(f"{k} x{v}" for k, v in {**dup_loads, **dup_cfg, **dup_gtm}.items()) +
                    "). Hard-coded gtag plus the same tag in GTM is the usual cause; each copy can fire the conversion.",
                confidence="high" if (dup_cfg or dup_gtm) else "medium", risk="low",
                evidence={"gtag_loads": dict(s["gtag_loads"]), "configs": dict(s["configs"]), "gtm_loads": dict(s["gtm_loads"])}))
        if dup_send:
            approvals.append(Approval(
                skill=SKILL, kind="duplicate_conversion_event", entity=final, severity="high",
                action=f"Fire each conversion label once on {final}",
                why="The page source sends the same conversion label more than once: " + ", ".join(f"{k} x{v}" for k, v in dup_send.items()) + ". Every page view can record two conversions.",
                confidence="medium", risk="low", evidence={"send_to": dict(s["send_to"])}))
        on_page_aw = set(s["aw_ids"]) | {i for g in page.get("gtm", {}).values() for i in g.get("aw_ids", [])}
        if not on_page_aw and not s["gtm_ids"]:
            notes.append(f"{final}: no Google Ads tag and no GTM container found in the HTML (it may be injected by a CMP or tag server; check with --render or Tag Assistant).")
        if s["gtag_loads"] or s["gtm_ids"]:
            consent_seen = s["consent_default"] or any(g.get("mentions_consent") for g in page.get("gtm", {}).values())
            if not consent_seen:
                notes.append(f"{final}: no consent mode default found in page or GTM. Required for EEA/UK traffic; without it Google drops modeled conversions there.")
        if expected:
            exp_ids = set(expected.get("aw_ids", []))
            if exp_ids and on_page_aw and not (exp_ids & on_page_aw):
                approvals.append(Approval(
                    skill=SKILL, kind="wrong_account_tag", entity=final, severity="critical",
                    action=f"Replace the Google Ads tag on {final} with this account's tag",
                    why=f"The page loads {', '.join(sorted(on_page_aw))} but the account's conversion actions expect {', '.join(sorted(exp_ids))}. Conversions from this page go to a different account or nowhere.",
                    confidence="high", risk="medium", evidence={"on_page": sorted(on_page_aw), "expected": sorted(exp_ids)}))
            exp_labels = set(expected.get("labels", []))
            page_labels = set(s["send_to"]) | {f"{i}/{l}" for g in page.get("gtm", {}).values() for i in g.get("aw_ids", []) for l in g.get("labels", [])}
            if page.get("render", {}).get("conversion_labels"):
                page_labels |= set(page["render"]["conversion_labels"])
            page["expected_labels_found"] = sorted(exp_labels & page_labels)

        r = page.get("render") or {}
        if r.get("conversion_labels"):
            for lab, n in r["conversion_labels"].items():
                if n > 1:
                    approvals.append(Approval(
                        skill=SKILL, kind="double_fire", entity=final, severity="critical",
                        action=f"Stop {lab} firing {n} times on one load of {final}",
                        why=f"A headless load recorded {n} conversion requests for the same label. Each one counts unless counting is One per click.",
                        confidence="high", risk="low", evidence={"label": lab, "requests": n}))

    lines = [f"# Site tag check · {gads.fmt_cid(cid) or 'no account'}", ""]
    for p in pages:
        if "error" in p:
            lines += [f"## {p['url']}", "", f"Could not fetch: {p['error']}", ""]
            continue
        lines += [f"## {p['url']}", "",
                  f"- gtag.js loads: {p['gtag_loads'] or 'none'}",
                  f"- gtag config calls: {p['configs'] or 'none'}",
                  f"- GTM containers: {', '.join(p['gtm_ids']) or 'none'}",
                  f"- Google Ads IDs: {', '.join(p['aw_ids']) or 'none in HTML'} · GA4: {', '.join(p['ga4_ids']) or 'none in HTML'}",
                  f"- Conversion labels in source: {p['send_to'] or 'none'}",
                  f"- Consent mode default: {'yes' if p['consent_default'] else 'not in HTML'} · enhanced conversions user_data: {'yes' if p['ec_user_data'] else 'not in HTML'}"]
        for g, info in (p.get("gtm") or {}).items():
            if "error" in info:
                lines.append(f"- GTM {g}: could not fetch container ({info['error']})")
            else:
                lines.append(f"- GTM {g} contains: Ads {', '.join(info['aw_ids']) or 'none'}, {info['ads_conversion_tags']} Ads conversion tag(s) ({info['paused_ads_conversion_tags']} paused), labels {', '.join(info['labels']) or 'none as literals'}, GA4 {', '.join(info['ga4_ids']) or 'none'}")
        if p.get("render"):
            r = p["render"]
            lines.append(f"- Rendered: {r.get('error') or json.dumps(r.get('counts'))}" + (f" · conversion hits {r.get('conversion_labels')}" if r.get("conversion_labels") else "") + (f" · GA4 events {r.get('ga4_events')}" if r.get("ga4_events") else ""))
        if "expected_labels_found" in p:
            lines.append(f"- Expected conversion labels found: {', '.join(p['expected_labels_found']) or 'none'}")
        lines.append("")
    if notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in notes] + [""]
    lines += ["Static mode reads HTML and GTM containers. It proves a tag is installed, not that it fires on a real conversion. Confirm firing with --render on the thank-you URL or Tag Assistant."]
    headline = f"Checked {len(pages)} page(s): {len(approvals)} tag issue(s)" + (f", {len(notes)} note(s)" if notes else "")
    summary = {"headline": headline, "pages": len(pages), "issues": len(approvals)}
    d = gads.write_results(SKILL, cid, approvals, summary, "\n".join(lines) + "\n",
                           extra_files={"site_tags.json": json.dumps(pages, indent=2, default=str)})
    print(headline)
    print(f"-> {d}/{SKILL}.md  ·  queue: {d}/approvals.md")


if __name__ == "__main__":
    main()
