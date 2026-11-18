"""Layout work — one merged Gallery (images, Compose ads, AI ads, Core shots) with type, status
and score filters, bulk actions across kinds, and one launch kit. No live API."""
from __future__ import annotations

import io
import zipfile

import pytest

from creative_intelligence.db import get_connection, init_db


@pytest.fixture
def client(tmp_path, monkeypatch):
    import creative_intelligence.webapp.app as app_module
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "t.db"
    monkeypatch.setenv("CI_DB_PATH", str(db))
    init_db(db)
    png = tmp_path / "x.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    c = get_connection(db)
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Honey Mango', 'fruit')")
        c.execute("INSERT INTO adstudio_images (id, product_id, title, status, image_path, aspect_ratio)"
                  " VALUES (1, 1, 'Orchard', 'done', ?, '4:5')", (str(png),))
        c.execute("INSERT INTO adstudio_images (id, product_id, title, status, image_path, aspect_ratio)"
                  " VALUES (2, 1, 'Cut', 'done', ?, '4:5')", (str(png),))
        c.execute("INSERT INTO qa_scores (source, source_id, total, passed, scores_json, rubric_version)"
                  " VALUES ('image', 2, 2.1, 0, '{}', 1)")
        c.execute("INSERT INTO adstudio_composites (id, product_id, image_id, headline, output_path, is_ready)"
                  " VALUES (1, 1, 1, 'Custard, not fibre', ?, 1)", (str(png),))
        c.execute("INSERT INTO adstudio_textads (id, product_id, brief_id, kind, variant, title, headline, status, image_path,"
                  " aspect_ratio) VALUES (1, 1, 1, 'variant', 'bold_type', 'Bold Type', 'Zero strings', 'done', ?, '9:16')",
                  (str(png),))
        c.execute("INSERT INTO adstudio_core (id, product_id, kind, image_path) VALUES (1, 1, 'core1', ?)", (str(png),))
    c.close()
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(db))
    return app_module.app.test_client()


def keys(d):
    return {(i["kind"], i["id"]) for i in d["items"]}


def test_all_four_kinds_in_one_list_with_counts(client):
    d = client.get("/api/adstudio/gallery?product_id=1").get_json()
    assert keys(d) == {("image", 1), ("compose", 1), ("ai", 1), ("core", 1)}       # QA failure hidden
    assert d["counts"]["type"] == {"all": 5, "image": 2, "compose": 1, "ai": 1, "core": 1, "video": 0}
    assert d["counts"]["score"]["qa_rejected"] == 1 and d["counts"]["launch_kit"] == 1


def test_filters(client):
    get = lambda q: client.get("/api/adstudio/gallery?product_id=1&" + q).get_json()
    assert keys(get("type=ai")) == {("ai", 1)}
    assert keys(get("status=ready")) == {("compose", 1)}
    assert keys(get("score=qa_rejected")) == {("image", 2)}
    assert keys(get("status=notexported")) == {("ai", 1)}


def test_bulk_across_kinds_skips_what_a_kind_cannot_do(client):
    items = [{"kind": "image", "id": 1}, {"kind": "ai", "id": 1}, {"kind": "core", "id": 1}]
    r = client.post("/api/adstudio/gallery/bulk", json={"action": "ready", "items": items}).get_json()
    assert r == {"affected": 0, "skipped": 3}                    # images/core never ready; AI ad text unchecked
    r = client.post("/api/adstudio/gallery/bulk", json={"action": "favorite", "items": items}).get_json()
    assert r["affected"] == 3
    d = client.get("/api/adstudio/gallery?product_id=1").get_json()
    assert all(i["is_favorite"] for i in d["items"] if (i["kind"], i["id"]) in {("image", 1), ("ai", 1), ("core", 1)})
    r = client.post("/api/adstudio/gallery/bulk", json={"action": "delete", "items": [{"kind": "core", "id": 1}]})
    assert r.get_json()["affected"] == 1
    assert client.post("/api/adstudio/gallery/bulk", json={"action": "nope", "items": items}).status_code == 400


def test_launch_kit_has_ready_ads_and_manifest(client):
    r = client.get("/api/adstudio/export-gallery?product_id=1&ready=1")
    z = zipfile.ZipFile(io.BytesIO(r.data))
    names = z.namelist()
    assert "manifest.csv" in names and len(names) == 2
    assert "Compose ads" in z.read("manifest.csv").decode()
    everything = zipfile.ZipFile(io.BytesIO(client.get("/api/adstudio/export-gallery?product_id=1").data))
    assert len(everything.namelist()) == 6                          # 5 pictures + manifest


def test_products_carry_a_thumbnail(client):
    p = client.get("/api/adstudio/products").get_json()["products"][0]
    assert "thumb_photo_id" in p


