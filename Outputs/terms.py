#!/usr/bin/env python3
"""
terms.py - supplier payment terms -> the payment milestones of one PO.

The master-data app holds one free-text line per supplier ("30% Deposit, 70%
balance against BL copy") plus a few structured fields (deposit_pct, due_days,
basis). This module turns that into at most two milestones per PO:

    deposit   pct of the USD value, due on the PO date (the PI is paid to
              start production)
    balance   the rest, due relative to ETD or ETA to port
    full      100% in one payment when there is no deposit

Every milestone carries the rule it came from in plain words, so the report can
always show WHY a line is due on the date it is.

Special arrangements (the "Special arrangements" sheet of the team's payment
request workbook, and anything added later) live in overrides.json and win
over the parsed text.
"""
from __future__ import annotations

import re

from masters import _key, _norm

# Words that say nothing about WHICH supplier a name is.
_NOISE = {
    "co", "ltd", "limited", "inc", "corp", "corporation", "company", "the", "and",
    "industry", "industrial", "trade", "trading", "technology", "tech", "intl",
    "international", "enterprise", "enterprises", "group", "products", "product",
    "goods", "manufacture", "manufacturing", "factory", "equipment", "fitness",
    "sports", "sport", "sporting", "gym", "new", "material", "materials", "leisure",
    "llc", "pty", "plc", "gmbh", "co.,ltd", "e", "commerce", "electronic",
    "supply", "chain",
    # Cities and provinces: many suppliers share one, so it identifies nobody
    # ("Nantong Ruiming" is not "Nantong Kylin", "Shandong Crossmax" is not VOG).
    "qingdao", "nantong", "rizhao", "zhejiang", "shandong", "xiamen", "yongkang",
    "shenzhen", "ningbo", "hangzhou", "zibo", "qingzhou", "jiangsu", "hebei",
    "dezhou", "guangzhou", "shanghai", "fujian", "jinhua", "wuyi", "taiwan", "china",
}


def _tokens(name: str) -> list[str]:
    s = re.sub(r"[^a-z0-9 ]+", " ", _key(name))
    return [t for t in s.split() if t and t not in _NOISE]


class TermsBook:
    """Supplier payment terms, matched to Cin7 supplier names."""

    def __init__(self, terms_rows: list[dict], aliases: dict[str, str] | None = None):
        self.rows = [r for r in terms_rows if _norm(r.get("supplier_name"))]
        self.by_key = {_key(r["supplier_name"]): r for r in self.rows}
        self.aliases = {_key(k): v for k, v in (aliases or {}).items()}
        self._tok = [(set(_tokens(r["supplier_name"])), _tokens(r["supplier_name"]), r)
                     for r in self.rows]

    def match(self, supplier: str) -> dict | None:
        k = _key(supplier)
        if not k:
            return None
        if k in self.aliases:
            return self.by_key.get(_key(self.aliases[k]))
        if k in self.by_key:
            return self.by_key[k]
        toks = _tokens(supplier)
        if not toks:
            return None
        best, best_score = None, 0.0
        for tset, tlist, row in self._tok:
            if not tset:
                continue
            inter = tset & set(toks)
            if not inter:
                continue
            score = len(inter) / min(len(tset), len(toks))
            # The first distinctive word ("bodylonger", "imbell", "csp") carries
            # the identity; a shared city name ("qingdao") alone does not.
            if tlist[0] == toks[0] or tlist[0] in toks or toks[0] in tset:
                score += 0.5
            if score > best_score:
                best, best_score = row, score
        return best if best_score >= 1.0 else None


# ------------------------------------------------------------------- parsing
def _pct(v) -> float | None:
    try:
        f = float(str(v).strip().rstrip("%"))
        return f if 0 < f <= 100 else None
    except (TypeError, ValueError):
        return None


