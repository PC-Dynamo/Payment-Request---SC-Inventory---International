#!/usr/bin/env python3
"""
ledger.py - everything a PERSON records, kept apart from the Cin7 cache.

  line_state   edits to an auto line (a PO's deposit / balance / full payment):
               status, paid date, invoice no., amount or due-date override, note
  manual_line  lines added by hand - the AUD document / handling charges
               (customs brokerage, GST, duty, cartage...) and forwarder freight
  audit        who changed what, when (AWST)

SQLite in Outputs/ledger.db. A refresh rebuilds the Cin7 side and never
touches this file, so a note or a "Paid" mark cannot be overwritten by Cin7.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "ledger.db"
AWST = dt.timezone(dt.timedelta(hours=8))

STATUSES = ["For Payment", "Requested", "Paid", "On Hold", "Not Payable"]
CURRENCIES = ["USD", "AUD", "GBP", "EUR"]
# What a payment actually went out in (Accounts, 26 Oct 2026). Accounts also
# records the AUD it cost, for the company's financial report.
PAID_CURRENCIES = ["AUD", "USD", "EUR", "GBP", "CNY"]
CHARGE_TYPES = {                       # type -> default currency
    "Freight Payment": "USD",
    "Customs Brokerage": "AUD",
    "GST Payment": "AUD",
    "Government Duty": "AUD",
    "Document Charge": "AUD",
    "Handling Charge": "AUD",
    "Cartage": "AUD",
    "Local Cartage Delivery": "AUD",
    "Detention Charges": "AUD",
    "Inspection": "USD",
    "Supplier Payment": "USD",
    "Other": "AUD",
}

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS line_state (
  line_id TEXT PRIMARY KEY,
  status TEXT, request_date TEXT, paid_date TEXT, due_date TEXT,
  amount REAL, currency TEXT, invoice_no TEXT, comment TEXT,
  seed_key TEXT, updated_by TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS manual_line (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  seed_key TEXT UNIQUE,
  entry_date TEXT, vendor TEXT, pay_type TEXT, po_refs TEXT,
  branch TEXT, state TEXT, segment TEXT, doc_no TEXT,
  currency TEXT, amount REAL, due_date TEXT, request_date TEXT,
  status TEXT, paid_date TEXT, comment TEXT,
  created_by TEXT, created_at TEXT, updated_by TEXT, updated_at TEXT,
  deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS remarks (
  block TEXT PRIMARY KEY, text TEXT, updated_by TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS line_archive (
  line_id TEXT PRIMARY KEY, po TEXT, snapshot TEXT, archived_at TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT, who TEXT, action TEXT, line_id TEXT, detail TEXT
);
"""

# plan_due is not typed by anyone: it is the line's due date at the moment its
# request date was set, so the plan can follow the PO if it slips.
LINE_FIELDS = ("status", "request_date", "paid_date", "due_date", "amount", "currency",
               "invoice_no", "comment", "pay_type", "vendor_entity", "acc_scheduled_date",
               "paid_amount", "finalised", "plan_due", "paid_currency", "paid_amount_aud")
MANUAL_FIELDS = ("entry_date", "vendor", "pay_type", "po_refs", "branch", "state", "segment",
                 "doc_no", "currency", "amount", "due_date", "request_date", "status",
                 "paid_date", "comment", "acc_scheduled_date", "paid_amount", "finalised",
                 "paid_currency", "paid_amount_aud")

# Columns added after release. CREATE TABLE IF NOT EXISTS never alters a live
# table, so every new column must also be listed here.
MIGRATIONS = {
    "line_state": {"pay_type": "TEXT", "vendor_entity": "TEXT",
                   "acc_scheduled_date": "TEXT", "paid_amount": "REAL", "finalised": "INTEGER",
                   "plan_due": "TEXT", "paid_currency": "TEXT", "paid_amount_aud": "REAL"},
    "manual_line": {"acc_scheduled_date": "TEXT", "paid_amount": "REAL", "finalised": "INTEGER",
                    "paid_currency": "TEXT", "paid_amount_aud": "REAL"},
}


def now_awst() -> str:
    return dt.datetime.now(AWST).strftime("%Y-%m-%d %H:%M")


