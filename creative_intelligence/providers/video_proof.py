"""
Kling 3.0 proof clip: one short video from a product's primary reference photo, to check the video
model works end to end (Replicate adapter, budget check, cost log, storage) before Phase 2 builds on it.

    python -m creative_intelligence.providers.video_proof --product 1                 # dry run (default): what it would do and cost
    python -m creative_intelligence.providers.video_proof --product 1 --no-dry-run    # makes the clip (paid)

The clip is saved in the "videos" storage area; its cost is logged as kind "video" and counts
toward the generation caps.
"""
from __future__ import annotations

import argparse
import sys
import time

PROMPT = ("Slow, steady push-in on the fruit exactly as in the photo, soft natural light, a few leaves moving "
          "in a light breeze. Keep the fruit's colour, shape and markings exactly as shown. No text.")


def main(argv: list[str] | None = None) -> int:
    from creative_intelligence.adstudio import costs
    from creative_intelligence.db import get_connection, init_db
    from creative_intelligence.providers import model, replicate
    from creative_intelligence.storage import storage
    from creative_intelligence.webapp.common import _primary_photo_uris, _product_dict
    ap = argparse.ArgumentParser(prog="python -m creative_intelligence.providers.video_proof")
    ap.add_argument("--product", type=int, required=True)
    ap.add_argument("--seconds", type=int, default=5)
    ap.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True,
                    help="on by default: report only; --no-dry-run makes the (paid) clip")
    a = ap.parse_args(argv)
    video_model = model("video")          # reads config first (it loads .env locally)
    init_db()
    conn = get_connection()
    prod = _product_dict(conn, a.product)
    if not prod:
        print(f"no product {a.product}")
        return 1
    refs = _primary_photo_uris(conn, a.product, limit=1)
    est = costs.price(conn, model("video")) * a.seconds
    print(f"{video_model}: {a.seconds} s clip of {prod['name']} from {'its primary photo' if refs else 'text only'};"
          f" estimated ${est:.2f} (price unverified until checked on Admin)")
    if a.dry_run:
        print("[dry run] nothing generated. Re-run with --no-dry-run to make it.")
        return 0
    url = replicate.generate_video(PROMPT, refs[0] if refs else None, duration=a.seconds)
    ref = storage().put_url("videos", f"proof_p{a.product}_{int(time.time())}.mp4", url, min_bytes=10_000)
    cost = costs.log_video(conn, model("video"), a.seconds, "video-proof", a.product)
    conn.close()
    print(f"saved {ref} (${cost:.2f} logged)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
