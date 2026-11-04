#!/usr/bin/env python3
"""
validate_copy.py - check drafted RSA copy and build an Editor-ready file.

  python3 validate_copy.py --drafts drafts.csv [--brief rsa_brief.json] [--out rsa_drafts.csv]

drafts.csv columns (one asset per row):
  ad_id        ad being refreshed (from rsa_brief.json); blank for a brand-new ad
  campaign, ad_group, final_url   needed when ad_id is blank or no brief is given
  kind         headline | description | path1 | path2
  text         the copy
  pin          optional: HEADLINE_1..3 / DESCRIPTION_1..2
  replaces     optional: exact text of the existing asset this one replaces

Checks: character limits (30/90/15, double-width chars count 2), editorial
policy, duplicates within the draft and against kept assets, keyword coverage,
final counts. Exit code 1 if any hard error, so nothing broken gets shipped.

Output rsa_drafts.csv uses Google Ads Editor columns for responsive search ads.
Importing it creates a NEW RSA per row. Pause the old ad after import so
history stays readable.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from copyrules import LIMITS, MAX_DESCRIPTIONS, MAX_HEADLINES, check_text, key, similar, toks, width  # noqa: E402


def kw_in(text, kw):
    kt = toks(kw)
    return bool(kt) and kt.issubset(toks(text))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--drafts", required=True)
    ap.add_argument("--brief", help="rsa_brief.json from audit_rsas.py")
    ap.add_argument("--out", help="Output path (default: rsa_drafts.csv next to drafts)")
    ap.add_argument("--min-headlines", type=int, default=10)
    a = ap.parse_args()

    brief = {}
    if a.brief:
        brief = {str(b["ad_id"]): b for b in json.loads(Path(a.brief).read_text())}
    rows = list(csv.DictReader(open(a.drafts, newline="", encoding="utf-8-sig")))

    groups: dict[str, dict] = {}
    for r in rows:
        gid = str(r.get("ad_id") or "") or f"new:{r.get('campaign', '')}>{r.get('ad_group', '')}"
        g = groups.setdefault(gid, {"rows": [], "ad_id": r.get("ad_id", ""), "campaign": r.get("campaign", ""),
                                    "ad_group": r.get("ad_group", ""), "final_url": r.get("final_url", "")})
        g["rows"].append(r)

    errors, warnings, out_rows = [], [], []
    for gid, g in groups.items():
        b = brief.get(str(g["ad_id"]), {})
        camp = g["campaign"] or b.get("campaign", "")
        adg = g["ad_group"] or b.get("ad_group", "")
        url = g["final_url"] or b.get("final_url", "")
        label = f"{camp} > {adg}"
        if not camp or not adg:
            errors.append(f"[{gid}] missing campaign/ad_group (add columns or pass --brief)")
        if not url:
            errors.append(f"[{label}] missing final_url")

        replaced = {key(r["replaces"]) for r in g["rows"] if r.get("replaces")}
        replaced |= {key(x["text"]) for x in b.get("replace", [])}
        kept_h = [h for h in b.get("keep_headlines", []) if key(h["text"]) not in replaced]
        kept_d = [d for d in b.get("keep_descriptions", []) if key(d["text"]) not in replaced]
        if b.get("recommend_unpin"):  # audit flagged over-pinning: kept assets go in unpinned
            kept_h = [{**h, "pin": ""} for h in kept_h]
            kept_d = [{**d, "pin": ""} for d in kept_d]
        new_h, new_d = [], []
        path1, path2 = b.get("path1", ""), b.get("path2", "")

        for r in g["rows"]:
            kind = (r.get("kind") or "").strip().lower()
            text = (r.get("text") or "").strip()
            pin = (r.get("pin") or "").strip().upper()
            if kind not in ("headline", "description", "path1", "path2"):
                errors.append(f"[{label}] unknown kind '{kind}' for '{text}'")
                continue
            lim_kind = "path" if kind.startswith("path") else kind
            for e in check_text(text, lim_kind):
                errors.append(f"[{label}] {kind} '{text}' ({width(text)}/{LIMITS[lim_kind]}): {e}")
            if kind == "path1":
                path1 = text
            elif kind == "path2":
                path2 = text
            else:
                pool = kept_h + new_h if kind == "headline" else kept_d + new_d
                dup = next((p["text"] for p in pool if key(p["text"]) == key(text)), None)
                near = next((p["text"] for p in pool if similar(p["text"], text)), None)
                if dup:
                    errors.append(f"[{label}] duplicate {kind}: '{text}' already exists")
                    continue
                if near:
                    warnings.append(f"[{label}] '{text}' is close to '{near}' (different angle?)")
                (new_h if kind == "headline" else new_d).append({"text": text, "pin": pin})

        heads = (kept_h + new_h)
        descs = (kept_d + new_d)
        if len(heads) > MAX_HEADLINES:
            warnings.append(f"[{label}] {len(heads)} headlines; keeping first {MAX_HEADLINES} (new ones first)")
            heads = (new_h + kept_h)[:MAX_HEADLINES]
        if len(descs) > MAX_DESCRIPTIONS:
            warnings.append(f"[{label}] {len(descs)} descriptions; keeping first {MAX_DESCRIPTIONS} (new ones first)")
            descs = (new_d + kept_d)[:MAX_DESCRIPTIONS]
        if len(heads) < 3 or len(descs) < 2:
            errors.append(f"[{label}] RSA needs at least 3 headlines and 2 descriptions (has {len(heads)}/{len(descs)})")
        elif len(heads) < a.min_headlines:
            warnings.append(f"[{label}] only {len(heads)} headlines; aim for 12-15")
        if len(descs) < 4:
            warnings.append(f"[{label}] {len(descs)} descriptions; aim for 4")
        pinned = [h for h in heads if h.get("pin")]
        if len(pinned) >= 3:
            warnings.append(f"[{label}] {len(pinned)} pinned headlines limit combinations")
        if path2 and not path1:
            errors.append(f"[{label}] path2 set without path1")

        top = b.get("top_keywords", [])
        if top and not any(kw_in(h["text"], k) for h in heads for k in top[:3]):
            errors.append(f"[{label}] no headline contains a top keyword ({', '.join(top[:3])})")
        elif top and not any(kw_in(h["text"], top[0]) for h in heads):
            warnings.append(f"[{label}] top keyword '{top[0]}' not in any headline")

        row = {"Campaign": camp, "Ad group": adg, "Ad type": "Responsive search ad", "Status": "Enabled"}
        for i in range(MAX_HEADLINES):
            h = heads[i] if i < len(heads) else {"text": "", "pin": ""}
            row[f"Headline {i + 1}"] = h["text"]
            row[f"Headline {i + 1} position"] = h.get("pin", "").replace("HEADLINE_", "") if h.get("pin") else ""
        for i in range(MAX_DESCRIPTIONS):
            d = descs[i] if i < len(descs) else {"text": "", "pin": ""}
            row[f"Description {i + 1}"] = d["text"]
            row[f"Description {i + 1} position"] = d.get("pin", "").replace("DESCRIPTION_", "") if d.get("pin") else ""
        row.update({"Path 1": path1, "Path 2": path2, "Final URL": url, "Replaces ad ID": g["ad_id"]})
        out_rows.append(row)
        print(f"{label}: {len(heads)} headlines ({len(new_h)} new), {len(descs)} descriptions ({len(new_d)} new)")

    for w in warnings:
        print("WARN  " + w)
    for e in errors:
        print("ERROR " + e)
    out = Path(a.out) if a.out else Path(a.drafts).with_name("rsa_drafts.csv")
    if out_rows and not errors:
        buf = io.StringIO()
        wr = csv.DictWriter(buf, fieldnames=list(out_rows[0].keys()))
        wr.writeheader()
        wr.writerows(out_rows)
        out.write_text(buf.getvalue())
    status = "FAILED" if errors else "PASSED"
    print(f"{status}: {len(errors)} errors, {len(warnings)} warnings. {'Fix errors before shipping.' if errors else f'Wrote {out}'}")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
