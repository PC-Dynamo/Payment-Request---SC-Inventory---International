#!/usr/bin/env python3
"""
selftest.py - checks the rules and the built data. Exit 1 on any failure.

  python selftest.py          # rules + checks on the current cache/payload
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ledger as L  # noqa: E402
import payments as P  # noqa: E402
import terms as T  # noqa: E402

FAILS: list[str] = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL", msg)


def D(s):
    return dt.date.fromisoformat(s)


def test_request_days():
    # 2026-09-25 is a Friday, 09-29 Tuesday, 09-30 Wednesday
    check(P.next_request_day(D("2026-09-25")) == D("2026-09-25"), "Friday is a request day")
    check(P.next_request_day(D("2026-09-26")) == D("2026-09-29"), "Saturday -> next Tuesday")
    check(P.next_request_day(D("2026-09-30")) == D("2026-10-02"), "Wednesday -> Friday")
    check(P.request_day_for(D("2026-10-08"), D("2026-09-25")) == D("2026-10-06"),
          "due Thu 8 Oct -> requested Tue 6 Oct")
    check(P.request_day_for(D("2026-10-09"), D("2026-09-25")) == D("2026-10-09"),
          "due on a Friday -> that Friday")
    check(P.request_day_for(D("2026-01-01"), D("2026-09-25")) == D("2026-09-25"),
          "overdue -> next request day, never dropped")


def test_po_list():
    check(P.po_list("PO 128830 - Impulse \nPO-127734 / PO128830") == ["PO-128830", "PO-127734"],
          "po_list parses and de-duplicates mixed PO spellings")
    check(P.po_list("PI-2026-1") == [], "po_list ignores short numbers")


def test_terms():
    cases = [
        ("Full payment - should be made at least one week before the shipment arrives", None, 0, "eta", -7),
        ("20% Deposit, 80% against copy of shipping docs", "20", 20, "etd", 7),
        ("45 days from the sailing day", None, 0, "etd", 45),
        ("100% payment within 14 days of container receipt", "100", 0, "eta", 14),
        ("30% deposit,  Balance payment when products arrieved Lisbon, Portugal port.", "30", 30, "eta", 0),
        ("100% advance payment", "100", 0, "order", 0),
        ("30% T/T before mass production,70% at sight at B/L", None, 30, "etd", 7),
        ("40% Deposit, 60% to be paid before booking the shipping space", "40", 40, "etd", -7),
        ("30% Deposit, 70% final payment - Paid 7 days after shipment", "30", 30, "etd", 7),
    ]
    for text, dep, want_dep, anchor, off in cases:
        r = T.parse_terms({"terms": text, "deposit_pct": dep,
                           "due_days": 45 if "sailing" in text else (14 if "14 days" in text else
                                                                     (7 if "7 days after" in text else None)),
                           "basis": ""})
        check((r["deposit_pct"], r["balance_anchor"], r["balance_offset"]) == (want_dep, anchor, off),
              f"terms {text[:40]!r} -> {r['deposit_pct'], r['balance_anchor'], r['balance_offset']}")
    check(T.parse_terms(None)["source"] == "default", "missing terms fall back to a default")

    book = T.TermsBook([{"supplier_name": "Nantong Kylin"}, {"supplier_name": "Qingdao CSP Industry and Trade Co.,ltd"},
                        {"supplier_name": "Shandong VOG Sports Products Co Ltd"}, {"supplier_name": "FDF"}])
    check(book.match("NANTONG RUIMING SUPPLY CHAIN CO.,LTD.") is None, "a shared city name is not a match")
    check(book.match("SHANDONG CROSSMAX FITNESS CO.,LTD") is None, "Crossmax is not VOG")
    check((book.match("FDF Limited") or {}).get("supplier_name") == "FDF", "FDF Limited -> FDF")
    check((book.match("Qingdao CSP Industry and Trade Co.,ltd") or {}).get("supplier_name")
          == "Qingdao CSP Industry and Trade Co.,ltd", "exact name matches")


def test_merge():
    base = {"source": "auto", "po": "PO-10001", "po_list": ["PO-10001"], "vendor": "X", "state": "WA",
            "segment": "Retail", "currency": "USD", "flags": [], "assumed_paid": None}
    auto = [
        {**base, "line_id": "PO-10001|deposit", "kind": "deposit", "amount_auto": 30.0, "due_auto": "2026-09-01"},
        {**base, "line_id": "PO-10001|balance", "kind": "balance", "amount_auto": 70.0, "due_auto": "2026-11-20"},
        {**base, "line_id": "PO-2|full", "po": "PO-2", "kind": "full", "amount_auto": 5.0, "due_auto": None},
        {**base, "line_id": "PO-3|deposit", "po": "PO-3", "kind": "deposit", "amount_auto": 9.0,
         "due_auto": "2026-01-01", "assumed_paid": "deposit assumed paid"},
    ]
    states = {"PO-10001|balance": {"amount": 71.5, "status": None}}
    manual = [{"id": 1, "po_refs": "PO 10001", "pay_type": "GST Payment", "currency": "AUD", "amount": 10.0,
               "due_date": None, "status": None}]
    rows, when = P.merge(auto, states, manual, D("2026-09-25"), 4)
    by = {r["line_id"]: r for r in rows}
    check(by["PO-10001|deposit"]["status"] == "For Payment" and by["PO-10001|deposit"]["overdue"],
          "overdue deposit is For Payment and flagged")
    check(by["PO-10001|deposit"]["request_date"] == "2026-09-25", "overdue line joins the next request")
    check(by["PO-10001|balance"]["status"] == "Forecast" and by["PO-10001|balance"]["amount"] == 71.5,
          "future balance is Forecast and keeps the amount override")
    check(not by["PO-10001|balance"]["in_window"], "20 Nov is outside the 4-week window")
    check(by["PO-2|full"]["status"] == "Date TBC", "no ETA -> Date TBC")
    check(by["PO-3|deposit"]["status"] == "Paid" and by["PO-3|deposit"]["status_assumed"],
          "assumed-paid deposit")
    check(by["M1"]["status"] == "For Payment" and by["M1"]["state"] == "WA",
          "manual line with no due date goes in this request and inherits the PO state")
    check(by["M1"]["this_request"], "manual line is in this request batch")


def test_ledger():
    with tempfile.TemporaryDirectory() as tmp:
        L.DB = Path(tmp) / "t.db"
        L.set_line_state("PO-1|full", {"status": "Paid", "paid_date": "2026-09-25"}, "t")
        check(L.all_line_states()["PO-1|full"]["status"] == "Paid", "line state stored")
        L.set_line_state("PO-1|full", {"status": ""}, "t")
        check(L.all_line_states()["PO-1|full"]["status"] is None, "blank clears an override")
        for bad in ({"paid_date": "31/12/2026"}, {"status": "Maybe"}, {"amount": "abc"}):
            try:
                L.set_line_state("PO-1|full", bad, "t")
                check(False, f"invalid value accepted: {bad}")
            except ValueError:
                pass
        mid = L.add_manual({"pay_type": "Freight Payment", "amount": "1,200"}, "t")
        m = L.all_manual()[0]
        check(m["currency"] == "USD" and m["amount"] == 1200.0, "freight defaults to USD, amount parsed")
        check(L.add_manual({"pay_type": "Cartage", "amount": 1}, "t", seed_key="k") is not None and
              L.add_manual({"pay_type": "Cartage", "amount": 1}, "t", seed_key="k") is None,
              "seed key imports a line once")
        L.delete_manual(mid, "t")
        check(all(x["id"] != mid for x in L.all_manual()), "deleted line hidden")
        check(len(L.recent_audit()) >= 5, "every change is audited")
    L.DB = HERE / "ledger.db"


def test_data():
    cache_p, payload_p = HERE / "payment_cache.json", HERE / "report_payload.json"
    if not cache_p.exists() or not payload_p.exists():
        print("  (no cache/payload yet - data checks skipped)")
        return
    cache = json.loads(cache_p.read_text(encoding="utf-8"))
    payload = json.loads(payload_p.read_text(encoding="utf-8"))
    auto = payload["auto_lines"]
    open_pos = {r for r, p in cache["pos"].items()
                if p["is_open"] and p["stage_raw"] not in P.BULK_STAGES}
    listed = {ln["po"] for ln in auto}
    check(open_pos == listed, f"every open PO has payment lines ({len(open_pos ^ listed)} differ)")
    by_po: dict[str, float] = {}
    for ln in auto:
        by_po[ln["po"]] = by_po.get(ln["po"], 0) + ln["pct"]
        check(ln["amount_auto"] >= 0, f"{ln['line_id']} negative amount")
        check(ln["state"] in ("NSW", "VIC", "QLD", "WA", "SA", ""), f"{ln['line_id']} bad state {ln['state']}")
        check(ln["segment"] in ("Retail", "Commercial"), f"{ln['line_id']} bad segment")
    check(all(abs(v - 100) < 1e-6 for v in by_po.values()), "each PO's milestones add to 100%")
    check(len({ln["line_id"] for ln in auto}) == len(auto), "line ids are unique")
    no_state = [ln["po"] for ln in auto if not ln["state"]]
    check(not no_state, f"POs with no state: {no_state[:5]}")
    wm = cache["meta"].get("po_watermark")
    check(bool(wm), "incremental watermark is set")
    rows, _ = P.merge(auto, {}, [], dt.date.today(), 4, payload.get("po_index"), payload.get("branch_state"))
    check(all(r["request_date"] is None or dt.date.fromisoformat(r["request_date"]).weekday() in (1, 4)
              for r in rows), "every request date is a Tuesday or Friday")


def test_uat_rules():
    """Rules added after the 6 Oct 2026 UAT."""
    import update_cache as U
    check(U._iso("2026-10-24T17:30:00Z") == "2026-10-25", "Cin7 UTC timestamp -> Perth date")
    check(U._iso("2026-10-24T03:00:00Z") == "2026-10-24", "morning UTC stays the same Perth day")
    rule = T.parse_terms({"terms": "20% Deposit, 80% against copy of shipping docs", "deposit_pct": "20"})
    cal = {"deposit_rate": 0.33, "deposit_pos": 36, "anchor": "eta", "offset_days": -9, "n": 51}
    r2 = P._calibrated(rule, cal)
    check(r2["deposit_pct"] == 0 and r2["balance_anchor"] == "eta" and r2["balance_offset"] == -9,
          "rarely-used deposit dropped, timing from history")
    check(P._calibrated(rule, {"deposit_rate": 0.9, "deposit_pos": 10})["deposit_pct"] == 20,
          "a deposit the team still uses is kept")
    po = {"is_open": True, "stage_raw": "New", "is_approved": False, "supplier": "X", "po_date": "2026-10-01",
          "eta_cin7": "2026-12-01", "value_usd": 100.0, "usd_exact": True, "ship": {}, "state": "WA"}
    lines, st = P.auto_lines({"pos": {"PO-50001": po}}, [], {}, {}, {})
    rows, _ = P.merge(lines, {}, [], D("2026-10-05"), 4)
    check(all(r["status"] == "Awaiting Approval" and not r["in_window"] for r in rows),
          "unapproved New PO is listed but never requested")
    po2 = dict(po, stage_raw="On Its Way", is_approved=True)
    lines, _ = P.auto_lines({"pos": {"PO-50002": po2}}, [], {}, {}, {})
    rows, _ = P.merge(lines, {"PO-50002|balance": {"status": "Requested", "request_date": "2026-10-06",
                                                    "amount": 99.0}}, [], D("2026-10-05"), 4)
    full = [r for r in rows if r["kind"] == "full"][0]
    check(full["status"] == "Requested" and full["amount"] == 99.0,
          "a record on the old balance line carries over to the single full payment")


def main() -> int:
    for fn in (test_request_days, test_po_list, test_terms, test_merge, test_ledger, test_uat_rules, test_data):
        print(fn.__name__)
        fn()
    print(f"\n{'FAILED' if FAILS else 'OK'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
