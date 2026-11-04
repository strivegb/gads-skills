"""
gads.py - shared runtime for the gads-* Google Ads skills.

Identical copy ships inside every gads-* skill so each skill stays
self-contained. If you change it, change it everywhere (see
SHARED_RUNTIME.md in any gads-* skill).

Responsibilities
  * Auth + GAQL against the Google Ads API (live mode)
  * Loading Google Ads UI CSV exports (offline mode)
  * Per-account profile (brand terms, targets, budgets)
  * Approval items: one schema, one merged queue per account per day

Live mode needs the `google-ads` Python library. On this machine it lives
in the google-ads-mcp pipx venv:
    ~/Library/Application Support/pipx/venvs/google-ads-mcp/bin/python
Offline (CSV) mode is pure stdlib and runs on any Python 3.9+.

Auth (Google Ads API, post Sept 2026):
  Developer tokens were sunset on 2026-09-09. Access is granted to the
  Google Cloud project that owns the OAuth client. We use Application
  Default Credentials with the adwords scope:
      gcloud auth application-default login \
        --client-id-file="$HOME/.config/google-ads/client_secret.json" \
        --scopes=https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform
  Optional env:
      GOOGLE_ADS_LOGIN_CUSTOMER_ID   manager (MCC) id when accessing via an MCC
      GOOGLE_ADS_DEVELOPER_TOKEN     only for legacy setups; ignored by the API now
      GOOGLE_ADS_CONFIGURATION_FILE_PATH  use a google-ads.yaml instead of ADC
      GADS_OUT                       output root (default ./gads-out)
      GADS_PROFILE_DIR               profile root (default ~/.config/gads-skills/accounts)
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

RUNTIME_VERSION = "1.1.0"
ADWORDS_SCOPE = "https://www.googleapis.com/auth/adwords"

# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------


def clean_cid(cid: str | int | None) -> str:
    """'123-456-7890' -> '1234567890'."""
    if cid is None:
        return ""
    return re.sub(r"\D", "", str(cid))


def fmt_cid(cid: str) -> str:
    c = clean_cid(cid)
    return f"{c[:3]}-{c[3:6]}-{c[6:]}" if len(c) == 10 else c


def micros(v: Any) -> float:
    try:
        return float(v or 0) / 1_000_000
    except (TypeError, ValueError):
        return 0.0


def num(v: Any) -> float:
    """Parse numbers from API values or UI exports: '1,234.5', '12.3%', '--', ' < 10%'."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "")
    s = s.replace("<", "").replace(">", "").strip()
    for sym in ("$", "₹", "€", "£", "A$", "C$", "US$"):
        s = s.replace(sym, "")
    if s in ("", "--", "-", "—", " --"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def pct(v: Any) -> float:
    """Impression share style values -> 0..1. Accepts 0.42, '42%', '< 10%'."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, str) and "%" in v:
        return num(v) / 100
    x = num(v)
    return x / 100 if x > 1 else x


def safe_div(a: float, b: float) -> float | None:
    return a / b if b else None


def money(x: float | None, cur: str = "") -> str:
    if x is None:
        return "n/a"
    sym = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£", "AUD": "A$", "CAD": "C$", "SGD": "S$"}.get(cur, "")
    return f"{sym}{x:,.0f}" if abs(x) >= 100 else f"{sym}{x:,.2f}"


def today() -> dt.date:
    return dt.date.today()


def date_window(days: int, end: dt.date | None = None, include_today: bool = False) -> tuple[str, str]:
    """Closed window ending yesterday by default (today is always partial)."""
    end = end or today()
    if not include_today:
        end = end - dt.timedelta(days=1)
    start = end - dt.timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def gaql_dates(start: str, end: str) -> str:
    return f"segments.date BETWEEN '{start}' AND '{end}'"


def gaql_str(s: str) -> str:
    """Escape a string literal for GAQL."""
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


# --------------------------------------------------------------------------
# Live mode: Google Ads API
# --------------------------------------------------------------------------

_CLIENT = None


def get_client():
    """Build a GoogleAdsClient from ADC (default) or a google-ads.yaml."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    try:
        from google.ads.googleads.client import GoogleAdsClient  # type: ignore
    except ImportError:
        die(
            "google-ads library not available in this Python.\n"
            "Run with the google-ads-mcp venv python:\n"
            '  PY="$HOME/Library/Application Support/pipx/venvs/google-ads-mcp/bin/python"\n'
            "or use offline mode with --csv <export.csv>."
        )
    yaml_path = os.environ.get("GOOGLE_ADS_CONFIGURATION_FILE_PATH")
    if yaml_path:
        _CLIENT = GoogleAdsClient.load_from_storage(yaml_path)
        return _CLIENT
    import google.auth  # type: ignore

    try:
        creds, _ = google.auth.default(scopes=[ADWORDS_SCOPE])
    except Exception as e:  # DefaultCredentialsError
        die(f"No Application Default Credentials: {e}\n{AUTH_HELP}")
    kwargs: dict[str, Any] = {"credentials": creds, "use_proto_plus": True}
    tok = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN")
    if tok:
        kwargs["developer_token"] = tok
    login = clean_cid(os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID"))
    if login:
        kwargs["login_customer_id"] = login
    _CLIENT = GoogleAdsClient(**kwargs)
    return _CLIENT


AUTH_HELP = """Re-authenticate with the Google Ads scope:
  gcloud auth application-default login \\
    --client-id-file="$HOME/.config/google-ads/client_secret.json" \\
    --scopes=https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform
If the account sits under a manager account, also:
  export GOOGLE_ADS_LOGIN_CUSTOMER_ID=<manager id, digits only>"""


def flatten(d: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


def query(customer_id: str, gaql: str, soft: bool = False) -> list[dict]:
    """Run GAQL with search_stream. Returns flat dicts keyed like 'campaign.name'.

    Enums come back as names ('ENABLED'), int64 as str (to_dict default),
    repeated fields as lists. Use num()/micros() to coerce.
    soft=True returns [] on API errors (for optional data like simulations).
    """
    client = get_client()
    svc = client.get_service("GoogleAdsService")
    cid = clean_cid(customer_id)
    rows: list[dict] = []
    try:
        stream = svc.search_stream(customer_id=cid, query=gaql)
        for batch in stream:
            for r in batch.results:
                d = type(r).to_dict(r, use_integers_for_enums=False, preserving_proto_field_name=True)
                rows.append(flatten(d))
    except Exception as e:  # GoogleAdsException, RefreshError, etc.
        if soft and "Reauthentication" not in str(e):
            print(f"note: optional query skipped ({type(e).__name__})", file=sys.stderr)
            return []
        handle_api_error(e, gaql)
    return rows


def handle_api_error(e: Exception, gaql: str = "") -> None:
    name = type(e).__name__
    msg = str(e)
    if "Reauthentication" in msg or "invalid_grant" in msg or "RefreshError" in name:
        die(f"Google auth expired.\n{AUTH_HELP}")
    failure = getattr(e, "failure", None)
    if failure is not None:
        lines = [f"Google Ads API error (request_id={getattr(e, 'request_id', '?')}):"]
        for err in failure.errors:
            lines.append(f"  - {err.error_code}: {err.message}")
        if gaql:
            lines.append("Query was:\n" + gaql)
        die("\n".join(lines))
    die(f"{name}: {msg}")


def accessible_customers() -> list[str]:
    client = get_client()
    res = client.get_service("CustomerService").list_accessible_customers()
    return [r.split("/")[-1] for r in res.resource_names]


def account_info(customer_id: str) -> dict:
    rows = query(
        customer_id,
        """SELECT customer.id, customer.descriptive_name, customer.currency_code,
                  customer.time_zone, customer.optimization_score,
                  customer.manager
           FROM customer LIMIT 1""",
    )
    r = rows[0] if rows else {}
    return {
        "id": clean_cid(r.get("customer.id", customer_id)),
        "name": r.get("customer.descriptive_name", ""),
        "currency": r.get("customer.currency_code", ""),
        "time_zone": r.get("customer.time_zone", ""),
        "optimization_score": r.get("customer.optimization_score"),
        "manager": r.get("customer.manager", False),
    }


# --------------------------------------------------------------------------
# Offline mode: Google Ads UI CSV exports
# --------------------------------------------------------------------------


def _norm_header(h: str) -> str:
    h = h.strip().lower().replace("﻿", "")
    h = re.sub(r"[^a-z0-9%/]+", "_", h).strip("_")
    return h


def read_ui_csv(path: str | Path) -> list[dict]:
    """Read a Google Ads UI export (CSV or TSV, UTF-8 or UTF-16).

    UI exports start with 1-2 title rows ("Search terms report",
    "November 1, 2026 - November 30, 2026") before the header, and end
    with "Total: ..." rows. Both are stripped. Headers are normalized:
    "Cost / conv." -> "cost_/_conv", "Search term" -> "search_term".
    """
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            text = raw.decode(enc)
            if "\x00" in text:
                continue
            break
        except UnicodeDecodeError:
            continue
    lines = text.splitlines()
    sample = "\n".join(lines[:12])
    delim = "\t" if sample.count("\t") > sample.count(",") else ","
    # header = first line whose cell count matches the widest of the first
    # 12 lines. Title rows like "October 4, 2026 - November 2, 2026" contain
    # commas, so a fixed ">= 3 cells" test is not enough.
    head = [len(next(csv.reader([ln], delimiter=delim), [])) for ln in lines[:12]]
    widest = max(head) if head else 0
    start = next((i for i, n in enumerate(head) if n == widest), 0)
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])), delimiter=delim)
    out = []
    for row in reader:
        first = next(iter(row.values()), "") or ""
        if str(first).strip().lower().startswith("total"):
            continue
        out.append({_norm_header(k): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k})
    return out


