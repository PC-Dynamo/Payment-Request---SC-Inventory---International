#!/usr/bin/env python3
"""
build_report.py - payment_cache.json -> report_payload.json + the Excel output.

The payload holds the Cin7-derived auto lines only; the web app merges the
ledger (people's edits and the manual AUD lines) on every page load. The
workbook written here is a snapshot of the merged list at build time:

  Outputs/Payment Request - SC Inventory - International DD-MM-YYYY.xlsx
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ledger as L  # noqa: E402
import payments as P  # noqa: E402
import sources as S  # noqa: E402

CACHE = HERE / "payment_cache.json"
PAYLOAD = HERE / "report_payload.json"
OVERRIDES = HERE / "overrides.json"
AWST = dt.timezone(dt.timedelta(hours=8))
STATES = ["NSW", "VIC", "QLD", "WA", "SA"]
TITLE = "Payment Request - SC Inventory - International"


def load_overrides() -> dict:
    try:
        return json.loads(OVERRIDES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _branch_maps() -> tuple[dict[str, str], dict[str, str]]:
    """Branch spelling (lower) -> state code, and -> the tidy branch name,
    from the Supplier and Branch master ("MALAGA" -> "Malaga Branch +WA DC")."""
    try:
        import masters as M
        m = M.load_masters()
        return ({k: v for k, v in m["branch_state"].items() if v},
                {k: v for k, v in m["branch_name"].items() if v})
    except Exception:  # noqa: BLE001 - a missing master only costs the fallback
        return {}, {}


def ledger_id(ln: dict, states: dict) -> str:
    """Which ledger record an auto line uses: a 'full' line may still use the
    record made on its old 'balance' line (see payments.merge)."""
    if ln["line_id"] not in states and ln.get("kind") == "full" \
            and f"{ln['po']}|deposit" not in states and f"{ln['po']}|balance" in states:
        return f"{ln['po']}|balance"
    return ln["line_id"]


def effective_state(ln: dict, states: dict) -> dict:
    """The ledger record behind an auto line."""
    return states.get(ledger_id(ln, states)) or {}


def is_payment_record(st: dict) -> bool:
    """A real payment record (sent, paid or written off) - worth keeping forever."""
    return bool(st and (st.get("status") in ("Paid", "Requested", "Not Payable")
                        or st.get("paid_date") or st.get("paid_amount") is not None))


def closed_archive(snaps: list[dict], auto: list[dict], cache: dict) -> list[dict]:
    """Archived lines whose PO is no longer open in Cin7 (open POs show live)."""
    live = {ln["po"] for ln in auto}
    out = []
    for snap in snaps:
        if snap.get("po") in live:
            continue
        c = (cache.get("pos") or {}).get(snap.get("po")) or {}
        snap = dict(snap)
        snap.update({"archived": True, "assumed_paid": None, "awaiting_approval": False,
                     "stage": c.get("stage") or "Closed in Cin7"})
        snap["flags"] = [x for x in snap.get("flags") or [] if "closed in Cin7" not in x] + [
            "PO closed in Cin7 - kept from the payment record"]
        out.append(snap)
    return out


def build_payload() -> dict:
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    ov = load_overrides()
    terms = S.load_payment_terms()
    cal_path = HERE / "calibration.json"
    calibration = json.loads(cal_path.read_text(encoding="utf-8")) if cal_path.exists() else {}
    states = L.all_line_states()
    auto, stats = P.auto_lines(cache, terms.get("payment_terms") or [], ov, calibration, states)
    L.archive_lines([ln for ln in auto if is_payment_record(effective_state(ln, states))])
    # A planned request date recorded before plans kept their due date (or on a
    # line that has just become a deposit) is anchored to today's due date.
    dues = {}
    for ln in auto:
        lid = ledger_id(ln, states)
        dues[lid] = (states.get(lid) or {}).get("due_date") or ln.get("due_auto")
    L.backfill_plan_due(dues)
    archived = closed_archive(L.archived_snapshots(), auto, cache)
    stats["archived"] = len(archived)
    meta = cache.get("meta", {})
    branch_state, branch_name = _branch_maps()
    now = dt.datetime.now(AWST)
    return {
        "meta": {
            "title": TITLE,
            "report_built_awst": now.strftime("%Y-%m-%d %H:%M AWST"),
            "cache_built_awst": meta.get("generated_awst"),
            "cache_mode": meta.get("mode"),
            "cin7_calls": meta.get("cin7_calls"),
            "po_watermark": meta.get("po_watermark"),
            "shipment": meta.get("shipment"),
            "master": meta.get("master"),
            "master_counts": meta.get("master_counts"),
            "terms": {**(meta.get("terms") or {}),
                      "snapshot_generated": terms.get("snapshot_generated"),
                      "fetched_utc": terms.get("fetched_utc")},
            "unmapped_skus": meta.get("unmapped_skus") or [],
            "unknown_suppliers": meta.get("unknown_suppliers") or [],
            "stats": stats,
            "calibration": {k: calibration.get(k) for k in ("built", "source", "deposit_since")},
            "run_log": meta.get("run_log") or [],
            "forecast_weeks": int(ov.get("forecast_weeks") or 4),
            "pay_weekdays": {k.upper(): [int(x) for x in v]
                             for k, v in (ov.get("pay_weekdays") or {}).items()} or None,
            "cin7_url_template": ov.get("cin7_url_template") or "",
            "states": STATES,
            "statuses": L.STATUSES,
            "currencies": L.CURRENCIES,
            "paid_currencies": L.PAID_CURRENCIES,
            "charge_types": L.CHARGE_TYPES,
        },
        "auto_lines": auto,
        # Paid / requested lines of POs Cin7 no longer lists as open.
        "archived_lines": archived,
        # Every cached PO, open or recently received, so a manual charge line
        # that names a PO inherits its branch, state and channel.
        "po_index": {ref: {"branch": r.get("branch"), "state": r.get("state"),
                           "supplier": r.get("supplier"),
                           "segment": r.get("segment"), "stage": r.get("stage"),
                           "cin7_id": r.get("cin7_id"),
                           "forwarder": (r.get("ship") or {}).get("forwarder") or "",
                           "etd": (r.get("ship") or {}).get("etd"),
                           "eta_port": (r.get("ship") or {}).get("eta_shipper"),
                           "eta_cin7": r.get("eta_cin7"),
                           "container": (r.get("ship") or {}).get("container_raw")
                           or r.get("container_cin7") or ""}
                     for ref, r in cache.get("pos", {}).items()},
        "branch_state": branch_state,
        "branch_name": branch_name,
        "terms_table": [
            {k: r.get(k) for k in ("supplier_name", "terms", "deposit_pct", "due_days", "basis")}
            for r in (terms.get("payment_terms") or []) if (r.get("supplier_type") or "") != "Local"],
        "special_arrangements": ov.get("special_arrangements") or {},
    }


def merged(payload: dict, today: dt.date | None = None) -> tuple[list[dict], dict]:
    today = today or dt.datetime.now(AWST).date()
    lines = list(payload["auto_lines"]) + list(payload.get("archived_lines") or [])
    return P.merge(lines, L.all_line_states(), L.all_manual(), today,
                   payload["meta"].get("forecast_weeks", 4),
                   payload.get("po_index") or {}, payload.get("branch_state") or {},
                   payload.get("branch_name") or {}, payload["meta"].get("pay_weekdays"))


def filter_rows(rows: list[dict], state: str = "", segment: str = "",
                window: str = "window") -> list[dict]:
    out = []
    for r in rows:
        if state and r.get("state") != state:
            continue
        if segment and r.get("segment") != segment:
            continue
        if window == "request" and not r.get("this_request"):
            continue
        if window == "window" and not r.get("in_window"):
            continue
        if window == "open" and r.get("status") in ("Paid", "Not Payable"):
            continue
        out.append(r)
    return out


# ------------------------------------------------------------------- workbook
COLUMNS = [       # the team's payment request register, in its column order
    ("Entry Date", "entry_date", 11, "date"),
    ("Vendor Entity Name", "vendor_entity", 30, None),
    ("Supplier Name", "supplier", 28, None),
    ("PO Number", "po", 16, None),
    ("Document / Invoice No.", "invoice_no", 18, None),
    ("Branch", "branch", 22, None),
    ("State", "state", 7, None),
    ("Retail / Commercial", "segment", 11, None),
    ("Stage", "stage", 16, None),
    ("ETD", "etd", 11, "date"),
    ("ETA (Forwarder)", "eta_port", 11, "date"),
    ("ETA (CIN7)", "eta_cin7", 11, "date"),
    ("Container Size", "container_size", 11, None),
    ("Payment Type", "pay_type", 18, None),
    ("Payment Status", "status", 13, None),
    ("Finalised", "_final", 9, None),
    ("Payee", "payee", 10, None),
    ("Payment Request Date", "request_date", 12, "date"),
    ("Due Date", "due_date", 11, "date"),
    ("Accounts Scheduled Payment Date", "acc_scheduled_date", 13, "date"),
    ("Value (USD)", "_usd", 13, "money"),
    ("Value (AUD)", "_aud", 13, "money"),
    ("Value (GBP)", "_gbp", 12, "money"),
    ("Value (EUR)", "_eur", 12, "money"),
    ("Paid Date", "paid_date", 11, "date"),
    ("Paid Amount", "paid_amount", 13, "money"),
    ("Paid Currency", "paid_currency", 9, None),
    ("Paid Amount (AUD)", "paid_amount_aud", 13, "money"),
    ("Comment", "comment", 34, None),
    ("Container Number", "container_number", 16, None),
    ("%", "pct", 6, None),
    ("Payment Terms", "terms_rule", 40, None),
    ("Data notes", "_flags", 36, None),
]


NAVY = "1B2A4A"
PEACH = "FCE4D6"

# The team's own register ("updated" sheet of the payment request workbook):
# same sheet name, same header text (quirks included), same column order, so a
# report built on that workbook can be pointed at the app's backup instead.
TEAM_COLUMNS = [
    ("Entry Date", "entry_date", "date"), ("Vendor Entity Name", "vendor_entity", None),
    ("Supplier Name", "supplier", None), ("PO Number", "po", None),
    ("Document / Invoice No.", "invoice_no", None), ("Branch", "branch", None),
    ("Stage", "stage", None), ("ETD", "etd", "date"), ("ETA\n(Forwarder)", "eta_port", "date"),
    ("ETA (CIN7)", "eta_cin7", "date"), ("Container Size", "container_size", None),
    ("Payment Status/Type", "_status_type", None), ("Payment Request Date", "request_date", "date"),
    ("Due Date ", "due_date", "date"), ("Accounts Scheduled Payment Date", "acc_scheduled_date", "date"),
    ("Value (USD)", "_usd", "money"), ("Value (AUD)", "_aud", "money"), ("Value (GBP)", "_gbp", "money"),
    (" Value (EUR) ", "_eur", "money"), ("Paid Date", "paid_date", "date"),
    ("Paid Amount", "paid_amount", "money"), ("Comment", "comment", None),
    ("Container Number", "container_number", None), ("%", "_pct", "pct"),
    # Added for Accounts (26 Oct 2026) after the team's columns, so a report
    # reading the workbook's columns by position is not shifted.
    ("Paid Currency", "paid_currency", None), ("Paid Amount (AUD)", "paid_amount_aud", "money"),
]


def status_type(row: dict) -> str:
    """The team's 'Payment Status/Type' wording for a line."""
    st = row.get("status")
    if st == "Not Payable" and row.get("allocated_from") and row.get("status_assumed"):
        return f"Allocated from {row['allocated_from']}"      # the team's own wording
    if st == "Not Payable":
        return "Not payable"
    if row.get("source") == "manual":
        return row.get("pay_type") or "Other"
    paid = st == "Paid"
    kind = row.get("kind")
    pct = row.get("pct")
    if kind == "deposit":
        txt = f"{pct:g}% deposit {'paid' if paid else 'for payment'}" if pct else (
            "Deposit paid" if paid else "Deposit for payment")
    elif kind == "balance":
        txt = "Balance paid" if paid else "Balance for payment"
    else:
        txt = "Paid" if paid else "For Payment"
    if row.get("status_assumed"):
        txt += " (assumed)"
    return txt


