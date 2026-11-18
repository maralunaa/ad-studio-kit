"""
The launch kit: every ready Compose ad, AI ad and video of one product, each file named by its Asset ID (= the
Meta ad name), with the manifest the agency uploads from. The Gallery zip and the Drive export both read it here.

Drive layout (worker job `drive_kit`, the files can be videos of tens of MB):
    Meta Ads/ (ADSTUDIO_DRIVE_FOLDER_ID: the test folder until switchover)
      <Product>/
        '2026-10-22 · Sunrise Mango · 12 ready ads'/   every file + the 'Launch sheet' Google Sheet
"""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
from typing import Any, Callable

HEADER = ["file", "meta_ad_name", "type", "style", "length_s", "size", "headline", "subhead", "cta",
          "meta_button", "status", "qa_score", "ai_generated", "ai_person", "checks_to_fix", "checks_to_review"]
PATHS = {"image": ("adstudio_images", "image_path"), "compose": ("adstudio_composites", "output_path"),
         "ai": ("adstudio_textads", "image_path"), "core": ("adstudio_core", "image_path"),
         "video": ("adstudio_video_renders", "output_path")}
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".mp4": "video/mp4"}


def files(conn, product_id: int, ready: bool = True) -> list[dict[str, Any]]:
    """[{path, name, row}] — ready=True: the launch kit; False: everything finished in the Gallery."""
    from creative_intelligence.adstudio import assets
    from creative_intelligence.webapp.routes import gallery as g
    assets.ensure_assets(conn, product_id)
    out = []
    for i in g._gallery_items(conn, product_id):
        if i["id"] <= 0 or (ready and not (i["kind"] in ("compose", "ai", "video") and i["is_ready"])):
            continue
        table, col = PATHS[i["kind"]]
        row = conn.execute(f"SELECT {col} FROM {table} WHERE id=?", (i["id"],)).fetchone()
        if not row or not row[0] or not Path(row[0]).exists():
            continue
        name = ((i["asset"] or {}).get("asset_id") or f"{i['kind']}_{i['id']}") + (Path(row[0]).suffix or ".png")
        out.append({"path": row[0], "name": name, "key": f"{i['kind']}:{i['id']}", "row": [
            name, (i["asset"] or {}).get("asset_id", ""), g.GALLERY_KINDS[i["kind"]], i.get("style", ""),
            i.get("length_s", ""), i.get("aspect_ratio", ""), i["title"], i.get("sub", ""), i.get("cta", ""),
            g._meta_button(i.get("cta", "")), g._gallery_status(i), (i["qa"] or {}).get("total", ""), "yes",
            "yes" if i.get("ai_person") else "no", g._check_list(i, "fail"), g._check_list(i, "warn")]})
    return out


def manifest(rows: list[list[Any]], header: list[str] = HEADER) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def _token(conn) -> str:
    from creative_intelligence.adstudio import drive
    r = conn.execute("SELECT refresh_token FROM adstudio_drive_auth WHERE id=1").fetchone()
    if not r:
        raise drive.DriveError("Google Drive isn't connected: Gallery → Connect Google Drive.")
    return drive.access_token(r[0])


def connected(conn) -> bool:
    return conn.execute("SELECT 1 FROM adstudio_drive_auth WHERE id=1").fetchone() is not None


def to_drive(db: Callable, product_id: int, day: str) -> dict[str, Any]:
    """Upload the product's launch kit to a new dated folder plus the launch sheet. Raises DriveError."""
    from creative_intelligence import config
    from creative_intelligence.adstudio import drive
    conn = db()
    try:
        prod = conn.execute("SELECT id, name, drive_folder_id FROM adstudio_products WHERE id=?",
                            (product_id,)).fetchone()
        if prod is None:
            raise drive.DriveError("product not found")
        kit = files(conn, product_id)
        if not kit:
            raise drive.DriveError("Nothing is marked ready for this product.")
        token = _token(conn)
        parent = os.getenv("ADSTUDIO_DRIVE_FOLDER_ID", config.ADSTUDIO_DRIVE_FOLDER_ID)
        if not parent or not drive.get_folder(token, parent):
            raise drive.DriveError("The Meta Ads export folder is missing or in the trash (ADSTUDIO_DRIVE_FOLDER_ID).")
        pfolder = prod["drive_folder_id"]
        if not pfolder or not drive.get_folder(token, pfolder):
            pname = drive.clean(prod["name"])
            pfolder = drive.find_folder(token, pname, parent) or drive.create_folder(token, pname, parent)
            with conn:
                conn.execute("UPDATE adstudio_products SET drive_folder_id=? WHERE id=?", (pfolder, product_id))
        name = drive.export_folder_name(prod["name"], day, len(kit), drive.child_names(token, pfolder), "ready ad")
        fid = drive.create_folder(token, name, pfolder)
        rows, failed = [], []
        for f in kit:
            try:
                up = drive.upload_resumable(token, f["path"], f["name"], fid,
                                            MIME.get(Path(f["path"]).suffix.lower(), "application/octet-stream"))
                rows.append([*f["row"], up.get("webViewLink", "")])
            except drive.DriveError as exc:
                failed.append(f"{f['name']}: {exc}")
        sheet = drive.upload(token, f"Launch sheet — {name}", fid, manifest(rows, [*HEADER, "drive_link"]).encode(),
                             "text/csv", as_mime=drive.SHEET) if rows else {}
        with conn:
            conn.execute(
                "INSERT INTO adstudio_drive_exports (product_id, folder_id, folder_name, folder_url, n_ads, ad_ids,"
                " sheet_url, status, finished_at) VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
                (product_id, fid, name, drive.folder_url(fid), len(kit), json.dumps([r[1] for r in rows]),
                 sheet.get("webViewLink"), "partial" if failed else "done"))
        return {"uploaded": len(rows), "of": len(kit), "folder_name": name, "folder_url": drive.folder_url(fid),
                "sheet_url": sheet.get("webViewLink"), "failed": failed}
    finally:
        conn.close()