def pick(row: dict, *names: str, default: Any = "") -> Any:
    """First present key among normalized UI header candidates."""
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return default


# --------------------------------------------------------------------------
# Account profile
# --------------------------------------------------------------------------

PROFILE_TEMPLATE = {
    "customer_id": "",
    "name": "",
    "brand_terms": [],          # ["acme", "acme corp", "acmecorp"]
    "competitor_terms": [],     # used to separate competitor traffic
    "never_negate": [],         # terms that must never be blocked
    "irrelevant_themes": [],    # extra junk themes: ["jobs", "free", "diy"]
    "target_cpa": None,         # account-level business CPA target
    "target_roas": None,        # 3.5 means 350%
    "monthly_budget": None,     # total monthly cap in account currency
    "campaign_budgets": {},     # {"Campaign name": monthly_cap}
    "primary_conversions": [],  # names of conversion actions that matter
    "site_urls": [],            # pages to check tags on (tracking skill)
    "notes": "",
}


def profile_path(cid: str) -> Path:
    root = Path(os.environ.get("GADS_PROFILE_DIR", Path.home() / ".config/gads-skills/accounts"))
    return root / f"{clean_cid(cid) or 'offline'}.json"


def load_profile(cid: str) -> dict:
    p = profile_path(cid)
    prof = dict(PROFILE_TEMPLATE)
    if p.exists():
        prof.update(json.loads(p.read_text()))
    prof["customer_id"] = clean_cid(cid) or "offline"
    return prof


