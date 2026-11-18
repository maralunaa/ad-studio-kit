"""
The knowledge base: the Meta Creative Playbook and the machine-readable files derived from it (knowledge/).

  meta-creative-playbook.md   the playbook export (source of truth; edit the Claude Docs doc, not this file)
  styles.yaml                 every static and video style: family, route, inputs, restrictions, priority
  hooks.yaml                  hook types (definition, why, examples, best-fit styles) and hook rules
  rules.yaml                  hard limits: copy lengths, safe zones, contrast, word counts, captions,
                              video length and export spec, CTA, emoji, pre-launch checklists
  compliance.yaml             facts only, Shopify prices, real reviews, personal attributes, health wording
  variations.yaml             what each variation tests and whether Meta counts it as a new concept
  metrics.yaml                metric definitions, benchmarks, diagnosis rules, fatigue triggers

Every value group names the playbook section it comes from (`source: "<H2> › <bold sub-heading>"`, or
"house: …" for the user's rules that sit above the playbook); errors() checks each one still exists.
Files are re-read when they change on disk.

Approved updates (Admin → Knowledge, K3) live in an overlay on the data volume: overlay.json holds approved
value changes ({file, key, value}) applied over the repo files, and playbook.md a newer playbook export once it
is adopted. They take effect without a deploy and are folded into the repo later by a PR; removing an override
puts the repo value back. version() covers the overlay too.

Prompt files reference knowledge by key, e.g. {{knowledge:hooks.types}} or {{knowledge:rules.cta}};
prompts.text() fills them in (expand()), and prompts.stamp() adds "knowledge@<version()>" for such prompts, so
every asset that records its prompt_version also records the knowledge it was made under. used_by() lists
which prompts read each key.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"
PLAYBOOK = "meta-creative-playbook.md"
FILES = ("styles", "hooks", "rules", "compliance", "variations", "metrics")

FAMILIES = {"product_real", "animation", "native_lofi", "education"}
MEDIA = {"static", "video"}
STATUSES = {"live", "planned", "retired"}   # retired: kept for the record, never offered
ROUTES = {"compose", "video_style", "winners", "animate", "none"}
MODES = {"presenter", "narrated", "hands"}
INPUTS = {"shots", "facts", "real_reviews", "real_comments", "presenter", "character", "offer", "season",
          "licensed_music"}
NEW_CONCEPT = {"yes", "no", "usually", "per_product", "partly", "unless_angle_changes"}

_cache: dict[Path, tuple[tuple[float, float], Any]] = {}


def _dir() -> Path:
    return Path(os.getenv("KNOWLEDGE_DIR") or KNOWLEDGE_DIR)


def overlay_dir() -> Path:
    """Approved updates on the data volume, next to the database (KNOWLEDGE_OVERLAY_DIR overrides)."""
    if os.getenv("KNOWLEDGE_OVERLAY_DIR"):
        return Path(os.environ["KNOWLEDGE_OVERLAY_DIR"])
    from creative_intelligence.db import get_db_path
    return get_db_path().parent / "knowledge"


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def overrides() -> list[dict[str, Any]]:
    """Approved value changes ({file, key, value, proposal, approved_at}), oldest first."""
    p = overlay_dir() / "overlay.json"
    if not p.exists():
        return []
    return json.loads(p.read_text()).get("overrides", [])


def save_overrides(items: list[dict[str, Any]]) -> None:
    d = overlay_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "overlay.json.tmp"
    tmp.write_text(json.dumps({"overrides": items}, indent=1))
    tmp.replace(d / "overlay.json")


def set_value(data: dict[str, Any], key: str, value: Any) -> None:
    """Set a dotted key ("rules.cta.allowed.shop_now.label", "styles.fruit_story.when") inside a file's data."""
    name, *path = key.split(".")
    if not path:
        raise KeyError(f"{key!r} names a whole file")
    node: Any = data
    if name == "styles":
        node = next((s for s in data["styles"] if s["key"] == path[0]), None)
        if node is None:
            raise KeyError(f"no style {path[0]!r}")
        path = path[1:]
        if not path:
            raise KeyError("set a style's fields one at a time")
    for part in path[:-1]:
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"no knowledge at {key!r}")
        node = node[part]
    if not isinstance(node, dict) or path[-1] not in node:
        raise KeyError(f"no knowledge at {key!r}")
    node[path[-1]] = value


