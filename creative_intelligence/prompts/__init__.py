"""
Prompts, as versioned text files: creative_intelligence/prompts/<name>.v<N>.txt

  text(name)            the newest version's text, exactly as written in the file
  text(name, box=...)   the same with ${box}-style placeholders filled in
                        ({{knowledge:key}} references are always filled in from knowledge/; see knowledge.py)
  version(name)         its newest version number
  stamp(*names)         "name@vN+other@vM", stored on every generated image and ad (prompt_version)
                        so results can be compared across prompt changes; "+knowledge@pb16-…" is added when
                        one of them reads the knowledge base

Brand pack (creative_intelligence/brand.py): a file brands/<BRAND>/prompts/<name>.vN.txt replaces the core prompt
of that name, and every prompt may use the ${brand_*} placeholders (brand.prompt_values():
${brand}, ${brand_short}, ${brand_about}, ${brand_packaging} …), filled in from brand.yaml. Shot cards in brands/<BRAND>/shots/ add to or replace the core ones.

To change a prompt, copy the newest file to the next number and edit the copy; old versions stay
for the record. Code that builds prompts around these files (concepts.py, textads.py, core.py)
says which files it used.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from string import Template

DIR = Path(__file__).parent
_FILE = re.compile(r"^(?P<name>[a-z0-9-]+)\.v(?P<v>\d+)\.txt$")


def _brand_dir(sub: str) -> Path | None:
    from creative_intelligence import brand
    return brand.path(sub)


@lru_cache(maxsize=None)
def _files() -> dict[str, tuple[int, Path]]:
    """Each prompt's newest version and the folder it lives in; a brand pack's prompt of a name replaces the core one."""
    out: dict[str, tuple[int, Path]] = {}
    for folder in (DIR, _brand_dir("prompts")):
        if folder is None:
            continue
        seen: dict[str, int] = {}
        for f in folder.glob("*.txt"):
            m = _FILE.match(f.name)
            if m:
                seen[m["name"]] = max(seen.get(m["name"], 0), int(m["v"]))
        for n, v in seen.items():
            out[n] = (v, folder)
    return out


def versions() -> dict[str, int]:
    return {n: v for n, (v, _) in _files().items()}


versions.cache_clear = _files.cache_clear       # type: ignore[attr-defined]  (callers clear it after a change)


def version(name: str) -> int:
    v = versions().get(name)
    if not v:
        raise KeyError(f"no prompt file for {name!r} in {DIR}")
    return v


@lru_cache(maxsize=None)
def _raw(name: str, v: int) -> str:
    folder = _files().get(name, (0, DIR))[1]
    f = folder / f"{name}.v{v}.txt"
    return (f if f.exists() else DIR / f"{name}.v{v}.txt").read_text(encoding="utf-8")


def text(name: str, v: int | None = None, **values: str) -> str:
    from creative_intelligence import brand
    raw = _raw(name, v or version(name))
    out = Template(raw).safe_substitute({**brand.prompt_values(), **values})
    if "{{knowledge:" in out:
        from creative_intelligence import knowledge
        out = knowledge.expand(out)
    return out


SHOTS = DIR / "shots"
_CARD = re.compile(r"^(?P<name>[a-z0-9_]+)\.v(?P<v>\d+)\.yaml$")


@lru_cache(maxsize=None)
def shot_cards() -> dict[str, dict]:
    """Every shot recipe card (prompts/shots/<name>.vN.yaml), newest version of each, in file order."""
    import yaml
    newest: dict[str, tuple[int, Path]] = {}
    for folder in (SHOTS, _brand_dir("shots")):          # the brand pack's cards add to or replace the core ones
        if folder is None:
            continue
        seen: dict[str, int] = {}
        for f in folder.glob("*.yaml"):
            m = _CARD.match(f.name)
            if m:
                seen[m["name"]] = max(seen.get(m["name"], 0), int(m["v"]))
        for n, v in seen.items():
            newest[n] = (v, folder)
    out = {}
    for name in sorted(newest):
        v, folder = newest[name]
        from creative_intelligence import brand
        raw = Template((folder / f"{name}.v{v}.yaml").read_text(encoding="utf-8")).safe_substitute(brand.prompt_values())
        card = yaml.safe_load(raw)
        card["version"] = v
        out[name] = card
    return out


def stamp(*names: str) -> str:
    names = tuple(n for n in names if n)
    out = "+".join(f"{n}@v{version(n)}" for n in names)
    if any("{{knowledge:" in _raw(n, version(n)) for n in names):
        from creative_intelligence import knowledge
        out += f"+knowledge@{knowledge.version()}"
    return out
