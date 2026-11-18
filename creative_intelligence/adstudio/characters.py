"""
Fruit characters (brief S2 #1): a character system for short animated episodes.

  bible          name, personality, voice (a house voice), do's and don'ts, kept with the character
  make_cast()    "Make characters": five caricature characters for a product in one click (cheerful, goofy,
                 grumpy, sleepy, sad), written from a code template (free, instant), with one look sheet each queued
  make_sheets()  queue look-sheet options (Nano Banana Pro, from the product's reference photos): a caricature with
                 the real fruit as its body, front, three-quarter and side views plus expressions; the
                 "character_sheet" job makes them
  approve()      the owner approves one sheet ("Use this one"): the character is then approved, in use for the product,
                 and its sheet becomes the reference for every character shot (QA checks it stays on-model)
  for_product()  the approved character a product's episodes use (the last one picked with "Use this one")
  require()      the same, or a ValueError that says exactly what to do next
  next_step()    the one next step for the Characters panel (make, draw, wait, pick, ready)

Characters are AI-made and flagged like every AI asset. An episode is animated from start to finish (feedback
2026-10-26: "full animation, not turn into real fruit"); the offer card at the end shows the product.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from creative_intelligence import prompts

SHEETS_PER_RUN = 2
PANEL = "Video › Create › Characters"

# "Make characters": five caricatures with clearly different personalities. {fruit} is the fruit word of the product
# name ("Honey Mango" -> "Mango"). Each gets its own house voice where possible (config.HOUSE_VOICES).
CAST = [
    {"mood": "cheerful", "name": "Sunny {fruit}", "voice": "June",
     "personality": "a sunny, bouncy {fruit} who thinks today is the best day ever and can't wait to be cut open and shared",
     "look": "a huge beaming grin, wide sparkling eyes, arms flung wide open, mid-bounce",
     "dos": "be warm, upbeat and a little over the top; cheer for whoever gets to taste it"},
    {"mood": "funny", "name": "Goofy {fruit}", "voice": "Luis",
     "personality": "a goofy {fruit} who tells terrible fruit puns, laughs at its own jokes and trips over its own feet",
     "look": "a lopsided goofy grin, one eyebrow way up, tongue poking out, wobbling off balance",
     "dos": "be silly and self-mocking; one pun at most per line; land the joke fast"},
    {"mood": "grumpy", "name": "Grumpy {fruit}", "voice": "Jack",
     "personality": "a dramatic, grumpy {fruit} who sighs and complains about everything, until someone finally tastes it",
     "look": "a deep theatrical scowl, heavy furrowed brows, arms crossed, one hand on its forehead in despair",
     "dos": "be dramatic and deadpan; grumble, then melt the moment it is tasted"},
    {"mood": "sleepy", "name": "Sleepy {fruit}", "voice": "Tess",
     "personality": "a sleepy, droopy {fruit} who yawns, sighs and feels a little sad that nobody has tried it yet",
     "look": "heavy half-closed eyes, a giant yawn, slumped shoulders, a single small tear on its cheek",
     "dos": "be slow, soft and sweetly sad; perk right up when someone finally tries it"},
    {"mood": "sad", "name": "Teary {fruit}", "voice": "Wade",
     "personality": "a sad, teary {fruit} who is sure nobody will ever pick it, until someone tastes it and it lights up",
     "look": "big watery eyes, a wobbly bottom lip, one fat tear rolling down, hugging itself",
     "dos": "be sweetly sad, never gloomy or bleak; turn to pure joy the moment it is tasted"},
]
HOUSE_DOS = "keep every line short and in character; only say approved facts; end by naming {product} and the offer"
HOUSE_DONTS = ("never say where the fruit comes from, where it is grown or which country or region it is from; "
               "no health or nutrition claims beyond the approved facts; never claim to be a real fruit, a customer "
               "or a farmer; never mock customers or other people")


def _fruit_word(product_name: str) -> str:
    words = [w for w in (product_name or "").split() if w.isalpha()]
    return words[-1].capitalize() if words else "Fruit"


def cast_bibles(product: dict[str, Any]) -> list[dict[str, Any]]:
    """The caricature bibles for a product (cheerful, funny, grumpy, sleepy, sad), from CAST (no Claude call)."""
    from creative_intelligence import config
    voices = [v["name"] for v in config.HOUSE_VOICES]
    name = (product.get("name") or "the fruit").strip()
    fruit, used, out = _fruit_word(name), set(), []
    for c in CAST:
        voice = c["voice"] if c["voice"] in voices and c["voice"] not in used else \
            next((v for v in voices if v not in used), voices[0])
        used.add(voice)
        out.append({"mood": c["mood"], "name": c["name"].format(fruit=fruit), "voice": voice,
                    "personality": c["personality"].format(fruit=fruit.lower()), "look": c["look"],
                    "dos": f"{c['dos']}; {HOUSE_DOS.format(product=name)}", "donts": HOUSE_DONTS})
    return out


def make_cast(conn, product_id: int) -> tuple[list[int], list[int], str]:
    """Make characters: the five caricatures (any the product already has by name are skipped) and one look sheet
    each (one paid job per character). Returns (character ids, sheet ids, the reason when nothing was made)."""
    from creative_intelligence import budget
    from creative_intelligence.webapp.common import _product_dict
    product = _product_dict(conn, product_id)
    if product is None:
        return [], [], "Pick a product first."
    have = {r[0] for r in conn.execute("SELECT name FROM characters WHERE product_id=? AND status<>'retired'", (product_id,))}
    todo = [b for b in cast_bibles(product) if b["name"] not in have]
    if not todo:
        return [], [], "This product already has every character: use Make again on a card for a new look."
    try:
        budget.check(conn, "generation", estimate(conn, len(todo)))
    except budget.BudgetExceeded as exc:
        return [], [], str(exc)
    cids, sids = [], []
    for b in todo:
        cid, why = create(conn, product_id, b)
        if cid is None:
            return cids, sids, why
        cids.append(cid)
        sids += make_sheets(conn, cid, 1, checked=True)
    return cids, sids, ""


def create(conn, product_id: int, d: dict[str, Any]) -> tuple[int | None, str]:
    from creative_intelligence import config
    name, personality = (d.get("name") or "").strip(), (d.get("personality") or "").strip()
    voice = d.get("voice") or ""
    if not name or not personality:
        return None, "Give the character a name and a personality."
    if voice not in {v["name"] for v in config.HOUSE_VOICES}:
        return None, "Pick one of the house voices."
    with conn:
        cid = conn.execute("INSERT INTO characters (product_id, name, personality, voice, dos, donts, look, created_at)"
                           " VALUES (?,?,?,?,?,?,?,?)",
                           (product_id, name, personality, voice, (d.get("dos") or "").strip(), (d.get("donts") or "").strip(),
                            (d.get("look") or "").strip(), datetime.utcnow().isoformat(timespec="seconds"))).lastrowid
    return cid, ""


def listing(conn, product_id: int) -> list[dict[str, Any]]:
    chosen = for_product(conn, product_id)
    out = []
    for r in conn.execute("SELECT * FROM characters WHERE product_id=? AND status<>'retired' ORDER BY id", (product_id,)):
        c = dict(r)
        c["in_use"] = bool(chosen and chosen["id"] == c["id"])
        c["sheets"] = [dict(s) for s in conn.execute(
            "SELECT id, status, error, created_at FROM character_sheets WHERE character_id=? AND status<>'rejected'"
            " ORDER BY id DESC", (c["id"],))]
        out.append(c)
    return out


def estimate(conn, n: int = SHEETS_PER_RUN) -> float:
    from creative_intelligence.adstudio.costs import price
    from creative_intelligence.providers import model
    return round(price(conn, model("image")) * n, 2)


def sheet_prompt(c: dict[str, Any], product: dict[str, Any]) -> str:
    """The look-sheet prompt: a caricature with the real fruit as its body, its personality in the face and pose."""
    return prompts.text("character-sheet", character=c["name"], personality=c["personality"],
                        product=product.get("name") or "the fruit",
                        look=(c.get("look") or "").strip() or "an exaggerated expression and pose that shows its personality")


def make_sheets(conn, character_id: int, n: int = SHEETS_PER_RUN, checked: bool = False) -> list[int]:
    """Queue n look sheets for a character (checked: the generation cap was already checked for the whole batch)."""
    from creative_intelligence import budget, jobs
    from creative_intelligence.webapp.common import _product_dict
    c = conn.execute("SELECT * FROM characters WHERE id=?", (character_id,)).fetchone()
    if c is None:
        raise ValueError("character not found")
    if not checked:
        budget.check(conn, "generation", estimate(conn, n))
    product = _product_dict(conn, c["product_id"]) or {}
    prompt = sheet_prompt(dict(c), product)
    now = datetime.utcnow().isoformat(timespec="seconds")
    with conn:
        ids = [conn.execute("INSERT INTO character_sheets (character_id, prompt, created_at) VALUES (?,?,?)",
                            (character_id, prompt, now)).lastrowid for _ in range(max(1, min(n, 4)))]
    jobs.enqueue(conn, "character_sheet", {"sheet_ids": ids})
    return ids


def process(sheet_ids: list[int], db: Callable[[], Any], generate: Callable[..., str] | None = None) -> dict[str, Any]:
    """The character_sheet job: each sheet in turn, from the product's reference photos."""
    from creative_intelligence.adstudio import costs
    from creative_intelligence.providers import model, replicate
    from creative_intelligence.storage import storage
    from creative_intelligence.webapp.common import _primary_photo_uris
    generate = generate or replicate.generate_image
    conn = db()
    made = 0
    try:
        for sid in sheet_ids:
            row = conn.execute("SELECT s.*, c.product_id FROM character_sheets s JOIN characters c ON c.id=s.character_id"
                               " WHERE s.id=?", (sid,)).fetchone()
            if row is None or row["status"] != "queued":
                continue
            try:
                url = generate(row["prompt"], _primary_photo_uris(conn, row["product_id"], limit=3), "1:1")
                path = storage().put_url("presenters", f"character{row['character_id']}_sheet{sid}.png", url)
                costs.log_image(conn, model("image"), "character", sid)
                with conn:
                    conn.execute("UPDATE character_sheets SET path=?, status='done' WHERE id=?", (path, sid))
                made += 1
            except Exception as exc:
                with conn:
                    conn.execute("UPDATE character_sheets SET status='failed', error=? WHERE id=?", (str(exc)[:300], sid))
        return {"made": made}
    finally:
        conn.close()


