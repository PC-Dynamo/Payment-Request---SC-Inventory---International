#!/usr/bin/env python3
"""
compare_workbook.py - the app's pay runs against the team's payment workbook.

For the Excel + app parallel run: for each pay run, the lines the app requests
(For Payment / Requested / Forecast on that date) and the workbook rows with that payment
request date are matched by PO(s) and currency, and their amounts compared.
Read-only: nothing is imported or changed.

  python compare_workbook.py --from-sharepoint      # the live workbook; next USD + AUD runs
  python compare_workbook.py --file team.xlsx --run 2026-10-27 --run 2026-10-29
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build_report as BR  # noqa: E402
import seed_from_reference as SR  # noqa: E402


def compare(rows: list[dict], book_rows: list[dict], run: str) -> tuple[int, list[str], int, int]:
    app, xl = {}, {}
    for r in rows:
        # A later run's lines are still "Forecast" until it is the next run.
        if r.get("request_date") == run and r["status"] in ("For Payment", "Requested", "Forecast"):
            app.setdefault((tuple(sorted(r.get("po_list") or [r["po"]])), r["currency"]), []).append(r)
    for r in book_rows:
        if r["req"] == run and r["status"]:
            amt, cur = SR._money(r)
            xl.setdefault((tuple(sorted(r["pos"])) or (r["po_text"],), cur or "?"), []).append(r)
    ok, out = 0, []
    for key in sorted(set(app) | set(xl), key=str):
        a, x = app.get(key, []), xl.get(key, [])
        asum = round(sum(float(r["amount"] or 0) for r in a), 2)
        xsum = round(sum(SR._money(r)[0] or 0 for r in x), 2)
        if a and x and abs(asum - xsum) < 0.01:
            ok += 1
            continue
        out.append(f"  {'/'.join(key[0])[:40]:41} {key[1]:4} app {asum:>11,.2f} ({len(a)})  workbook {xsum:>11,.2f} ({len(x)})  "
                   + "; ".join(f"{r['pay_type']}|{r['status']}" for r in a)[:60]
                   + ("  || " + "; ".join(r["status"].replace("\n", " / ")[:30] for r in x) if x else ""))
    return ok, out, sum(map(len, app.values())), sum(map(len, xl.values()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", help="team workbook (.xlsx)")
    ap.add_argument("--from-sharepoint", action="store_true", help="download the live workbook first (server)")
    ap.add_argument("--run", action="append", help="pay-run date YYYY-MM-DD (repeatable; default: next USD + AUD runs)")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in Perth)")
    a = ap.parse_args()
    if not (a.file or a.from_sharepoint):
        ap.error("give --file or --from-sharepoint")
    book = SR.fetch_team_workbook() if a.from_sharepoint else Path(a.file)
    today = dt.date.fromisoformat(a.today) if a.today else None
    rows, when = BR.merged(json.loads(BR.PAYLOAD.read_text(encoding="utf-8")), today)
    book_rows = SR.load_reference(book)
    print(f"workbook: {book.name} - {len(book_rows)} rows; app built "
          f"{json.loads(BR.PAYLOAD.read_text(encoding='utf-8'))['meta'].get('report_built_awst')}")
    for run in a.run or [when["next_request"], when["next_request_aud"]]:
        ok, diffs, n_app, n_book = compare(rows, book_rows, run)
        print(f"pay run {run}: {ok} match, {len(diffs)} differ (app {n_app} lines, workbook {n_book} rows)")
        print("\n".join(diffs) if diffs else "  (no differences)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