def load(name: str) -> dict[str, Any]:
    """One derived file (e.g. "rules"), parsed, with approved overrides applied; re-read when it or the overlay
    changes on disk."""
    import yaml
    if name not in FILES:
        raise KeyError(f"unknown knowledge file: {name}")
    p = _dir() / f"{name}.yaml"
    stamp = (p.stat().st_mtime, _mtime(overlay_dir() / "overlay.json"))
    hit = _cache.get(p)
    if hit and hit[0] == stamp:
        return hit[1]
    data = yaml.safe_load(p.read_text())
    mine = [o for o in overrides() if o["file"] == name]
    if mine:
        data = copy.deepcopy(data)
        for o in mine:
            set_value(data, o["key"], o["value"])
    _cache[p] = (stamp, data)
    return data


def playbook() -> str:
    """The playbook in force: an adopted newer export from the overlay, else the repo's."""
    adopted = overlay_dir() / PLAYBOOK
    return adopted.read_text() if adopted.exists() else (_dir() / PLAYBOOK).read_text()


def playbook_info() -> dict[str, Any]:
    """Revision and export date from the playbook's header."""
    m = re.search(r"Exported: (\d{4}-\d{2}-\d{2}) · doc revision (\d+)", playbook())
    return {"exported": m[1], "revision": int(m[2])} if m else {"exported": None, "revision": None}


def version() -> str:
    """pb<revision>-<first 8 of a sha256 over the playbook and every derived file>."""
    h = hashlib.sha256()
    for f in (PLAYBOOK, *(f"{n}.yaml" for n in FILES)):
        h.update((_dir() / f).read_bytes())
    if ov := overrides():                                                # approved updates count too
        h.update(json.dumps(ov, sort_keys=True).encode())
    if (overlay_dir() / PLAYBOOK).exists():
        h.update((overlay_dir() / PLAYBOOK).read_bytes())
    return f"pb{playbook_info()['revision'] or 0}-{h.hexdigest()[:8]}"