def save_profile(cid: str, prof: dict) -> Path:
    p = profile_path(cid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(prof, indent=2))
    return p


# --------------------------------------------------------------------------
# Approval items + merged queue
# --------------------------------------------------------------------------

RISK = ("low", "medium", "high")
SEVERITY = ("critical", "high", "medium", "low")  # critical = breaks measurement or spend safety
CONF = ("low", "medium", "high")


@dataclass
class Approval:
    """One decision waiting for a human yes/no.

    action:  imperative, specific, reversible-sounding.
             "Add 214 negatives to shared list 'Junk - Global'"
    why:     one or two sentences with the number that justifies it.
    impact:  estimated monthly effect in account currency (+ saves / + gains).
    evidence: small dict of the numbers a reviewer would check.
    change:  machine-readable payload the apply step (or a human in
             Editor) uses. Keep it exact.
    """

    skill: str
    kind: str
    action: str
    why: str
    entity: str = ""
    impact_monthly: float | None = None
    impact_label: str = ""          # "saves" | "protects" | "gains" | "fixes measurement"
    confidence: str = "medium"
    risk: str = "low"
    evidence: dict = field(default_factory=dict)
    change: dict = field(default_factory=dict)
    files: list = field(default_factory=list)
    severity: str = "medium"
    id: str = ""
    status: str = "pending"

    def __post_init__(self):
        if self.confidence not in CONF:
            self.confidence = "medium"
        if self.risk not in RISK:
            self.risk = "medium"
        if self.severity not in SEVERITY:
            self.severity = "medium"
        if not self.id:
            h = hashlib.sha1(f"{self.skill}|{self.kind}|{self.entity}|{self.action}".encode()).hexdigest()[:8]
            self.id = f"{self.skill.split('-')[-1][:6]}-{h}"


