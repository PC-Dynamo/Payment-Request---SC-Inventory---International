#!/usr/bin/env python3
"""
masters.py - the Supplier and Branch Info master, read into lookup tables.

Source of truth:
  General - Supply Chain/1. Excel/Master Data/Supplier and Branch Info.xlsx
    - sheet "Branch"          Cin7 branch/delivery name -> State
    - sheet "State"           long state name -> 5 state codes (WA/NSW/VIC/SA/QLD)
    - sheet "SKU Information" SKU -> Supplier, Local/International, Commercial/not

On the Windows box the workbook is read from OneDrive; it is usually open in
Excel, so it is COPIED to the cache first (a direct openpyxl read raises
PermissionError). On the Linux server that path does not exist, so the workbook
comes down from SharePoint over Microsoft Graph with the credentials the other
SC apps already mount. The report always states the master's own date.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import urllib.parse
from pathlib import Path

import openpyxl

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / "cache"
MASTER_CACHE = CACHE_DIR / "master_supplier_branch.xlsx"

ONEDRIVE_MASTER = Path(os.environ.get(
    "SUPPLIER_MASTER_LIVE",
    r"C:\Users\phnxd\OneDrive - Dynamo fitness\General - Supply Chain"
    r"\1. Excel\Master Data\Supplier and Branch Info.xlsx"))

GRAPH_ENV = os.environ.get("GRAPH_ENV", "/app/secrets/graph.env")
SP_HOST = os.environ.get("SP_HOST", "dynamofitness565.sharepoint.com")
SP_SITE = os.environ.get("SP_SITE", "/sites/SupplyChain")
SP_SUPPLIER = os.environ.get(
    "SP_SUPPLIER_MASTER", "General/1. Excel/Master Data/Supplier and Branch Info.xlsx")

STATE_CODES = ["NSW", "VIC", "QLD", "WA", "SA"]
STALE_AFTER_DAYS = 7


# ------------------------------------------------------------------ retrieval
def _graph_client():
    if not os.path.exists(GRAPH_ENV):
        return None
    import requests
    env: dict[str, str] = {}
    with open(GRAPH_ENV) as fh:
        for ln in fh:
            ln = ln.strip()
            if "=" in ln and not ln.startswith("#"):
                k, v = ln.split("=", 1)
                env[k] = v
    tok = requests.post(
        f"https://login.microsoftonline.com/{env['GRAPH_TENANT_ID']}/oauth2/v2.0/token",
        data={"client_id": env["GRAPH_CLIENT_ID"],
              "client_secret": env["GRAPH_CLIENT_SECRET"],
              "scope": "https://graph.microsoft.com/.default",
              "grant_type": "client_credentials"}, timeout=60).json()["access_token"]
    H = {"Authorization": f"Bearer {tok}"}
    site_id = requests.get(f"https://graph.microsoft.com/v1.0/sites/{SP_HOST}:{SP_SITE}",
                           headers=H, timeout=60).json()["id"]
    drive_id = requests.get(f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive",
                            headers=H, timeout=60).json()["id"]
    return H, drive_id


def _from_sharepoint(dest: Path, force: bool = False) -> str:
    """Download the master from SharePoint. Never raises - a master sync failure
    must be visible in the report, not fatal to the run."""
    import requests
    try:
        client = _graph_client()
        if not client:
            return "no graph credentials"
        H, drive_id = client
        q = urllib.parse.quote(SP_SUPPLIER)
        meta = requests.get(f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{q}",
                            headers=H, timeout=60)
        if meta.status_code != 200:
            return f"not found on SharePoint ({meta.status_code})"
        j = meta.json()
        remote = dt.datetime.strptime(j["lastModifiedDateTime"][:19],
                                      "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
        if not force and dest.exists():
            local = dt.datetime.fromtimestamp(dest.stat().st_mtime, tz=dt.timezone.utc)
            if local >= remote:
                return "up-to-date"
        blob = requests.get(
            f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{q}:/content",
            headers=H, timeout=300)
        if blob.status_code != 200:
            return f"download failed ({blob.status_code})"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(blob.content)
        tmp.replace(dest)
        return f"refreshed from SharePoint ({j['lastModifiedDateTime']})"
    except Exception as exc:  # noqa: BLE001
        return f"graph error: {exc}"


def sync_master(force: bool = False) -> dict:
    """Refresh the cached master and report where it came from and how old it is."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    status = None
    if ONEDRIVE_MASTER.exists():
        try:
            shutil.copy2(ONEDRIVE_MASTER, MASTER_CACHE)   # the live file is often open
            status = "copied from OneDrive"
        except Exception as exc:  # noqa: BLE001
            status = f"OneDrive copy failed: {exc}"
    if status is None or not MASTER_CACHE.exists() or force and not ONEDRIVE_MASTER.exists():
        sp = _from_sharepoint(MASTER_CACHE, force=force)
        status = f"{status}; {sp}" if status else sp
    if not MASTER_CACHE.exists():
        return {"status": f"MISSING ({status})", "file_date": None, "age_days": None,
                "stale": True}
    mtime = dt.datetime.fromtimestamp(MASTER_CACHE.stat().st_mtime, tz=dt.timezone.utc)
    age = (dt.datetime.now(dt.timezone.utc) - mtime).days
    return {"status": status, "file_date": mtime.date().isoformat(),
            "age_days": age, "stale": age > STALE_AFTER_DAYS}