@contextmanager
def connect(path: Path | None = None):
    with _LOCK:
        con = sqlite3.connect(str(path or DB), timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.executescript(SCHEMA)
            for table, cols in MIGRATIONS.items():
                have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
                for col, typ in cols.items():
                    if col not in have:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            yield con
            con.commit()
        finally:
            con.close()


# ------------------------------------------------------------- validation
def _date(v) -> str | None:
    if v in (None, ""):
        return None
    s = str(v).strip()[:10]
    dt.date.fromisoformat(s)          # raises ValueError on junk
    return s


def _amount(v) -> float | None:
    if v in (None, ""):
        return None
    f = float(str(v).replace(",", "").replace("$", "").strip())
    if not (-1e9 < f < 1e9):
        raise ValueError("amount out of range")
    return round(f, 2)


def clean(field: str, value):
    """Validate one user-supplied value. Raises ValueError with a readable message."""
    if field in ("request_date", "paid_date", "due_date", "entry_date", "acc_scheduled_date", "plan_due"):
        try:
            return _date(value)
        except ValueError:
            raise ValueError(f"{field.replace('_', ' ')} must be a date (YYYY-MM-DD)")
    if field in ("amount", "paid_amount", "paid_amount_aud"):
        try:
            return _amount(value)
        except ValueError:
            raise ValueError(f"{field.replace('_', ' ').replace(' aud', ' (AUD)')} must be a number")
    if field == "paid_currency":
        v = (value or "").strip().upper()
        if v and v not in PAID_CURRENCIES:
            raise ValueError(f"paid currency must be one of {', '.join(PAID_CURRENCIES)}")
        return v or None
    if field == "finalised":
        # "Finalised" = the PI / invoice amount is confirmed (the team's Excel
        # leaves such rows white; unconfirmed ones are peach). Blank = automatic.
        if value in (None, ""):
            return None
        return 1 if str(value).strip().lower() in ("1", "true", "yes", "y", "on") else 0
    if field == "status":
        v = (value or "").strip()
        if v and v not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        return v or None
    if field == "currency":
        v = (value or "").strip().upper()
        if v and v not in CURRENCIES:
            raise ValueError(f"currency must be one of {', '.join(CURRENCIES)}")
        return v or None
    if field == "pay_type":
        v = (value or "").strip()
        return v[:60] or None
    v = "" if value is None else str(value).strip()
    return v[:600] if field == "comment" else v[:200]


def _audit(con, who: str, action: str, line_id: str, detail: dict) -> None:
    con.execute("INSERT INTO audit(at, who, action, line_id, detail) VALUES (?,?,?,?,?)",
                (now_awst(), who, action, line_id, json.dumps(detail, default=str)))


# ------------------------------------------------------------------ reads
def all_line_states() -> dict[str, dict]:
    with connect() as con:
        return {r["line_id"]: dict(r) for r in con.execute("SELECT * FROM line_state")}


def all_manual(include_deleted: bool = False) -> list[dict]:
    with connect() as con:
        q = "SELECT * FROM manual_line" + ("" if include_deleted else " WHERE deleted = 0")
        return [dict(r) for r in con.execute(q + " ORDER BY id")]


def stored(line_id: str) -> dict:
    """What is recorded for one line (auto 'PO-x|kind' or manual 'M<id>'), {} if nothing."""
    with connect() as con:
        if line_id.startswith("M") and line_id[1:].isdigit():
            r = con.execute("SELECT * FROM manual_line WHERE id = ?", (int(line_id[1:]),)).fetchone()
        else:
            r = con.execute("SELECT * FROM line_state WHERE line_id = ?", (line_id,)).fetchone()
        return dict(r) if r else {}


# ------------------------------------------------------- special arrangements
# Two free-text blocks on the "Special Arrangements" tab: one for Accounts, one
# for Supply Chain. Seeded once from what each team had already written down.
REMARK_BLOCKS = {"acc": "Accounts (ACC)", "sc": "Supply Chain (SC)"}
REMARK_MAX = 4000
DEFAULT_REMARKS = {
    "acc": ("Instructions (from the Special arrangements tab of the payment request workbook):\n"
            "CSP - 14 days - prior to arrival\n"
            "FDF - 45 days - after arrival"),
    "sc": ("K-Life: the deposit is paid on the bulk (China stock) order, then allocated to the "
           "shipment POs.\n"
           "Kpower, Iron Master, Bodylonger, Imbell: no payment on their bulk orders.\n"
           "CSP: no deposit since July 2026 - one payment before arrival.\n"
           "Crossmax: 30% deposit; balance about 5 days before the ready date."),
}
DEFAULT_REMARK_WHO = {"acc": "seed: payment request workbook", "sc": "seed: SC notes (Phoenix, Idelyn)"}


def get_remarks() -> dict:
    out = {}
    with connect() as con:
        for b, label in REMARK_BLOCKS.items():
            r = con.execute("SELECT * FROM remarks WHERE block = ?", (b,)).fetchone()
            if r is None:
                con.execute("INSERT INTO remarks(block, text, updated_by, updated_at) VALUES (?,?,?,?)",
                            (b, DEFAULT_REMARKS[b], DEFAULT_REMARK_WHO[b], now_awst()))
                r = con.execute("SELECT * FROM remarks WHERE block = ?", (b,)).fetchone()
            out[b] = {"label": label, **dict(r)}
    return out


def set_remark(block: str, text, who: str) -> dict:
    if block not in REMARK_BLOCKS:
        raise ValueError("unknown remarks block")
    text = str(text or "").replace("\r\n", "\n").strip()
    if len(text) > REMARK_MAX:
        raise ValueError(f"remarks are limited to {REMARK_MAX:,} characters")
    get_remarks()                              # make sure the defaults exist first
    with connect() as con:
        before = con.execute("SELECT text FROM remarks WHERE block = ?", (block,)).fetchone()
        con.execute("INSERT OR REPLACE INTO remarks(block, text, updated_by, updated_at) VALUES (?,?,?,?)",
                    (block, text, who, now_awst()))
        _audit(con, who, f"edit {REMARK_BLOCKS[block]} remarks", f"remarks:{block}",
               {"before": before[0] if before else None, "after": text})
    return get_remarks()[block]


# ------------------------------------------------------------ payment archive
# A PO drops out of Cin7's open list when it is received, and its payment lines
# would go with it. Every line someone recorded a payment against is kept here
# as a snapshot, so the register (and the OneDrive backup) keeps its history.
def archive_lines(lines: list[dict]) -> int:
    if not lines:
        return 0
    with connect() as con:
        for ln in lines:
            con.execute("INSERT OR REPLACE INTO line_archive(line_id, po, snapshot, archived_at) "
                        "VALUES (?,?,?,?)", (ln["line_id"], ln.get("po"), json.dumps(ln, default=str),
                                             now_awst()))
    return len(lines)


def archived_snapshots() -> list[dict]:
    with connect() as con:
        return [json.loads(r["snapshot"]) for r in con.execute("SELECT snapshot FROM line_archive")]


def recent_audit(limit: int = 200) -> list[dict]:
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]


