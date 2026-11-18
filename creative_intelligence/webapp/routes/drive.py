"""Google Drive: connect, and export AI ads for agencies."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

from creative_intelligence.webapp import common
from creative_intelligence.webapp.common import _asset_ids, _product_dict

bp = Blueprint("drive", __name__)
log = logging.getLogger("adstudio")
_HERE = Path(__file__).parent.parent


# ── Google Drive: export AI ads to a new folder for outside agencies ───────
_DRIVE_STATES: dict[str, tuple[float, str]] = {}   # OAuth state → (expiry, product id)


def _drive_auth(conn) -> dict[str, Any] | None:
    r = conn.execute("SELECT email, refresh_token, connected_at FROM adstudio_drive_auth WHERE id=1").fetchone()
    return dict(r) if r else None


def _drive_parent() -> str:
    from creative_intelligence import config
    return os.getenv("ADSTUDIO_DRIVE_FOLDER_ID", config.ADSTUDIO_DRIVE_FOLDER_ID)


def _drive_redirect_uri() -> str:
    proto = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip()
    return f"{proto}://{request.host}/drive/callback"


@bp.get("/api/drive/status")
def drive_status() -> Any:
    from creative_intelligence.adstudio import drive
    conn = common.db()
    auth = _drive_auth(conn)
    conn.close()
    parent = _drive_parent()
    return jsonify({"configured": drive.configured(), "connected": bool(auth),
                    "email": auth["email"] if auth else "",
                    "folder_url": drive.folder_url(parent) if parent else "",
                    "redirect_uri": _drive_redirect_uri()})


@bp.get("/drive/connect")
def drive_connect() -> Any:
    import secrets, time
    from flask import redirect
    from creative_intelligence.adstudio import drive
    if not drive.configured():
        return ("Google Drive isn't set up: add GOOGLE_OAUTH_CLIENT_ID and "
                "GOOGLE_OAUTH_CLIENT_SECRET to the environment (see .env.example).", 400)
    now = time.time()
    for k in [k for k, v in _DRIVE_STATES.items() if v[0] < now]:
        _DRIVE_STATES.pop(k, None)
    state = secrets.token_urlsafe(24)
    pid = request.args.get("product_id", "")
    _DRIVE_STATES[state] = (now + 600, pid if pid.isdigit() else "")
    return redirect(drive.auth_url(_drive_redirect_uri(), state))


@bp.get("/drive/callback")
def drive_callback() -> Any:
    import time
    from flask import redirect
    from creative_intelligence.adstudio import drive
    exp, pid = _DRIVE_STATES.pop(request.args.get("state", ""), (0, ""))
    if exp < time.time():
        return ("This sign-in link expired or didn't come from Ad Studio — try Connect again.", 400)
    back = "/gallery/assets?" + (f"product_id={pid}&" if pid else "")
    if request.args.get("error"):
        return redirect(back + "drive=declined")
    try:
        token, email = drive.exchange_code(request.args.get("code", ""), _drive_redirect_uri())
    except drive.DriveError as exc:
        return (f"Google sign-in failed: {exc}", 400)
    from creative_intelligence import config
    allowed = os.getenv("ADSTUDIO_DRIVE_ACCOUNT", config.ADSTUDIO_DRIVE_ACCOUNT).strip().lower()
    if allowed and email != allowed:
        return (f"Signed in as {email or 'an unknown account'}, but exports may only use {allowed}. "
                "Sign in with that account.", 403)
    conn = common.db()
    with conn:
        conn.execute("INSERT OR REPLACE INTO adstudio_drive_auth (id, email, refresh_token, connected_at)"
                     " VALUES (1, ?, ?, datetime('now'))", (email, token))
    conn.close()
    return redirect(back + "drive=connected")


@bp.post("/api/drive/disconnect")
def drive_disconnect() -> Any:
    conn = common.db()
    with conn:
        conn.execute("DELETE FROM adstudio_drive_auth WHERE id=1")
    conn.close()
    return jsonify({"ok": True})


def _drive_token(conn) -> str:
    from creative_intelligence.adstudio import drive
    auth = _drive_auth(conn)
    if not auth:
        raise drive.DriveError("Google Drive isn't connected — click “Connect Google Drive” first.")
    return drive.access_token(auth["refresh_token"])


@bp.post("/api/adstudio/drive-export")
def adstudio_drive_export_start() -> Any:
    """
    Body: {product_id, ids, date: 'YYYY-MM-DD' (the user's local date)}.
    Checks every ad can go to an agency, then makes
    Meta Ads / <Product> / '<date> · <Product> · N AI ads'. The frontend then
    uploads the ads one by one (…/upload/<ad_id>) and calls …/finish.
    """
    import re as _re
    from datetime import date as _date
    from creative_intelligence.adstudio import drive
    d = request.get_json(force=True, silent=True) or {}
    pid = int(d.get("product_id") or 0)
    ids = list(dict.fromkeys(int(x) for x in d.get("ids", []) if str(x).isdigit()))
    day = d.get("date") if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d.get("date", ""))) else _date.today().isoformat()
    if not ids:
        return jsonify({"error": "Select some ads to export."}), 400
    conn = common.db()
    prod = _product_dict(conn, pid)
    if prod is None:
        conn.close(); return jsonify({"error": "product not found"}), 404
    ph = ",".join("?" * len(ids))
    rows = {r["id"]: dict(r) for r in conn.execute(
        f"SELECT id, product_id, status, image_path, text_checked, review_status FROM adstudio_textads"
        f" WHERE id IN ({ph})", ids).fetchall()}
    problems = {
        "not found / other product": [i for i in ids if i not in rows or rows[i]["product_id"] != pid],
        "not rendered yet": [i for i in ids if i in rows and (rows[i]["status"] != "done" or not rows[i]["image_path"])],
        "text not checked": [i for i in ids if i in rows and not rows[i]["text_checked"]],
        "rejected": [i for i in ids if i in rows and rows[i]["review_status"] == "rejected"],
    }
    bad = {k: v for k, v in problems.items() if v}
    if bad:
        conn.close()
        return jsonify({"error": "Some selected ads can't go to an agency: "
                                 + "; ".join(f"{len(v)} {k}" for k, v in bad.items()), "problems": bad}), 400
    parent = _drive_parent()
    try:
        token = _drive_token(conn)
        if not drive.get_folder(token, parent):
            raise drive.DriveError("The Meta Ads export folder is missing or in the trash "
                                   "(ADSTUDIO_DRIVE_FOLDER_ID).")
        pfolder = prod.get("drive_folder_id")
        if not pfolder or not drive.get_folder(token, pfolder):
            pname = drive.clean(prod["name"])
            pfolder = drive.find_folder(token, pname, parent) or drive.create_folder(token, pname, parent)
            with conn:
                conn.execute("UPDATE adstudio_products SET drive_folder_id=? WHERE id=?", (pfolder, pid))
        name = drive.export_folder_name(prod["name"], day, len(ids), drive.child_names(token, pfolder))
        fid = drive.create_folder(token, name, pfolder)
    except drive.DriveError as exc:
        conn.close(); return jsonify({"error": str(exc)}), 502
    with conn:
        cur = conn.execute(
            "INSERT INTO adstudio_drive_exports (product_id, folder_id, folder_name, folder_url, n_ads, ad_ids)"
            " VALUES (?,?,?,?,?,?)", (pid, fid, name, drive.folder_url(fid), len(ids), json.dumps(ids)))
    conn.close()
    return jsonify({"export_id": cur.lastrowid, "folder_name": name, "folder_url": drive.folder_url(fid),
                    "product_folder_url": drive.folder_url(pfolder), "ids": ids})


@bp.post("/api/adstudio/drive-export/<int:eid>/upload/<int:ad_id>")
def adstudio_drive_export_upload(eid: int, ad_id: int) -> Any:
    from creative_intelligence.adstudio import drive
    conn = common.db()
    e = conn.execute("SELECT * FROM adstudio_drive_exports WHERE id=?", (eid,)).fetchone()
    ids = json.loads(e["ad_ids"]) if e else []
    if e is None or ad_id not in ids:
        conn.close(); return jsonify({"error": "not part of this export"}), 404
    done = conn.execute("SELECT file_url FROM adstudio_drive_export_items WHERE export_id=? AND textad_id=?",
                        (eid, ad_id)).fetchone()
    if done:                                  # retry after a dropped response — don't upload twice
        conn.close(); return jsonify({"ok": True, "file_url": done["file_url"]})
    ad = dict(conn.execute("SELECT * FROM adstudio_textads WHERE id=?", (ad_id,)).fetchone())
    prod = _product_dict(conn, e["product_id"]) or {}
    asset_name = _asset_ids(conn, "textad", [ad_id]).get(ad_id)
    name = f"{asset_name}.png" if asset_name else drive.ad_file_name(prod.get("name", "ad"), ids.index(ad_id) + 1, ad)
    try:
        if not Path(ad["image_path"] or "").exists():
            raise drive.DriveError("image file is missing on the server")
        f = drive.upload_file(_drive_token(conn), ad["image_path"], name, e["folder_id"])
    except drive.DriveError as exc:
        conn.close(); return jsonify({"error": str(exc)}), 502
    with conn:
        conn.execute("INSERT OR IGNORE INTO adstudio_drive_export_items (export_id, textad_id, file_id,"
                     " file_name, file_url) VALUES (?,?,?,?,?)", (eid, ad_id, f["id"], name, f.get("webViewLink")))
    conn.close()
    return jsonify({"ok": True, "file_url": f.get("webViewLink")})


@bp.post("/api/adstudio/drive-export/<int:eid>/finish")
def adstudio_drive_export_finish(eid: int) -> Any:
    """Add the 'Ad copy' Google Sheet (one row per uploaded ad) and close the export."""
    import csv, io
    from creative_intelligence.adstudio import drive
    conn = common.db()
    e = conn.execute("SELECT * FROM adstudio_drive_exports WHERE id=?", (eid,)).fetchone()
    if e is None:
        conn.close(); return jsonify({"error": "not found"}), 404
    rows = conn.execute(
        "SELECT i.file_name, i.file_url, i.textad_id, t.kind, t.variant, t.title, t.headline, t.subhead, t.cta, t.body,"
        " t.aspect_ratio, b.brief_json FROM adstudio_drive_export_items i"
        " JOIN adstudio_textads t ON t.id=i.textad_id LEFT JOIN adstudio_briefs b ON b.id=t.brief_id"
        " WHERE i.export_id=? ORDER BY i.file_name", (eid,)).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    # Agency-facing: no internal review notes.
    # "Meta ad name" is the Asset ID: the agency names the ad with it so results link back.
    # "AI-generated" tells the agency to disclose AI content when uploading.
    aid = _asset_ids(conn, "textad", [r["textad_id"] for r in rows])
    w.writerow(["file", "Meta ad name", "format", "on-image headline", "on-image subhead", "CTA button",
                "primary text", "angle", "aspect ratio", "AI-generated", "link"])
    for r in rows:
        bj = json.loads(r["brief_json"] or "{}")
        fmt = r["title"] if r["kind"] == "variant" else f"Scene: {r['title']}"
        w.writerow([r["file_name"], aid.get(r["textad_id"], ""), fmt, r["headline"], r["subhead"], r["cta"],
                    r["body"] or "", bj.get("angle", ""), r["aspect_ratio"], "yes", r["file_url"] or ""])
    sheet_url = None
    try:
        if rows:
            s = drive.upload(_drive_token(conn), f"Ad copy — {e['folder_name']}", e["folder_id"],
                             buf.getvalue().encode(), "text/csv", as_mime=drive.SHEET)
            sheet_url = s.get("webViewLink")
    except drive.DriveError as exc:
        conn.close(); return jsonify({"error": f"Ads uploaded, but the copy sheet failed: {exc}"}), 502
    with conn:
        conn.execute("UPDATE adstudio_drive_exports SET status=?, sheet_url=?, finished_at=datetime('now')"
                     " WHERE id=?", ("done" if len(rows) == e["n_ads"] else "partial", sheet_url, eid))
    conn.close()
    return jsonify({"uploaded": len(rows), "of": e["n_ads"], "folder_url": e["folder_url"],
                    "folder_name": e["folder_name"], "sheet_url": sheet_url})
