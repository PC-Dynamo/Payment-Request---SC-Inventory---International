#!/usr/bin/env bash
# Import the team's live payment request workbook (SharePoint, Supply Chain site)
# into the Payment Request app, during the Excel + app parallel run.
#
# Edits people made in the app are kept; lines still holding only workbook data
# follow the workbook (seed_from_reference.py --sync). Run on request:
#   /srv/team/sc/payment-request/sync-workbook.sh
# or once at a set time (UTC; 14:30 AWST = 06:30 UTC), e.g.:
#   systemd-run --on-calendar='2026-10-26 06:30:00 UTC' --unit=payreq-import-1430 \
#     /srv/team/sc/payment-request/sync-workbook.sh
set -uo pipefail
cd "$(dirname "$0")" || exit 1

LOG=sync.log
C=payment-request-app-1
say(){ echo "[$(date -u +%FT%TZ)] $*" >> "$LOG"; }

say "=== workbook import start ==="
mkdir -p backup
docker exec "$C" python -c "import sqlite3; s=sqlite3.connect('/app/Outputs/ledger.db'); d=sqlite3.connect('/app/Outputs/ledger.backup.tmp'); s.backup(d); d.close(); s.close()" \
  && mv -f Outputs/ledger.backup.tmp "backup/ledger_pre-import_$(TZ=Australia/Perth date +%Y-%m-%d_%H%M).db" \
  && say "ok: ledger backup" || { say "FAILED: ledger backup - nothing imported"; exit 1; }

for step in "seed_from_reference.py --sync --from-sharepoint" "build_report.py"; do
  # shellcheck disable=SC2086
  if ! docker exec "$C" python /app/Outputs/$step >> "$LOG" 2>&1; then
    say "FAILED: $step"
    exit 1
  fi
  say "ok: $step"
done
say "=== workbook import done ==="