# ----------------------------------------------------------------- writes
def set_line_state(line_id: str, changes: dict, who: str) -> dict:
    """Merge `changes` into the stored state of an auto line. A value of ''
    clears the override so the line goes back to what Cin7 and the terms say."""
    vals = {f: clean(f, changes[f]) for f in LINE_FIELDS if f in changes}
    if not vals:
        raise ValueError("nothing to save")
    with connect() as con:
        cur = con.execute("SELECT * FROM line_state WHERE line_id = ?", (line_id,)).fetchone()
        row = dict(cur) if cur else {"line_id": line_id}
        before = {f: row.get(f) for f in vals}
        row.update(vals)
        row["updated_by"], row["updated_at"] = who, now_awst()
        cols = ["line_id", *LINE_FIELDS, "seed_key", "updated_by", "updated_at"]
        con.execute(f"INSERT OR REPLACE INTO line_state({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})", [row.get(c) for c in cols])
        _audit(con, who, "edit line", line_id, {"before": before, "after": vals})
    return row


def split_full_line(po_ref: str, who: str) -> bool:
    """Someone typed a deposit as the payment type of a PO's single "full"
    payment: from now on the PO is paid in two parts. Its record moves to the
    deposit line; the balance line follows from the terms, less the deposit.
    False if the PO already has a deposit record (nothing to move)."""
    dst = f"{po_ref}|deposit"
    with connect() as con:
        if con.execute("SELECT 1 FROM line_state WHERE line_id = ?", (dst,)).fetchone():
            return False
        cur = None
        for kind in ("full", "balance"):     # the full line's record, or the old balance record it reads
            cur = con.execute("SELECT * FROM line_state WHERE line_id = ?", (f"{po_ref}|{kind}",)).fetchone()
            if cur:
                break
        src = cur["line_id"] if cur else f"{po_ref}|full"
        moved = {}
        if cur:
            moved = {f: cur[f] for f in LINE_FIELDS if cur[f] not in (None, "")}
            con.execute("UPDATE line_state SET line_id = ? WHERE line_id = ?", (dst, src))
        # Logged on the deposit line, so the fields stay the person's (a workbook
        # sync never overwrites them).
        _audit(con, who, "split full payment -> deposit + balance", dst, {"from": src, "after": moved})
    return True


