#!/usr/bin/env python3
"""
payments.py - open POs + payment terms -> payment lines; ledger merged on top.

Two steps, deliberately separate:

  auto_lines(cache, ...)   Cin7-derived. One line per payment milestone of every
                           open international PO (deposit / balance / full).
                           Rebuilt on each refresh (Tue + Fri 07:00 AWST).
  merge(auto, ledger, day) Today-dependent. Applies what people recorded, works
                           out each line's payment-request date and status, and
                           adds the manual (AUD) lines. Runs on every page load,
                           so the list is right on a Wednesday as well.

Payment requests go out on the refresh days, Tuesday and Friday. A line is
requested on the last request day on or before it falls due - or on the next
request day if that has already passed (overdue lines are never dropped).
"""
from __future__ import annotations

import datetime as dt
import re

import masters as M
import terms as T

REQUEST_WEEKDAYS = (1, 4)                 # Tuesday, Friday
# Bulk orders sitting in China are the General Planning app's flow: no vessel,
# no ETA, and the shipment POs later allocated from them are not paid twice.
BULK_STAGES = {"Production-China Stock", "In Stock China Warehouse"}
# By these stages a pre-shipment payment has necessarily been made.
SHIPPED_STAGES = {"On Its Way", "Dispatched"}


def _d(s) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _s(d: dt.date | None) -> str | None:
    return d.isoformat() if d else None


def next_request_day(day: dt.date) -> dt.date:
    """Today if today is a request day, else the next Tuesday/Friday."""
    for i in range(7):
        c = day + dt.timedelta(days=i)
        if c.weekday() in REQUEST_WEEKDAYS:
            return c
    return day


def request_day_for(due: dt.date, today: dt.date) -> dt.date:
    """The last request day on/before `due`, but never before the next one."""
    nxt = next_request_day(today)
    for i in range(7):
        c = due - dt.timedelta(days=i)
        if c.weekday() in REQUEST_WEEKDAYS:
            return max(c, nxt)
    return nxt


def container_parts(raw: str) -> tuple[str, str]:
    """'FFAU1303365 (40HC) / TEMU0598351 (20GP)' -> ('40HC, 20GP', 'FFAU1303365, TEMU0598351')"""
    raw = raw or ""
    nums = re.findall(r"\b([A-Z]{4}\d{6,7})\b", raw.upper())
    sizes = re.findall(r"\b(20GP|40GP|40HC|40HQ|40NOR|40HICUBE|45HC|LCL)\b", raw.upper())
    return ", ".join(dict.fromkeys(sizes)), ", ".join(dict.fromkeys(nums))


def po_list(text: str) -> list[str]:
    """'PO 128830 - Impulse / PO-127734' -> ['PO-128830', 'PO-127734']"""
    return [f"PO-{n}" for n in dict.fromkeys(re.findall(r"(?<!\d)(\d{5,6})(?!\d)", text or ""))]


# ------------------------------------------------------------------ auto lines
def auto_lines(cache: dict, terms_rows: list[dict], overrides: dict) -> tuple[list[dict], dict]:
    book = T.TermsBook(terms_rows, overrides.get("term_aliases"))
    special = {M._key(k): v for k, v in (overrides.get("special_arrangements") or {}).items()}
    transit = overrides.get("transit_weeks") or {}
    lines: list[dict] = []
    stats = {"open_pos": 0, "bulk_excluded": 0, "no_terms": set(), "no_eta": 0}

    for ref, po in sorted(cache.get("pos", {}).items()):
        if not po.get("is_open"):
            continue
        if po.get("stage_raw") in BULK_STAGES:
            stats["bulk_excluded"] += 1
            continue
        stats["open_pos"] += 1
        ship = po.get("ship") or {}
        trow = book.match(po["supplier"])
        sp = special.get(M._key((trow or {}).get("supplier_name") or po["supplier"]))
        rule = T.parse_terms(trow, sp)
        if not trow and not sp:
            stats["no_terms"].add(po["supplier"])

        eta_port = _d(ship.get("eta_shipper"))
        eta_cin7 = _d(po.get("eta_cin7")) or _d(ship.get("eta"))
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
        if etd_est and rule["balance_anchor"] == "etd":
            flags.append(f"ETD estimated (ETA - {transit.get(po.get('state') or '', 5)} wks)")
        if po.get("unmapped_skus"):
            flags.append(f"{len(po['unmapped_skus'])} SKU(s) not in master")

        base = {
            "source": "auto",
            "po": ref,
            "po_list": [ref],
            "cin7_id": po.get("cin7_id"),
            "vendor": po["supplier"],
            "terms_supplier": (trow or {}).get("supplier_name"),
            "branch": po.get("branch"),
            "state": po.get("state") or "",
            "segment": po.get("segment") or "Retail",
            "segment_mixed": po.get("segment_mixed"),
            "stage": po.get("stage"),
            "stage_raw": po.get("stage_raw"),
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
        mile = []
        if dep:
            mile.append(("deposit", f"Deposit {dep:g}%", dep, "order", 0))
            mile.append(("balance", f"Balance {100 - dep:g}%", 100 - dep,
                         rule["balance_anchor"], rule["balance_offset"]))
        else:
            mile.append(("full", "Full payment 100%", 100.0,
                         rule["balance_anchor"], rule["balance_offset"]))
        for kind, label, pct, anchor, offset in mile:
            a = anchors.get(anchor)
            due = a + dt.timedelta(days=offset) if a else None
            # What must already have been paid, given how far the PO has got.
            assumed = None
            if kind == "deposit" and po.get("past_deposit_stage"):
                assumed = f"deposit assumed paid - PO is {po.get('stage')}"
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
            })
    stats["no_terms"] = sorted(stats["no_terms"])
    return lines, stats


