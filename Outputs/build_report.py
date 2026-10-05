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


def build_payload() -> dict:
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    ov = load_overrides()
    terms = S.load_payment_terms()
    cal_path = HERE / "calibration.json"
    calibration = json.loads(cal_path.read_text(encoding="utf-8")) if cal_path.exists() else {}
    auto, stats = P.auto_lines(cache, terms.get("payment_terms") or [], ov,
                               calibration, L.all_line_states())
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
            "cin7_url_template": ov.get("cin7_url_template") or "",
            "states": STATES,
            "statuses": L.STATUSES,
            "currencies": L.CURRENCIES,
            "charge_types": L.CHARGE_TYPES,
        },
        "auto_lines": auto,
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
    return P.merge(payload["auto_lines"], L.all_line_states(), L.all_manual(), today,
                   payload["meta"].get("forecast_weeks", 4),
                   payload.get("po_index") or {}, payload.get("branch_state") or {},
                   payload.get("branch_name") or {})


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
    ("Comment", "comment", 34, None),
    ("Container Number", "container_number", 16, None),
    ("%", "pct", 6, None),
    ("Payment Terms", "terms_rule", 40, None),
    ("Data notes", "_flags", 36, None),
]


def write_xlsx(rows: list[dict], meta: dict, path: Path, subtitle: str = "") -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Payment Request"
    navy = "1B2A4A"
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

    totals = {"_usd": 0.0, "_aud": 0.0, "_gbp": 0.0, "_eur": 0.0, "paid_amount": 0.0}
    r = hdr
    for row in rows:
        r += 1
        cur = row.get("currency") or "USD"
        amt = float(row.get("amount") or 0)
        vals = {f"_{c.lower()}": (amt if cur == c else None) for c in ("USD", "AUD", "GBP", "EUR")}
        vals["_flags"] = "; ".join(row.get("flags") or [])
        vals["_final"] = "" if row.get("status") in ("Paid", "Not Payable") else (
            "Yes" if row.get("finalised") else "No")
        for k in totals:
            totals[k] += (vals.get(k) if k.startswith("_") else row.get(k)) or 0
        for i, (_, key, _, kind) in enumerate(COLUMNS, 1):
            v = vals.get(key) if key.startswith("_") else row.get(key)
            if kind == "date" and v:
                v = dt.date.fromisoformat(str(v)[:10])
            if key == "status" and row.get("status_assumed"):
                v = "Paid (assumed)"
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

    # Payment terms in force, so the workbook explains its own due dates.
    t = wb.create_sheet("Payment Terms")
    t.append(["Supplier (master-data)", "Terms", "Deposit %", "Due days", "Basis"])
    for c in t[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=navy)
    for row in meta.get("_terms_table") or []:
        t.append([row.get("supplier_name"), row.get("terms"), row.get("deposit_pct"),
                  row.get("due_days"), row.get("basis")])
    for col, w in zip("ABCDE", (40, 80, 10, 10, 14)):
        t.column_dimensions[col].width = w

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp.xlsx")
    wb.save(tmp)
    tmp.replace(path)
    return path


def xlsx_name(day: dt.date | None = None) -> str:
    day = day or dt.datetime.now(AWST).date()
    return f"{TITLE} {day.strftime('%d-%m-%Y')}.xlsx"


def main() -> int:
    payload = build_payload()
    tmp = PAYLOAD.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tmp.replace(PAYLOAD)
    rows, when = merged(payload)
    meta = {**payload["meta"], **when, "_terms_table": payload["terms_table"]}
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
    if s["no_terms"]:
        print(f"no payment terms in master-data for: {', '.join(s['no_terms'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
