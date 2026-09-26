#!/usr/bin/env python3
"""
update_cache.py - build and incrementally refresh `payment_cache.json`.

One row per international PO (PO level, not SKU level). Each run:

  1. re-reads the Supplier and Branch Info master (branch -> state, SKU ->
     Retail/Commercial, supplier -> Local/International);
  2. refreshes supplier payment terms from the master-data cache, WEEKLY;
  3. pulls from Cin7 only the POs modified since the last run (`modifiedDate`
     watermark) - a first run, or --full, reads every open PO plus the last
     FULL_DAYS of receipts instead;
  4. merges ETA to port / ETD / forwarder from the shipment-schedule cache;
  5. re-rolls every cached PO against the current master, and if a SKU or a
     supplier cannot be identified, re-reads the master once and tries again.

USD: Cin7 stores international POs in landed AUD. The supplier is paid FOB
USD, so the USD value is qty x `priceColumns.costUSD`, PINNED per PO+SKU the
first time it is seen (costUSD is a live field and would otherwise drift).
Tested against the team's own payment workbook: median ratio 1.000.

Usage
  python update_cache.py                  # incremental (the scheduled run)
  python update_cache.py --full           # re-read every open PO
  python update_cache.py --refresh-terms  # force the weekly payment-terms read
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import masters as M  # noqa: E402
import sources as S  # noqa: E402
from cin7_client import PO_FIELDS, Cin7Client, fetch_products_usd  # noqa: E402

CACHE = HERE / "payment_cache.json"
AWST = dt.timezone(dt.timedelta(hours=8))

FULL_DAYS = 120                # receipts this recent are kept, to close open lines
WATERMARK_OVERLAP_H = 2        # re-read a little behind the watermark; cheap, safe
USD_RECHECK_DAYS = 7
RATIO_MIN, RATIO_MAX = 1.0, 4.0
DEFAULT_RATIO = 1.8            # AUD:USD fallback when nothing better is known

STAGE_LABELS = {
    "New": "New", "NEW": "New",
    "Production-Allocated": "Production Allocated",
    "Production-China Stock": "Production - China Stock",
    "In Stock China Warehouse": "In Stock China Warehouse",
    "On Its Way": "On Its Way",
    "Dispatched": "Dispatched",
    "Received": "Received",
}
# Stages past which a supplier has, in practice, been paid the deposit - they
# do not start production without it.
PAST_DEPOSIT_STAGES = {"Production-Allocated", "Production-China Stock",
                       "In Stock China Warehouse", "On Its Way", "Dispatched", "Received"}

_NONSTOCK = [
    re.compile(r"^freight|^fumigat|^inspect|^survey|^cartage|^duty|^customs|^insurance"
               r"|^courier|^delivery\b|^shipping\b", re.I),
    re.compile(r"mould\s*cost|tooling|sample\b|artwork|logo\s*fee|deposit\b", re.I),
    re.compile(r"^DCarePO-", re.I),
    re.compile(r"\bpart\b|\bparts\b|\bspare\b|^P-ARTS|^MS0\d{9,}$", re.I),
    re.compile(r"^\d{1,6}$"),
]


def _iso(v) -> str | None:
    if not v:
        return None
    s = str(v)[:10]
    try:
        dt.date.fromisoformat(s)
        return s
    except ValueError:
        return None


def _utc_stamp(v: str | None) -> str:
    """Cin7 silently returns nothing for a watermark without the trailing Z."""
    s = str(v).strip().replace(" ", "T")
    if s.endswith("Z"):
        return s
    if len(s) == 10:
        return f"{s}T00:00:00Z"
    return f"{s.split('.')[0]}Z"


def load_cache() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"meta": {}, "pos": {}, "usd_pins": {}, "usd_misses": []}


def save_cache(cache: dict) -> None:
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(CACHE)


# ----------------------------------------------------------------- rollups
def _is_nonstock(code: str, name: str) -> bool:
    text = f"{code} {name}"
    return any(rx.search(code) or rx.search(text) for rx in _NONSTOCK)


def compact_lines(po: dict) -> list[list]:
    """[code, name, qty, aud, qty_received] - all a PO-level rollup ever needs."""
    out = []
    for ln in po.get("lineItems") or []:
        q = float(ln.get("qty") or 0)
        unit = float(ln.get("unitPrice") or 0)
        disc = float(ln.get("discount") or 0)
        out.append([M._norm(ln.get("code")), M._norm(ln.get("name"))[:60], q,
                    round(q * unit * (1 - disc / 100.0), 2),
                    float(ln.get("qtyShipped") or 0)])
    return out


def rollup(ref: str, lines: list[list], mst: dict, pins: dict,
           supplier_ratio: float | None) -> dict:
    aud = usd = aud_matched = 0.0
    comm = retail = 0.0
    unknown: set[str] = set()
    costed = 0
    qty = qty_recv = 0.0
    for code, name, q, line_aud, q_recv in lines:
        aud += line_aud
        qty += q
        qty_recv += q_recv
        key = code.upper()
        info = mst["sku"].get(key)
        if info:
            if info["segment"] == "Commercial":
                comm += line_aud
            else:
                retail += line_aud
        elif code and not _is_nonstock(code, name):
            unknown.add(code)
        cost = pins.get(f"{ref}|{key}")
        if cost:
            usd += q * cost
            aud_matched += line_aud
            costed += 1

    ratio = (aud_matched / usd) if usd > 0 else None
    ratio_ok = ratio is not None and RATIO_MIN <= ratio <= RATIO_MAX
    if ratio_ok and aud_matched >= 0.5 * aud:
        usd_est = usd * (aud / aud_matched) if aud_matched else usd
        src = ("Cin7 costUSD" if abs(aud - aud_matched) < 0.01
               else f"Cin7 costUSD, scaled for {len(lines) - costed} uncosted line(s)")
    elif supplier_ratio:
        usd_est = aud / supplier_ratio
        src = f"estimated from supplier AUD:USD ratio {supplier_ratio:.2f}"
    else:
        usd_est = aud / DEFAULT_RATIO
        src = f"estimated at default AUD:USD ratio {DEFAULT_RATIO}"
    return {
        "value_aud": round(aud, 2),
        "value_usd": round(usd_est, 2),
        "usd_source": src,
        "usd_exact": src == "Cin7 costUSD",
        "aud_usd_ratio": round(ratio, 3) if ratio else None,
        "usd_coverage": round(costed / len(lines), 3) if lines else 0.0,
        "value_commercial": round(comm, 2),
        "value_retail": round(retail, 2),
        # Mixed POs are counted under the side that carries more value, so a
        # Retail/Commercial filter never double-counts a payment.
        "segment": "Commercial" if comm > retail else "Retail",
        "segment_mixed": bool(comm > 0 and retail > 0),
        "qty_total": round(qty, 2),
        "receipt_pct": round(100 * qty_recv / qty, 1) if qty else None,
        "unmapped_skus": sorted(unknown),
        "line_count": len(lines),
    }


def _container(raw) -> str:
    return M._norm(raw)


def build_row(po: dict, mst: dict, prev: dict) -> dict:
    ref = po.get("reference") or str(po.get("id"))
    cf = po.get("customFields") or {}
    stage = po.get("stage") or ""
    supplier = M._norm(po.get("company"))
    received = _iso(po.get("fullyReceivedDate"))
    return {
        "po": ref,
        "cin7_id": po.get("id"),
        "supplier": supplier,
        "origin_cin7": M._norm(cf.get("orders_1005")),
        "supplier_type_master": M.supplier_type(mst, supplier),
        "stage_raw": stage,
        "stage": STAGE_LABELS.get(stage, stage or "Unknown"),
        "past_deposit_stage": stage in PAST_DEPOSIT_STAGES,
        "is_void": bool(po.get("isVoid")),
        "is_approved": bool(po.get("isApproved")),
        "received_date": received,
        "is_open": not received and not po.get("isVoid") and stage != "Received",
        "branch_cin7": M._norm(po.get("deliveryCompany")),
        "branch": M.resolve_branch(mst, po.get("deliveryCompany") or ""),
        "state": M.resolve_state(mst, po.get("deliveryCompany") or "",
                                 po.get("deliveryState") or ""),
        "po_date": _iso(po.get("invoiceDate")) or _iso(po.get("createdDate")),
        "created_date": _iso(po.get("createdDate")),
        "modified_date": _utc_stamp(po.get("modifiedDate") or "1970-01-01")[:19],
        "eta_cin7": _iso(po.get("estimatedDeliveryDate")),
        "supplier_invoice": M._norm(po.get("supplierInvoiceReference")),
        "container_cin7": _container(cf.get("orders_1004")),
        "po_total_aud_cin7": round(float(po.get("total") or 0), 2),
        "currency_cin7": po.get("currencyCode") or "AUD",
        "lines": compact_lines(po),
        "first_seen": prev.get("first_seen") or dt.datetime.now(AWST).date().isoformat(),
    }


def _is_international(row: dict) -> bool:
    o = row["origin_cin7"].lower()
    if o:
        return o == "international"
    return row["supplier_type_master"].lower() == "international"


def _supplier_ratios(cache: dict) -> dict[str, float]:
    """Median AUD:USD per supplier from POs whose USD is fully costed."""
    by: dict[str, list[float]] = {}
    for r in cache["pos"].values():
        if r.get("usd_exact") and r.get("aud_usd_ratio") and \
                RATIO_MIN <= r["aud_usd_ratio"] <= RATIO_MAX:
            by.setdefault(M._key(r["supplier"]), []).append(r["aud_usd_ratio"])
    return {k: statistics.median(v) for k, v in by.items() if v}


def reroll(cache: dict, mst: dict) -> None:
    """Re-derive every cached PO from its stored lines and the current master.
    Two passes: the first establishes which POs are fully costed, the second
    lets poorly-costed POs borrow their supplier's typical AUD:USD ratio."""
    pins = cache.get("usd_pins", {})
    ratios: dict[str, float] = {}
    for _ in range(2):
        for ref, row in cache["pos"].items():
            row["supplier_type_master"] = M.supplier_type(mst, row["supplier"])
            row["branch"] = M.resolve_branch(mst, row["branch_cin7"])
            row["state"] = M.resolve_state(mst, row["branch_cin7"], "") or row.get("state", "")
            row.update(rollup(ref, row.get("lines") or [], mst, pins,
                              ratios.get(M._key(row["supplier"]))))
        ratios = _supplier_ratios(cache)


