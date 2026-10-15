# Payment Request — SC Inventory International

Live payment-request dashboard for Dynamo Fitness Supply Chain: what is owed to
international suppliers (USD), to forwarders for freight (USD), and the AUD
document and handling charges (customs brokerage, GST, duty, cartage), request
by request.

Live: <https://sc.dynamofitness.au/Applications/payment-request/>
(also **Tools → Payment Request-Intl** in the SC Dashboard, and a tab on the
[admin access page](https://sc.dynamofitness.au/Applications/sc-dashboard/api/admin-access))

## What it does

- **Every open international PO is on the list automatically** (PO level, not
  SKU level), split into its payment milestones from the supplier's payment
  terms: *Deposit x%* on the PO date, then *Balance* relative to ETD or ETA to
  port, or one *Full payment*.
- **Pay runs: Tuesday for supplier payments and USD freight, Thursday for AUD charges** (as the team runs them). A line joins the last pay run on
  or before its due date; a line still unpaid after its pay run moves to the next one (↻).
- **AUD lines and freight are added by hand** (“+ Add AUD / freight line”).
  Name the PO(s) and the line inherits branch, state and channel.
- People set status (For Payment → Requested → Paid, On Hold, Not Payable),
  paid date, invoice number, amount or due date. Every edit is logged with the
  signed-in address. “Mark selected as Requested” submits a batch.
- One white page: KPIs, four charts and the table, all filtered by
  **State (All / NSW / VIC / QLD / WA / SA)** and **Channel (All / Retail /
  Commercial)**, over *This request / 4-week rolling / All unpaid / All*.
- Excel export of the current view; each scheduled run also writes
  `Outputs/Payment Request - SC Inventory - International DD-MM-YYYY.xlsx`.

- **Special Arrangements tab:** one free-text block for Accounts (ACC) and one
  for Supply Chain (SC), each saved with who and when, plus the timing rule the
  app applies to every supplier.
- **Payment history is kept** after Cin7 receives a PO: every line with a
  recorded payment (paid, requested or not payable) is archived, so the register
  and the backup do not lose it when the PO leaves Cin7's open list.
- **OneDrive backup** every Tuesday and Thursday 17:00 AWST, after that day's
  pay run: *Payment Request for Four-week Rolling Forecast - Claude.ai Latest.xlsx*
  in General - Supply Chain / 1. Excel / Procurement Reports, replacing the last
  copy. Its first sheet, `updated`, has the same layout as the team's manual
  workbook, so reports built on that workbook can be pointed at it.

## Where the data comes from

| Data | Source | Refresh |
|---|---|---|
| Open POs, AUD value, stage, branch | Cin7 v1 API, **read-only**, `modifiedDate` delta only | Mon + Wed 13:00 AWST (afternoon before each pay run) |
| USD value | qty × Cin7 `costUSD`, pinned per PO line when first seen | with the POs; product costs weekly |
| ETA to port, ETD, container, forwarder | Shipment Schedule cache `/srv/team/sc/shipment-schedule` | it rebuilds Mon + Thu 14:00 AWST |
| Payment terms | SC Master Data cache `/srv/team/sc/master-data` | **weekly** (Monday run) |
| Branch → state, SKU → Retail/Commercial, supplier → Local/International | *Supplier and Branch Info.xlsx* (OneDrive / SharePoint) | every run; forced re-read when a new SKU or supplier cannot be identified |
| Status, paid dates, manual lines | `Outputs/ledger.db` (SQLite), people's edits | live |

Cin7 holds international POs in landed AUD. The supplier is paid FOB USD, so
USD is rebuilt from `costUSD`. Checked against 604 POs in the team's payment
workbook, the median ratio is 1.000. Lines marked `~` are estimates; type the
invoice amount to override.

China-stock bulk orders (`Production - China Stock`, `In Stock China
Warehouse`) are left out: they have no vessel or ETA yet, and belong to the
General Planning bulk-order flow.

A deposit on a PO already in production, and a pre-shipment payment on a PO
already on the water, show as **Paid (assumed)** until someone records
otherwise.

Payment-term exceptions (special arrangements) and supplier-name aliases are in
`Outputs/overrides.json`.

## Layout

```
Outputs/
  cin7_client.py        read-only Cin7 client (GET only)
  masters.py            Supplier and Branch Info master
  sources.py            shipment-schedule ETA + master-data payment terms
  update_cache.py       Cin7 delta -> payment_cache.json
  terms.py              payment terms -> milestones
  payments.py           milestones + ledger -> dated payment lines
  build_report.py       report_payload.json + dated .xlsx
  ledger.py             people's edits and manual lines (SQLite) + audit
  seed_from_reference.py  one-off import of the team's payment workbook
  selftest.py           rules + data checks
  overrides.json        special arrangements, aliases, transit weeks
server/                 Flask app, template, Dockerfile, compose
refresh-payment.sh      the Mon/Wed cron job
upload-onedrive.sh      the Tue/Thu 17:00 OneDrive backup
```

## Run locally (Windows)

```bash
cd Outputs
python update_cache.py --full      # first run; afterwards just update_cache.py
python build_report.py
python selftest.py
python ../server/app.py            # http://localhost:8080
```

Locally the two server caches are fetched over SSH from `root@170.64.233.186`.
Set `CIN7_USER` / `CIN7_KEY` in the environment.

## Server (170.64.233.186)

```bash
cd /srv/team/sc/payment-request/server
docker compose up -d --build
docker exec payment-request-app-1 python /app/Outputs/update_cache.py --full
docker exec payment-request-app-1 python /app/Outputs/build_report.py
```

- Port `127.0.0.1:8112`, behind nginx `location ^~ /Applications/payment-request/`
  with `/_authz`, so access is granted per person on the admin page (slug
  `payment-request`).
- Cin7 credentials come from the Shipment Schedule app's `server/.env`.
- Cron (UTC; 13:00 AWST Mon/Wed = 05:00 UTC Mon/Wed):
  `0 5 * * 1,3 /srv/team/sc/payment-request/refresh-payment.sh`
- Monday 13:00 runs before the Shipment Schedule's 14:00 ETA re-read; the app moves
  forwarder ETAs by any change in Cin7's own ETA to bridge that hour.
- OneDrive backup cron (UTC; 17:00 AWST Tue/Thu = 09:00 UTC):
  `0 9 * * 2,4 /srv/team/sc/payment-request/upload-onedrive.sh` (log: `upload.log`)
- Back up `Outputs/ledger.db`: it is the only data here that cannot be rebuilt from Cin7.
