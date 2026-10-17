#!/usr/bin/env python3
"""
payments.py - open POs + payment terms -> payment lines; ledger merged on top.

Two steps, deliberately separate:

  auto_lines(cache, ...)   Cin7-derived. One line per payment milestone of every
                           open international PO (deposit / balance / full).
                           Rebuilt on each refresh (Mon + Wed 13:00 AWST).
  merge(auto, ledger, day) Today-dependent. Applies what people recorded, works
                           out each line's payment-request date and status, and
                           adds the manual (AUD) lines. Runs on every page load,
                           so the list is right on a Wednesday as well.

Pay runs: Tuesday for supplier payments and USD freight, Thursday for AUD
charges. A line joins the last pay run on or before it falls due - or the next
run if that has already passed (overdue lines are never dropped).
"""
from __future__ import annotations

import datetime as dt
import re

import masters as M
import terms as T

# Pay runs, as the team actually runs them (payment workbook, Jul-Oct 2026):
# supplier payments and USD freight go in the TUESDAY run (339 of 342 rows),
# AUD document / handling charges in the THURSDAY run (230 of 238 rows).
# The app's data refresh (Mon + Wed 13:00 AWST, the afternoon before each run)
# is a separate schedule.
PAY_WEEKDAYS = {"USD": (1,), "GBP": (1,), "EUR": (1,), "AUD": (3,)}
DEFAULT_PAY_WEEKDAYS = (1,)
# Bulk orders sitting in China are the General Planning app's flow: no vessel,
# no ETA, and the shipment POs later allocated from them are not paid twice.
BULK_STAGES = {"Production-China Stock", "In Stock China Warehouse"}
# By these stages a pre-shipment payment has necessarily been made.
SHIPPED_STAGES = {"On Its Way", "Dispatched"}
# The team requests a deposit about this many days after the PO is approved
# (median 5-6 days in the payment workbook). A deposit on an approved PO older
# than DEPOSIT_ASSUME_DAYS is taken as paid unless someone records otherwise.
DEPOSIT_LAG_DAYS = 6
DEPOSIT_ASSUME_DAYS = 21
# Below this share of recent POs with a deposit, a supplier is treated as
# "full payment" by default (UAT 6 Oct 2026: Kpower 33%, Great Century 0%).
DEPOSIT_RATE_MIN = 0.5
# A request date someone planned (in the app or the team's workbook) was set
# against the due date of that day. When the PO slips by more than a week -
# its ETA moved in Cin7 or the forwarder report - the plan moves with it
# (UAT 16 Oct 2026: PO-135508 / PO-119793 kept 20 Oct after their ETA moved
# four weeks; the team moved them to 17 Nov).
REPLAN_MIN_DAYS = 7


def _d(s) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _s(d: dt.date | None) -> str | None:
    return d.isoformat() if d else None


def weekdays_for(currency: str | None, pay_weekdays: dict | None = None) -> tuple:
    table = pay_weekdays or PAY_WEEKDAYS
    return tuple(table.get((currency or "USD").upper(), table.get("USD", DEFAULT_PAY_WEEKDAYS)))


def next_request_day(day: dt.date, weekdays: tuple = DEFAULT_PAY_WEEKDAYS) -> dt.date:
    """Today if today is a pay-run day, else the next one."""
    for i in range(7):
        c = day + dt.timedelta(days=i)
        if c.weekday() in weekdays:
            return c
    return day


def request_day_for(due: dt.date, today: dt.date, weekdays: tuple = DEFAULT_PAY_WEEKDAYS) -> dt.date:
    """The last pay-run day on/before `due`, but never before the next one."""
    nxt = next_request_day(today, weekdays)
    for i in range(7):
        c = due - dt.timedelta(days=i)
        if c.weekday() in weekdays:
            return max(c, nxt)
    return nxt


def container_parts(raw: str) -> tuple[str, str]:
    """'FFAU1303365 (40HC) / TEMU0598351 (20GP)' -> ('40HC, 20GP', 'FFAU1303365, TEMU0598351')"""
    raw = raw or ""
    nums = re.findall(r"\b([A-Z]{4}\d{6,7})\b", raw.upper())
    sizes = re.findall(r"\b(20GP|40GP|40HC|40HQ|40NOR|40HICUBE|45HC|LCL)\b", raw.upper())
    return ", ".join(dict.fromkeys(sizes)), ", ".join(dict.fromkeys(nums))