def parse_terms(row: dict | None, special: dict | None = None) -> dict:
    """-> {deposit_pct, deposit_anchor, balance_anchor, balance_offset, rule, source}

    Anchors: 'order' (PO date), 'etd', 'eta' (ETA to port)."""
    if special:
        return {
            "deposit_pct": _pct(special.get("deposit_pct")) or 0.0,
            "deposit_anchor": "order",
            "balance_anchor": special.get("anchor", "eta"),
            "balance_offset": int(special.get("offset_days", 0)),
            "rule": special.get("rule") or "special arrangement",
            "source": "special arrangement",
            "terms_text": (row or {}).get("terms") or "",
        }
    if not row or not _norm(row.get("terms")):
        return {"deposit_pct": 0.0, "deposit_anchor": "order", "balance_anchor": "eta",
                "balance_offset": -7, "source": "default",
                "rule": "no terms on file - full payment 7 days before ETA",
                "terms_text": ""}

    text = _norm(row.get("terms"))
    t = text.lower()
    basis = _key(row.get("basis"))
    due_days = row.get("due_days")
    try:
        due_days = int(due_days) if due_days not in (None, "") else None
    except (TypeError, ValueError):
        due_days = None

    pcts = [float(p) for p in re.findall(r"(\d+(?:\.\d+)?)\s*%", t)]
    dep = _pct(row.get("deposit_pct"))
    if dep is None and pcts and pcts[0] < 100:
        dep = pcts[0]
    if dep is not None and dep >= 100:
        dep = 0.0          # 100% "deposit" = one full payment
    dep = dep or 0.0

    # ---- the balance (or the single full payment): first matching rule wins
    anchor, offset, why = None, 0, ""
    m = re.search(r"(\d+)\s*days?\s*(?:prior|before)\s*(?:to\s*)?(?:the\s*)?(?:shipment\s*)?arri[ve]", t)
    if m:
        anchor, offset, why = "eta", -int(m.group(1)), f"{m.group(1)} days before ETA"
    elif re.search(r"(one|1)\s*week\s*before", t) and re.search(r"arri[ve]", t):
        anchor, offset, why = "eta", -7, "7 days before ETA"
    elif re.search(r"(before|prior)\b[^.;]*arri[ve]", t):
        anchor, offset, why = "eta", -7, "before arrival - 7 days before ETA"
    elif re.search(r"arri[ve]", t):
        anchor, offset, why = "eta", 0, "on arrival at port (ETA)"
    elif re.search(r"receipt|received", t) or basis == "goods received":
        n = due_days or int((re.search(r"within\s*(\d+)\s*days", t) or [0, 0])[1] or 0)
        anchor, offset, why = "eta", n, f"{n} days after arrival (ETA)"
    elif "sailing" in t or basis == "sailing date":
        n = due_days if due_days is not None else int((re.search(r"(\d+)\s*days", t) or [0, 0])[1] or 0)
        anchor, offset, why = "etd", n, f"{n} days after sailing (ETD)"
    elif re.search(r"after\s*shipment", t):
        n = due_days if due_days is not None else 7
        anchor, offset, why = "etd", n, f"{n} days after shipment (ETD)"
    elif re.search(r"b/?l\b|bl copy|shipping doc|at sight", t) or basis == "b/l copy":
        anchor, offset, why = "etd", 7, "against B/L copy - ETD + 7 days"
    elif re.search(r"before[^.;]*(ship|load|booking|deliver)", t):
        anchor, offset, why = "etd", -7, "before shipment - 7 days before ETD"
    elif re.search(r"order finished|mass production|production", t):
        anchor, offset, why = "etd", -14, "when production finishes - 14 days before ETD"
    elif re.search(r"advance|prepaid|t/t in advance", t) or basis == "prepaid":
        anchor, offset, why = "order", 0, "paid in advance on the PO date"
    elif basis == "shipment":
        anchor, offset, why = "etd", -7, "before shipment - 7 days before ETD"
    else:
        anchor, offset, why = "eta", -7, "terms unclear - 7 days before ETA"

    if dep and anchor == "order":
        dep = 0.0            # "30% deposit ... 100% advance" collapses to one payment
    rule = (f"{dep:g}% deposit on PO date; {100 - dep:g}% balance {why}" if dep
            else f"100% {why}")
    return {"deposit_pct": dep, "deposit_anchor": "order", "balance_anchor": anchor,
            "balance_offset": offset, "rule": rule, "source": "master-data",
            "terms_text": text}