def set_sheet_status(conn, sheet_id: int, status: str) -> str:
    """approved ("Use this one"): this sheet becomes the character's look, the character is approved and becomes the
    one the product's episodes use; rejected: hidden."""
    if status not in ("approved", "rejected"):
        return "bad status"
    row = conn.execute("SELECT * FROM character_sheets WHERE id=?", (sheet_id,)).fetchone()
    if row is None or not row["path"]:
        return "sheet not found"
    with conn:
        if status == "approved":
            conn.execute("UPDATE character_sheets SET status='done' WHERE character_id=? AND status='approved'",
                         (row["character_id"],))
            conn.execute("UPDATE characters SET approved_sheet_id=?, status='approved', chosen_at=? WHERE id=?",
                         (sheet_id, datetime.utcnow().isoformat(timespec="microseconds"), row["character_id"]))
        else:
            conn.execute("UPDATE characters SET approved_sheet_id=NULL, status='draft' WHERE id=? AND approved_sheet_id=?",
                         (row["character_id"], sheet_id))
        conn.execute("UPDATE character_sheets SET status=? WHERE id=?", (status, sheet_id))
    return ""


def for_product(conn, product_id: int) -> dict[str, Any] | None:
    """The approved character the product's episodes use: the one last picked with "Use this one", with its approved
    sheet's path, or None."""
    r = conn.execute("SELECT c.*, s.path AS sheet_path FROM characters c JOIN character_sheets s ON s.id=c.approved_sheet_id"
                     " WHERE c.product_id=? AND c.status='approved'"
                     " ORDER BY c.chosen_at IS NULL, c.chosen_at DESC, c.id DESC LIMIT 1", (product_id,)).fetchone()
    return dict(r) if r else None