# ------------------------------------------------------------------------ main
def run(full: bool = False, refresh_terms: bool = False) -> dict:
    started = dt.datetime.now(dt.timezone.utc)
    cache = load_cache()
    log: list[str] = []

    master_meta = M.sync_master()
    mst = M.load_masters()
    log.append(f"master: {master_meta['status']} ({mst['counts']})")

    terms_meta = S.sync_payment_terms(force=refresh_terms)
    log.append(f"payment terms: {terms_meta['status']}")

    client = Cin7Client()
    ok, detail = client.ping()
    if not ok:
        raise SystemExit(f"Cin7 unreachable: {detail}")

    watermark = cache.get("meta", {}).get("po_watermark")
    if full or not watermark:
        since = (dt.date.today() - dt.timedelta(days=FULL_DAYS)).isoformat()
        wheres = ["fullyReceivedDate IS NULL", f"fullyReceivedDate >= '{_utc_stamp(since)}'"]
        log.append(f"full pull: open POs + receipts since {since}")
    else:
        wm = dt.datetime.fromisoformat(watermark) - dt.timedelta(hours=WATERMARK_OVERLAP_H)
        wheres = [f"modifiedDate >= '{_utc_stamp(wm.isoformat(timespec='seconds'))}'"]
        log.append(f"incremental pull: modified since {wm.isoformat(timespec='seconds')}Z")

    pulled: dict[str, dict] = {}
    for w in wheres:
        # Void POs are pulled too, so a PO voided after it was cached is closed
        # rather than left open forever.
        batch = list(client.paginate("PurchaseOrders", {"where": w, "fields": PO_FIELDS}))
        for po in batch:
            pulled[po.get("reference") or str(po.get("id"))] = po
        log.append(f"  where[{w}] -> {len(batch)} POs")

    added = updated = 0
    for ref, po in pulled.items():
        prev = cache["pos"].get(ref, {})
        row = build_row(po, mst, prev)
        if not _is_international(row):
            if ref in cache["pos"]:           # reclassified Local - drop it
                del cache["pos"][ref]
            continue
        if not prev and not row["is_open"] and not full and watermark:
            # A delta can return an old, long-received PO that someone touched.
            # Only keep it if it is recent enough to matter to open payments.
            cutoff = (dt.date.today() - dt.timedelta(days=FULL_DAYS)).isoformat()
            if (row["received_date"] or "") < cutoff:
                continue
        added += 0 if prev else 1
        updated += 1 if prev else 0
        cache["pos"][ref] = row
    log.append(f"pulled {len(pulled)} POs -> {added} new, {updated} updated international")

    # Drop long-closed POs so the cache stays the size of the payment horizon.
    cutoff = (dt.date.today() - dt.timedelta(days=FULL_DAYS)).isoformat()
    stale = [r for r, row in cache["pos"].items()
             if not row["is_open"] and ((row.get("received_date") or "") < cutoff or row["is_void"])]
    for r in stale:
        del cache["pos"][r]
    if stale:
        log.append(f"pruned {len(stale)} POs received before {cutoff} or voided")

    # ---- USD pins: Products is the heaviest call, so only when it can matter
    pins = cache.setdefault("usd_pins", {})
    misses = set(cache.get("usd_misses", []))
    last = cache.get("meta", {}).get("products_fetched_utc")
    stale_products = True
    if last:
        try:
            stale_products = (dt.datetime.now(dt.timezone.utc)
                              - dt.datetime.fromisoformat(last)).days >= USD_RECHECK_DAYS
        except ValueError:
            pass
    need = {(ref, ln[0].upper()) for ref, row in cache["pos"].items() if row["is_open"]
            for ln in row.get("lines") or []
            if ln[0] and f"{ref}|{ln[0].upper()}" not in pins and ln[0].upper() not in misses}
    if need or stale_products:
        costs = fetch_products_usd(client)
        cache["meta"]["products_fetched_utc"] = dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds")
        newly = 0
        for ref, row in cache["pos"].items():
            for ln in row.get("lines") or []:
                key = ln[0].upper()
                if key and f"{ref}|{key}" not in pins and costs.get(key):
                    pins[f"{ref}|{key}"] = costs[key]
                    newly += 1
        codes = {ln[0].upper() for row in cache["pos"].values()
                 for ln in row.get("lines") or [] if ln[0]}
        cache["usd_misses"] = sorted(c for c in codes if not costs.get(c))
        log.append(f"products: {len(costs)} USD costs read, {newly} lines pinned, "
                   f"{len(cache['usd_misses'])} SKUs with no USD cost")
    else:
        log.append("products: skipped - every open line is pinned or known uncosted")
    # Pins of pruned POs are dead weight.
    live = set(cache["pos"])
    cache["usd_pins"] = {k: v for k, v in pins.items() if k.split("|", 1)[0] in live}

    reroll(cache, mst)

    # ---- Requirement 4: an unidentified SKU or supplier sends us back to the master
    unknown_skus = sorted({s for r in cache["pos"].values() if r["is_open"]
                           for s in r.get("unmapped_skus") or []})
    unknown_sup = sorted({r["supplier"] for r in cache["pos"].values()
                          if r["is_open"] and not r["supplier_type_master"]})
    known_gaps = set(cache.get("meta", {}).get("unmapped_skus") or []) |         set(cache.get("meta", {}).get("unknown_suppliers") or [])
    new_gaps = (set(unknown_skus) | set(unknown_sup)) - known_gaps
    if unknown_skus or unknown_sup:
        log.append(f"{len(unknown_skus)} SKUs / {len(unknown_sup)} suppliers not in the master cache"
                   f" ({len(new_gaps)} new since the last run)")
    if new_gaps:
        log.append("new unidentified SKU/supplier - re-reading the master file")
        master_meta = M.sync_master(force=True)
        mst = M.load_masters()
        reroll(cache, mst)
        unknown_skus = sorted({s for r in cache["pos"].values() if r["is_open"]
                               for s in r.get("unmapped_skus") or []})
        unknown_sup = sorted({r["supplier"] for r in cache["pos"].values()
                              if r["is_open"] and not r["supplier_type_master"]})
        log.append(f"after re-read: {len(unknown_skus)} SKUs / {len(unknown_sup)} "
                   "suppliers still unidentified")

    # ---- ETA to port etc. from the shipment-schedule cache
    ship, ship_meta = S.load_shipment_eta()
    hit = 0
    for ref, row in cache["pos"].items():
        s = ship.get(ref)
        row["ship"] = s or {}
        hit += 1 if s else 0
    log.append(f"shipment-schedule: {ship_meta['status']}, built "
               f"{ship_meta.get('generated_awst')}; matched {hit}/{len(cache['pos'])} POs")

    latest = max((r["modified_date"] for r in cache["pos"].values()), default="")
    cache["meta"] = {
        **cache.get("meta", {}),
        "generated_utc": started.isoformat(timespec="seconds"),
        "generated_awst": started.astimezone(AWST).strftime("%Y-%m-%d %H:%M AWST"),
        "po_watermark": max(latest, watermark or ""),
        "po_count": len(cache["pos"]),
        "open_count": sum(1 for r in cache["pos"].values() if r["is_open"]),
        "master": master_meta,
        "master_counts": mst["counts"],
        "terms": {**terms_meta, "fetched_utc": S.load_payment_terms().get("fetched_utc")},
        "shipment": ship_meta,
        "unmapped_skus": unknown_skus,
        "unknown_suppliers": unknown_sup,
        "cin7_calls": client.calls,
        "run_log": log,
        "mode": "full" if (full or not watermark) else "incremental",
        "source": "cin7-api-v1 (read-only)",
        "duration_s": round((dt.datetime.now(dt.timezone.utc) - started).total_seconds(), 1),
    }
    save_cache(cache)
    for line in log:
        print("  " + line)
    print(f"cache: {len(cache['pos'])} POs ({cache['meta']['open_count']} open), "
          f"{client.calls} Cin7 calls -> {CACHE.name}")
    return cache


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", action="store_true", help="re-read every open PO")
    ap.add_argument("--refresh-terms", action="store_true",
                    help="re-read payment terms now instead of weekly")
    a = ap.parse_args()
    try:
        run(full=a.full, refresh_terms=a.refresh_terms)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