def _cell_values(row: dict) -> dict:
    cur = row.get("currency") or "USD"
    amt = float(row.get("amount") or 0)
    vals = {f"_{c.lower()}": (amt if cur == c else None) for c in ("USD", "AUD", "GBP", "EUR")}
    vals["_flags"] = "; ".join(row.get("flags") or [])
    vals["_final"] = "" if row.get("status") in ("Paid", "Not Payable") else (
        "Yes" if row.get("finalised") else "No")
    vals["_status_type"] = status_type(row)
    vals["_pct"] = (row["pct"] / 100.0) if row.get("pct") is not None else None
    return vals


def _write_cell(ws, r, i, v, kind, border=None):
    if kind == "date" and v:
        v = dt.date.fromisoformat(str(v)[:10])
    c = ws.cell(row=r, column=i, value=v)
    if border is not None:
        c.border = border
    if kind == "date":
        c.number_format = "DD/MM/YYYY"
    elif kind == "money":
        c.number_format = "#,##0.00"
    elif kind == "pct":
        c.number_format = "0%"
    return c


def _sheet_team_register(ws, rows: list[dict]) -> None:
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    for i, (label, _, _) in enumerate(TEAM_COLUMNS, 1):
        c = ws.cell(row=1, column=i, value=label)
        c.font = Font(bold=True)
    widths = (11, 30, 26, 14, 18, 22, 16, 11, 11, 11, 10, 22, 12, 11, 13, 13, 13, 12, 12, 11, 13, 34, 16, 6, 9, 13)
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for r, row in enumerate(rows, 2):
        vals = _cell_values(row)
        for i, (_, key, kind) in enumerate(TEAM_COLUMNS, 1):
            _write_cell(ws, r, i, vals.get(key) if key.startswith("_") else row.get(key), kind)
        if row.get("not_final"):
            for i in range(1, len(TEAM_COLUMNS) + 1):
                ws.cell(row=r, column=i).fill = PatternFill("solid", fgColor=PEACH)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(TEAM_COLUMNS))}{max(2, len(rows) + 1)}"


