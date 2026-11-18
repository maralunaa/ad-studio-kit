"""Template text adapts to the photo: fixed colours are preferences, and every layout stays
legible (contrast measured the way QA legibility measures it) on light, mid and dark photos.

No external API: images are generated PNGs.
"""
from __future__ import annotations

import pytest
from PIL import Image

from creative_intelligence.adstudio import qa
from creative_intelligence.adstudio.render_template import MIN_CONTRAST, _px, render, render_layer_to
from creative_intelligence.adstudio.templates import COLORS, TEMPLATES, template_for

COPY = {"headline": "Silky custard texture, zero strings", "sub": "Picked ripe, shipped fast",
        "cta": "Get Yours", "price": "$35", "badge": "In season now",
        "review": "Best mango I've ever had.", "author": "Dana R."}
SIZE = (1080, 1350)
PHOTOS = {"white": (250, 248, 242), "light": (225, 215, 190), "mid_grey": (122, 122, 122),
          "mid_warm": (150, 120, 90), "dark": (25, 22, 20)}
DRAWN = [k for k, t in TEMPLATES.items() if t["elements"]]


@pytest.fixture
def photo(tmp_path):
    def make(name):
        p = tmp_path / f"{name}.png"
        Image.new("RGB", SIZE, PHOTOS[name]).save(p)
        return str(p)
    return make


def _headline_colour(img, template):
    """Colour of the headline glyphs: the pixel in its box furthest from the box's median."""
    el = next(e for e in template_for(template)["elements"] if e.get("field") == "headline")
    x1, y1, x2, y2 = (int(v) for v in _px(el["box"], *img.size))
    px = list(img.crop((x1, y1, x2, y2)).getdata())
    med = sorted(px, key=sum)[len(px) // 2]
    return max(px, key=lambda p: sum(abs(a - b) for a, b in zip(p, med)))


@pytest.mark.parametrize("name", PHOTOS)
@pytest.mark.parametrize("template", DRAWN)
def test_every_template_is_legible_on_any_photo(photo, template, name):
    m = {}
    render(photo(name), template, COPY, m)
    assert m["min_contrast"] >= MIN_CONTRAST
    assert qa.legibility_score(m) >= 4


@pytest.mark.parametrize("name", PHOTOS)
def test_video_layer_is_legible_too(photo, tmp_path, name):
    for template in DRAWN:
        m = {}
        render_layer_to(photo(name), str(tmp_path / "layer.png"), template, COPY, m)
        assert m["min_contrast"] >= MIN_CONTRAST, template


def test_premium_on_a_light_photo_switches_to_dark_text(photo):
    m = {}
    img, drawn = render(photo("white"), "premium", COPY, m)
    assert drawn["headline"] == COPY["headline"]
    assert _headline_colour(img, "premium") == COLORS["ink"]
    assert m["min_contrast"] >= 7


def test_premium_keeps_its_cream_on_a_dark_photo(photo):
    img, _ = render(photo("dark"), "premium", COPY)
    assert _headline_colour(img, "premium") == COLORS["cream"]


def test_text_on_its_own_panel_keeps_the_template_colour(photo):
    """Benefit Stack's white-on-navy panel reads on any photo, so a light photo changes nothing."""
    img, _ = render(photo("white"), "benefit_stack", COPY)
    assert _headline_colour(img, "benefit_stack") == COLORS["white"]


def test_mid_tone_shades_behind_light_text(photo):
    """On mid grey neither white nor ink reaches 4.5:1, so a soft shade goes behind the text."""
    p = photo("mid_grey")
    bg = PHOTOS["mid_grey"]
    assert max(qa.contrast(COLORS["white"], bg), qa.contrast(COLORS["ink"], bg)) < MIN_CONTRAST
    m = {}
    img, _ = render(p, "premium", COPY, m)
    assert m["min_contrast"] >= MIN_CONTRAST
    assert _headline_colour(img, "premium") == COLORS["cream"]
    assert img.getpixel((SIZE[0] // 2, SIZE[1] - 10)) == bg        # only the patch behind the text
