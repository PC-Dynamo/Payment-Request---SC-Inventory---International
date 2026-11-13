#!/usr/bin/env bash
# Scheduled refresh for the Payment Request - SC Inventory - International app.
#
# Friday 08:05 and Monday 12:05 AWST - after the team updates ETAs in Cin7; SC
# then updates the app by Monday 13:00 for the Tuesday pay run (Phoenix, 2 Nov
# 2026; was Mon + Wed 13:00).
# (The Shipment Schedule re-reads forwarder ETAs Mon + Thu 14:00; until then
#  the app shifts forwarder ETAs by any change in Cin7's own ETA.)
# Prod runs UTC and AWST = UTC+8, so 08:05 AWST Fri = 00:05 UTC Fri and
# 12:05 AWST Mon = 04:05 UTC Mon. Five past, not on the hour: po-gateway calls
# Cin7 every 15 min, and on 13 Nov 2026 the 08:00 run was refused (HTTP 429):
#   5 0 * * 5  /srv/team/sc/payment-request/refresh-payment.sh
#   5 4 * * 1  /srv/team/sc/payment-request/refresh-payment.sh
#
# Payment terms are re-read from master-data on the Monday run (weekly).
# Verifies the payload actually moved rather than trusting the exit code.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

LOG=refresh.log
C=payment-request-app-1
say(){ echo "[$(date -u +%FT%TZ)] $*" >> "$LOG"; }

say "=== scheduled refresh start ==="
BEFORE=$(stat -c %Y Outputs/report_payload.json 2>/dev/null || echo 0)

# People's edits are the one thing Cin7 cannot rebuild: snapshot the ledger first
# (SQLite online backup, safe while the app is writing), keep 60 copies.
mkdir -p backup
docker exec "$C" python -c "import sqlite3; s=sqlite3.connect('/app/Outputs/ledger.db'); d=sqlite3.connect('/app/Outputs/ledger.backup.tmp'); s.backup(d); d.close(); s.close()"   && mv -f Outputs/ledger.backup.tmp "backup/ledger_$(TZ=Australia/Perth date +%Y-%m-%d_%H%M).db"   && say "ok: ledger backup" || say "WARNING: ledger backup failed"
ls -1t backup/ledger_*.db 2>/dev/null | tail -n +61 | xargs -r rm -f

TERMS=""
[ "$(TZ=Australia/Perth date +%u)" = "1" ] && TERMS="--refresh-terms"

for step in "update_cache.py $TERMS" "build_report.py"; do
  # shellcheck disable=SC2086
  if ! docker exec "$C" python /app/Outputs/$step >> "$LOG" 2>&1; then
    say "FAILED: $step"
    exit 1
  fi
  say "ok: $step"
done

AFTER=$(stat -c %Y Outputs/report_payload.json 2>/dev/null || echo 0)
if [ "$AFTER" -le "$BEFORE" ]; then
  say "WARNING: report_payload.json did not change - treating as a failed run"
  exit 1
fi
# Keep 12 weeks of dated workbooks.
find Outputs -maxdepth 1 -name 'Payment Request - SC Inventory - International *.xlsx' -mtime +84 -delete
say "=== refresh complete, payload rebuilt ==="
