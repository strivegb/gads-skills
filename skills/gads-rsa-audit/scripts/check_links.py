#!/usr/bin/env python3
"""
check_links.py - landing page checker for ad final URLs (works like Google's Link Checker script).

Offline:  python3 check_links.py --csv ads.csv            # Final URL column of enabled ads
Live:     $PY check_links.py -c 1234567890                 # final URLs of every enabled ad
URLs:     python3 check_links.py --urls https://acme.com/a file:///tmp/page.html

Each unique final URL is fetched once with GET (User-Agent gads-skills-link-checker/1.0),
redirects are followed and recorded, and the result is flagged as:
  broken       HTTP 4xx or 5xx, or a redirect loop
  unreachable  timeout, DNS or connection error (retried once)
  soft_404     HTTP 200 whose page says "page not found", "no longer available", "out of stock" ...
  off_domain   a redirect ends on a different domain
  homepage     a deep URL redirects to the site homepage
ValueTrack {placeholders} get test values ({keyword} -> test) or are removed before fetching.

Writes rsa_links.csv (path via --out). audit_rsas.py --check-links runs the same check and turns
problems into approval items (broken pages behind ads with spend are critical).
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gads  # noqa: E402
from gads import micros  # noqa: E402

UA = "gads-skills-link-checker/1.0"
MAX_BODY = 512 * 1024
MAX_HOPS = 10
BROKEN = ("broken", "unreachable", "soft_404")
REDIRECT = ("off_domain", "homepage")
BLOCKED = ("blocked",)  # site refused the checker (bot protection); a person must check
DEFAULT_FAIL_TEXT = [
    "page not found", "404 not found", "page could not be found", "page cannot be found",
    "page does not exist", "page doesn't exist", "no longer available", "no longer exists",
    "out of stock", "sold out", "currently unavailable", "product is unavailable",
]
# Test values for ValueTrack parameters. Anything not listed (including {_custom}) is removed.
VALUETRACK = {
    "keyword": "test", "matchtype": "e", "network": "g", "device": "c", "creative": "0",
    "campaignid": "0", "adgroupid": "0", "gclid": "test", "random": "0", "adposition": "",
    "targetid": "", "feeditemid": "", "extensionid": "", "loc_physical_ms": "", "loc_interest_ms": "",
    "placement": "", "target": "", "devicemodel": "", "lpurl": "", "unescapedlpurl": "", "escapedlpurl": "",
}
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_TWO_PART_SLD = {"co", "com", "org", "net", "gov", "ac", "edu"}


def clean_url(url: str) -> str:
    """Replace ValueTrack placeholders with test values so the URL can be fetched."""
    def sub(m):
        inner = m.group(1).strip()
        low = inner.lower()
        if ":" in low:
            cond, _, val = inner.partition(":")
            return val if cond.lower() in ("ifnotmobile", "ifsearch") else ""
        return VALUETRACK.get(low, "")
    url = _PLACEHOLDER.sub(sub, (url or "").strip())
    return url.replace("?&", "?").rstrip("?&")


def first_url(v) -> str:
    """Final URL cells can be a plain URL, a list, or '["https://..."]' text."""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else ""
    s = str(v or "").strip().strip("[]").split(",")[0].strip().strip("'\"")
    return "" if s in ("--", "-") else s


def reg_domain(host: str) -> str:
    """Rough registrable domain: shop.acme.com -> acme.com, www.acme.co.uk -> acme.co.uk."""
    parts = (host or "").lower().strip(".").split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _TWO_PART_SLD:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # surface 3xx as HTTPError so each hop is recorded
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _fetch_once(url: str, timeout: float) -> dict:
    chain, cur = [], url
    for _ in range(MAX_HOPS + 1):
        req = urllib.request.Request(cur, headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.8"})
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                status = resp.getcode() or 200
                body = resp.read(MAX_BODY)
                charset = resp.headers.get_content_charset() if resp.headers else None
                chain.append((status, cur))
                return {"status": status, "final_url": cur, "chain": chain, "error": "",
                        "body": body.decode(charset or "utf-8", "replace")}
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") if e.headers else None
            chain.append((e.code, cur))
            if e.code in (301, 302, 303, 307, 308) and loc:
                cur = urllib.parse.urljoin(cur, loc)
                continue
            return {"status": e.code, "final_url": cur, "chain": chain, "error": f"HTTP {e.code}", "body": ""}
        except urllib.error.URLError as e:
            r = e.reason
            if isinstance(r, FileNotFoundError) or (isinstance(r, OSError) and getattr(r, "errno", None) == 2):
                chain.append((404, cur))
                return {"status": 404, "final_url": cur, "chain": chain, "error": "file not found", "body": ""}
            if isinstance(r, socket.gaierror):
                msg = "DNS lookup failed"
            elif isinstance(r, (socket.timeout, TimeoutError)):
                msg = "timed out"
            elif isinstance(r, ConnectionRefusedError):
                msg = "connection refused"
            else:
                msg = str(r)[:120]
            return {"status": None, "final_url": cur, "chain": chain, "error": msg, "body": ""}
        except (socket.timeout, TimeoutError):
            return {"status": None, "final_url": cur, "chain": chain, "error": "timed out", "body": ""}
        except (OSError, ValueError, http.client.HTTPException) as e:
            return {"status": None, "final_url": cur, "chain": chain, "error": f"{type(e).__name__}: {e}"[:120], "body": ""}
    return {"status": None, "final_url": cur, "chain": chain, "error": "redirect loop", "body": ""}


def fetch(url: str, timeout: float = 10, retries: int = 1) -> dict:
    res = _fetch_once(url, timeout)
    for _ in range(retries):
        if res["status"] is not None or res["error"] == "redirect loop":
            break
        res = _fetch_once(url, timeout)
    return res


def assess(url: str, res: dict, fail_text=DEFAULT_FAIL_TEXT) -> tuple[str, str]:
    """Return (problem, detail). problem is '' when the page looks fine."""
    st = res.get("status")
    if res.get("error") == "redirect loop":
        return "broken", f"more than {MAX_HOPS} redirects"
    if st is None:
        return "unreachable", res.get("error") or "no response"
    if st in (401, 403, 429):
        return "blocked", f"HTTP {st}: the site refused the checker, which bot protection often does"
    if st >= 400:
        return "broken", res.get("error") if res.get("error") == "file not found" else f"HTTP {st}"
    body = (res.get("body") or "").lower()
    hit = next((t for t in fail_text if t and t.lower() in body), None)
    if hit:
        return "soft_404", f"HTTP {st} but the page says '{hit}'"
    if len(res.get("chain") or []) > 1:
        a, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(res["final_url"])
        if a.scheme in ("http", "https") and a.hostname and b.hostname and reg_domain(a.hostname) != reg_domain(b.hostname):
            return "off_domain", f"redirects to {b.hostname}"
        deep = a.path.strip("/") not in ("", "index.html")
        if deep and b.path.strip("/") in ("", "index.html", "index.php"):
            return "homepage", "redirects to the homepage"
    return "", ""


def collect(ads: list[dict]) -> dict[str, dict]:
    """Unique final URLs of enabled ads -> ads, ad groups and cost behind each."""
    out: dict[str, dict] = {}
    for a in ads:
        u = first_url(a.get("final_url"))
        if not u:
            continue
        rec = out.setdefault(u, {"ad_ids": [], "ad_groups": [], "cost": 0.0, "impr": 0.0})
        rec["ad_ids"].append(str(a.get("ad_id", "")))
        g = f"{a.get('campaign', '')} > {a.get('ad_group', '')}"
        if g not in rec["ad_groups"]:
            rec["ad_groups"].append(g)
        rec["cost"] += float(a.get("cost") or 0)
        rec["impr"] += float(a.get("impr") or 0)
    return out


def check_urls(urls, timeout: float = 10, workers: int = 4, fail_text=DEFAULT_FAIL_TEXT) -> dict[str, dict]:
    urls = list(dict.fromkeys(u for u in urls if u))

    def one(u):
        target = clean_url(u)
        res = fetch(target, timeout)
        problem, detail = assess(target, res, fail_text)
        return u, {"url": u, "fetched_url": target, "status": res["status"] if res["status"] is not None else "",
                   "final_url": res["final_url"], "problem": problem, "detail": detail,
                   "chain": " -> ".join(f"{c} {x}" for c, x in res["chain"]) or res["error"]}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as ex:
        return dict(ex.map(one, urls))


def link_rows(checked: dict[str, dict], usage: dict[str, dict]) -> list[dict]:
    rows = []
    for u, c in checked.items():
        us = usage.get(u, {})
        rows.append({**c, "ads": len(us.get("ad_ids", [])), "ad_ids": " ".join(us.get("ad_ids", [])),
                     "ad_groups": " | ".join(us.get("ad_groups", [])), "cost": round(us.get("cost", 0), 2)})
    rank = {p: i for i, p in enumerate(BROKEN + REDIRECT + ("",))}
    return sorted(rows, key=lambda r: (rank.get(r["problem"], 9), -r["cost"]))


def load_live_ads(cid: str, start: str, end: str) -> list[dict]:
    base = "campaign.status = 'ENABLED' AND ad_group.status = 'ENABLED' AND ad_group_ad.status = 'ENABLED'"
    cost = {str(r.get("ad_group_ad.ad.id")): micros(r.get("metrics.cost_micros")) for r in gads.query(cid, f"""
        SELECT ad_group_ad.ad.id, metrics.cost_micros FROM ad_group_ad
        WHERE {base} AND {gads.gaql_dates(start, end)}""")}
    return [{"ad_id": str(r.get("ad_group_ad.ad.id")), "campaign": r.get("campaign.name", ""),
             "ad_group": r.get("ad_group.name", ""), "final_url": r.get("ad_group_ad.ad.final_urls") or [],
             "cost": cost.get(str(r.get("ad_group_ad.ad.id")), 0.0)}
            for r in gads.query(cid, f"""
        SELECT campaign.name, ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad.final_urls
        FROM ad_group_ad WHERE {base}""")]


def main():
    ap = gads.common_args(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    ap.add_argument("--urls", nargs="*", help="Check these URLs directly.")
    ap.add_argument("--timeout", type=float, default=10)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--fail-text", action="append", default=[], help="Extra soft 404 phrase (repeatable).")
    ap.add_argument("--out", help="CSV path (default: rsa_links.csv in the gads-out folder)")
    a = ap.parse_args()
    fail_text = DEFAULT_FAIL_TEXT + a.fail_text

    if a.urls:
        usage, cid = {u: {} for u in a.urls}, gads.clean_cid(a.customer) or "offline"
    elif a.csv:
        from audit_rsas import load_csv  # every enabled ad row in the export, any ad type
        ads = load_csv(a.csv, None, None, all_types=True)[0]
        usage, cid = collect(ads), gads.clean_cid(a.customer) or "offline"
    else:
        if not a.customer:
            gads.die("Pass --urls, --csv or --customer.")
        cid = gads.clean_cid(a.customer)
        usage = collect(load_live_ads(cid, *gads.resolve_window(a)))
    if not usage:
        gads.die("No final URLs found.")
    rows = link_rows(check_urls(usage, a.timeout, a.workers, fail_text), usage)
    out = Path(a.out) if a.out else gads.out_dir(cid) / "rsa_links.csv"
    out.write_text(gads.to_csv(rows))
    bad = [r for r in rows if r["problem"]]
    print(f"Checked {len(rows)} landing pages: {sum(r['problem'] in BROKEN for r in rows)} broken, "
          f"{sum(r['problem'] in REDIRECT for r in rows)} bad redirects")
    for r in bad:
        print(f"  {r['problem']:<12} {r['url']}  ({r['detail']})")
    print(f"-> {out}")
    if a.json:
        print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
