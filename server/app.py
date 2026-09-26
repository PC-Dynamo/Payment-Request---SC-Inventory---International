#!/usr/bin/env python3
"""
Payment Request - SC Inventory - International - web app.

One page (white, single tab): KPIs, filterable charts, and the payment request
table. Auto lines come from the Cin7 cache; people edit status / paid date /
invoice no. / amount, and add the manual AUD (and freight) lines. All edits go
to Outputs/ledger.db, never to Cin7 and never into the Cin7 cache.

Auth: none of its own. On the SC live server nginx gates /Applications/<slug>/
through the hub's Microsoft login and forwards the signed-in address in
X-Auth-User; this container binds to loopback only.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file, send_from_directory

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
OUTPUTS = ROOT / "Outputs"
sys.path.insert(0, str(OUTPUTS))

import build_report as BR  # noqa: E402
import ledger as L  # noqa: E402
import payments as P  # noqa: E402

AWST = dt.timezone(dt.timedelta(hours=8))
app = Flask(__name__, template_folder=str(APP_DIR / "templates"))
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")

_refresh_lock = threading.Lock()
_refresh_state: dict = {"running": False, "started": None, "finished": None, "ok": None, "log": []}
_payload_cache: dict = {"mtime": None, "data": None}


def _who() -> str:
    """The signed-in user as nginx forwards it (auth_request_set overwrites any
    browser-supplied value). Used to attribute an edit, never to authorise."""
    for h in ("X-Auth-User", "X-Auth-Email", "X-Forwarded-Email"):
        v = request.headers.get(h)
        if v:
            return v.strip()[:120]
    return "unknown"


def _payload() -> dict | None:
    if not BR.PAYLOAD.exists():
        return None
    m = BR.PAYLOAD.stat().st_mtime
    if _payload_cache["mtime"] != m:
        _payload_cache["data"] = json.loads(BR.PAYLOAD.read_text(encoding="utf-8"))
        _payload_cache["mtime"] = m
    return _payload_cache["data"]


def _bad(msg: str, code: int = 400):
    return jsonify(ok=False, error=msg), code


# ----------------------------------------------------------------------- pages
@app.route("/healthz")
def healthz():
    p = _payload()
    return jsonify(ok=True, payload=bool(p),
                   built=(p or {}).get("meta", {}).get("report_built_awst"))


@app.route("/")
def index():
    return render_template("dashboard.html", who=_who())


@app.route("/vendor/<path:filename>")
def vendor(filename: str):
    return send_from_directory(OUTPUTS / "vendor", os.path.basename(filename), max_age=86400)


@app.route("/api/data")
def api_data():
    p = _payload()
    if not p:
        return _bad("report not built yet; use Refresh", 503)
    rows, when = BR.merged(p)
    meta = {**p["meta"], **when}
    return Response(json.dumps({"meta": meta, "rows": rows, "terms_table": p["terms_table"],
                                "special_arrangements": p["special_arrangements"]}),
                    mimetype="application/json", headers={"Cache-Control": "no-store"})


# ----------------------------------------------------------------------- edits
def _known_auto(line_id: str) -> bool:
    p = _payload() or {}
    return any(ln["line_id"] == line_id for ln in p.get("auto_lines") or [])


@app.route("/api/line", methods=["POST"])
def api_line():
    body = request.get_json(silent=True) or {}
    line_id = str(body.get("line_id") or "")
    changes = body.get("changes") or {}
    if not isinstance(changes, dict):
        return _bad("changes must be an object")
    today = dt.datetime.now(AWST).date()
    # Defaults only fill a date nobody has entered - never overwrite one.
    have = L.stored(line_id)
    if changes.get("status") == "Requested" and not changes.get("request_date")             and not have.get("request_date"):
        changes["request_date"] = P.next_request_day(today).isoformat()
    if changes.get("status") == "Paid" and not changes.get("paid_date")             and not have.get("paid_date"):
        changes["paid_date"] = today.isoformat()
    try:
        if line_id.startswith("M") and line_id[1:].isdigit():
            if "invoice_no" in changes:              # one name in the UI for both kinds
                changes["doc_no"] = changes.pop("invoice_no")
            if "vendor_entity" in changes:           # a manual line's payee IS its vendor
                changes["vendor"] = changes.pop("vendor_entity")
            row = L.update_manual(int(line_id[1:]), changes, _who())
        elif _known_auto(line_id):
            row = L.set_line_state(line_id, changes, _who())
        else:
            return _bad("unknown line", 404)
    except KeyError:
        return _bad("unknown line", 404)
    except ValueError as exc:
        return _bad(str(exc))
    return jsonify(ok=True, row=row)


@app.route("/api/manual", methods=["POST"])
def api_manual_add():
    body = request.get_json(silent=True) or {}
    try:
        new_id = L.add_manual(body, _who())
    except ValueError as exc:
        return _bad(str(exc))
    return jsonify(ok=True, id=new_id, line_id=f"M{new_id}")


@app.route("/api/manual/<int:mid>", methods=["DELETE"])
def api_manual_delete(mid: int):
    try:
        L.delete_manual(mid, _who())
    except KeyError:
        return _bad("unknown line", 404)
    return jsonify(ok=True)


@app.route("/api/submit-request", methods=["POST"])
def api_submit():
    """Mark every 'For Payment' line in the current request as Requested."""
    body = request.get_json(silent=True) or {}
    ids = body.get("line_ids")
    if not isinstance(ids, list) or not ids:
        return _bad("no lines selected")
    p = _payload()
    rows, when = BR.merged(p)
    by_id = {r["line_id"]: r for r in rows}
    who, done = _who(), 0
    for lid in ids[:500]:
        r = by_id.get(str(lid))
        if not r or r["status"] != "For Payment":
            continue
        ch = {"status": "Requested", "request_date": when["next_request"]}
        if r["source"] == "manual":
            L.update_manual(r["manual_id"], ch, who)
        else:
            L.set_line_state(r["line_id"], ch, who)
        done += 1
    return jsonify(ok=True, requested=done, request_date=when["next_request"])


@app.route("/api/audit")
def api_audit():
    return jsonify(L.recent_audit(300))


# ----------------------------------------------------------------------- export
@app.route("/export.xlsx", methods=["POST"])
def export_selected():
    """Export exactly the rows the page is showing (all filters applied there)."""
    p = _payload()
    if not p:
        return _bad("report not built yet", 503)
    try:
        ids = json.loads(request.form.get("line_ids") or "[]")
    except json.JSONDecodeError:
        return _bad("bad line list")
    label = str(request.form.get("label") or "")[:300]
    rows, when = BR.merged(p)
    want = {str(i) for i in ids} if isinstance(ids, list) else set()
    order = {lid: n for n, lid in enumerate(ids)} if isinstance(ids, list) else {}
    rows = sorted((r for r in rows if r["line_id"] in want), key=lambda r: order.get(r["line_id"], 0))
    return _send_xlsx(rows, p, when, label)


def _send_xlsx(rows, p, when, sub):
    buf = io.BytesIO()
    tmp = OUTPUTS / "cache" / f"export-{os.getpid()}-{threading.get_ident()}.xlsx"
    BR.write_xlsx(rows, {**p["meta"], **when, "_terms_table": p["terms_table"]}, tmp, sub)
    buf.write(tmp.read_bytes())
    tmp.unlink(missing_ok=True)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=BR.xlsx_name(),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/export.xlsx")
def export_xlsx():
    p = _payload()
    if not p:
        return _bad("report not built yet", 503)
    state = request.args.get("state", "")
    seg = request.args.get("segment", "")
    window = request.args.get("window", "window")
    rows, when = BR.merged(p)
    rows = BR.filter_rows(rows, state if state in BR.STATES else "",
                          seg if seg in ("Retail", "Commercial") else "",
                          window if window in ("request", "window", "open", "all") else "window")
    label = {"request": f"request {when['next_request']}", "window": "4-week rolling forecast",
             "open": "all unpaid", "all": "all lines"}.get(window, "")
    sub = f"{label} · {state or 'all states'} · {seg or 'Retail + Commercial'}"
    return _send_xlsx(rows, p, when, sub)


# ---------------------------------------------------------------------- refresh
def _refresh_worker(full: bool) -> None:
    log: list[str] = []
    ok = True
    try:
        steps = [[sys.executable, "update_cache.py"] + (["--full"] if full else []),
                 [sys.executable, "build_report.py"]]
        for cmd in steps:
            log.append("$ " + " ".join(cmd[1:]))
            pr = subprocess.run(cmd, cwd=str(OUTPUTS), capture_output=True, text=True, timeout=3000)
            log.extend((pr.stdout or "").strip().splitlines()[-30:])
            if pr.returncode != 0:
                log.extend((pr.stderr or "").strip().splitlines()[-30:])
                ok = False
                break
    except Exception as exc:  # noqa: BLE001
        log.append(f"error: {exc}")
        ok = False
    finally:
        _refresh_state.update(running=False, ok=ok, log=log,
                              finished=dt.datetime.now(AWST).strftime("%Y-%m-%d %H:%M AWST"))
        _refresh_lock.release()


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    if not _refresh_lock.acquire(blocking=False):
        return jsonify(ok=False, error="a refresh is already running", state=_refresh_state), 409
    full = bool((request.get_json(silent=True) or {}).get("full"))
    _refresh_state.update(running=True, ok=None, log=[], finished=None,
                          started=dt.datetime.now(AWST).strftime("%Y-%m-%d %H:%M AWST"))
    threading.Thread(target=_refresh_worker, args=(full,), daemon=True).start()
    return jsonify(ok=True, started=_refresh_state["started"])


@app.route("/api/refresh/status")
def api_refresh_status():
    return jsonify(_refresh_state)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8080")), debug=False)