def _sheet_remarks(wb, meta: dict) -> None:
    from openpyxl.styles import Alignment, Font
    rm = meta.get("_remarks") or {}
    if not rm:
        return
    ws = wb.create_sheet("Special Arrangements")
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 110
    ws["A1"] = "Special arrangements"
    ws["A1"].font = Font(size=13, bold=True, color=NAVY)
    r = 3
    for block in ("acc", "sc"):
        b = rm.get(block) or {}
        ws.cell(row=r, column=1, value=b.get("label") or block).font = Font(bold=True)
        c = ws.cell(row=r, column=2, value=b.get("text") or "")
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[r].height = max(30, 15 * (1 + (b.get("text") or "").count("\n")))
        ws.cell(row=r + 1, column=2, value=f"Last edited by {b.get('updated_by')} · {b.get('updated_at')}"
                ).font = Font(size=9, color="6B7688")
        r += 3


def _sheet_terms(wb, meta: dict) -> None:
    from openpyxl.styles import Font, PatternFill
    t = wb.create_sheet("Payment Terms")
    t.append(["Supplier (master-data)", "Terms", "Deposit %", "Due days", "Basis"])
    for c in t[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=NAVY)
    for row in meta.get("_terms_table") or []:
        t.append([row.get("supplier_name"), row.get("terms"), row.get("deposit_pct"),
                  row.get("due_days"), row.get("basis")])
    for col, w in zip("ABCDE", (40, 80, 10, 10, 14)):
        t.column_dimensions[col].width = w