# Who is paid. Rizqi (UAT): keep factory invoices apart from forwarder /
# customs broker invoices (Mainfreight, Ice Cargo).
FORWARDER_TYPES = {"Freight Payment", "Customs Brokerage", "GST Payment", "Government Duty",
                   "Cartage", "Local Cartage Delivery", "Detention Charges", "Document Charge",
                   "Handling Charge"}


def payee_of(row: dict) -> str:
    if row.get("source") == "auto" or row.get("pay_type") == "Supplier Payment":
        return "Supplier"
    return "Forwarder" if row.get("pay_type") in FORWARDER_TYPES else "Other"


def po_list(text: str) -> list[str]:
    """'PO 128830 - Impulse / PO-127734' -> ['PO-128830', 'PO-127734']"""
    return [f"PO-{n}" for n in dict.fromkeys(re.findall(r"(?<!\d)(\d{5,6})(?!\d)", text or ""))]


# ------------------------------------------------------------------ auto lines
def _calibrated(rule: dict, cal: dict | None) -> dict:
    """Apply what the team's own payment history says to the master-data rule.
    Percentages stay from master-data; whether a deposit is used, and when the
    balance is requested, follow practice where there is enough history."""
    if not cal:
        return rule
    rule = dict(rule)
    notes = []
    rate = cal.get("deposit_rate")
    if rate is not None and rate < DEPOSIT_RATE_MIN and rule["deposit_pct"]:
        notes.append(f"no deposit by default - only {rate:.0%} of {cal.get('deposit_pos')} recent POs had one")
        rule["deposit_pct"] = 0.0
    elif rate is not None and rate >= DEPOSIT_RATE_MIN and not rule["deposit_pct"] and cal.get("deposit_pct"):
        rule["deposit_pct"] = float(cal["deposit_pct"])
        notes.append(f"{cal['deposit_pct']:g}% deposit - taken on {rate:.0%} of {cal.get('deposit_pos')} recent POs")
    # Paid in advance on the PO date: there is no shipping date to measure the
    # team's habit against, so history does not move it.
    if cal.get("anchor") and cal.get("n", 0) >= 5 and rule["balance_anchor"] != "order":
        off = int(cal["offset_days"])
        where = "ETD" if cal["anchor"] == "etd" else "forwarder ETA"
        rule["balance_anchor"], rule["balance_offset"] = cal["anchor"], off
        notes.append(f"requested {abs(off)} days {'before' if off <= 0 else 'after'} {where} "
                     f"(team history, {cal['n']} payments)")
    if notes:
        dep = rule["deposit_pct"]
        head = f"{dep:g}% deposit after approval; {100 - dep:g}% balance" if dep else "100%"
        rule["rule"] = f"{head} - " + "; ".join(notes)
        rule["source"] = f"{rule['source']} + team history"
    return rule


def _evidence(st: dict) -> bool:
    """A record that proves this payment line exists as recorded."""
    return bool(st.get("status") in ("Paid", "Requested") or st.get("paid_date")
                or st.get("paid_amount") is not None or st.get("amount") is not None
                or st.get("invoice_no") or st.get("request_date"))


def deposit_pct_of(pay_type) -> float | None:
    """'Deposit 30%' -> 30.0; 0.0 for a deposit with no percentage; None if the
    payment type is not a deposit at all."""
    text = str(pay_type or "")
    if "deposit" not in text.lower():
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    pct = float(m.group(1)) if m else 0.0
    return pct if 0 < pct < 100 else 0.0


