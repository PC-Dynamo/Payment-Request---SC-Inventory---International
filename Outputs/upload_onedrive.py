#!/usr/bin/env python3
"""
upload_onedrive.py - back up the payment request register to OneDrive.

Every Tuesday and Thursday 17:00 AWST (after that day's pay run) the register
is written to

  General - Supply Chain / 1. Excel / Procurement Reports /
    Payment Request for Four-week Rolling Forecast - Claude.ai Latest.xlsx

and replaces the previous copy. Sheets:

  updated               the team's own register layout (same sheet name, header
                        text and column order as the manual workbook), so a
                        report built on that workbook can be pointed here
  App register          the app's view, incl. state, channel, payee, final flag
  Special Arrangements  the ACC and SC remark blocks
  Payment Terms         master-data terms in force

All lines are included - open, paid and requested - built from the current
Cin7 cache and everyone's edits at the time of the upload. No Cin7 call is made.

Uses the Microsoft Graph credentials the other SC apps mount (graph.env).

  python upload_onedrive.py            # build + upload
  python upload_onedrive.py --dry-run  # build only, keep the file in Outputs/
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build_report as BR  # noqa: E402
import ledger as L  # noqa: E402
import masters as M  # noqa: E402

NAME = "Payment Request for Four-week Rolling Forecast - Claude.ai Latest.xlsx"
SP_FOLDER = os.environ.get("SP_UPLOAD_FOLDER", "General/1. Excel/Procurement Reports")


def build(path: Path) -> tuple[Path, int]:
    payload = BR.build_payload()
    rows, when = BR.merged(payload)
    meta = {**payload["meta"], **when, "_terms_table": payload["terms_table"],
            "_remarks": L.get_remarks()}
    return BR.write_register_xlsx(rows, meta, path), len(rows)


def upload(path: Path) -> str:
    import requests
    client = M._graph_client()
    if not client:
        raise SystemExit(f"no Graph credentials at {M.GRAPH_ENV} - upload skipped")
    H, drive_id = client
    target = urllib.parse.quote(f"{SP_FOLDER}/{NAME}")
    r = requests.put(f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{target}:/content",
                     headers={**H, "Content-Type": "application/octet-stream"},
                     data=path.read_bytes(), timeout=300)
    if not r.ok:
        raise SystemExit(f"upload failed {r.status_code}: {r.text[:300]}")
    j = r.json()
    return f"{j.get('name')} ({j.get('size', 0):,} bytes) -> {SP_FOLDER}; modified {j.get('lastModifiedDateTime')}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="build the workbook, do not upload")
    a = ap.parse_args()
    path, n = build(HERE / NAME)
    print(f"built {path.name}: {n} lines")
    if a.dry_run:
        return 0
    print("uploaded", upload(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
