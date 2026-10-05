#!/usr/bin/env python3
"""
calibrate_from_reference.py - learn WHEN the team actually pays each supplier.

Master-data terms say what the contract allows ("80% against B/L copy"); the
team's payment request workbook shows what really happens. UAT on the 6 Oct
2026 pay run showed the two differ: most balances are requested ~10 days before
the forwarder ETA whatever the contract says, and many suppliers that "take a
deposit" have not been paid one on most recent POs.

For every supplier with enough history this writes, to calibration.json:

  deposit_rate    share of its POs since DEPOSIT_SINCE that had a deposit row
  anchor/offset   the balance / full payment request date measured against ETD
                  or forwarder ETA - whichever the team's dates track more
                  tightly (lower median absolute deviation) - and the median gap
  n               rows behind each figure

Supplier names in the workbook are matched to Cin7 supplier names from the
cache, so the result is keyed the same way as the payment lines.

  python calibrate_from_reference.py [--file workbook.xlsx]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import masters as M  # noqa: E402
import seed_from_reference as S  # noqa: E402
import terms as T  # noqa: E402

OUT = HERE / "calibration.json"
CACHE = HERE / "payment_cache.json"
DEPOSIT_SINCE = "2026-04-01"      # deposit habits changed; only recent POs count
MIN_TIMING_ROWS = 5
MIN_DEPOSIT_POS = 4


def _days(a, b):
    if not a or not b:
        return None
    return (dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days


def _mad(xs):
    m = statistics.median(xs)
    return statistics.median(abs(x - m) for x in xs)


def load_rows(path: Path) -> list[dict]:
    """The supplier payment rows (not freight / duty) with their dates."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["updated"]
    it = ws.iter_rows(values_only=True)
    hdr = [re.sub(r"\s+", " ", str(h or "")).strip().lower() for h in next(it)]

    def col(prefix):
        return next(i for i, h in enumerate(hdr) if h.startswith(prefix))

    ix = {"sup": col("supplier name"), "po": col("po number"), "st": col("payment status"),
          "req": col("payment request date"), "entry": col("entry date"), "etd": col("etd"),
          "etaf": col("eta (forwarder)"), "etac": col("eta (cin7)")}
    out = []
    for r in it:
        if not r or not any(r):
            continue
        g = lambda k: r[ix[k]] if ix[k] < len(r) else None  # noqa: E731
        status = str(g("st") or "").strip().lower()
        if not status or S._charge_type(status):
            continue
        pos = re.findall(r"(?<!\d)(\d{5,6})(?!\d)", str(g("po") or ""))
        if len(pos) != 1:
            continue
        out.append({"sup": S._txt(g("sup")), "po": f"PO-{pos[0]}", "status": status,
                    "req": S._date(g("req")), "entry": S._date(g("entry")),
                    "etd": S._date(g("etd")), "etaf": S._date(g("etaf"))})
    wb.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", help="reference workbook (default: newest in Reference/)")
    a = ap.parse_args()
    ref = Path(a.file) if a.file else max((HERE.parent / "Reference").glob("Payment Request*.xlsx"),
                                          key=lambda p: p.stat().st_mtime)
    rows = load_rows(ref)

    # Map workbook supplier names (and PO numbers, which are exact) to Cin7 names.
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    po_sup = {ref_: r["supplier"] for ref_, r in cache["pos"].items()}
    book = T.TermsBook([{"supplier_name": s} for s in sorted(set(po_sup.values()))])
    groups: dict[str, list[dict]] = {}
    unmatched = set()
    for r in rows:
        name = po_sup.get(r["po"]) or (book.match(r["sup"]) or {}).get("supplier_name")
        if not name:
            unmatched.add(r["sup"])
            continue
        groups.setdefault(name, []).append(r)

    out = {}
    for name, rs in sorted(groups.items()):
        rec: dict = {"supplier": name}
        recent = {r["po"] for r in rs if (r["entry"] or "") >= DEPOSIT_SINCE}
        dep = {r["po"] for r in rs if "deposit" in r["status"] and r["po"] in recent}
        if len(recent) >= MIN_DEPOSIT_POS:
            rec["deposit_rate"] = round(len(dep) / len(recent), 2)
            rec["deposit_pos"] = len(recent)
        bal = [r for r in rs if "deposit" not in r["status"]]
        best = None
        for anchor, field in (("etd", "etd"), ("eta", "etaf")):
            xs = [d for d in (_days(r["req"], r[field]) for r in bal) if d is not None and -120 < d < 120]
            if len(xs) >= MIN_TIMING_ROWS:
                cand = (_mad(xs), anchor, int(statistics.median(xs)), len(xs))
                if best is None or cand[0] < best[0]:
                    best = cand
        if best:
            rec.update({"anchor": best[1], "offset_days": best[2], "n": best[3], "mad": best[0]})
        if len(rec) > 1:
            out[M._key(name)] = rec

    payload = {"built": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "source": ref.name,
               "deposit_since": DEPOSIT_SINCE, "suppliers": out,
               "unmatched_names": sorted(unmatched)[:50]}
    OUT.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    for k, v in out.items():
        print(f"{v['supplier'][:38]:39} dep {v.get('deposit_rate', '-'):>4} ({v.get('deposit_pos', 0):2})  "
              f"{v.get('anchor', '-'):>3} {v.get('offset_days', ''):>4}  n={v.get('n', 0):3} mad={v.get('mad', '')}")
    print(f"-> {OUT.name}: {len(out)} suppliers; {len(unmatched)} workbook names not matched to Cin7")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