def auto_lines(cache: dict, terms_rows: list[dict], overrides: dict,
               calibration: dict | None = None, states: dict | None = None) -> tuple[list[dict], dict]:
    book = T.TermsBook(terms_rows, overrides.get("term_aliases"))
    cal_by = {k: v for k, v in ((calibration or {}).get("suppliers") or {}).items()}
    # Per PO, what people already recorded decides the split: a recorded
    # deposit/balance keeps two lines, a recorded full payment keeps one.
    recorded: dict[str, set] = {}
    for lid, st in (states or {}).items():
        po_ref, _, kind = lid.partition("|")
        # A deposit line someone typed as "Deposit x%" (relabelling a full
        # payment - Healthland, UAT 16 Oct) is a deposit even with nothing else on it.
        if not _evidence(st) and not (kind == "deposit" and deposit_pct_of(st.get("pay_type")) is not None):
            continue          # e.g. only the "Final" box ticked: says nothing about the split
        recorded.setdefault(po_ref, set()).add(kind)
    special = {M._key(k): v for k, v in (overrides.get("special_arrangements") or {}).items()}
    transit = overrides.get("transit_weeks") or {}
    lines: list[dict] = []
    stats = {"open_pos": 0, "bulk_excluded": 0, "no_terms": set(), "no_eta": 0,
             "awaiting_approval": 0, "calibrated": 0}

    # Suppliers whose BULK order takes the deposit, later allocated to shipment
    # POs (K-Life). Kpower / Iron Master / Bodylonger / Imbell bulk orders need
    # no payment, so their China-stock POs stay out of the list.
    bulk_dep = {M._key(x) for x in overrides.get("bulk_deposit_suppliers") or []}
    bulk_dep_book = T.TermsBook([{"supplier_name": x} for x in overrides.get("bulk_deposit_suppliers") or []])
    is_bulk_dep = lambda name: M._key(name) in bulk_dep or bool(bulk_dep_book.match(name))  # noqa: E731
    open_bulk = {}
    for ref, po in cache.get("pos", {}).items():
        if po.get("is_open") and po.get("stage_raw") in BULK_STAGES and is_bulk_dep(po["supplier"]):
            open_bulk.setdefault(M._key(po["supplier"]), []).append(ref)
    stats["bulk_included"] = 0

    for ref, po in sorted(cache.get("pos", {}).items()):
        if not po.get("is_open"):
            continue
        bulk = po.get("stage_raw") in BULK_STAGES
        if bulk and not is_bulk_dep(po["supplier"]):
            stats["bulk_excluded"] += 1
            continue
        if bulk:
            stats["bulk_included"] += 1
        stats["open_pos"] += 1
        ship = po.get("ship") or {}
        trow = book.match(po["supplier"])
        sp = special.get(M._key((trow or {}).get("supplier_name") or po["supplier"]))
        rule = T.parse_terms(trow, sp)
        if not sp:
            cal = cal_by.get(M._key(po["supplier"]))
            if cal:
                rule = _calibrated(rule, cal)
                stats["calibrated"] += 1
        if not trow and not sp:
            stats["no_terms"].add(po["supplier"])
        # Unapproved New POs are drafts in Cin7: nothing is requested on them.
        # (A bulk-order deposit is requested before Cin7 approval - the team
        # paid K-Life PO-135014's deposit while it was unapproved.)
        awaiting = po.get("stage_raw") in ("New", "NEW") and not po.get("is_approved") and not bulk
        if awaiting:
            stats["awaiting_approval"] += 1

        eta_port = _d(ship.get("eta_shipper"))
        eta_cin7 = _d(po.get("eta_cin7")) or _d(ship.get("eta"))
        # Monday's ETA updates reach Cin7 mostly before 13:00 (81% since Aug),
        # but the Shipment Schedule re-reads forwarder ETAs only at 14:00. This
        # app reads Cin7's own ETA live, so when that has moved since the
        # Shipment Schedule's last run, move the forwarder ETA by the same amount
        # (Cin7 ETA = forwarder ETA + a fixed buffer, usually 7 days). One day is
        # ignored: the two apps round Cin7's UTC timestamps differently.
        eta_shifted = 0
        ship_cin7 = _d(ship.get("eta"))
        if eta_port and eta_cin7 and ship_cin7 and abs((eta_cin7 - ship_cin7).days) > 1:
            eta_shifted = (eta_cin7 - ship_cin7).days
            eta_port = eta_port + (eta_cin7 - ship_cin7)
        eta_used = eta_port or eta_cin7
        etd = _d(ship.get("etd"))
        etd_est = False
        if not etd and eta_used:
            weeks = transit.get(po.get("state") or "", 5)
            etd, etd_est = eta_used - dt.timedelta(weeks=weeks), True
        anchors = {"order": _d(po.get("po_date")), "etd": etd, "eta": eta_used}
        if not eta_used:
            stats["no_eta"] += 1

        flags = []
        if rule["source"] == "default":
            flags.append("payment terms not in master-data")
        if not po.get("usd_exact"):
            flags.append(f"USD {po.get('usd_source')}")
        if not eta_port and eta_cin7:
            flags.append("no forwarder ETA - Cin7 ETA used")
        if eta_shifted:
            flags.append(f"forwarder ETA moved {eta_shifted:+d} days with Cin7's ETA "
                         "(Shipment Schedule not refreshed yet)")
        if etd_est and rule["balance_anchor"] == "etd":
            flags.append(f"ETD estimated (ETA - {transit.get(po.get('state') or '', 5)} wks)")
        if po.get("unmapped_skus"):
            flags.append(f"{len(po['unmapped_skus'])} SKU(s) not in master")
        review = ""
        if bulk:
            review = "Bulk order deposit - allocate it to the shipment POs drawn from this order"
        elif open_bulk.get(M._key(po["supplier"])) or is_bulk_dep(po["supplier"]):
            review = ("Check before paying the deposit: it may already be paid on the bulk order "
                      + ", ".join(sorted(open_bulk.get(M._key(po["supplier"]), [])) or "(bulk order)"))
        if review:
            flags.append(review)

        base = {
            "source": "auto",
            "po": ref,
            "po_list": [ref],
            "cin7_id": po.get("cin7_id"),
            "vendor": po["supplier"],
            "terms_supplier": (trow or {}).get("supplier_name"),
            # A bulk order is not stock for any one state until it is allocated.
            "branch": "Bulk" if bulk else po.get("branch"),
            "state": "" if bulk else (po.get("state") or ""),
            "segment": po.get("segment") or "Retail",
            "segment_mixed": po.get("segment_mixed"),
            "stage": "Bulk (China stock)" if bulk else po.get("stage"),
            "stage_raw": po.get("stage_raw"),
            "bulk": bulk,
            "entry_date": po.get("po_date"),
            "supplier_invoice": po.get("supplier_invoice"),
            "etd": _s(etd), "etd_estimated": etd_est,
            "eta_port": _s(eta_port), "eta_cin7": _s(eta_cin7),
            "container": ship.get("container_raw") or po.get("container_cin7") or "",
            "forwarder": ship.get("forwarder") or "",
            "delay_days": ship.get("delay_days"),
            "po_value_usd": po.get("value_usd"),
            "po_value_aud": po.get("value_aud"),
            "usd_exact": po.get("usd_exact"),
            "terms_text": rule.get("terms_text"),
            "terms_rule": rule["rule"],
            "terms_source": rule["source"],
            "flags": flags,
        }
        usd = float(po.get("value_usd") or 0)
        dep = float(rule["deposit_pct"] or 0)
        rec = recorded.get(ref, set())
        if "deposit" in rec:
            # A deposit was recorded on this PO: it keeps two lines whatever the
            # supplier's default, and a stale "full" record from an earlier sync
            # does not undo that. A balance alone proves nothing - the 25 Sep
            # seed filed plain "For Payment" rows under "balance".
            typed = deposit_pct_of(((states or {}).get(f"{ref}|deposit") or {}).get("pay_type"))
            if typed or not dep:
                dep = float(typed or T.parse_terms(trow, sp)["deposit_pct"]
                            or (cal_by.get(M._key(po["supplier"])) or {}).get("deposit_pct") or 30)
                base["terms_rule"] = (f"{dep:g}% deposit recorded for this PO; {100 - dep:g}% balance"
                                      + (" - " + rule["rule"].split(" - ", 1)[1] if " - " in rule["rule"] else ""))
        elif "full" in rec:
            if dep:
                base["terms_rule"] = "100% - recorded as one payment for this PO"
            dep = 0.0
        mile = []
        if bulk:
            # Only the deposit is paid on the bulk order; balances follow on
            # the shipment POs it is allocated to.
            dep = dep or float(T.parse_terms(trow, sp)["deposit_pct"] or 30)
            mile.append(("deposit", f"Bulk deposit {dep:g}%", dep, "order", DEPOSIT_LAG_DAYS))
        elif dep:
            mile.append(("deposit", f"Deposit {dep:g}%", dep, "order", DEPOSIT_LAG_DAYS))
            mile.append(("balance", f"Balance {100 - dep:g}%", 100 - dep,
                         rule["balance_anchor"], rule["balance_offset"]))
        if not bulk and not dep:
            mile.append(("full", "Full payment 100%", 100.0,
                         rule["balance_anchor"], rule["balance_offset"]))
        for kind, label, pct, anchor, offset in mile:
            a = anchors.get(anchor)
            due = a + dt.timedelta(days=offset) if a else None
            # What must already have been paid, given how far the PO has got.
            assumed = None
            po_age = (dt.date.today() - anchors["order"]).days if anchors["order"] else 999
            if kind == "deposit" and not bulk and po.get("past_deposit_stage") and (
                    po.get("stage_raw") in SHIPPED_STAGES or po_age > DEPOSIT_ASSUME_DAYS):
                assumed = f"deposit assumed paid - PO is {po.get('stage')}, raised {po_age} days ago"
            elif (po.get("stage_raw") in SHIPPED_STAGES and
                  (anchor == "order" or (anchor == "etd" and offset < 0))):
                assumed = f"pre-shipment payment assumed paid - PO is {po.get('stage')}"
            lines.append({
                **base,
                "line_id": f"{ref}|{kind}",
                "kind": kind,
                "pay_type": label,
                "pct": pct,
                "currency": "USD",
                "amount_auto": round(usd * pct / 100.0, 2),
                "due_auto": _s(due),
                "due_anchor": anchor,
                "assumed_paid": assumed,
                "awaiting_approval": awaiting,
                "review": review if (kind == "deposit" or bulk) else "",
            })
    stats["no_terms"] = sorted(stats["no_terms"])
    return lines, stats