def out_dir(cid: str, day: str | None = None) -> Path:
    root = Path(os.environ.get("GADS_OUT", "gads-out"))
    d = root / (clean_cid(cid) or "offline") / (day or today().isoformat())
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_results(
    skill: str,
    cid: str,
    approvals: list[Approval],
    summary: dict,
    report_md: str,
    currency: str = "",
    extra_files: dict[str, str] | None = None,
) -> Path:
    """Write <skill>.json, <skill>.md, any extra files, and rebuild approvals.md."""
    d = out_dir(cid)
    payload = {
        "skill": skill,
        "runtime": RUNTIME_VERSION,
        "customer_id": clean_cid(cid),
        "currency": currency,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
        "approvals": [asdict(a) for a in approvals],
    }
    (d / f"{skill}.json").write_text(json.dumps(payload, indent=2, default=str))
    (d / f"{skill}.md").write_text(report_md)
    for name, content in (extra_files or {}).items():
        (d / name).write_text(content)
    rebuild_queue(d)
    return d


def rebuild_queue(d: Path) -> Path:
    """Merge every gads-*.json in the folder into one approvals.md, ranked."""
    items, summaries, cur = [], [], ""
    for f in sorted(d.glob("gads-*.json")):
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        cur = cur or data.get("currency", "")
        summaries.append((data["skill"], data.get("summary", {}).get("headline", "")))
        items.extend(data.get("approvals", []))
    rank_risk = {"low": 0, "medium": 1, "high": 2}
    rank_conf = {"high": 0, "medium": 1, "low": 2}
    rank_sev = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    # Severity first (a broken conversion poisons every other number), then money.
    items.sort(key=lambda a: (rank_sev.get(a.get("severity", "medium"), 2), -(a.get("impact_monthly") or 0),
                              rank_conf.get(a["confidence"], 1), rank_risk.get(a["risk"], 1)))
    total = sum(a.get("impact_monthly") or 0 for a in items if a.get("impact_label") in ("saves", "protects", "gains"))
    lines = [
        f"# Approval queue · {d.parent.name} · {d.name}",
        "",
        f"**{len(items)} items waiting.**" + (f" Estimated monthly impact of approving all: **{money(total, cur)}**." if total else ""),
        "",
        "| Skill | Finding |",
        "|---|---|",
    ]
    lines += [f"| {s} | {h} |" for s, h in summaries]
    lines += ["", "## Decisions", ""]
    for i, a in enumerate(items, 1):
        imp = f" · {a['impact_label']} ~{money(a['impact_monthly'], cur)}/mo" if a.get("impact_monthly") else ""
        sev = " · ⚠ critical" if a.get("severity") == "critical" else ""
        lines.append(f"- [ ] **{i}. {a['action']}**{imp}{sev}")
        lines.append(f"  - {a['why']}")
        lines.append(f"  - `{a['id']}` · {a['skill']} · confidence {a['confidence']} · risk {a['risk']}" + (f" · files: {', '.join(a['files'])}" if a.get("files") else ""))
    lines += ["", "Tick the box, or tell Claude `approve <id>` / `reject <id>`. Nothing has been changed in the account."]
    p = d / "approvals.md"
    p.write_text("\n".join(lines) + "\n")
    return p



# --------------------------------------------------------------------------
# Write safety (used by every apply_*.py)
#
#   1. Validate run (no --execute): the API checks every operation with
#      validate_only=True. On success record_validation() stores a token
#      (hash of the plan file) next to the plan and prints it.
#   2. Execute run: --execute --confirm <token>. check_execute() refuses
#      unless the token matches the plan file as it is now AND a successful
#      validation of that exact file exists. Editing the file (for example
#      removing rejected rows) changes the token, so it must be re-validated.
#   3. After writing, the apply script reads the account back and compares
#      (verify_readback) and every write is appended to changes.log.jsonl.
# --------------------------------------------------------------------------


def plan_token(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]


def _validation_path(path: str | Path) -> Path:
    p = Path(path)
    return p.with_name(p.name + ".validated.json")


def add_write_args(ap):
    ap.add_argument("--execute", action="store_true", help="Actually write. Without it, validate only.")
    ap.add_argument("--confirm", default="", help="Token printed by the validate run. Required with --execute.")
    return ap


