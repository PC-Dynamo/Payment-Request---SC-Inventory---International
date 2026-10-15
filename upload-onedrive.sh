#!/usr/bin/env bash
# OneDrive backup of the payment request register.
#
# Tuesday and Thursday 17:00 AWST - after that day's pay run (Tuesday: supplier
# payments + USD freight; Thursday: AUD charges). Host runs UTC, AWST = UTC+8:
#   0 9 * * 2,4  /srv/team/sc/payment-request/upload-onedrive.sh
#
# Writes "Payment Request for Four-week Rolling Forecast - Claude.ai Latest.xlsx"
# to General - Supply Chain/1. Excel/Procurement Reports, replacing the last copy.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

LOG=upload.log
C=payment-request-app-1
say(){ echo "[$(date -u +%FT%TZ)] $*" >> "$LOG"; }

say "=== OneDrive backup start ==="
if docker exec "$C" python /app/Outputs/upload_onedrive.py >> "$LOG" 2>&1; then
  say "=== OneDrive backup done ==="
else
  say "FAILED: OneDrive backup"
  exit 1
fi
