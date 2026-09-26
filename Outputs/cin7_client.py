#!/usr/bin/env python3
"""
cin7_client.py - read-only CIN7 v1 REST client for the Payment Request app.

GET only. Never POST/PUT/PATCH/DELETE - Cin7 stays the single source of truth
and this app only reads purchase orders and product costs from it.

Credentials: CIN7_USER / CIN7_KEY from the environment (server/.env on the box).
Rate limit is 3/sec, 60/min - paginate() paces itself and backs off on 429.
"""
from __future__ import annotations

import os
import time

import requests
from requests.auth import HTTPBasicAuth

BASE_URL = os.environ.get("CIN7_BASE_URL", "https://api.cin7.com/api/v1")
USERNAME = os.environ.get("CIN7_USER") or "dynamofitnessAU"
PASSWORD = os.environ.get("CIN7_KEY") or ""

_PAGE_PAUSE = 0.35  # ~3 req/s ceiling


class Cin7Error(RuntimeError):
    pass


class Cin7Client:
    def __init__(self, username: str | None = None, password: str | None = None,
                 base_url: str | None = None, timeout: int = 120):
        self.auth = HTTPBasicAuth(username or USERNAME, password or PASSWORD)
        self.base = (base_url or BASE_URL).rstrip("/")
        self.timeout = timeout
        self.calls = 0

    def get(self, endpoint: str, params: dict | None = None, retries: int = 5):
        url = f"{self.base}/{endpoint.lstrip('/')}"
        delay = 2.0
        last = None
        for _ in range(retries):
            try:
                self.calls += 1
                r = requests.get(url, auth=self.auth, params=params, timeout=self.timeout)
                if r.status_code == 200:
                    return r.json()
                last = f"HTTP {r.status_code}: {r.text[:240]}"
                # 429 and 5xx are worth retrying; other 4xx are our own fault
                if r.status_code < 500 and r.status_code != 429:
                    break
            except requests.RequestException as exc:
                last = str(exc)
            time.sleep(delay)
            delay *= 2
        raise Cin7Error(f"CIN7 GET {endpoint} failed: {last}")

    def paginate(self, endpoint: str, params: dict | None = None,
                 rows: int = 250, max_pages: int = 400):
        """Yield rows across pages, stopping on the first short page."""
        params = dict(params or {})
        params["rows"] = rows
        page = 1
        while page <= max_pages:
            params["page"] = page
            batch = self.get(endpoint, params)
            if isinstance(batch, dict):
                batch = batch.get("data", [])
            if not isinstance(batch, list):
                break
            yield from batch
            if len(batch) < rows:
                return
            page += 1
            time.sleep(_PAGE_PAUSE)

    def ping(self) -> tuple[bool, str]:
        if not self.auth.password:
            return False, "no CIN7_KEY configured"
        try:
            self.get("Products", params={"rows": 1, "page": 1, "fields": "id"})
            return True, "ok"
        except Exception as exc:  # noqa: BLE001 - status text for the UI
            return False, str(exc)


# `customFields.orders_1004` is the container, `orders_1005` Local/International.
PO_FIELDS = (
    "id,reference,stage,status,invoiceDate,createdDate,modifiedDate,"
    "fullyReceivedDate,estimatedDeliveryDate,"
    "branchId,company,memberId,deliveryCompany,deliveryState,"
    "total,productTotal,freightTotal,currencyCode,currencyRate,isVoid,isApproved,"
    "supplierInvoiceReference,customFields,lineItems"
)


def fetch_purchase_orders(client: Cin7Client, where: str) -> list[dict]:
    """All non-void POs matching `where`."""
    return [p for p in client.paginate("PurchaseOrders",
                                       {"where": where, "fields": PO_FIELDS})
            if not p.get("isVoid")]


def fetch_products_usd(client: Cin7Client) -> dict[str, float]:
    """{product option code (upper): costUSD}. costUSD is a LIVE field, so the
    cache pins it per PO+SKU at first sighting (see update_cache.py)."""
    out: dict[str, float] = {}
    for prod in client.paginate("Products", {"fields": "id,productOptions"}, rows=250):
        for opt in prod.get("productOptions") or []:
            code = (opt.get("productOptionCode") or opt.get("code") or "").strip()
            if not code:
                continue
            usd = float((opt.get("priceColumns") or {}).get("costUSD") or 0.0)
            if usd:
                out[code.upper()] = usd
    return out