def approved(conn, character_id: int) -> dict[str, Any] | None:
    """One character, if it is (still) approved, with its approved sheet's path."""
    r = conn.execute("SELECT c.*, s.path AS sheet_path FROM characters c JOIN character_sheets s ON s.id=c.approved_sheet_id"
                     " WHERE c.id=? AND c.status='approved'", (character_id,)).fetchone()
    return dict(r) if r else None


def next_step(conn, product_id: int) -> dict[str, str]:
    """The one next step for this product's fruit character, in plain words: {step, text}. step is make (no
    characters), sheet (no look sheet yet), wait (sheets being drawn), failed (every sheet failed), pick (a sheet is
    ready, none picked) or ready (an approved character is in use)."""
    if (ch := for_product(conn, product_id)) is not None:
        return {"step": "ready", "text": f"{ch['name']} is in use: the Fruit characters style writes its episodes."}
    cs = listing(conn, product_id)
    if not cs:
        return {"step": "make", "text": f"This product has no fruit character yet. Open {PANEL} and click Make characters."}
    sheets = [s for c in cs for s in c["sheets"]]
    if any(s["status"] == "done" for s in sheets):
        return {"step": "pick", "text": f"Pick a character: in {PANEL}, click Use this one under the look you like."}
    if any(s["status"] == "queued" for s in sheets):
        return {"step": "wait", "text": "The look sheets are still being drawn (a minute or two). Then click Use this one "
                                        f"in {PANEL}."}
    if sheets:
        err = next((s["error"] for s in sheets if s["error"]), "")
        return {"step": "failed", "text": f"The look sheets failed{' (' + err + ')' if err else ''}. In {PANEL}, click "
                                          "Try again on a character."}
    names = ", ".join(c["name"] for c in cs[:4])
    return {"step": "sheet", "text": f"{names} {'has' if len(cs) == 1 else 'have'} no look sheet yet, and a character can "
                                     f"only be used once you approve its look. In {PANEL}, click Make look sheet, then "
                                     "Use this one."}


