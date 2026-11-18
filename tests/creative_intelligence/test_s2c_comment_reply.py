"""S2c: Comment reply. A presenter answers one real comment from the library, shown as a sticker (exact words, name
hidden, inside the safe zone) drawn into the first shot so captions still show. Never an invented comment."""
from __future__ import annotations

from pathlib import Path

import pytest

from creative_intelligence.adstudio import qa, reviews, styles as S, timeline, videotools
from tests.creative_intelligence.test_s2a_video_styles import SCRIPT, _Claude, _shot, conn  # noqa: F401  (fixture)

P = {"id": 1, "name": "Honey Mango", "product_type": "fruit"}


@pytest.mark.skip(reason="Comment reply retired 2026-10-30")
def test_needs_a_real_comment_from_the_library(conn, tmp_path):
    _shot(conn, 1, "cross_section_macro", tmp_path)
    with pytest.raises(ValueError, match="real comment"):
        S.write(conn, P, "comment_reply", client=_Claude())
    rid, _ = reviews.add(conn, {"kind": "review", "text": "Lovely", "author": "Ann Lee", "source": "google",
                                "posted_on": "2026-09-01", "product_id": 1})
    with pytest.raises(ValueError, match="real comment"):                    # a review isn't a comment
        S.write(conn, P, "comment_reply", client=_Claude(), comment_id=rid)
    cid, _ = reviews.add(conn, {"kind": "comment", "text": "Is it really worth $54?", "author": "Jo Smith",
                                "source": "meta_comment", "posted_on": "2026-10-02", "product_id": 1})
    fake = _Claude(SCRIPT)
    s = S.write(conn, P, "comment_reply", client=fake, comment_id=cid)
    ask = fake.sent[0]["messages"][0]["content"]
    assert "“Is it really worth $54?”" in ask and "COMMENT REPLY" in ask and "Jo" not in ask        # name never sent
    assert s["comment"] == {"id": cid, "text": "Is it really worth $54?", "source": "Facebook/Instagram comment",
                            "posted_on": "2026-10-02", "kind": "comment"}                # a real comment, not a question


def test_sticker_sits_in_the_safe_zone_and_shows_no_name(tmp_path):
    for aspect, (W, H) in (("9:16", (1080, 1920)), ("4:5", (1080, 1350))):
        m: dict = {"frame": [W, H]}
        timeline.comment_sticker_png("Is it really worth $54?", W, H, aspect, tmp_path / "c.png", m)
        assert qa.outside_safe_zone(m) == [] and m["min_contrast"] >= 4.5


def test_sticker_is_drawn_into_the_first_shot(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(videotools, "frame", lambda src, out, aspect, box: (Path(out), {}))
    monkeypatch.setattr(videotools, "probe", lambda p: {"duration": 5.0, "width": 1080, "height": 1920, "fps": 24})
    monkeypatch.setattr(videotools, "run", lambda args, timeout=600: calls.append(args) or "")
    timeline._segment({"src": "clip.mp4", "dur": 3.0, "sticker": "Is it worth it?"}, "9:16", 30, tmp_path / "s.mp4", tmp_path, 0)
    args = calls[-1]
    assert "-filter_complex" in args and "overlay=0:0" in args[args.index("-filter_complex") + 1]
    assert (tmp_path / "sticker0.png").exists()


def test_every_text_line_keeps_its_on_screen_words():
    """Regression: the sticker step once sat inside the per-line loop's body and dropped all but the last text line."""
    import inspect
    src = inspect.getsource(S.process)
    loop = src.index('for li, (x, k, (a, b)) in enumerate(zip(lines, keys, spans)):')
    sticker = src.index('clips[0]["sticker"]')
    kinetic = src.index('texts.append({"template": look.get("text", "kinetic")')
    assert loop < kinetic < sticker
    indent = lambda i: i - src.rfind("\n", 0, i) - 1                      # noqa: E731
    assert indent(kinetic) > indent(sticker)                               # kinetic inside the loop, sticker after it
