"""Pages: the six sections (Products · Ideas · Images · Video · Gallery · Ad Pack, each with its stages), Admin, the
redirects from the old pages, and the page config.

    /products                  the catalogue and the product profile
    /ideas/<stage>             bank · market · voice (Phase 2a: the idea bank, competitors, customer voice)
    /images/<stage>            create · review · compose · shots (Product shots = the old Core photos page)
    /video/<stage>             create · shots · edit · review
    /gallery/<stage>           assets · performance
    /adpack/<stage>            request · plan · packs (Part C: one request → one pack ready for Meta)
    /adstudio, /core           old pages: redirect to their new home (query kept); /gallery/research → /ideas/market
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, abort, jsonify, redirect, render_template, request

from creative_intelligence import knowledge
from creative_intelligence.webapp.common import _textfree

bp = Blueprint("pages", __name__)
log = logging.getLogger("adstudio")
_HERE = Path(__file__).parent.parent


# ─────────────────────────────────────────────
# AD STUDIO
# Reference-photo product ads → Nano Banana Pro (image_input). Isolated module.
# ─────────────────────────────────────────────

SECTIONS = [("products", "Products"), ("ideas", "Ideas"), ("images", "Images"), ("video", "Video"), ("gallery", "Gallery"),
            ("adpack", "Ad Pack")]
STAGES = {"ideas": [("visuals", "Visuals"), ("bank", "Idea bank"), ("market", "Market"), ("voice", "Customer voice")],
          "images": [("create", "Create"), ("review", "Review"), ("compose", "Compose"), ("shots", "Product shots")],
          "video": [("create", "Create"), ("shots", "Shots"), ("edit", "Edit"), ("review", "Review")],
          "gallery": [("assets", "Assets"), ("performance", "Performance")],
          "adpack": [("request", "Request"), ("plan", "Plan"), ("packs", "Packs")]}
SOON: set[tuple[str, str]] = set()          # stages still to come (none right now)


@bp.app_context_processor
def _sections() -> dict[str, Any]:
    from creative_intelligence import brand
    return {"BRAND": {"name": brand.name(), "short": brand.short()}, "SECTIONS": SECTIONS, "STAGES": STAGES, "SOON": SOON}


def _studio(section: str, stage: str = "") -> Any:
    if section != "products" and stage not in dict(STAGES[section]):
        abort(404)
    if (section, stage) == ("images", "shots"):
        return render_template("core.html", section=section, stage=stage)
    if section in ("ideas", "adpack"):
        return render_template(f"{section}.html", section=section, stage=stage)
    return render_template("adstudio.html", section=section, stage=stage)


def _qs() -> str:
    return ("?" + request.query_string.decode()) if request.query_string else ""


@bp.get("/products")
def products_page() -> Any:
    return _studio("products")


@bp.get("/ideas")
@bp.get("/images")
@bp.get("/video")
@bp.get("/gallery")
@bp.get("/adpack")
def section_page() -> Any:
    section = request.path.strip("/")
    return redirect(f"/{section}/{STAGES[section][0][0]}{_qs()}")


@bp.get("/gallery/research")
def old_research_page() -> Any:
    return redirect("/ideas/market" + _qs())


@bp.get("/<any(ideas, images, video, gallery, adpack):section>/<stage>")
def stage_page(section: str, stage: str) -> Any:
    return _studio(section, stage)


OLD_TABS = {"compose_core": "/images/compose", "drive": "/gallery/assets"}


@bp.get("/adstudio")
def adstudio_page() -> Any:
    """The old one-page app: send old links (Drive sign-in return, Compose a Core shot) to the right stage."""
    to = next((path for key, path in OLD_TABS.items() if request.args.get(key)), "/products")
    return redirect(to + _qs())


@bp.get("/core")
def core_page() -> Any:
    return redirect("/images/shots" + _qs())


@bp.get("/api/adstudio/config")
def adstudio_config() -> Any:
    from creative_intelligence.adstudio.formats import format_list, ASPECT_RATIOS, DEFAULT_ASPECT
    from creative_intelligence.adstudio.profiles import PROFILES
    types = {k: {"label": v["label"], "notes_label": v["notes_label"],
                 "archetypes": [x["name"] for x in v["formats"].values()]}
             for k, v in PROFILES.items()}
    from creative_intelligence.adstudio import textads
    from creative_intelligence.adstudio.templates import template_list
    return jsonify({"formats": format_list(), "aspect_ratios": ASPECT_RATIOS,
                    "default_aspect": DEFAULT_ASPECT, "product_types": types,
                    "ad_variants": textads.variant_list(),
                    "hook_types": [{"key": t["key"], "name": t["name"], "definition": t["definition"]}
                                   for t in knowledge.hook_types()],
                    # style pickers come from knowledge/styles.yaml: adding a style is a knowledge change
                    "static_styles": [{"key": s["key"], "name": s["name"], "when": s["when"], "family": s["family"],
                                       "templates": s["route"]["templates"]}
                                      for s in knowledge.styles("static", "live") if s["route"]["kind"] == "compose"],
                    "video_styles": [{"key": s["key"], "name": s["name"], "family": s["family"],
                                      "summary": " → ".join(s.get("shot_order") or []) or s["when"],
                                      "length_s": s.get("length_s"), "modes": s.get("modes", []),
                                      "default_mode": s.get("default_mode"), "needs_presenter": "presenter" in s["inputs"],
                                      "voice": s.get("voice", True), "route": s["route"]["kind"],
                                      "live": s["status"] == "live" and
                                      s["route"]["kind"] in ("video_style", "winners", "animate", "listicle")}
                                     for s in knowledge.styles("video") if s["status"] != "retired"],
                    "ad_default_aspect": textads.DEFAULT_ASPECT,
                    "templates": [t for t in template_list() if t["key"] != "scene"],
                    "textfree_ai_ads": _textfree()})


# ── Admin page (wrap-up) ─────────────────────────────────────────────────────
@bp.get("/admin")
def admin_page() -> Any:
    return render_template("admin.html")


# ── The brand pack's logo and colours (brands/<BRAND>/: logo.png, brand.yaml › colors) ──────────────────────
@bp.get("/brand/logo.png")
def brand_logo() -> Any:
    from flask import send_file
    from creative_intelligence import brand
    return send_file(brand.path("logo.png") or (_HERE / "static" / "mark.png"), mimetype="image/png", max_age=3600)


@bp.get("/brand.css")
def brand_css() -> Any:
    """The brand accent over studio.css's default, so the app's primary actions wear the brand's colour."""
    from flask import Response
    from creative_intelligence import brand
    css = f":root{{--accent:{brand.get('colors.accent')};--accent-ink:{brand.get('colors.accent_ink')}}}\n"
    return Response(css, mimetype="text/css", headers={"Cache-Control": "max-age=3600"})