# -------------------------------------------------------------------- parsing
def _norm(v) -> str:
    """Cin7 and the master both carry non-breaking spaces, tabs and padding."""
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v).replace("\xa0", " ")).strip()


def _key(v) -> str:
    return _norm(v).lower().rstrip(" ,")


def load_masters(path: Path | None = None) -> dict:
    src = path or MASTER_CACHE
    wb = openpyxl.load_workbook(src, data_only=True, read_only=True)

    state_map: dict[str, str] = {}
    if "State" in wb.sheetnames:
        for row in wb["State"].iter_rows(min_row=2, values_only=True):
            if not row or not row[0] or _key(row[0]) == "state":
                continue
            state_map[_key(row[0])] = _norm(row[1]).upper()
    for code in STATE_CODES:
        state_map.setdefault(code.lower(), code)

    branch_state: dict[str, str] = {}
    branch_name: dict[str, str] = {}
    if "Branch" in wb.sheetnames:
        for row in wb["Branch"].iter_rows(min_row=2, values_only=True):
            if not row or not row[0] or _key(row[0]) == "branch":
                continue
            raw, aging, state, dynamics = (list(row) + [None] * 4)[:4]
            code = _norm(state).upper()
            if code not in STATE_CODES:
                code = state_map.get(_key(state), "")
            tidy = _norm(aging) or _norm(raw)
            for spelling in (raw, aging, dynamics):
                k = _key(spelling)
                if k:
                    branch_state.setdefault(k, code)
                    branch_name.setdefault(k, tidy)

    sku: dict[str, dict] = {}
    supplier_type: dict[str, str] = {}
    if "SKU Information" in wb.sheetnames:
        it = wb["SKU Information"].iter_rows(values_only=True)
        header = [_norm(h).lower() for h in next(it)]

        def col(*names, default=None):
            for n in names:
                for i, h in enumerate(header):
                    if h.startswith(n):
                        return i
            return default

        i_code = col("sku code", "code", default=0)
        i_sup = col("supplier name", default=2)
        i_type = col("type", default=3)
        i_comm = col("commercial/")
        for row in it:
            if not row or not row[i_code]:
                continue
            code = _norm(row[i_code]).upper()
            sup = _norm(row[i_sup]) if i_sup is not None else ""
            typ = _norm(row[i_type]).title() if i_type is not None else ""
            comm = _key(row[i_comm]) if i_comm is not None else ""
            sku[code] = {
                "supplier": sup,
                "type": typ,                                   # Local / International
                # "not commercial" -> Retail; anything else non-blank -> Commercial
                "segment": "Retail" if (not comm or comm.startswith("not")) else "Commercial",
            }
            if sup and typ:
                supplier_type.setdefault(_key(sup), typ)
    wb.close()
    return {
        "state_map": state_map,
        "branch_state": branch_state,
        "branch_name": branch_name,
        "sku": sku,
        "supplier_type": supplier_type,
        "counts": {"branches": len(branch_state), "skus": len(sku),
                   "suppliers": len(supplier_type)},
    }


# ------------------------------------------------------------------- resolvers
# Cin7 spells the same branch several ways ("Brendale Branch + QLD DC" vs
# "Brendale Branch + QLD DC , QLD"). Require a real overlap and take the
# LONGEST candidate so a short key such as "Commercial" cannot swallow others.
_MIN_PREFIX = 8


def _best_prefix(table: dict[str, str], k: str):
    best = None
    for cand in table:
        if len(cand) < _MIN_PREFIX or len(k) < _MIN_PREFIX:
            continue
        if (k.startswith(cand) or cand.startswith(k)) and (best is None or len(cand) > len(best)):
            best = cand
    return table[best] if best else None


def resolve_state(m: dict, delivery_company: str, delivery_state: str) -> str:
    """The branch master wins; Cin7's own deliveryState is the fallback."""
    k = _key(delivery_company)
    if m["branch_state"].get(k):
        return m["branch_state"][k]
    hit = _best_prefix(m["branch_state"], k)
    if hit:
        return hit
    ks = _key(delivery_state)
    if m["state_map"].get(ks):
        return m["state_map"][ks]
    up = _norm(delivery_state).upper()
    return up if up in STATE_CODES else ""


def resolve_branch(m: dict, delivery_company: str) -> str:
    k = _key(delivery_company)
    return m["branch_name"].get(k) or _best_prefix(m["branch_name"], k) or _norm(delivery_company)


def supplier_type(m: dict, supplier: str) -> str:
    """Local / International for a Cin7 supplier name, '' when unknown."""
    k = _key(supplier)
    if k in m["supplier_type"]:
        return m["supplier_type"][k]
    return _best_prefix(m["supplier_type"], k) or ""
