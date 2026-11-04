"""copyrules.py - shared RSA copy rules for audit_rsas.py and validate_copy.py."""
from __future__ import annotations

import re
import unicodedata

LIMITS = {"headline": 30, "description": 90, "path": 15}
MAX_HEADLINES, MAX_DESCRIPTIONS = 15, 4
ACRONYMS = {"AI", "API", "B2B", "B2C", "CRM", "CPA", "CPC", "CPL", "ROI", "ROAS", "SaaS", "SEO", "SEM", "PPC", "HR",
            "US", "USA", "UK", "EU", "UAE", "IT", "GST", "VAT", "TAX", "FAQ", "PDF", "SMS", "SOC", "ISO", "GDPR",
            "HIPAA", "KPI", "ERP", "SDK", "SSO", "UI", "UX", "IOS", "NYC", "LA", "SF", "D2C", "DTC", "24/7", "EMI",
            "ATS", "HRMS", "HRIS", "PEO", "CFO", "CEO", "CMO", "CTO", "SMB", "SME", "MSME", "AWS", "GCP", "QA", "OEM"}


def width(s: str) -> int:
    """Google counts double-width (CJK, full-width) characters as 2."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def key(s: str) -> str:
    """Case and punctuation insensitive identity for dedupe."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def toks(s: str) -> set[str]:
    return set(key(s).split())


def similar(a: str, b: str, thresh: float = 0.75) -> bool:
    """Near-duplicate: same key, or Jaccard token overlap >= thresh (on 3+ token strings)."""
    ka, kb = key(a), key(b)
    if ka == kb:
        return True
    ta, tb = toks(a), toks(b)
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    # "HR Software" vs "HR Software Online": one adds a single filler word
    if len(small) >= 2 and small <= big and len(big - small) <= 1:
        return True
    if len(small) >= 2 and len(small) == len(big) and len(small ^ big) <= 2 and len(small & big) >= 2 and len(small) <= 3:
        return True  # "HR Software Online" vs "HR Software Tool"
    if min(len(ta), len(tb)) < 3:
        return False
    return len(ta & tb) / len(ta | tb) >= thresh


EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U0000FE0F]")
PHONE = re.compile(r"(\+?\d[\d\s().-]{7,}\d)")
REPEAT_PUNCT = re.compile(r"([!?.,:;\-*/&])\1+")
GIMMICK = re.compile(r"(click here|click now|\bc l i c k\b)", re.I)


def check_text(text: str, kind: str) -> list[str]:
    """Return a list of violations for one headline/description/path."""
    errs = []
    lim = LIMITS[kind]
    w = width(text)
    if not text.strip():
        return ["empty"]
    if w > lim:
        errs.append(f"too long: {w}/{lim}")
    if kind == "headline" and "!" in text:
        errs.append("exclamation mark in headline")
    if kind == "description" and text.count("!") > 1:
        errs.append("more than one exclamation mark")
    if REPEAT_PUNCT.search(text):
        errs.append("repeated punctuation")
    if EMOJI.search(text):
        errs.append("emoji/symbol")
    if PHONE.search(text):
        errs.append("phone number in ad text (use a call asset)")
    if GIMMICK.search(text):
        errs.append("'click here' style CTA")
    if text != text.strip() or "  " in text:
        errs.append("extra spaces")
    for word in re.findall(r"[A-Za-z][A-Za-z0-9/&]*", text):
        if len(word) >= 3 and word.isupper() and word not in ACRONYMS and word.upper() not in {a.upper() for a in ACRONYMS}:
            errs.append(f"ALL CAPS word '{word}'")
            break
    if kind == "path" and (" " in text or "/" in text):
        errs.append("path cannot contain spaces or slashes")
    return errs