def check_execute(args, plan_path: str | Path) -> bool:
    """Return validate_only. Dies if --execute is not backed by a matching validation."""
    if not getattr(args, "execute", False):
        return True
    tok = plan_token(plan_path)
    vp = _validation_path(plan_path)
    rec = json.loads(vp.read_text()) if vp.exists() else {}
    if rec.get("token") != tok:
        die(f"Refusing to write: {Path(plan_path).name} has not been validated in its current form.\n"
            "Run the same command without --execute first, then pass the token it prints.")
    if args.confirm != tok:
        die(f"Refusing to write: --confirm must be the token from the validate run ({tok}).")
    return False


def record_validation(plan_path: str | Path, n_ops: int, extra: dict | None = None) -> str:
    tok = plan_token(plan_path)
    rec = {"token": tok, "validated_at": dt.datetime.now().isoformat(timespec="seconds"), "operations": n_ops}
    rec.update(extra or {})
    _validation_path(plan_path).write_text(json.dumps(rec, indent=2))
    print(f"Validation passed for {n_ops} operation(s). Nothing was changed.")
    print(f"To write after the user approves: add --execute --confirm {tok}")
    return tok


def log_change(cid: str, skill: str, record: dict) -> Path:
    """Append one write to <GADS_OUT>/<cid>/changes.log.jsonl (audit trail)."""
    root = Path(os.environ.get("GADS_OUT", "gads-out")) / (clean_cid(cid) or "offline")
    root.mkdir(parents=True, exist_ok=True)
    p = root / "changes.log.jsonl"
    rec = {"at": dt.datetime.now().isoformat(timespec="seconds"), "skill": skill, **record}
    with p.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    return p


def verify_readback(expected: dict, actual: dict, tol: float = 0.01) -> list[str]:
    """Compare what we meant to write with what the account now holds.

    expected/actual map a key (resource name, keyword text...) to a value.
    Returns human-readable mismatches; empty list means every write landed.
    """
    bad = []
    for k, want in expected.items():
        got = actual.get(k)
        if isinstance(want, (int, float)) and isinstance(got, (int, float)):
            if abs(got - want) > max(tol * abs(want), 0.005):
                bad.append(f"{k}: expected {want}, account has {got}")
        elif got != want:
            bad.append(f"{k}: expected {want!r}, account has {got!r}")
    return bad

# --------------------------------------------------------------------------
# CLI helpers
# --------------------------------------------------------------------------


def die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def common_args(ap):
    ap.add_argument("--customer", "-c", help="Customer ID (digits or 123-456-7890). Required in live mode.")
    ap.add_argument("--csv", nargs="*", help="Offline mode: Google Ads UI export(s) instead of the API.")
    ap.add_argument("--days", type=int, default=30, help="Lookback window in days, ending yesterday (default 30).")
    ap.add_argument("--start", help="Explicit start date YYYY-MM-DD (overrides --days).")
    ap.add_argument("--end", help="Explicit end date YYYY-MM-DD.")
    ap.add_argument("--currency", default="", help="Currency code for offline mode (USD, INR...).")
    ap.add_argument("--json", action="store_true", help="Print the result payload as JSON to stdout.")
    return ap


def resolve_window(args) -> tuple[str, str]:
    if args.start:
        return args.start, (args.end or (today() - dt.timedelta(days=1)).isoformat())
    return date_window(args.days)


def md_table(rows: Iterable[Iterable[Any]], header: list[str]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x).replace("|", "/") for x in r) + " |")
    return "\n".join(out)


def to_csv(rows: list[dict], cols: list[str] | None = None) -> str:
    if not rows:
        return ""
    cols = cols or list(rows[0].keys())
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="gads runtime self-check / helpers")
    ap.add_argument("cmd", choices=["whoami", "accounts", "profile-init", "profile-show", "rebuild-queue"])
    ap.add_argument("--customer", "-c")
    ap.add_argument("--dir")
    a = ap.parse_args()
    if a.cmd == "accounts":
        for c in accessible_customers():
            print(fmt_cid(c))
    elif a.cmd == "whoami":
        print(json.dumps(account_info(a.customer), indent=2))
    elif a.cmd == "profile-init":
        prof = load_profile(a.customer)
        print(f"wrote {save_profile(a.customer, prof)} (edit brand_terms, targets, budgets)")
    elif a.cmd == "profile-show":
        print(json.dumps(load_profile(a.customer), indent=2))
    elif a.cmd == "rebuild-queue":
        print(rebuild_queue(Path(a.dir)))