def styles(medium: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    """Styles, optionally only one medium (static/video) or status (live/planned), in priority order."""
    out = [s for s in load("styles")["styles"]
           if (medium is None or s["medium"] == medium) and (status is None or s["status"] == status)]
    return sorted(out, key=lambda s: s["priority"])


def style(key: str) -> dict[str, Any]:
    for s in load("styles")["styles"]:
        if s["key"] == key:
            return s
    raise KeyError(f"unknown style: {key}")


def hook_types() -> list[dict[str, Any]]:
    return load("hooks")["types"]["items"]


def hook_type_keys() -> list[str]:
    return [t["key"] for t in hook_types()]


def cta_label(value: str | None) -> str:
    """A CTA as people read it: "shop_now" -> "Shop now" (feedback 2026-10-30: a button said "shop_now")."""
    v = (value or "").strip()
    allowed = load("rules")["cta"]["allowed"]
    key = v.lower().replace(" ", "_")
    if key in allowed:
        return allowed[key]["label"]
    return v.replace("_", " ").capitalize() if "_" in v and " " not in v else v


def personal_attribute(text: str) -> list[str]:
    """Phrases that speak to what someone is (a condition or identity) instead of what they want."""
    out = []
    for p in load("compliance")["personal_attribute"]["patterns"]:
        out += [m.group(0) for m in re.finditer(p, text or "", re.I)]
    return out


# ── knowledge in prompts ──────────────────────────────────────────────────
KEY = re.compile(r"\{\{knowledge:([a-z_]+(?:\.[a-z0-9_:]+)*)\}\}")


def _lookup(key: str) -> tuple[Any, str]:
    """(value, the playbook section it comes from: its own source or the nearest one above it)."""
    name, *path = key.split(".")
    node: Any = load(name)
    src = ""
    for i, part in enumerate(path):
        if name == "styles" and i == 0:
            node = style(part)
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            raise KeyError(f"no knowledge at {key!r}")
        if isinstance(node, dict) and isinstance(node.get("source"), str):
            src = node["source"]
    return node, src


def get(key: str) -> Any:
    """A value by dotted key: "rules.cta", "rules.on_image_text.fruit_fill", "styles.fruit_story" (a style by key)."""
    return _lookup(key)[0]


def _strip(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if k not in ("source", "tagger", "priority", "status", "route")}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def render(key: str) -> str:
    """The value as prompt text, headed by the playbook section it comes from."""
    import yaml
    node, src = _lookup(key)
    if key == "hooks.types":
        body = "\n".join(f"- {t['key']} ({t['name']}): {t['definition']} Why: {t['why']} "
                         f"Example pattern (not copy): {' / '.join(t['examples'])}" for t in node["items"])
    elif isinstance(node, dict) and set(node) <= {"source", "items"} and \
            all(isinstance(i, dict) and "text" in i for i in node.get("items", [])):
        body = "\n".join(f"- {i['text']}" for i in node["items"])
    else:
        own = isinstance(node, dict) and "source" in node
        body = yaml.safe_dump(_strip(node if own else {key.rsplit(".", 1)[-1]: node}), sort_keys=False,
                              allow_unicode=True, width=120).strip()
    head = f"[Meta Creative Playbook: {src}]" if src and not src.startswith("house:") else "[house rules]"
    return f"{head}\n{body}"


def keys_in(text: str) -> list[str]:
    return list(dict.fromkeys(KEY.findall(text or "")))


def expand(text: str) -> str:
    return KEY.sub(lambda m: render(m.group(1)), text)


# What reads each knowledge key in code (prompts are found by scanning them, see used_by()).
CODE_USES: dict[str, list[str]] = {
    "styles": ["Images and Video style pickers (/api/adstudio/config)", "styles.catalog()", "Fruit Story modes"],
    "hooks.types": ["Hook University hook_type tag", "Hooks & ideas badge", "asset hook_type tag"],
    "rules.safe_zones": ["rubric: safe_zone", "templates.safe_area()", "video captions and end card"],
    "rules.on_image_text": ["rubric: word_count", "rubric: legibility (contrast)"],
    "rules.captions": ["pre-export: caption_size"],
    "rules.cta": ["rubric: cta_match", "launch kit: meta_button"],
    "rules.emoji": ["Hook University emoji share and checks"],
    "rules.export": ["finished-video encoding", "pre-export: export_spec"],
    "rules.checklists": ["pre-launch checklist (Gallery, launch kit)"],
    "compliance.personal_attribute": ["Hook University screen", "copy check (facts.check_copy)"],
    "metrics.diagnosis": ["Gallery and Results hints"],
    "metrics.minimum_read": ["Gallery and Results hints"],
    "rules.ideas_mix": ["idea share in image runs, Hook University, AI ads and video scripts (ideas.assign)"],
}


def used_by() -> dict[str, list[str]]:
    """Knowledge key → the prompts (newest versions) that read it."""
    from creative_intelligence import prompts
    out: dict[str, list[str]] = {}
    for name in sorted(prompts.versions()):
        for k in keys_in(prompts._raw(name, prompts.version(name))):
            out.setdefault(k, []).append(name)
    return out


# ── checks ─────────────────────────────────────────────────────────────────
def sources() -> list[tuple[str, str, str]]:
    """(file, where, source) for every value group that names its playbook section."""
    out: list[tuple[str, str, str]] = []

    def walk(name: str, node: Any, where: str) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("source"), str):
                out.append((name, where, node["source"]))
            for k, v in node.items():
                walk(name, v, f"{where}.{k}" if where else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(name, v, f"{where}[{v.get('key', i) if isinstance(v, dict) else i}]")

    for n in FILES:
        walk(n, load(n), "")
    return out


def _sections(md: str) -> dict[str, str]:
    """H2 heading → its text, up to the next H2."""
    parts = re.split(r"^## (.+)$", md, flags=re.M)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def source_found(source: str, sections: dict[str, str]) -> bool:
    if source.startswith("house:"):
        return True
    head, _, sub = (p.strip() for p in source.partition("›"))
    if head not in sections:
        return False
    return not sub or f"**{sub}" in sections[head]


def _check_styles(errs: list[str]) -> set[str]:
    seen: set[str] = set()
    for s in load("styles")["styles"]:
        k = s.get("key", "?")
        miss = [f for f in ("key", "name", "family", "medium", "route", "inputs", "restrictions", "priority",
                            "status", "source") if f not in s]
        if miss:
            errs.append(f"styles.{k}: missing {', '.join(miss)}")
            continue
        if k in seen:
            errs.append(f"styles.{k}: duplicate key")
        seen.add(k)
        for field, allowed in (("family", FAMILIES), ("medium", MEDIA), ("status", STATUSES)):
            if s[field] not in allowed:
                errs.append(f"styles.{k}: {field} {s[field]!r} not one of {sorted(allowed)}")
        route = s["route"].get("kind")
        if route not in ROUTES:
            errs.append(f"styles.{k}: route {route!r} not one of {sorted(ROUTES)}")
        if s["status"] == "live" and route == "none":
            errs.append(f"styles.{k}: live but has no production route")
        if route == "compose" and not s["route"].get("templates"):
            errs.append(f"styles.{k}: compose route needs templates")
        if route in ("video_style", "winners") and not s["route"].get("prompt"):
            errs.append(f"styles.{k}: {route} route needs a prompt")
        if bad := set(s["inputs"]) - INPUTS:
            errs.append(f"styles.{k}: unknown inputs {sorted(bad)}")
        if "modes" in s and (set(s["modes"]) - MODES or s.get("default_mode") not in s["modes"]):
            errs.append(f"styles.{k}: modes must be from {sorted(MODES)} with default_mode among them")
        if ls := s.get("length_s"):
            if len(ls) != 2 or ls[0] > ls[1]:
                errs.append(f"styles.{k}: length_s must be [min, max]")
    return seen


def errors() -> list[str]:
    """Everything wrong with the knowledge files; [] when they load, validate and trace to the playbook."""
    errs: list[str] = []
    try:
        for n in FILES:
            if not isinstance(load(n).get("version"), int):
                errs.append(f"{n}: missing integer version")
        style_keys = _check_styles(errs)

        types = hook_types()
        if len({t["key"] for t in types}) != len(types):
            errs.append("hooks: duplicate hook type keys")
        for t in types:
            miss = [f for f in ("key", "name", "definition", "why", "examples", "best_fit", "tagger") if not t.get(f)]
            if miss:
                errs.append(f"hooks.{t.get('key', '?')}: missing {', '.join(miss)}")
            elif bad := set(t["best_fit"]) - style_keys:
                errs.append(f"hooks.{t['key']}: best_fit names unknown styles {sorted(bad)}")

        rules = load("rules")
        for size, z in rules["safe_zones"].items():
            if size != "source" and not (0 <= z["top"] < z["bottom"] <= 1 and 0 <= z["side"] < 0.5):
                errs.append(f"rules.safe_zones.{size}: needs 0 <= top < bottom <= 1 and side < 0.5")
        for kind in ("static", "video"):
            for item in rules["checklists"][kind]:
                if item.get("auto") and not item.get("check"):
                    errs.append(f"rules.checklists.{kind}: auto item without a check: {item['text']}")
        if not 0 < rules["emoji"]["share_of_ads"] < 1:
            errs.append("rules.emoji.share_of_ads must be a fraction")
        if not 0 <= rules["ideas_mix"]["share"] <= 1:
            errs.append("rules.ideas_mix.share must be between 0 and 1")

        for p in load("compliance")["personal_attribute"]["patterns"]:
            try:
                re.compile(p, re.I)
            except re.error as exc:
                errs.append(f"compliance.personal_attribute: bad pattern {p!r} ({exc})")

        for item in load("variations")["changes"]["items"]:
            if item.get("new_concept") not in NEW_CONCEPT:
                errs.append(f"variations.{item.get('key', '?')}: new_concept {item.get('new_concept')!r}")

        for r in load("metrics")["diagnosis"]["rules"]:
            if not (r.get("when") and r.get("hint")):
                errs.append(f"metrics.diagnosis.{r.get('key', '?')}: needs when and hint")

        for k in used_by():
            try:
                get(k)
            except KeyError:
                errs.append(f"a prompt reads {{{{knowledge:{k}}}}}, which doesn't exist")

        secs = _sections(playbook())
        for name, where, src in sources():
            if not source_found(src, secs):
                errs.append(f"{name}.{where}: source not in the playbook: {src!r}")
    except Exception as exc:                        # a missing file or broken YAML is one clear error
        errs.append(f"knowledge failed to load: {type(exc).__name__}: {exc}")
    return errs