# ----------------------------------------------------------------------- merge
def _po_context(auto: list[dict]) -> dict[str, dict]:
    ctx = {}
    for ln in auto:
        ctx.setdefault(ln["po"], ln)
    return ctx


def replanned(row: dict, st: dict, due: dt.date | None, today: dt.date,
              pay_weekdays: dict | None = None) -> dict:
    """The record with its planned request date moved by as much as the due date
    has moved since the plan was made (`plan_due`), if that is more than a week.
    A line already sent to Accounts, paid or written off keeps its date."""
    req, base = _d(st.get("request_date")), _d(st.get("plan_due"))
    if not (req and base and due) or st.get("status") in ("Requested", "Paid", "Not Payable"):
        return st
    shift = (due - base).days
    if abs(shift) <= REPLAN_MIN_DAYS:
        return st
    new = request_day_for(req + dt.timedelta(days=shift), today,
                          weekdays_for(row.get("currency"), pay_weekdays))
    row["replanned_from"] = _s(req)
    row["flags"] = [*(row.get("flags") or []),
                    f"request date moved from {req:%d/%m} to {new:%d/%m}: the due date moved "
                    f"{shift:+d} days after the request date was planned"]
    return {**st, "request_date": _s(new)}


def merge(auto: list[dict], states: dict[str, dict], manual: list[dict],
          today: dt.date, forecast_weeks: int = 4, po_index: dict | None = None,
          branch_state: dict | None = None,
          branch_name: dict | None = None,
          pay_weekdays: dict | None = None) -> tuple[list[dict], dict]:
    nxt = next_request_day(today, weekdays_for("USD", pay_weekdays))
    nxt_aud = next_request_day(today, weekdays_for("AUD", pay_weekdays))
    horizon = nxt + dt.timedelta(weeks=forecast_weeks)
    out: list[dict] = []

    def finish(row: dict, due: dt.date | None, st: dict) -> dict:
        wd = weekdays_for(row.get("currency"), pay_weekdays)
        nxt_l = next_request_day(today, wd)       # this line's next pay run
        req = _d(st.get("request_date"))
        status = st.get("status")
        if not status and row.get("awaiting_approval"):
            if req is None:
                status = "Awaiting Approval"
            else:
                # Scheduled by hand on a draft PO: Great Century's PI is paid in
                # advance and the PO approved in Cin7 when it is paid (UAT 16 Oct).
                row["flags"] = [*(row.get("flags") or []),
                                "draft PO in Cin7 (not approved yet) - in the pay run because a "
                                "request date was set; approve it when paying"]
        if not status:
            if row.get("assumed_paid"):
                status = "Paid"
                row["status_assumed"] = True
            elif req is not None:
                status = "For Payment" if req <= nxt_l else "Forecast"
            elif due is None:
                status = "Date TBC"
            else:
                status = "For Payment" if request_day_for(due, today, wd) <= nxt_l else "Forecast"
        if req is None and status not in ("Paid", "Not Payable", "Date TBC", "Awaiting Approval"):
            req = request_day_for(due, today, wd) if due else nxt_l
        carried = None
        if status == "For Payment" and req is not None and req < nxt_l:
            # Planned for a pay run that has passed and still not paid: it goes
            # in the next run (UAT: three POs still showed 6 Oct on 9 Oct).
            carried, req = req, nxt_l
        stored_final = st.get("finalised")
        if stored_final is None:
            # Default: confirmed once someone has entered the PI amount or invoice.
            final = bool(row.get("source") == "manual" or row.get("amount_overridden")
                         or row.get("invoice_no"))
        else:
            final = bool(stored_final)
        row.update({
            "finalised": final,
            "finalised_stored": stored_final,
            "payee": payee_of(row),
            "status_stored": st.get("status") or "",
            "request_stored": st.get("request_date") or "",
            "not_final": bool(not final and status not in ("Paid", "Not Payable", "Awaiting Approval")),
            "status": status,
            "due_date": _s(due),
            "request_date": _s(req),
            "paid_date": st.get("paid_date"),
            "comment": st.get("comment") or "",
            "updated_by": st.get("updated_by"),
            "updated_at": st.get("updated_at"),
            "overdue": bool(due and due < today and status in ("For Payment", "Requested", "On Hold")),
            "next_run": _s(nxt_l),
            "carried_from": _s(carried),
            "in_request": bool(status == "For Payment" and req and req <= nxt_l),
            # The whole batch for this line's next pay run: still to send + already sent.
            "this_request": bool((status == "For Payment" and req and req <= nxt_l)
                                 or (status == "Requested" and req == nxt_l)),
            "in_window": bool(status in ("For Payment", "Requested", "On Hold", "Forecast")
                              and req and req <= horizon),
        })
        return row

    # What was actually invoiced / paid as a deposit, per PO, so the balance is
    # the PO value less the real deposit rather than a fixed percentage of it.
    dep_actual = {}
    for ln in auto:
        if ln["kind"] == "deposit":
            st = states.get(ln["line_id"], {})
            v = st.get("paid_amount") if st.get("paid_amount") is not None else st.get("amount")
            if v is not None and (st.get("currency") or "USD") == "USD":
                dep_actual[ln["po"]] = float(v)
    for ln in auto:
        st = states.get(ln["line_id"], {})
        if not st and ln["kind"] == "full" and f"{ln['po']}|deposit" not in states:
            # Recorded against the old "balance" line before this PO became a
            # single payment: it is the same payment, so it carries over.
            st = states.get(f"{ln['po']}|balance", {})
        row = dict(ln)
        row["currency"] = st.get("currency") or ln["currency"]
        auto_amt = ln["amount_auto"]
        if ln["kind"] == "balance" and ln["po"] in dep_actual and ln.get("po_value_usd"):
            auto_amt = round(max(0.0, float(ln["po_value_usd"]) - dep_actual[ln["po"]]), 2)
            row["amount_basis"] = "PO value less the deposit recorded"
        row["amount"] = st["amount"] if st.get("amount") is not None else auto_amt
        row["amount_overridden"] = st.get("amount") is not None
        row["invoice_no"] = st.get("invoice_no") or ""
        row["pay_type"] = st.get("pay_type") or ln.get("pay_type", "")
        row["pay_type_overridden"] = bool(st.get("pay_type"))
        row["supplier"] = ln.get("vendor", "")
        row["vendor_entity"] = st.get("vendor_entity") or ln.get("vendor", "")
        row["vendor"] = row["vendor_entity"]
        row["acc_scheduled_date"] = st.get("acc_scheduled_date")
        row["paid_amount"] = st.get("paid_amount")
        due = _d(st.get("due_date")) or _d(ln["due_auto"])
        row["due_overridden"] = bool(st.get("due_date"))
        out.append(finish(row, due, replanned(row, st, due, today, pay_weekdays)))

    ctx = {**(po_index or {}), **_po_context(auto)}
    bstate = {M._key(k): v for k, v in (branch_state or {}).items()}
    bname = {M._key(k): v for k, v in (branch_name or {}).items()}
    for m in manual:
        pos = po_list(m.get("po_refs") or "")
        first = next((ctx[p] for p in pos if p in ctx), {})
        segs = {ctx[p].get("segment") for p in pos if p in ctx} - {None, ""}
        typed = m.get("branch") or ""
        tk = M._key(typed)
        tidy = bname.get(tk) or (next((v for k, v in sorted(bname.items())
                                      if len(tk) >= 5 and k.startswith(tk)), "") if tk else "")
        branch = tidy or typed or first.get("branch") or ""
        state = (m.get("state") or bstate.get(M._key(branch)) or first.get("state")
                 or next((v for k, v in bstate.items() if len(k) >= 5 and M._key(branch)
                          and k.startswith(M._key(branch))), ""))
        row = {
            "source": "manual",
            "line_id": f"M{m['id']}",
            "manual_id": m["id"],
            "kind": "charge",
            "po": m.get("po_refs") or "",
            "po_list": pos,
            "cin7_id": first.get("cin7_id"),
            "vendor": m.get("vendor") or first.get("forwarder") or "",
            "vendor_entity": m.get("vendor") or first.get("forwarder") or "",
            "pay_type": m.get("pay_type") or "Other",
            "branch": branch,
            "state": state,
            # A charge shared by Retail and Commercial POs is reported once,
            # under the segment given by hand or else its first PO.
            "segment": m.get("segment") or (first.get("segment") if segs else "") or "",
            "segment_mixed": len(segs) > 1,
            "stage": first.get("stage") or "",
            "entry_date": m.get("entry_date"),
            "etd": first.get("etd"), "eta_port": first.get("eta_port"),
            "eta_cin7": first.get("eta_cin7"), "container": first.get("container") or "",
            "forwarder": first.get("forwarder") or "",
            "terms_rule": "", "flags": [],
            "invoice_no": m.get("doc_no") or "",
            "supplier": ", ".join(dict.fromkeys(ctx[p].get("supplier") or ctx[p].get("vendor") or ""
                                                for p in pos if p in ctx) ).strip(", "),
            "acc_scheduled_date": m.get("acc_scheduled_date"),
            "paid_amount": m.get("paid_amount"),
            "pct": None,
            "currency": m.get("currency") or "AUD",
            "amount": m.get("amount") or 0.0,
            "created_by": m.get("created_by"),
        }
        st = {"status": m.get("status"), "request_date": m.get("request_date"),
              "finalised": m.get("finalised"),
              "paid_date": m.get("paid_date"), "comment": m.get("comment"),
              "updated_by": m.get("updated_by") or m.get("created_by"),
              "updated_at": m.get("updated_at") or m.get("created_at")}
        due = _d(m.get("due_date"))
        if due is None and not st["status"]:
            st["status"] = "For Payment"          # no due date given: pay in the next run
            st["request_date"] = st["request_date"] or _s(
                next_request_day(today, weekdays_for(row["currency"], pay_weekdays)))
        out.append(finish(row, due, st))

    for r in out:
        r["container_size"], r["container_number"] = container_parts(r.get("container") or "")
    out.sort(key=lambda r: (r.get("request_date") or "9999", r.get("due_date") or "9999",
                            r.get("vendor") or "", r.get("po") or ""))
    return out, {"today": _s(today), "next_request": _s(nxt), "next_request_aud": _s(nxt_aud),
                 "horizon": _s(horizon), "forecast_weeks": forecast_weeks}