def for_plan(conn, product_id: int) -> dict[str, Any]:
    """The character a planned batch writes for (feedback 2026-10-23: "auto-generate the character, with the option
    to edit or change it under Characters"): the one in use, else the newest made one; with none, the five are made
    now (look sheets queued) and the first is used. Its sheet is approved automatically once drawn (auto_approve)."""
    ch = for_product(conn, product_id)
    if ch is not None:
        return ch
    if not listing(conn, product_id):
        _, _, why = make_cast(conn, product_id)
        if why and not listing(conn, product_id):
            raise ValueError(why)
    first = listing(conn, product_id)[0]
    return dict(conn.execute("SELECT * FROM characters WHERE id=?", (first["id"],)).fetchone())


def auto_approve(conn, character_id: int) -> str:
    """Use the character's first finished look sheet when none is approved yet: '' when it is ready, 'wait' while its
    sheets are being drawn, or the reason it can't be (every sheet failed)."""
    if approved(conn, character_id) is not None:
        return ""
    rows = conn.execute("SELECT id, status, error FROM character_sheets WHERE character_id=? ORDER BY id",
                        (character_id,)).fetchall()
    done = next((r["id"] for r in rows if r["status"] == "done"), None)
    if done:
        return set_sheet_status(conn, done, "approved")
    if any(r["status"] == "queued" for r in rows):
        return "wait"
    if not rows:
        make_sheets(conn, character_id, 1)
        return "wait"
    return f"The look sheets failed ({next((r['error'] for r in rows if r['error']), 'no reason given')}). Try again in {PANEL}."


SCENE = "character_scene"
SCENE_TRIES = 2


def prepare(conn, product_id: int) -> dict[str, str]:
    """One video (feedback 2026-10-23: "this is still broken, this an auto generated character"): everything the
    Fruit characters style needs before its script, made without a trip to Characters: the characters (if none) and
    the first finished look approved. The scene shot is started here but never waited for (2026-10-26: "Making the
    scene shot takes a long time"): the script is written while it's made, and the render waits for it. {step,
    text}: step is ready, wait (call again shortly) or failed. The character can still be changed under Characters."""
    from creative_intelligence.adstudio import shots
    try:
        ch = for_plan(conn, product_id)
    except ValueError as exc:
        return {"step": "failed", "text": str(exc)}
    why = auto_approve(conn, ch["id"])
    if why == "wait":
        return {"step": "wait", "text": f"Drawing {ch['name']}'s look (a minute or two, about ${estimate(conn, 1):.2f})."}
    if why:
        return {"step": "failed", "text": why}
    if shots.passing(conn, product_id, SCENE):
        return {"step": "ready", "text": ""}
    tag = f"+character:{ch['id']}+"
    rows = conn.execute("SELECT status FROM adstudio_videos WHERE product_id=? AND style='shot' AND shot_type=?"
                        " AND instr(COALESCE(prompt_version, ''), ?) > 0 ORDER BY id DESC", (product_id, SCENE, tag)).fetchall()
    if len(rows) >= SCENE_TRIES and rows[0]["status"] not in ("queued", "running"):
        return {"step": "failed", "text": f"{ch['name']}'s scene shot didn't pass QA twice. In {PANEL}, make it again "
                                          "or pick another character."}
    if not rows or rows[0]["status"] not in ("queued", "running"):
        shots.create(conn, product_id, SCENE)          # made alongside the script; Make waits for it
    return {"step": "ready", "text": ""}


def require(conn, product_id: int) -> dict[str, Any]:
    """The character the product's episodes use, or a ValueError saying exactly what to do next."""
    ch = for_product(conn, product_id)
    if ch is None:
        raise ValueError(next_step(conn, product_id)["text"])
    return ch


def bible(c: dict[str, Any]) -> str:
    return json.dumps({"name": c["name"], "personality": c["personality"], "voice": c["voice"],
                       "do": c["dos"], "dont": c["donts"]}, ensure_ascii=False)