def _save(wb, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp.xlsx")
    wb.save(tmp)
    tmp.replace(path)
    return path


def write_register_xlsx(rows: list[dict], meta: dict, path: Path) -> Path:
    """The OneDrive backup: the team's register layout first, then the app's view."""
    from openpyxl import Workbook
    wb = Workbook()
    _sheet_team_register(wb.active, rows)
    wb.active.title = "updated"
    _sheet_app_register(wb.create_sheet("App register"), rows, meta,
                        "every line the app holds, incl. paid")
    _sheet_remarks(wb, meta)
    _sheet_terms(wb, meta)
    return _save(wb, path)


def write_xlsx(rows: list[dict], meta: dict, path: Path, subtitle: str = "") -> Path:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Payment Request"
    _sheet_app_register(ws, rows, meta, subtitle)
    _sheet_remarks(wb, meta)
    _sheet_terms(wb, meta)
    return _save(wb, path)


def _sheet_app_register(ws, rows: list[dict], meta: dict, subtitle: str = "") -> None:
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    navy = NAVY
    thin = Side(style="thin", color="E3E7EE")
    ws["A1"] = TITLE
    ws["A1"].font = Font(size=14, bold=True, color=navy)
    ws["A2"] = (f"Next request {meta.get('next_request')} · built {meta.get('report_built_awst')}"
                f"{' · ' + subtitle if subtitle else ''}")
    ws["A2"].font = Font(size=9, color="6B7688")
    hdr = 4
    for i, (label, _, width, _) in enumerate(COLUMNS, 1):
        c = ws.cell(row=hdr, column=i, value=label)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=navy)
        c.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[hdr].height = 30

    totals = {"_usd": 0.0, "_aud": 0.0, "_gbp": 0.0, "_eur": 0.0, "paid_amount": 0.0, "paid_amount_aud": 0.0}
    r = hdr
    for row in rows:
        r += 1
        vals = _cell_values(row)
        for k in totals:
            totals[k] += (vals.get(k) if k.startswith("_") else row.get(k)) or 0
        for i, (_, key, _, kind) in enumerate(COLUMNS, 1):
            v = vals.get(key) if key.startswith("_") else row.get(key)
            if kind == "date" and v:
                v = dt.date.fromisoformat(str(v)[:10])
            if key == "status" and row.get("status_assumed"):
                v = "Not payable (allocated)" if row.get("status") == "Not Payable" else "Paid (assumed)"
            c = ws.cell(row=r, column=i, value=v)
            c.border = Border(bottom=thin)
            if kind == "date":
                c.number_format = "DD/MM/YYYY"
            elif kind == "money":
                c.number_format = "#,##0.00"
        if row.get("not_final"):            # the team's peach = not finalised yet
            for i in range(1, len(COLUMNS) + 1):
                ws.cell(row=r, column=i).fill = PatternFill("solid", fgColor="FCE4D6")
        if row.get("overdue"):
            ws.cell(row=r, column=2).font = Font(color="A11B1B", bold=True)
    r += 1
    ws.cell(row=r, column=1, value="Total").font = Font(bold=True)
    for i, (_, key, _, _) in enumerate(COLUMNS, 1):
        if key in totals:
            c = ws.cell(row=r, column=i, value=round(totals[key], 2))
            c.font = Font(bold=True)
            c.number_format = "#,##0.00"
    ws.freeze_panes = ws.cell(row=hdr + 1, column=5)
    ws.auto_filter.ref = f"A{hdr}:{get_column_letter(len(COLUMNS))}{max(hdr + 1, r - 1)}"