def test_page_has_one_gallery_and_the_picker(client):
    html = client.get("/gallery/assets").get_data(as_text=True)
    assert 'id="pickerBtn"' in html and 'id="pGal"' in html
    assert "pAdGal" not in html and 'class="rail"' not in html


class _FakeDrive:
    """Folders and files in memory; the launch kit goes up with resumable uploads (videos are large)."""

    def __init__(self, monkeypatch):
        from creative_intelligence.adstudio import drive
        self.items, self.sheets, self.n = {"root": {"name": "Meta Ads", "parent": None}}, [], 0
        monkeypatch.setattr(drive, "access_token", lambda rt: "tok")
        monkeypatch.setattr(drive, "get_folder", lambda t, fid: self.items.get(fid))
        monkeypatch.setattr(drive, "child_names", lambda t, p: {v["name"] for v in self.items.values() if v["parent"] == p})
        monkeypatch.setattr(drive, "find_folder", lambda t, name, p: None)
        monkeypatch.setattr(drive, "create_folder", lambda t, name, p: self._new(name, p))
        monkeypatch.setattr(drive, "upload_resumable", lambda t, path, name, p, mime: {
            "id": self._new(name, p, mime), "webViewLink": f"https://drive/{name}"})
        monkeypatch.setattr(drive, "upload", lambda t, name, p, data, mime, as_mime=None: (
            self.sheets.append(data.decode()) or {"id": self._new(name, p), "webViewLink": "https://drive/sheet"}))
        monkeypatch.setenv("ADSTUDIO_DRIVE_FOLDER_ID", "root")

    def _new(self, name, parent, mime=None):
        self.n += 1
        self.items[f"f{self.n}"] = {"name": name, "parent": parent, "mime": mime}
        return f"f{self.n}"


def test_launch_kit_goes_to_drive_with_videos_and_a_sheet(client, tmp_path, monkeypatch):
    from creative_intelligence import jobs
    from creative_intelligence.adstudio import launchkit
    db = tmp_path / "t.db"
    queued = []
    monkeypatch.setattr(jobs, "enqueue", lambda c, kind, p, **k: queued.append((kind, p, k)) or 7)
    assert client.post("/api/adstudio/drive-kit", json={"product_id": 1}).status_code == 400   # Drive not connected
    mp4 = tmp_path / "v.mp4"
    mp4.write_bytes(b"mp4")
    c = get_connection(db)
    with c:
        c.execute("INSERT INTO adstudio_drive_auth (id, email, refresh_token) VALUES (1, 'm@x', 'rt')")
        c.execute("INSERT INTO adstudio_videos (id, product_id, source_kind, source_id, style, duration_s, status,"
                  " created_at) VALUES (1, 1, 'image', 1, 'fruit_story', 30, 'done', 'now')")
        c.execute("INSERT INTO adstudio_video_renders (id, video_id, product_id, aspect, output_path, is_ready, created_at)"
                  " VALUES (1, 1, 1, '9:16', ?, 1, 'now')", (str(mp4),))
    c.close()
    r = client.post("/api/adstudio/drive-kit", json={"product_id": 1, "date": "2026-10-22"}).get_json()
    assert r == {"job_id": 7, "n": 2} and queued[0][:2] == ("drive_kit", {"product_id": 1, "date": "2026-10-22"})
    fake = _FakeDrive(monkeypatch)
    out = jobs.HANDLERS["drive_kit"](queued[0][1], lambda: get_connection(db))
    assert out["uploaded"] == 2 and out["failed"] == [] and out["folder_name"] == "2026-10-22 · Honey Mango · 2 ready ads"
    files = [v for v in fake.items.values() if v.get("mime")]
    assert {f["mime"] for f in files} == {"image/png", "video/mp4"}
    assert all(f["name"].endswith((".png", ".mp4")) for f in files)
    assert "drive_link" in fake.sheets[0] and "https://drive/" in fake.sheets[0]
    c = get_connection(db)
    assert c.execute("SELECT status, n_ads FROM adstudio_drive_exports").fetchone()[:] == ("done", 2)
    assert c.execute("SELECT drive_folder_id FROM adstudio_products WHERE id=1").fetchone()[0]
    c.close()
    assert launchkit.files(get_connection(db), 1, ready=False)                     # zip and Drive read one list


def test_drive_kit_status_reads_the_job(client, tmp_path):
    c = get_connection(tmp_path / "t.db")
    with c:
        c.execute("INSERT INTO jobs (id, kind, status, payload_json, result_json, run_after, created_at) VALUES"
                  " (3, 'drive_kit', 'done', '{}', '{\"uploaded\": 2}', '2026-01-01', '2026-01-01')")
    c.close()
    assert client.get("/api/adstudio/drive-kit/3").get_json()["result"] == {"uploaded": 2}
    assert client.get("/api/adstudio/drive-kit/4").status_code == 404
    assert b"btnKitDrive" in client.get("/gallery/assets").data