def backfill_plan_due(dues: dict[str, str | None]) -> int:
    """Give every planned request date that has no plan_due one: the line's due
    date now. `dues` maps a ledger line id to that line's current due date."""
    n = 0
    with connect() as con:
        for r in con.execute("SELECT line_id FROM line_state WHERE request_date IS NOT NULL "
                             "AND plan_due IS NULL").fetchall():
            due = dues.get(r["line_id"])
            if due:
                con.execute("UPDATE line_state SET plan_due = ? WHERE line_id = ?", (due, r["line_id"]))
                n += 1
    return n


def add_manual(data: dict, who: str, seed_key: str | None = None) -> int | None:
    vals = {f: clean(f, data.get(f)) for f in MANUAL_FIELDS}
    vals["pay_type"] = vals["pay_type"] or "Other"
    if vals["amount"] is None:
        raise ValueError("amount is required")
    if not vals["currency"]:
        vals["currency"] = CHARGE_TYPES.get(vals["pay_type"], "AUD")
    vals["entry_date"] = vals["entry_date"] or dt.datetime.now(AWST).date().isoformat()
    with connect() as con:
        if seed_key and con.execute("SELECT 1 FROM manual_line WHERE seed_key = ?",
                                    (seed_key,)).fetchone():
            return None
        cols = ["seed_key", *MANUAL_FIELDS, "created_by", "created_at"]
        cur = con.execute(
            f"INSERT INTO manual_line({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [seed_key, *[vals[f] for f in MANUAL_FIELDS], who, now_awst()])
        new_id = cur.lastrowid
        _audit(con, who, "add manual line", f"M{new_id}", vals)
    return new_id


def update_manual(mid: int, changes: dict, who: str) -> dict:
    vals = {f: clean(f, changes[f]) for f in MANUAL_FIELDS if f in changes}
    if "pay_type" in vals and not vals["pay_type"]:
        del vals["pay_type"]                      # a manual line always has a type
    if "amount" in vals and vals["amount"] is None:
        raise ValueError("amount is required")
    if not vals:
        raise ValueError("nothing to save")
    with connect() as con:
        cur = con.execute("SELECT * FROM manual_line WHERE id = ? AND deleted = 0",
                          (mid,)).fetchone()
        if not cur:
            raise KeyError(mid)
        before = {f: cur[f] for f in vals}
        sets = ", ".join(f"{f} = ?" for f in vals)
        con.execute(f"UPDATE manual_line SET {sets}, updated_by = ?, updated_at = ? WHERE id = ?",
                    [*vals.values(), who, now_awst(), mid])
        _audit(con, who, "edit manual line", f"M{mid}", {"before": before, "after": vals})
        return dict(con.execute("SELECT * FROM manual_line WHERE id = ?", (mid,)).fetchone())


def delete_manual(mid: int, who: str) -> None:
    """Soft delete: the row stays for the audit trail."""
    with connect() as con:
        cur = con.execute("UPDATE manual_line SET deleted = 1, updated_by = ?, updated_at = ? "
                          "WHERE id = ? AND deleted = 0", (who, now_awst(), mid))
        if not cur.rowcount:
            raise KeyError(mid)
        _audit(con, who, "delete manual line", f"M{mid}", {})


def seed_line_state(line_id: str, vals: dict, seed_key: str) -> bool:
    """Seed only where nobody has recorded anything yet."""
    with connect() as con:
        if con.execute("SELECT 1 FROM line_state WHERE line_id = ?", (line_id,)).fetchone():
            return False
        cols = ["line_id", *LINE_FIELDS, "seed_key", "updated_by", "updated_at"]
        row = {"line_id": line_id, **{f: clean(f, vals.get(f)) for f in LINE_FIELDS},
               "seed_key": seed_key, "updated_by": "seed: payment request workbook",
               "updated_at": now_awst()}
        con.execute(f"INSERT INTO line_state({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [row.get(c) for c in cols])
    return True