def xlsx_name(day: dt.date | None = None) -> str:
    day = day or dt.datetime.now(AWST).date()
    return f"{TITLE} {day.strftime('%d-%m-%Y')}.xlsx"


def write_payload() -> dict:
    """Rebuild the auto lines from the cache and the ledger (no Cin7 call)."""
    payload = build_payload()
    tmp = PAYLOAD.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tmp.replace(PAYLOAD)
    return payload


def main() -> int:
    payload = write_payload()
    rows, when = merged(payload)
    meta = {**payload["meta"], **when, "_terms_table": payload["terms_table"],
            "_remarks": L.get_remarks()}
    window = filter_rows(rows, window="window")
    out = write_xlsx(window, meta, HERE / xlsx_name(),
                     f"{payload['meta']['forecast_weeks']}-week rolling forecast, all states")
    s = payload["meta"]["stats"]
    req = [r for r in rows if r["in_request"]]
    usd = sum(r["amount"] for r in req if r["currency"] == "USD")
    aud = sum(r["amount"] for r in req if r["currency"] == "AUD")
    print(f"payload -> {PAYLOAD.name} ({len(payload['auto_lines'])} auto lines, "
          f"{s['open_pos']} open POs, {s['bulk_excluded']} China-stock bulk POs excluded)")
    print(f"xlsx    -> {out.name} ({len(window)} lines in the "
          f"{payload['meta']['forecast_weeks']}-week window)")
    print(f"request {when['next_request']}: {len(req)} lines, USD {usd:,.2f}, AUD {aud:,.2f}")
    if s.get("archived"):
        print(f"kept {s['archived']} paid / requested lines of POs Cin7 no longer lists as open")
    if s["no_terms"]:
        print(f"no payment terms in master-data for: {', '.join(s['no_terms'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