# ----------------------------------------------------------------------- merge
def _po_context(auto: list[dict]) -> dict[str, dict]:
    ctx = {}
    for ln in auto:
        ctx.setdefault(ln["po"], ln)
    return ctx


def merge(auto: list[dict], states: dict[str, dict], manual: list[dict],
          today: dt.date, forecast_weeks: int = 4, po_index: dict | None = None,
          branch_state: dict | None = None,
          branch_name: dict | None = None) -> tuple[list[dict], dict]:
    nxt = next_request_day(today)
    horizon = nxt + dt.timedelta(weeks=forecast_weeks)
    out: list[dict] = []

    def finish(row: dict, due: dt.date | None, st: dict) -> dict:
        req = _d(st.get("request_date"))
        status = st.get("status")
        if not status:
            if row.get("assumed_paid"):
                status = "Paid"
                row["status_assumed"] = True
            elif req is not None:
                status = "For Payment" if req <= nxt else "Forecast"
            elif due is None:
                status = "Date TBC"
            else:
                status = "For Payment" if request_day_for(due, today) <= nxt else "Forecast"
        if req is None and status not in ("Paid", "Not Payable", "Date TBC"):
            req = request_day_for(due, today) if due else nxt
        row.update({
            "status_stored": st.get("status") or "",
            "request_stored": st.get("request_date") or "",
            "status": status,
            "due_date": _s(due),
            "request_date": _s(req),
            "paid_date": st.get("paid_date"),
            "comment": st.get("comment") or "",
            "updated_by": st.get("updated_by"),
            "updated_at": st.get("updated_at"),
            "overdue": bool(due and due < today and status in ("For Payment", "Requested", "On Hold")),
            "in_request": bool(status == "For Payment" and req and req <= nxt),
            # The whole batch for the next request day: still to send + already sent.
            "this_request": bool((status == "For Payment" and req and req <= nxt)
                                 or (status == "Requested" and req == nxt)),
            "in_window": bool(status in ("For Payment", "Requested", "On Hold", "Forecast")
                              and req and req <= horizon),
        })
        return row

    for ln in auto:
        st = states.get(ln["line_id"], {})
        row = dict(ln)
        row["currency"] = st.get("currency") or ln["currency"]
        row["amount"] = st["amount"] if st.get("amount") is not None else ln["amount_auto"]
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
        out.append(finish(row, due, st))

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
              "paid_date": m.get("paid_date"), "comment": m.get("comment"),
              "updated_by": m.get("updated_by") or m.get("created_by"),
              "updated_at": m.get("updated_at") or m.get("created_at")}
        due = _d(m.get("due_date"))
        if due is None and not st["status"]:
            st["status"] = "For Payment"          # no due date given: pay in this request
            st["request_date"] = st["request_date"] or _s(nxt)
        out.append(finish(row, due, st))

    for r in out:
        r["container_size"], r["container_number"] = container_parts(r.get("container") or "")
    out.sort(key=lambda r: (r.get("request_date") or "9999", r.get("due_date") or "9999",
                            r.get("vendor") or "", r.get("po") or ""))
    return out, {"today": _s(today), "next_request": _s(nxt), "horizon": _s(horizon),
                 "forecast_weeks": forecast_weeks}
