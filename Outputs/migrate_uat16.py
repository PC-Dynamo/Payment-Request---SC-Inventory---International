#!/usr/bin/env python3
"""
migrate_uat16.py - one-off ledger migration for the 16 Oct 2026 UAT fixes.
Run once on the server, after the new code is deployed and BEFORE build_report.py.

  1. plan_due for request dates planned before the app kept the due date a plan
     was made against: the line's due date in the app's own dated export, the
     earliest export given first (13-10-2026 = just after the 13 Oct workbook
     sync), so a PO that slipped since (PO-135508 / PO-119793) moves with its
     plan. Lines in no export are anchored to today's due date by the build.
  2. Healthland PO-142053 / PO-142054: "Deposit 30%" was typed on the single
     full-payment line on 16 Oct; the record moves to the deposit line, so the
     PO shows the deposit and the 70% balance.

  python migrate_uat16.py "Payment Request - SC Inventory - International 13-10-2026.xlsx" [...]
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ledger as L  # noqa: E402
import payments as P  # noqa: E402

RELABELLED = {"PO-142053": "muhammad.r@dynamofitness.com.au", "PO-142054": "muhammad.r@dynamofitness.com.au"}


def export_dues(path: Path) -> dict[str, str]:
    """{ledger line id: due date} from the 'Payment Request' sheet of an app export."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    hdr, out = None, {}
    for r in wb["Payment Request"].iter_rows(values_only=True):
        if hdr is None:
            if r and "PO Number" in r:
                hdr = {h: i for i, h in enumerate(r) if h}
            continue
        po, typ, due = r[hdr["PO Number"]], str(r[hdr["Payment Type"]] or "").lower(), r[hdr["Due Date"]]
        if not po or not due:
            continue
        due = due.date().isoformat() if hasattr(due, "date") else str(due)[:10]
        kind = ("deposit" if "deposit" in typ else "balance" if typ.startswith("balance")
                else "full" if typ.startswith("full") else None)
        if kind:
            out.setdefault(f"{po}|{kind}", due)
            if kind == "full":                  # a full line may still use its old balance record
                out.setdefault(f"{po}|balance", due)
    wb.close()
    return out


def main() -> int:
    dues: dict[str, str] = {}
    for p in sys.argv[1:]:
        for k, v in export_dues(Path(p)).items():
            dues.setdefault(k, v)
    print(f"plan_due from {len(sys.argv) - 1} export(s): {L.backfill_plan_due(dues)} planned lines anchored")
    for po, who in RELABELLED.items():
        st = L.stored(f"{po}|full")
        if st and P.deposit_pct_of(st.get("pay_type")) is not None:
            ok = L.split_full_line(po, f"migration 17 Oct: deposit typed on the full line by {who}")
            print(f"{po}: full payment -> deposit + balance {'done' if ok else 'skipped (deposit record exists)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
