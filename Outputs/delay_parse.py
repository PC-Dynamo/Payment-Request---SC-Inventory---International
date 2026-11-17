#!/usr/bin/env python3
# Vendored from the Shipment Schedule app (PC-Dynamo/Shipment-Schedule-for-
# International-Open-Order, Outputs/delay_parse.py as of main 87f9904, 15 Oct
# 2026) so this app reads the Order Department's Cin7 notes the same way, at its
# own refresh (17 Nov 2026). Unchanged below this header - keep the two in step.
"""
delay_parse.py - read Cin7 purchase-order `internalComments` and recover the
shipment's ETA history, so the report can show how far a PO has slipped from
the ETA that was FIRST entered.

Why this exists
---------------
Cin7 has no ETA field. The team records the arrival date in `estimatedDeliveryDate`
(the field Cin7 labels "ETD") and overwrites it every time the forwarder revises.
The audit trail therefore lives only in `internalComments`, which the order
department maintains as a newest-first log of timestamped entries:

    2 Sep 2026 4:48 pm - Dynamo Fitness Order Department
    not loaded with PO-130435 due to space
    new arrangement: ready Sept 10 + 6 weeks = Oct 22 + 10 days = ETA Nov 1

    31 Aug 2026 10:28 am - Dynamo Fitness Order Department
    As per Ice Cargo update report 31 Aug
    ETA 7 Oct 2026 + 7 days buffer = ETA 14 Oct 2026

Each entry states the ETA in force at that moment. The OLDEST entry that states
one is the original commitment; the newest is current. The difference is the
slip that marketing needs for pre-orders and the warehouse needs for loading.

This module is READ-ONLY on Cin7 - it only parses text that is already there.
Everything it produces is advisory and labelled as parsed, never as Cin7 fact.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------- months
_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

# entry header: "2 Sep 2026 4:48 pm - Dynamo Fitness Order Department"
_ENTRY_RE = re.compile(
    r"^[ \t]*(?P<d>\d{1,2})\s+(?P<mon>" + _MONTH_ALT + r")\s+(?P<y>\d{4})\s+"
    r"(?P<h>\d{1,2}):(?P<mi>\d{2})\s*(?P<ap>am|pm)\s*-\s*(?P<author>.*?)\s*$",
    re.I | re.M,
)

# A concrete calendar date inside prose: "14 Oct 2026", "Nov 1", "10 nov", "Sept 17".
# The (?!\d) guards matter: without them "Apr 2026" parses as April the 20th,
# because \d{1,2} happily takes the "20" out of the year.
_DAY_MON = re.compile(
    r"\b(?P<d>\d{1,2})(?!\d)(?:st|nd|rd|th)?\s+(?P<mon>" + _MONTH_ALT + r")\b\.?"
    r"(?:\s*,?\s*(?P<y>20\d{2})\b)?", re.I)
_MON_DAY = re.compile(
    r"\b(?P<mon>" + _MONTH_ALT + r")\b\.?\s+(?P<d>\d{1,2})(?!\d)(?:st|nd|rd|th)?"
    r"(?:\s*,?\s*(?P<y>20\d{2})\b)?", re.I)
_ISO = re.compile(r"\b(?P<y>20\d{2})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b")
_DMY = re.compile(r"\b(?P<d>\d{1,2})[/.](?P<m>\d{1,2})[/.](?P<y>20\d{2}|\d{2})\b")

# "+ 7 days buffer", "+ 10 days", "+7 day buffer"
_BUFFER_RE = re.compile(r"\+\s*(\d{1,2})\s*(?:day|days)\s*(?:buffer)?", re.I)
# "without buffer" / "no buffer" is an explicit statement that this revision
# carries none - it must reset the figure, not inherit the previous entry's.
_NO_BUFFER_RE = re.compile(r"\b(?:without|no|w/?o)\s+buffer\b", re.I)

_ETA_TOKEN = re.compile(r"\bE\.?T\.?A\.?\b", re.I)
_ETD_TOKEN = re.compile(r"\bE\.?T\.?D\.?\b", re.I)
_READY_TOKEN = re.compile(r"\b(?:ready|ex[- ]?factory|finish(?:ed)?)\b", re.I)
# "As per MF update report 31 Aug" - that date is the FORWARDER'S report date,
# never an ETA. Captured separately so it cannot pollute the ETA trail.
_SOURCE_RE = re.compile(r"as\s+per\s+(?P<who>.{0,28}?)\s*(?:update\s*)?report\b", re.I)
_NO_CHANGE_RE = re.compile(r"\bno\s+change\b", re.I)

# forwarder attribution, e.g. "As per MF update report 31 Aug"
_FORWARDERS = [
    (re.compile(r"\bice\s*cargo\b", re.I), "Ice Cargo"),
    (re.compile(r"\bmainfreight\b|\bMF\b", re.I), "Mainfreight"),
    (re.compile(r"\bdhl\b", re.I), "DHL"),
    (re.compile(r"\bfedex\b", re.I), "FedEx"),
]

# Delay causes, ordered most specific first. The label is what the report shows
# in place of the manual "Reason of Delay" column.
_REASONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"typhoon", re.I), "Typhoon / severe weather at origin"),
    (re.compile(r"\bbad weather\b|\bweather\b", re.I), "Bad weather"),
    (re.compile(r"port\s*congestion|congestion\s*(?:in|at)?", re.I), "Port congestion"),
    (re.compile(r"blank\s*sail|blank\s*vessel", re.I), "Blank sailing"),
    (re.compile(r"roll(?:ed)?\s*over|rollover|roll\s*on", re.I), "Rolled over to next vessel"),
    (re.compile(r"transship|tranship", re.I), "Transhipment delay"),
    (re.compile(r"(?:due to|because of|no|lack of|space)\s*(?:space|constraint)|"
                r"not\s+loaded|space\s*constraint", re.I), "Vessel space / not loaded"),
    (re.compile(r"\bcustoms\b|\bquarantine\b|\bAQIS\b|\binspection\b", re.I),
     "Customs / quarantine hold"),
    (re.compile(r"\bstrike\b|industrial action", re.I), "Industrial action"),
    (re.compile(r"production\s*delay|factory\s*delay|not\s*ready|delay\s*(?:in\s*)?production",
                re.I), "Production delay at supplier"),
    (re.compile(r"\bqc\b|quality\s*(?:issue|problem)|re-?work", re.I), "QC / rework"),
    (re.compile(r"deposit|payment\s*(?:not|pending|delay)|await.*payment", re.I),
     "Awaiting payment / deposit"),
    (re.compile(r"warehouse\s*space|our\s*request|as\s*requested\s*by\s*(?:accounts|us)"
                r"|deferred|defer\s*the|hold\s*(?:the\s*)?ship", re.I),
     "Re-planned by us (not a supplier or carrier delay)"),
    (re.compile(r"\bdelay(?:ed)?\b|\blate\b|postpone|push(?:ed)?\s*(?:back|to)", re.I),
     "Delayed (reason not stated)"),
]

_COMBINE_RE = re.compile(r"\b(PO-\d+[A-Z]?)\b", re.I)


# ------------------------------------------------------------------ utilities
def _mk_date(y: int, m: int, d: int) -> dt.date | None:
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def _resolve_year(cand: dt.date, ref: dt.date) -> dt.date:
    """Comments omit the year. An ETA is stated forward of the comment, so a
    candidate that lands well before the comment date belongs to next year (and
    one far ahead of it, to last year)."""
    best = cand
    for shift in (0, 1, -1):
        try:
            c = cand.replace(year=cand.year + shift)
        except ValueError:
            continue
        # accept a window of -45 .. +330 days around the comment stamp
        if -45 <= (c - ref).days <= 330:
            return c
        if abs((c - ref).days) < abs((best - ref).days):
            best = c
    return best


# A stated ETA more than this many days BEFORE the comment that states it is a
# typed-year mistake, not a real date. Seen live: "ETA 23 Dec + 10 days buffer =
# ETA 2 Jan 2025" written on 17 Nov 2025, where 2026 was meant. Corrected here
# and counted in `year_fixes` so the report can report the entry-quality issue
# instead of hiding it.
_BACKDATE_TOLERANCE_DAYS = 60


def _find_dates(text: str, ref: dt.date,
                notes: list[str] | None = None) -> list[tuple[int, dt.date]]:
    """All concrete dates in `text` as (character offset, date), in order."""
    found: list[tuple[int, dt.date]] = []

    def _keep(off: int, d: dt.date | None, explicit_year: bool, raw: str) -> None:
        if not d:
            return
        if explicit_year and (ref - d).days > _BACKDATE_TOLERANCE_DAYS:
            fixed = _resolve_year(d, ref)
            if fixed != d:
                if notes is not None:
                    notes.append(f"'{raw.strip()}' read as {fixed} "
                                 f"(stated year is before the comment date)")
                d = fixed
        found.append((off, d))

    for m in _ISO.finditer(text):
        _keep(m.start(), _mk_date(int(m["y"]), int(m["m"]), int(m["d"])), True, m.group(0))
    for m in _DMY.finditer(text):
        y = int(m["y"])
        y += 2000 if y < 100 else 0
        _keep(m.start(), _mk_date(y, int(m["m"]), int(m["d"])), True, m.group(0))
    for rx in (_DAY_MON, _MON_DAY):
        for m in rx.finditer(text):
            mon = _MONTHS.get(m["mon"].lower())
            day = int(m["d"])
            if not mon or not (1 <= day <= 31):
                continue
            if m["y"]:
                _keep(m.start(), _mk_date(int(m["y"]), mon, day), True, m.group(0))
            else:
                base = _mk_date(ref.year, mon, day)
                _keep(m.start(), _resolve_year(base, ref) if base else None,
                      False, m.group(0))
    # de-duplicate overlapping matches: keep the first date found at each offset
    found.sort(key=lambda t: t[0])
    out: list[tuple[int, dt.date]] = []
    for off, d in found:
        if out and off - out[-1][0] < 3 and d == out[-1][1]:
            continue
        out.append((off, d))
    return out


# --------------------------------------------------------------------- models
@dataclass
class Entry:
    """One timestamped block of an internalComments log."""
    stamp: dt.datetime
    author: str
    body: str
    eta: dt.date | None = None            # ETA in force after this entry
    eta_shipper: dt.date | None = None    # forwarder's raw ETA, pre-buffer
    buffer_days: int | None = None
    etd: dt.date | None = None
    # Which kind of date the ETD came from: "etd" where the entry actually
    # stated a departure, "ready" where it only gave a factory readiness date
    # and that was used to anchor the arithmetic. They are NOT the same thing -
    # a readiness date says when the goods exist, not when they sailed.
    etd_source: str | None = None
    forwarder: str | None = None
    reason: str | None = None
    no_change: bool = False               # entry reaffirmed the standing ETA
    combine_with: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "stamp": self.stamp.isoformat(timespec="minutes"),
            "author": self.author,
            "body": self.body,
            "eta": self.eta.isoformat() if self.eta else None,
            "eta_shipper": self.eta_shipper.isoformat() if self.eta_shipper else None,
            "buffer_days": self.buffer_days,
            "etd": self.etd.isoformat() if self.etd else None,
            "etd_source": self.etd_source,
            "forwarder": self.forwarder,
            "reason": self.reason,
            "no_change": self.no_change,
            "combine_with": self.combine_with,
        }


def _parse_entry(stamp: dt.datetime, author: str, body: str,
                 notes: list[str] | None = None) -> Entry:
    e = Entry(stamp=stamp, author=author.strip(), body=body.strip())
    ref = stamp.date()

    for rx, name in _FORWARDERS:
        if rx.search(body):
            e.forwarder = name
            break

    for rx, label in _REASONS:
        if rx.search(body):
            e.reason = label
            break

    e.combine_with = sorted({m.group(1).upper() for m in _COMBINE_RE.finditer(body)})
    e.no_change = bool(_NO_CHANGE_RE.search(body))

    # Parse LINE BY LINE, and within a line anchor each date to the token that
    # introduces it. Taking "the first date on the line" as the ETD is wrong:
    #   "ETD end of January + 7 weeks = March 20 + 10 days = ETA March 30"
    # has a vague ETD and TWO arrival dates, and March 20 is the pre-buffer
    # arrival, not a departure.
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Strip the forwarder attribution so its report date is out of scope.
        src = _SOURCE_RE.search(line)
        scan = line[src.end():] if src else line
        lds = _find_dates(scan, ref, notes)
        if not lds:
            continue

        def anchored(token_rx: re.Pattern, max_gap: int = 30) -> list[dt.date]:
            """Dates that belong to `token_rx` - immediately to its right, with
            no arithmetic in between.

            The gap test alone is not enough. In
                "ETD end of January + 7 weeks = March 20 + 10 days = ETA March 30"
            the ETD is the unparseable "end of January", yet "March 20" sits only
            28 characters away and was being recorded as the departure date. A
            "+" or "=" between the token and the date means the date is the
            RESULT of a calculation, not the token's own value, so it is rejected;
            that still admits real forms like "ETD Nantong 22 Nov 2025".
            """
            hits: list[dt.date] = []
            for tm in token_rx.finditer(scan):
                for off, dd in lds:
                    if not (tm.end() <= off <= tm.end() + max_gap):
                        continue
                    between = scan[tm.end():off]
                    if "+" in between or "=" in between:
                        break
                    hits.append(dd)
                    break
            return hits

        eta_hits = anchored(_ETA_TOKEN)
        # A readiness date is accepted as the ETD anchor because many entries
        # give nothing else - but it is recorded as such, because "ready 11 Jun"
        # followed by a receipt on 19 Jun is not an 8-day sea voyage, it is a
        # planned readiness date that the vessel has nothing to do with.
        etd_hits, etd_kind = anchored(_ETD_TOKEN), "etd"
        if not etd_hits:
            etd_hits, etd_kind = anchored(_READY_TOKEN), "ready"
        no_buf = bool(_NO_BUFFER_RE.search(line))
        buf = _BUFFER_RE.search(scan)

        if etd_hits:
            e.etd = etd_hits[0]
            e.etd_source = etd_kind

        if eta_hits or (_ETA_TOKEN.search(scan) and len(lds) >= 2):
            # The chain reads left to right and ENDS on the effective ETA:
            # "ETA 7 Oct + 7 days buffer = ETA 14 Oct" -> 14 Oct.
            e.eta = eta_hits[-1] if eta_hits else lds[-1][1]
            if len(eta_hits) >= 2:
                e.eta_shipper = eta_hits[0]
            elif len(lds) >= 2 and lds[-1][1] == e.eta:
                # One "ETA" token at the end of an arithmetic chain: the date
                # immediately before it is the pre-buffer arrival.
                prev = lds[-2][1]
                if prev != e.eta and prev not in etd_hits:
                    e.eta_shipper = prev
            else:
                e.eta_shipper = e.eta
            if no_buf:
                e.buffer_days = 0
            elif buf:
                e.buffer_days = int(buf.group(1))
        elif "=" in line and len(lds) >= 2:
            # No ETA keyword, but an arithmetic chain still lands on an arrival:
            # "ready Sept 10 + 6 weeks = Oct 22 + 10 days = Nov 1".
            e.eta = lds[-1][1]
            if len(lds) >= 3:
                e.eta_shipper = lds[-2][1]
            if no_buf:
                e.buffer_days = 0
            elif buf:
                e.buffer_days = int(buf.group(1))
    return e


def etd_source_of(entry: dict) -> str | None:
    """What kind of date gave a stored comment-log entry its ETD.

    "etd" where the entry stated a departure, "ready" where it only gave a
    factory readiness date. Entries written before the parser recorded this are
    re-parsed rather than guessed at from their text, because an entry can
    mention ETD in a form that cannot be dated ("ETD mid March") and still carry
    a readiness date that can.
    """
    if entry.get("etd_source"):
        return entry["etd_source"]
    if not entry.get("etd"):
        return None
    return _parse_entry(dt.datetime.fromisoformat(entry["stamp"]),
                        entry.get("author") or "",
                        entry.get("body") or "").etd_source


def parse_entries(comments: str | None,
                  notes: list[str] | None = None) -> list[Entry]:
    """Split an internalComments blob into entries, OLDEST FIRST.

    Cin7 stores the log newest-first; the report reasons chronologically, so the
    return order is reversed to oldest-first regardless of how it was stored."""
    if not comments or not comments.strip():
        return []
    text = comments.replace("\r\n", "\n").replace("\r", "\n")
    marks = list(_ENTRY_RE.finditer(text))
    if not marks:
        return []
    out: list[Entry] = []
    for i, m in enumerate(marks):
        body = text[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(text)]
        hour = int(m["h"]) % 12 + (12 if m["ap"].lower() == "pm" else 0)
        stamp = dt.datetime(int(m["y"]), _MONTHS[m["mon"].lower()], int(m["d"]),
                            hour, int(m["mi"]))
        out.append(_parse_entry(stamp, m["author"], body, notes))
    out.sort(key=lambda e: e.stamp)
    return out


# ------------------------------------------------------------------- rollup
def analyse(comments: str | None, cin7_eta: dt.date | None = None,
            pinned_baseline: str | None = None) -> dict:
    """Condense a PO's comment log into the fields the report shows.

    `cin7_eta`        - the live `estimatedDeliveryDate`, which is authoritative
                        for the CURRENT ETA.
    `pinned_baseline` - an ISO date already pinned in the cache from an earlier
                        run (or seeded from the manual workbook). Once set it is
                        never recomputed, so the baseline cannot drift when the
                        team edits an old comment.
    """
    notes: list[str] = []
    entries = parse_entries(comments, notes)
    with_eta = [e for e in entries if e.eta]

    baseline = None
    baseline_source = None
    if pinned_baseline:
        try:
            baseline = dt.date.fromisoformat(pinned_baseline)
            baseline_source = "pinned"
        except ValueError:
            baseline = None
    if baseline is None and with_eta:
        baseline = with_eta[0].eta
        baseline_source = "first comment"
    if baseline is None and cin7_eta:
        baseline = cin7_eta
        baseline_source = "cin7 (no comment history)"

    latest = with_eta[-1] if with_eta else None
    current = cin7_eta or (latest.eta if latest else None)

    # distinct ETAs in chronological order = the revision trail
    revisions: list[str] = []
    for e in with_eta:
        iso = e.eta.isoformat()
        if not revisions or revisions[-1] != iso:
            revisions.append(iso)
    if current and (not revisions or revisions[-1] != current.isoformat()):
        revisions.append(current.isoformat())

    delay_days = (current - baseline).days if (current and baseline) else None

    reason = None
    reason_stamp = None
    for e in reversed(entries):
        if e.reason:
            reason = e.reason
            reason_stamp = e.stamp.isoformat(timespec="minutes")
            break

    buffer_days = None
    for e in reversed(entries):
        if e.buffer_days is not None:
            buffer_days = e.buffer_days
            break

    forwarder = None
    for e in reversed(entries):
        if e.forwarder:
            forwarder = e.forwarder
            break

    etd = etd_source = None
    for e in reversed(entries):
        if e.etd:
            etd, etd_source = e.etd, e.etd_source
            break

    eta_shipper = None
    for e in reversed(entries):
        if e.eta_shipper:
            eta_shipper = e.eta_shipper
            break

    # The FIRST forwarder-sourced ETA is the shipping baseline - the date that
    # was true once the cargo was actually booked on a vessel. Everything before
    # it is procurement planning off a promised readiness date. Splitting the
    # slip at this point separates "the goods were not ready" from "the shipment
    # ran late", which are different problems with different owners.
    booked_eta = None
    booked_stamp = None
    for e in entries:
        if e.forwarder and e.eta:
            booked_eta = e.eta
            booked_stamp = e.stamp.isoformat(timespec="minutes")
            break

    combine: list[str] = []
    for e in reversed(entries):
        if e.combine_with:
            combine = e.combine_with
            break

    return {
        "entries": [e.as_dict() for e in entries],
        "entry_count": len(entries),
        "baseline_eta": baseline.isoformat() if baseline else None,
        "baseline_source": baseline_source,
        "current_eta": current.isoformat() if current else None,
        "eta_revisions": revisions,
        "revision_count": max(0, len(revisions) - 1),
        "delay_days": delay_days,
        "buffer_days": buffer_days,
        "delay_reason": reason,
        "delay_reason_stamp": reason_stamp,
        "forwarder": forwarder,
        "etd_parsed": etd.isoformat() if etd else None,
        "etd_source": etd_source,
        "eta_shipper": eta_shipper.isoformat() if eta_shipper else None,
        "booked_eta": booked_eta.isoformat() if booked_eta else None,
        "booked_stamp": booked_stamp,
        "combine_with": combine,
        "last_comment_stamp": entries[-1].stamp.isoformat(timespec="minutes") if entries else None,
        "last_comment_author": entries[-1].author if entries else None,
        "last_comment_body": entries[-1].body if entries else None,
        "data_notes": notes,
    }


def delay_band(delay_days: int | None, buffer_days: int | None) -> str:
    """Traffic light for the table. Buffer absorbs slip before it is a problem."""
    if delay_days is None:
        return "unknown"
    net = delay_days - (buffer_days or 0)
    if delay_days <= 0:
        return "on-time"
    if net <= 0:
        return "within-buffer"
    if net <= 7:
        return "minor"
    if net <= 21:
        return "major"
    return "critical"
