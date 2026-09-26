#!/usr/bin/env python3
"""
sources.py - the two caches this app reads from sibling SC apps on 170.64.233.186.

  shipment-schedule  /srv/team/sc/shipment-schedule/Outputs/shipment_cache.json
                     ETA to port (forwarder), Cin7 ETA, ETD, container, forwarder.
                     Rebuilt Mon + Thu 14:00 AWST after the team updates ETAs in
                     Cin7, which is why this app refreshes Tue + Fri 07:00 AWST.
  master-data        /srv/team/sc/master-data/data/cache/master_snapshot.json
                     Supplier payment terms. Re-read WEEKLY into
                     cache/payment_terms.json (requirement 10).

On the server both are bind-mounted read-only under /app/ext/. On the Windows
workstation they are fetched over SSH (root@170.64.233.186) into cache/.
Nothing here ever writes to either sibling app.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / "cache"

SSH_HOST = os.environ.get("SC_SSH_HOST", "root@170.64.233.186")
SHIPMENT_REMOTE = "/srv/team/sc/shipment-schedule/Outputs/shipment_cache.json"
MASTER_REMOTE = "/srv/team/sc/master-data/data/cache/master_snapshot.json"
SHIPMENT_MOUNT = Path(os.environ.get("SHIPMENT_CACHE", "/app/ext/shipment-schedule/shipment_cache.json"))
MASTER_MOUNT = Path(os.environ.get("MASTER_SNAPSHOT", "/app/ext/master-data/master_snapshot.json"))

SHIPMENT_LOCAL = CACHE_DIR / "shipment_cache.json"
TERMS_CACHE = CACHE_DIR / "payment_terms.json"
TERMS_REFRESH_DAYS = 6.5    # weekly; 6.5 so a Tuesday run is never missed by minutes

# The ETA fields this app needs, and nothing else - the full shipment cache
# also carries comment logs, which have no business in a payment report.
SHIPMENT_FIELDS = (
    "eta", "eta_shipper", "etd", "booked_eta", "baseline_eta", "forwarder",
    "container_raw", "container_sizes", "delay_days", "delay_band", "delay_reason",
    "actual_arrival", "status", "segment", "state", "branch",
)


def _scp(remote: str, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    try:
        p = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                            f"{SSH_HOST}:{remote}", str(tmp)],
                           capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"scp failed: {exc}"
    if p.returncode != 0 or not tmp.exists():
        return f"scp failed: {(p.stderr or '').strip()[:200]}"
    tmp.replace(dest)
    return f"fetched over SSH from {SSH_HOST}"


def _mtime_utc(p: Path) -> str:
    return dt.datetime.fromtimestamp(p.stat().st_mtime, tz=dt.timezone.utc).isoformat(
        timespec="seconds")


# --------------------------------------------------------------- shipment ETA
def load_shipment_eta() -> tuple[dict[str, dict], dict]:
    """{PO reference: ETA fields} plus a meta block saying where it came from."""
    if SHIPMENT_MOUNT.exists():
        src, how = SHIPMENT_MOUNT, "server mount (read-only)"
    else:
        how = _scp(SHIPMENT_REMOTE, SHIPMENT_LOCAL)
        src = SHIPMENT_LOCAL
        if not src.exists():
            return {}, {"status": f"MISSING ({how})", "generated_awst": None}
        if how.startswith("scp failed"):
            how = f"{how}; using last local copy"
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, {"status": f"unreadable: {exc}", "generated_awst": None}
    out = {ref: {k: row.get(k) for k in SHIPMENT_FIELDS}
           for ref, row in (data.get("pos") or {}).items()}
    meta = data.get("meta") or {}
    return out, {"status": how, "generated_awst": meta.get("generated_awst"),
                 "po_count": len(out), "file_utc": _mtime_utc(src)}


# -------------------------------------------------------------- payment terms
def _terms_age_days() -> float | None:
    if not TERMS_CACHE.exists():
        return None
    try:
        got = json.loads(TERMS_CACHE.read_text(encoding="utf-8")).get("fetched_utc")
        return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(got)).total_seconds() / 86400
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def sync_payment_terms(force: bool = False) -> dict:
    """Copy supplier payment terms out of the master-data snapshot, weekly."""
    age = _terms_age_days()
    if not force and age is not None and age < TERMS_REFRESH_DAYS:
        return {"status": f"cached ({age:.1f} days old, weekly refresh)", "refreshed": False}
    if MASTER_MOUNT.exists():
        src, how = MASTER_MOUNT, "server mount (read-only)"
    else:
        src = CACHE_DIR / "master_snapshot.json"
        how = _scp(MASTER_REMOTE, src)
        if how.startswith("scp failed"):
            return {"status": f"{how}; kept cached terms", "refreshed": False}
    try:
        snap = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": f"snapshot unreadable ({exc}); kept cached terms", "refreshed": False}
    tables = snap.get("tables") or {}
    keep = ("supplier_name", "supplier_type", "terms", "deposit_pct", "due_days",
            "basis", "currency", "remarks", "updated_at", "updated_by")
    terms = [{k: r.get(k) for k in keep} for r in tables.get("payment_term") or []]
    suppliers = [{"supplier_name": r.get("supplier_name"),
                  "supplier_type": r.get("supplier_type"),
                  "brands": r.get("brands")} for r in tables.get("supplier") or []]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"fetched_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               "snapshot_generated": snap.get("generated"), "source": how,
               "payment_terms": terms, "suppliers": suppliers}
    tmp = TERMS_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tmp.replace(TERMS_CACHE)
    if src.parent == CACHE_DIR and src.name == "master_snapshot.json":
        src.unlink(missing_ok=True)       # 9 MB of SKU master we do not need to keep
    return {"status": f"refreshed from master-data ({how}; snapshot {snap.get('generated')})",
            "refreshed": True, "terms": len(terms)}


def load_payment_terms() -> dict:
    if not TERMS_CACHE.exists():
        return {"payment_terms": [], "suppliers": [], "fetched_utc": None}
    return json.loads(TERMS_CACHE.read_text(encoding="utf-8"))
