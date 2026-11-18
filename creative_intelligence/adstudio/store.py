"""
Ad Studio's file helpers — uploaded reference photos and generated ads — on top of
creative_intelligence/storage.py (the volume, behind one interface).

Kept separate from every other store so Ad Studio never writes into the ads or
label pipelines.
"""
from __future__ import annotations

import base64
import mimetypes
import re
import time
from pathlib import Path


def _area(name: str) -> Path:
    from creative_intelligence.storage import storage
    return storage().area_dir(name)


def photos_dir() -> Path:
    return _area("photos")


def ads_dir() -> Path:
    return _area("ads")


def images_dir() -> Path:
    return _area("images")


def composites_dir() -> Path:
    return _area("composites")


def core_dir() -> Path:
    return _area("core")


def textads_dir() -> Path:
    return _area("textads")


def save_generated_image(source: str, product_id: int, image_id: int,
                         folder: Path | None = None) -> str:
    """Persist a generated image (text-free by default); return its ref (absolute path)."""
    from creative_intelligence.storage import storage
    area = folder.name if folder is not None else "images"
    return storage().put_url(area, f"img{image_id}_p{product_id}_{int(time.time())}.png", source)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40] or "x"


def save_reference_photo(raw: bytes, filename: str, product_id: int) -> str:
    """Persist an uploaded reference photo; return its ref (absolute path)."""
    from creative_intelligence.storage import storage
    ext = (Path(filename).suffix or ".png").lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        ext = ".png"
    return storage().put_bytes("photos", f"p{product_id}_{int(time.time()*1000)}{ext}", raw)


def save_ad_image(source: str, product_id: int, ad_id: int) -> str:
    """Persist a generated ad (http url / file uri / local path); return its ref."""
    from creative_intelligence.storage import storage
    return storage().put_url("ads", f"ad{ad_id}_p{product_id}_{int(time.time())}.png", source)


def to_data_uri(path: str, max_dim: int = 1024, quality: int = 85) -> str:
    """
    Downscaled base64 data URI of a reference image for Replicate image_input.

    Full-res phone photos (multi-MB) base64-encoded make the request body huge and
    time out on UPLOAD ("write operation timed out"). ~1024px JPEG is plenty for a
    product reference and keeps the whole payload well under ~200KB per image.
    """
    import io
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB")
        im.thumbnail((max_dim, max_dim))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception:
        p = Path(path)
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
        return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode("ascii")
