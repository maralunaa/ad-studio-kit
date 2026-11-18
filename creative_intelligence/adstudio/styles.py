"""
Video styles and variants (Video Studio V6), on the timeline engine.

A styled video is a script of LINES; each line is one of three kinds:
  narrator   the house voice reads it over a library shot
  presenter  an approved presenter says it on camera (a talking-head clip; ai_person)
  text       big on-screen words over a library shot, no voice
The style decides the mix. The styles are the live video styles in knowledge/styles.yaml (Fruit Story,
Talking head, Sensory / ASMR, Kinetic type, POV, or the structure of a decoded winning ad), each with its
prompt (prompts/style-<key>, which reads the style's playbook entry); adding a style is a knowledge entry plus
its prompt. Fruit Story has two modes: presenter-led (an AI narrator on camera, with cutaways) or narrated
(voice-over only, no presenter and no talking-head cost).

Variants are made from the same pieces without new shots: 3 hooks (the first line), 30 s and 15 s cuts
(lines marked short), 9:16 and 4:5, two caption styles. Each piece (a narrator line's voice-over, a presenter
clip) is made once and reused, so a variant only costs what is new in it (another hook). Every variant is a
render of the same video: its own Asset ID under one concept.

Templates and proven styles are starting points; new styles are tested next to them ("test").
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from itertools import product as cartesian
from pathlib import Path
from typing import Any, Callable

from creative_intelligence import config, knowledge, prompts
from creative_intelligence.adstudio import animate, prelaunch, shots, story, timeline, videotools, voices

KINDS = ("narrator", "presenter", "text")
TABLE = "presenter_table"        # Presenter at the table: on camera the whole video, every line a presenter line
# Talking head (feedback 2026-10-29): the shots fill the frame and the presenter's head talks in a circle in the
# corner the whole video, not the presenter full screen
PIP = ("talking_head",)
# captions at the top of the safe zone, clear of what fills the middle: the presenter's head in the bottom corner, the
# fruit character's face and the clay fruit (feedback 2026-11-03: "the text can't be hiding the character")
CAPTIONS_TOP = PIP + ("fruit_characters", "claymation")
MODE_LABELS = {"presenter": "presenter-led", "narrated": "narrated", "hands": "hands only"}
NO_PRESENTER_MODES = ("narrated", "hands")
ROUTES = ("video_style", "winners")
# Animated styles stay animated from the first line to the last, on their own cast cards (clay shots, the character
# scene), never a real-fruit shot (feedback 2026-10-26: "only the first shot was clay, the rest is images"; "these
# should be full clay or full animation, not turn into real fruit"). The offer card still shows the product.
# Sensory too (feedback 2026-10-26: "sensory should be fruit dripping, hands ripping the fruit in half"): only its own
# tactile cards plus the juice macro, so it can't drift into a kinetic-type look.
OWN_SHOTS_ONLY = {"claymation": (), "fruit_characters": (), "sensory": ("knife_cut", "cross_section_macro")}


def catalog() -> dict[str, dict[str, Any]]:
    """The video styles Ad Studio makes, from knowledge/styles.yaml (live video styles with a script route):
    {key: {label, presenter (can have presenter lines), voice, modes, default_mode, prompt, length_s, when}}."""
    out = {}
    for s in knowledge.styles("video", "live"):
        if s["route"]["kind"] not in ROUTES:
            continue
        out[s["key"]] = {"label": s["name"], "presenter": "presenter" in s["inputs"] or "presenter" in s.get("modes", []),
                         "voice": s.get("voice", True), "modes": s.get("modes", []), "default_mode": s.get("default_mode"),
                         "prompt": s["route"].get("prompt", ""), "route": s["route"]["kind"], "length_s": s.get("length_s"),
                         "when": s.get("when", ""), "shot_order": s.get("shot_order", []),
                         "requires_facts": s.get("requires_facts", []), "shot_types": s.get("shot_types", [])}
    return out


def reserved_shots() -> dict[str, list[str]]:
    """Shot types made only for one style (shot card `styles`), e.g. the supermarket fruit."""
    return {k: c["styles"] for k, c in prompts.shot_cards().items() if c.get("styles")}


def style_shots(product: dict[str, Any], style: str) -> list[str]:
    """The style's own shot types this product can have (2026-11-11: hands can't pull a mango or an orange apart, so a
    card's not_for fruit, or a retired card, drops out instead of being required)."""
    cards = prompts.shot_cards()
    return [t for t in catalog().get(style or "", {}).get("shot_types") or []
            if t in cards and shots.applies(cards[t], product)]


def menu_for(conn, product_id: int, style: str, plan: bool = False) -> list[dict[str, Any]]:
    """The library shots a style's script may use: passing shots that aren't reserved for a style, plus this style's
    own cards that it casts on lines (claymation, ripening). The supermarket comparison is never cast: a compare
    line uses it automatically. plan=True (storyboards): every shot card, made or not; missing ones are made once
    the storyboard is approved."""
    cards = prompts.shot_cards()
    own = [k for k, c in cards.items() if style in (c.get("styles") or []) and c.get("cast")]
    menu = [m for m in story.shot_menu(conn, product_id) if plan or m["in_library"]]
    for s in (shots.library(conn, product_id) if own else []):
        if s["type"] in own and (plan or s["status"] == "passing"):
            menu.append({"type": s["type"], "label": s["label"], "summary": s["summary"], "use": s["use"],
                         "in_library": s["status"] == "passing", "estimate": s["estimate"]})   # a storyboard makes the rest
    return menu


def missing_facts(conn, product: dict[str, Any], style: str) -> list[str]:
    need = catalog()[style]["requires_facts"]
    if not need:
        return []
    from creative_intelligence.adstudio import facts
    have = {f["category"] for f in facts.list_facts(conn, product.get("id"), facts._family(product))}
    return [c for c in need if c not in have]


def style_prompt(style: str) -> str:
    return catalog()[style]["prompt"]


def mode_for(style: str, mode: str | None) -> str | None:
    """The style's mode (its default when none or an unknown one is given); None for styles without modes."""
    st = catalog()[style]
    return (mode if mode in st["modes"] else st["default_mode"]) if st["modes"] else None


def label(style: str, mode: str | None = None) -> str:
    st = catalog().get(style)
    name = st["label"] if st else style.replace("_", " ").title()
    return f"{name} · {MODE_LABELS.get(mode, mode)}" if mode else name
TEXT_WORDS_PER_S = 2.2
# Presenter lines are spoken slower, with a beat between clips: Talking head and Presenter at the table came out at
# 35–37 s from a 30 s brief (2026-11-02), about 2.2 words a second once the end card is taken off.
PRESENTER_WORDS_PER_S = 2.2


def word_budget(seconds: int, style: str, mode: str | None) -> int:
    """Spoken words that fit `seconds`: presenter-led scripts get fewer, and the end card's seconds have no voice."""
    lipsynced = "character" in (knowledge.style(style).get("inputs") or [])    # a fruit character speaks as slowly
    if style in PIP or style == TABLE or mode == "presenter" or lipsynced:
        return round((seconds - timeline.END_CARD_S) * PRESENTER_WORDS_PER_S)
    return round(seconds * story.WORDS_PER_S)
# Each text-led format has its own look (feedback 2026-10-26: kinetic, sensory, fast-cut and ripening "all look the
# same, same text style and colours"): its type treatment (timeline.TEXT_LOOKS), how shots join, and its pace.
# The voiced formats differ by caption look (timeline.CAPTION_STYLES), how shots join, and their extras.
STYLE_LOOKS = {"fast_cut_montage": {"text": "label", "transition": 0.0, "min_s": 0.9},
               "sensory": {"text": "whisper", "transition": 0.8, "min_s": 2.6},
               "ripening_timelapse": {"text": "day", "transition": 0.0, "min_s": 2.4},   # slow (2026-11-03)
               "fruit_story": {"captions": "bold", "transition": 0.5},          # cinematic, slow dissolves
               "template": {"captions": "bold", "transition": 0.3},
               # hard cuts where a face or the clay is on screen (2026-11-03: cross-fades ghosted the presenter
               # and blended clay shots into each other)
               "talking_head": {"captions": "boxed", "transition": 0.0},
               "how_to_eat": {"captions": "boxed", "transition": 0.3, "steps": True},
               "presenter_table": {"captions": "casual", "transition": 0.0},
               "pov": {"captions": "casual", "transition": 0.12},               # quick, phone-native
               "comment_reply": {"captions": "casual", "transition": 0.25},
               "us_vs_supermarket": {"captions": "accent", "transition": 0.3},
               "claymation": {"captions": "hand", "transition": 0.0},
               "fruit_characters": {"captions": "bubble", "transition": 0.0},
               # the user's best video (2026-11-04): quick hard cuts, half a sentence of small text at a time
               "behind_the_farm": {"captions": "quiet", "transition": 0.0, "max_words": 6, "min_s": 1.2}}
# How each format wants its narrator to sound, over the voice's own persona (2026-11-04: "make voices and clips better
# in line with the new changes"): Wade's calm-farmer delivery was reading the clay stories too. A fruit character
# speaks in its own personality (narrator_style()).
VOICE_STYLES = {"claymation": "a warm, playful storyteller with a smile in the voice, like a stop-motion short",
                "sensory": "soft, slow and close to the microphone, almost a whisper",
                "how_to_eat": "friendly and clear, like showing a friend in the kitchen",
                "pov": "casual and excited, like opening a gift on camera"}


def narrator_voice(voice: str, presenter_voice: str = "") -> str:
    """A persona's own voice (the grandmother's) is only heard with that persona on camera; otherwise its stand-in."""
    if voice in config.PERSONA_ONLY_VOICES and voice != presenter_voice:
        return config.PERSONA_ONLY_VOICES[voice]
    return voice


def narrator_style(st: dict[str, Any]) -> str:
    """The delivery a narrator line gets in this video ('' = the voice's own)."""
    ch = st.get("character") or {}
    if ch:
        return (f"a lively animated cartoon fruit character, {ch.get('personality') or 'playful and bouncy'}; "
                "expressive, funny, with real reactions")
    return VOICE_STYLES.get(st.get("style") or "", "")
# One voice per video (feedback 2026-10-26: "the presenter is singing, then a different voice, then an Australian
# one"): Veo invents a new voice for every clip, so a styled video's presenter speaks in the house voice the narrator
# uses (an avatar model lip-syncs it). Veo's own voice stays for single talking clips (Video › Presenters).
# Kling Avatar: about a third cheaper than OmniHuman (feedback 2026-11-02, "make these cheaper").
DEFAULT_TALKING = "kling_avatar"
# What the Video Studio picks first (2026-11-05: "the seedance presenters are better"); DEFAULT_TALKING stays the
# fallback for videos made before, so their saved voice lines and clips keep matching
PICKED_TALKING = "seedance_text"
PIECES_AT_ONCE = 8        # voice lines and presenter clips made side by side: a 7-line script in one round, not two


def scene_method(method: str, style: str | None) -> str:
    """A presenter filling the frame with the product starts every line from the same scene picture, so the method has
    to take a picture (feedback 2026-11-10, Presenter at the table on Seedance-from-text: a different man in every
    clip and a bowl of random fruit). The Talking-head corner too (feedback 2026-11-11): drawn from the description it
    stood in the presenter's kitchen, so nothing was keyed out (the head sat in a square with its room), and it was a
    different person on every line; it has to start from the cut-out on green."""
    from creative_intelligence.adstudio import talking
    if method == "seedance_text":
        return talking.FALLBACK
    return method


def one_voice(method: str) -> str:
    from creative_intelligence.adstudio import talking
    return talking.FALLBACK if (talking.METHODS.get(method) or {}).get("kind") == "native" else method
MODEL = config.CI_CLAUDE_CHECK_MODEL

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["title", "hook_headline", "cta", "hooks", "lines"],
    "properties": {
        "title": {"type": "string"},
        "hook_headline": {"type": "string", "description": "2-6 words on screen during the first line"},
        "cta": {"type": "string", "description": "2-4 word call to action for the end card"},
        "hooks": {"type": "array", "items": {"type": "string"},
                  "description": "exactly 3 different first lines (line 1 and two alternatives), same kind as line 1"},
        "lines": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["text", "kind", "shot_type", "short"],
            "properties": {"text": {"type": "string"}, "kind": {"type": "string", "enum": list(KINDS)},
                           "shot_type": {"type": "string", "description": "a library shot type (ignored for presenter lines)"},
                           "short": {"type": "boolean", "description": "kept in the 15-second cut"},
                           "compare": {"type": "boolean", "description": "Us vs supermarket only: show the supermarket "
                                       "fruit above this shot (split screen)"}}}},
    }}


# ── the script ──────────────────────────────────────────────────────────────
def write(conn, product: dict[str, Any], style: str, seconds: int = 30, template: dict[str, Any] | None = None,
          idea: str = "", client: Any = None, mode: str | None = None, comment_id: int | None = None,
          plan: bool = False, n_hooks: int = 3, angle: str = "", avoid: list[str] | None = None,
          presenter_id: int | None = None) -> dict[str, Any]:
    """plan=True writes a storyboard: shots not made yet may be used (listed in missing_shots, made after approval).
    avoid: lines this video must not repeat on top of the product's recent ones (its batch siblings')."""
    from creative_intelligence.adstudio import costs, facts, presenters
    from creative_intelligence.adstudio.chat import _client, _product_line
    from creative_intelligence.adstudio.hook_university import origin_claim
    mem = bool(conn is not None and mode != "narrated" and catalog().get(style, {}).get("presenter")
               and presenters.tells_memories(conn, presenter_id))
    if style not in catalog():
        raise ValueError("unknown style")
    mode = mode_for(style, mode)
    if gaps := missing_facts(conn, product, style):
        names = ", ".join(facts.CATEGORIES.get(g, g) for g in gaps)
        raise ValueError(f"Add the approved {names} facts in the Product Hub first: this style only uses those.")
    character = None
    if "character" in (knowledge.style(style).get("inputs") or []):   # the character first: its shot needs it
        from creative_intelligence.adstudio import characters
        character = characters.for_plan(conn, product["id"]) if plan else characters.require(conn, product["id"])
    for t in ([] if plan else style_shots(product, style)):
        if not shots.passing(conn, product["id"], t):
            c = shots.card(t)
            if c.get("character") and character:
                raise ValueError(f"Make the {c['label']} shot for {character['name']} first: click Make the scene shot "
                                 "in Video › Create › Characters (or make it in Video › Shots).")
            raise ValueError(f"Make the {c['label']} shot in Video › Shots first.")
    comment = None
    if "real_comments" in (knowledge.style(style).get("inputs") or []):
        from creative_intelligence.adstudio import reviews
        got = [g for g in reviews.resolve(conn, [int(comment_id)] if str(comment_id or "").isdigit() else [],
                                          product["id"]) if g["kind"] in ("comment", "question")]
        if not got:
            raise ValueError("Pick a real comment or question from the library (Admin › Real reviews and comments).")
        comment = got[0]
    menu = menu_for(conn, product["id"], style, plan)
    budget = word_budget(seconds, style, mode)
    if not menu:
        raise ValueError("No shots in the library yet: make some in Video › Shots.")
    listing ="\n".join(f"- {m['type']}: {m['label']} — {m['summary']}" + ("" if m.get("in_library", True) else " (made for this video)")
                       for m in menu)
    ask = (f"{_product_line(product)}\n\n{facts.prompt_block(conn, product)}\n\nSTYLE:\n{prompts.text(style_prompt(style))}\n"
           + (f"\nMODE: {mode}. " + ("Presenter-led: the presenter lines described above are on camera.\n" if mode == "presenter"
              else "Hands only: no presenter lines at all; the narrator speaks over hands-only shots.\n" if mode == "hands"
              else "Narrated: voice-over only. No presenter lines at all; the narrator says the hook and why it's "
                   "different over the shots.\n") if mode else "")
           + (f"\nWINNING AD STRUCTURE:\n{json.dumps(template, indent=1)[:6000]}\n" if template else "")
           + (f"\nIdea to test: {idea}\n" if idea else "")
           + (f"\nOPENINGS THIS PRODUCT'S OTHER VIDEOS ALREADY USE (open differently, in this format's own way; never "
              f"reuse one or its idea):\n" + "\n".join(f"- {u}" for u in used) + "\n" if (used := used_openings(conn, product["id"])) else "")
           + (f"\nLINES THIS PRODUCT'S OTHER VIDEOS ALREADY SAY (never reuse one, nor a quote or fact put the same way; "
              f"lean on other approved facts and say things differently):\n" + "\n".join(f"- {u}" for u in said) + "\n"
              if (said := list(dict.fromkeys([*(avoid or []), *used_lines(conn, product["id"])]))[:60]) else "")
           + (f"\nTHIS VIDEO'S ANGLE: lead with {angle}; the other videos take other angles.\n" if angle else "")
           + (presenters.brief_block(conn, presenter_id) if conn is not None and mode != "narrated"
              and catalog().get(style, {}).get("presenter") else "")
           + (f"\nTHE CHARACTER (its approved bible; stay in character): {characters.bible(character)}\n" if character else "")
           + ((f"\nTHE REAL COMMENT TO ANSWER (shown as a sticker exactly as written; never change or extend it):\n"
               f"“{comment['text']}”\n" if comment["kind"] == "comment" else
               f"\nTHE QUESTION TO ANSWER (one people really ask, shown as a 'People often ask' card exactly as written; "
               f"it is not our customer's comment, so never say 'you asked' or thank anyone for it):\n“{comment['text']}”\n")
              if comment else "")
           + f"\nWrite a {seconds}-second video (about {budget} spoken words if it has a voice), "
             f"4 to 9 lines, plus {n_hooks} hooks that open in clearly different ways. Mark the lines to keep in a 15-second cut (the first line, the best middle "
             f"line or two, the last line).\n\nSHOTS IN THE LIBRARY (use only these):\n{listing}")
    client = client or _client(log=False)
    s = _ask(client, ask)
    costs.log_tokens(conn, MODEL, "copy", s.pop("_usage"), "styled-script", product["id"])
    s = _clean(s, menu, style, mode, n_hooks)
    s = speak(conn, client, s, product, character, menu)
    if (said := sum(len(ln["text"].split()) for ln in s["lines"])) > budget * 1.05:     # one trim, never padding
        short = _ask(client, f"{ask}\n\nYOUR DRAFT:\n{json.dumps({k: s[k] for k in SCHEMA['required']})}\n\nIt has {said} "
                             f"spoken words, so it runs long. Cut it to at most {budget} words (fewer or shorter lines; "
                             "keep the hooks, kinds and shots).")
        costs.log_tokens(conn, MODEL, "copy", short.pop("_usage"), "styled-script", product["id"])
        s = _clean(short, menu, style, mode, n_hooks)
    s["claims"] = _check(conn, product, {**s, "memories": mem, "style": style})
    repaired: list[str] = []
    for _ in range(REPAIRS):                    # the check can still find one after a repair (live test 2026-10-26)
        bad = claim_problems(s["claims"])
        if not bad:
            break
        fixed = _ask(client, f"{ask}\n\nYOUR DRAFT:\n{json.dumps({k: s[k] for k in SCHEMA['required']})}\n\nThe claims check "
                             f"found: {'; '.join(bad)}. Rewrite only what is needed so nothing goes beyond the approved "
                             "facts and no other fruit is named (keep kinds, shots and length).")
        costs.log_tokens(conn, MODEL, "copy", fixed.pop("_usage"), "styled-script", product["id"])
        repaired += bad
        s = {**_clean(fixed, menu, style, mode, n_hooks), "repaired": repaired}
        s["claims"] = _check(conn, product, {**s, "memories": mem, "style": style})
    s["flags"] = [f"Line {i + 1} says where it comes from" for i, ln in enumerate(s["lines"])
                  if origin_claim(ln["text"], product.get("name") or "")]
    s["style"], s["mode"] = style, mode
    s["memories"] = mem                                 # an edit is checked the same way again
    clay: list[str] = []
    if style in OWN_SHOTS_ONLY:
        cards = prompts.shot_cards()
        clay = [m["type"] for m in menu if style in (cards.get(m["type"]) or {}).get("styles", [])
                and (cards.get(m["type"]) or {}).get("cast")]
        clay += [t for t in OWN_SHOTS_ONLY[style] if clay and t in {m["type"] for m in menu}]
        for i, ln in enumerate(s["lines"] if clay else []):
            if ln["shot_type"] not in clay:
                ln["shot_type"] = clay[i % len(clay)]
    if plan and style in NO_OWN_SHOTS:          # every line and hook is the presenter at the table: no shots to make
        s["missing_shots"], s["hook_shots"] = [], []
    elif plan:
        have = {m["type"] for m in menu if m.get("in_library")}
        s["missing_shots"] = sorted(({ln["shot_type"] for ln in s["lines"]} | set(style_shots(product, style))) - have)
        s["hook_shots"] = hook_shots(s, sorted(have) or [m["type"] for m in menu])
        if clay:                                # every hook opens on clay (or the character) too
            s["hook_shots"] = [t if t in clay else clay[i % len(clay)] for i, t in enumerate(s["hook_shots"])]
    if character:
        if s["lines"][-1]["kind"] == "presenter":
            s["lines"][-1]["kind"] = "narrator"
        s["character"] = {"id": character["id"], "name": character["name"], "voice": character["voice"]}
    if comment:
        s["comment"] = {"id": comment["id"], "text": comment["text"], "source": comment["source"],
                        "posted_on": comment["posted_on"], "kind": comment["kind"]}
    return s


SPEAK_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["hooks", "lines"],
                "properties": {"hooks": {"type": "array", "items": {"type": "string"}},
                               "lines": {"type": "array", "items": {"type": "string"}}}}
SPEAK_SLACK = 4      # words a line may grow: a clipped fragment becomes a whole sentence (2026-11-04; was 2)
SPEAK_ASK = ("You edit dialogue for short video ads. Read each spoken line below OUT LOUD as {who} would say it to one "
             "friend, unscripted. Rewrite only the lines that sound written (a caption, a slogan, a product page, an "
             "announcer); leave the ones that already sound said exactly as they are. Keep every fact and the product "
             "name, the meaning and order of the lines, each line's job (the hook stays a hook, the last line still "
             "closes), and never add a claim, a number, a place or another fruit. A line may grow by a few words only to "
             "become a whole sentence. Use "
             "contractions, whole natural sentences (never clipped labels or commands strung together like \"Juice "
             "dripping. Score it. Taste it.\"), plain words; no ad words, no stacked adjectives, no em dashes, "
             "parentheses or semicolons, one exclamation mark at most. The words must fit the picture under them: a line "
             "that names an action (cut, slice, scoop, open, peel, press, pick) plays over a shot that shows exactly that "
             "action, otherwise it talks about what that shot does show (feedback 2026-11-03: \"watch me cut it\" over a "
             "close-up). Return the same number of hooks and lines.\n\n"
             "PRODUCT: {name}\n\nHOOKS:\n{hooks}\n\nLINES (each with the shot on screen):\n{lines}")


def shown(ln: dict[str, Any], menu: list[dict[str, Any]] | None = None) -> str:
    """What a line's shot shows, for Claude: its label and summary."""
    m = next((m for m in menu or [] if m["type"] == ln.get("shot_type")), None)
    c = m or (prompts.shot_cards().get(ln.get("shot_type") or "") or {})
    return f"{c.get('label', ln.get('shot_type', ''))}: {c.get('summary', '')}".strip(": ")


def speak(conn, client: Any, s: dict[str, Any], product: dict[str, Any], character: dict[str, Any] | None = None,
          menu: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The read-it-aloud pass (feedback 2026-11-03: "make it feel more natural"): one more call rewrites only the
    lines that sound written rather than said. Same count, same kinds and shots; anything off keeps the draft."""
    from creative_intelligence.adstudio import costs
    spoken = [i for i, ln in enumerate(s["lines"]) if ln["kind"] != "text"]
    if not spoken:
        return s
    who = (f"{character['name']}, the fruit character" if character else
           "the presenter" if any(s["lines"][i]["kind"] == "presenter" for i in spoken) else "a warm narrator")
    ask = SPEAK_ASK.format(who=who, name=product.get("name") or "", hooks="\n".join(f"- {h}" for h in s["hooks"]),
                           lines="\n".join(f"{n + 1}. {s['lines'][i]['text']}   [on screen: {shown(s['lines'][i], menu)}]"
                                           for n, i in enumerate(spoken)))
    try:
        resp = client.beta.messages.create(
            model=MODEL, max_tokens=2000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"format": {"type": "json_schema", "schema": SPEAK_SCHEMA}},
            messages=[{"role": "user", "content": ask}])
        out = json.loads(next(b.text for b in resp.content if b.type == "text"))
        costs.log_tokens(conn, MODEL, "copy", resp.usage, "styled-speak", product.get("id"))
    except Exception:                                # noqa: BLE001  (the draft stands)
        return s
    hooks, lines = out.get("hooks"), out.get("lines")
    if not (isinstance(hooks, list) and isinstance(lines, list) and len(hooks) == len(s["hooks"])
            and len(lines) == len(spoken) and all(isinstance(x, str) and x.strip() for x in hooks + lines)):
        return s
    new = [dict(ln) for ln in s["lines"]]
    for i, text in zip(spoken, lines):
        if len(text.split()) <= len(new[i]["text"].split()) + SPEAK_SLACK:   # a fragment may become a sentence
            new[i]["text"] = text.strip()
    hooks = [h.strip() for h in hooks]
    if new and new[0]["kind"] != "text":
        hooks[0] = new[0]["text"]                     # hook 1 is line 1 (as _clean keeps it)
    return {**s, "hooks": hooks, "lines": new}


FIT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["lines"],
              "properties": {"lines": {"type": "array", "items": {"type": "string"}}}}


def fit_shots(conn, s: dict[str, Any], product: dict[str, Any], plays: dict[int, str], client: Any = None
              ) -> dict[str, Any]:
    """A shot swapped before the voice is made (it didn't pass QA): the lines over it are rewritten for the shot
    that actually plays (feedback 2026-11-03: "watch me cut it" over a close-up when the knife cut failed).
    plays: {line index: the shot type that plays}. Same facts and length; anything off keeps the line, on its new
    shot all the same."""
    from creative_intelligence.adstudio import costs
    from creative_intelligence.adstudio.chat import _client
    new = [dict(ln) for ln in s["lines"]]
    idx = [i for i, t in sorted(plays.items()) if 0 <= i < len(new) and t and t != new[i].get("shot_type")]
    for i in idx:
        new[i]["shot_type"] = plays[i]
    talk = [i for i in idx if new[i]["kind"] != "text"]
    out = {**s, "lines": new}
    if not talk:
        return out
    ask = ("These lines of a short video ad now play over a different shot. Rewrite each so its words fit what the "
           "shot shows: never name an action the shot doesn't show (cut, slice, scoop, open, peel, press); talk about "
           "what is on screen. Keep the facts, the tone and the line's job, add no claim, number, place or other fruit, "
           "and never make it longer. Spoken English, contractions, no em dashes. Return the lines in the same order.\n\n"
           f"PRODUCT: {product.get('name') or ''}\n\nTHE WHOLE SCRIPT (for context):\n"
           + "\n".join(f"- {ln['text']}" for ln in new if ln["kind"] != "text")
           + "\n\nREWRITE THESE:\n" + "\n".join(f"{n + 1}. {new[i]['text']}   [now on screen: {shown(new[i])}]"
                                                for n, i in enumerate(talk)))
    try:
        client = client or _client(log=False)
        resp = client.beta.messages.create(
            model=MODEL, max_tokens=1500, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"format": {"type": "json_schema", "schema": FIT_SCHEMA}},
            messages=[{"role": "user", "content": ask}])
        got = json.loads(next(b.text for b in resp.content if b.type == "text")).get("lines")
        costs.log_tokens(conn, MODEL, "copy", resp.usage, "styled-fit", product.get("id"))
    except Exception:                                # noqa: BLE001  (the words stand, on the new shot)
        return out
    if isinstance(got, list) and len(got) == len(talk):
        for i, text in zip(talk, got):
            if isinstance(text, str) and text.strip() and len(text.split()) <= len(new[i]["text"].split()) + 2:
                new[i]["text"] = text.strip()
        if new[0]["kind"] != "text" and s.get("hooks"):
            out["hooks"] = [new[0]["text"]] + list(s["hooks"][1:])
    return out


def _ask(client: Any, ask: str) -> dict[str, Any]:
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=4000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        system=prompts.text("story-script-system"), output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": ask}])
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to write this script")
    out = json.loads(next(b.text for b in resp.content if b.type == "text"))
    out["_usage"] = resp.usage
    return out


def revise(conn, product: dict[str, Any], s: dict[str, Any], change: str, client: Any = None) -> dict[str, Any]:
    """Video › Edit, "make it more…" (feedback 2026-10-26): Claude rewrites a finished script around one request, under
    the same rules as writing it (style, approved facts, the shots it may use), then the claims check with repairs.
    Keeps the character, the comment it answers and as many hooks as it had."""
    from creative_intelligence.adstudio import costs, facts
    from creative_intelligence.adstudio.chat import _client, _product_line
    style, mode = s.get("style") or "", s.get("mode")
    if style not in catalog():
        raise ValueError("unknown style")
    change = (change or "").strip()[:500]
    if not change:
        raise ValueError("Say what to change.")
    n_hooks = max(1, len(s.get("hooks") or []))
    menu = menu_for(conn, product["id"], style, plan=True)
    listing = "\n".join(f"- {m['type']}: {m['label']} — {m['summary']}" for m in menu)
    current = {k: s.get(k) for k in SCHEMA["required"]}
    ask = (f"{_product_line(product)}\n\n{facts.prompt_block(conn, product)}\n\nSTYLE:\n{prompts.text(style_prompt(style))}\n"
           + (f"\nMODE: {mode}.\n" if mode else "")
           + (f"\nTHE CHARACTER (stay in character): {s['character']['name']}\n" if s.get("character") else "")
           + (f"\nTHE COMMENT IT ANSWERS (unchanged): “{s['comment']['text']}”\n" if s.get("comment") else "")
           + f"\nTHE CURRENT SCRIPT:\n{json.dumps(current)}\n\nCHANGE IT LIKE THIS: {change}\n"
           f"Change only what the request needs; keep the rest. Keep {n_hooks} hooks, 4 to 9 lines and the 15-second "
           f"marks. It never adds a claim the approved facts don't give.\n\nSHOTS (use only these):\n{listing}")
    client = client or _client(log=False)
    new = _ask(client, ask)
    costs.log_tokens(conn, MODEL, "copy", new.pop("_usage"), "styled-revise", product["id"])
    out = {**s, **_clean(new, menu, style, mode, n_hooks)}
    out["claims"] = _check(conn, product, out)
    repaired: list[str] = []
    for _ in range(REPAIRS):
        bad = claim_problems(out["claims"])
        if not bad:
            break
        fixed = _ask(client, f"{ask}\n\nYOUR DRAFT:\n{json.dumps({k: out[k] for k in SCHEMA['required']})}\n\nThe "
                             f"claims check found: {'; '.join(bad)}. Rewrite only what is needed.")
        costs.log_tokens(conn, MODEL, "copy", fixed.pop("_usage"), "styled-revise", product["id"])
        repaired += bad
        out = {**out, **_clean(fixed, menu, style, mode, n_hooks), "repaired": repaired}
        out["claims"] = _check(conn, product, out)
    have = [m["type"] for m in menu if m.get("in_library")]
    out["hook_shots"] = hook_shots(out, have or [m["type"] for m in menu])
    out["change"] = change
    return out


# Music (feedback 2026-10-26: "the music was the same on all of them"): each format starts from tracks that suit its
# pace (picked by title and tempo name, not by ear), then every track the product's recent videos used is skipped.
# Formats with a voice only get instrumentals; voiceless ones may use any track.
MUSIC_PREFS = {"fast_cut_montage": ["power-on-instrumental.m4a", "dancing-in-the-sun-instrumental.m4a", "be-happy-instrumental.m4a"],
               "kinetic": ["magic-instrumental.m4a", "paint-me-happy-loop.m4a", "it-s-all-good-instrumental.m4a"],
               "sensory": ["slow-it-down-instrumental.m4a", "a-warm-hug-instrumental.m4a", "heartline.m4a"],
               "ripening_timelapse": ["i-like-sundays-instrumental.m4a", "perfect-instrumental.m4a", "fields-of-gold.m4a"],
               "claymation": ["paint-me-happy-loop.m4a", "a-bit-of-fun.m4a", "be-happy-instrumental.m4a"],
               "fruit_characters": ["a-bit-of-fun.m4a", "be-happy-instrumental.m4a", "magic-instrumental.m4a"],
               "pov": ["i-like-sundays-instrumental.m4a", "it-s-all-good-instrumental.m4a"],
               "fruit_story": ["a-warm-hug-instrumental.m4a", "perfect-instrumental.m4a", "dancing-in-the-sun-instrumental.m4a"]}


def pick_track(conn, product_id: int, style: str, has_voice: bool) -> str:
    """A track for this video: the format's own first, never one the product's last few videos used."""
    cat = story.music_catalog()
    ok = [t["file"] for t in cat if t["vocals"] is False or not has_voice]
    order = [f for f in MUSIC_PREFS.get(style, []) if f in ok] + [f for f in ok if f not in MUSIC_PREFS.get(style, [])]
    recent: list[str] = []
    for r in conn.execute("SELECT copy_json FROM adstudio_videos WHERE product_id=? AND source_kind='styled'"
                          " ORDER BY id DESC LIMIT 8", (product_id,)):
        try:
            m = json.loads(r[0] or "{}").get("music_used")
        except ValueError:
            m = None
        if m:
            recent.append(m)
    return next((f for f in order if f not in recent), order[0] if order else "")


def used_openings(conn, product_id: int, n: int = 12) -> list[str]:
    """The first lines and hooks of the product's recent videos (live test 2026-10-26: five formats opened with
    "You've never seen a mango this pink")."""
    out: list[str] = []
    if conn is None:                            # a script written without the database (presets, tests)
        return out
    for r in conn.execute("SELECT copy_json FROM adstudio_videos WHERE product_id=? AND source_kind='styled'"
                          " ORDER BY id DESC LIMIT 20", (product_id,)):
        try:
            s = json.loads(r[0] or "{}")
        except ValueError:
            continue
        for t in [((s.get("lines") or [{}])[0]).get("text", "")] + list(s.get("hooks") or [])[:1]:
            if t and t not in out:
                out.append(t)
    return out[:n]


FIXED_ORDER = ("behind_the_farm", "ripening_timelapse")   # their structure leads, not an angle (2026-11-04: Behind
                                                         # the farm opened on how to cut, the angle's lead)


def angle_for(style: str, n: int) -> str:
    """The angle a batch video leads with ('' for formats with a fixed structure)."""
    return "" if style in FIXED_ORDER else ANGLES[n % len(ANGLES)]


def used_lines(conn, product_id: int, n: int = 40) -> list[str]:
    """The spoken lines of the product's recent videos and storyboards (feedback 2026-11-03: "make the scripts of
    each variant different"; five of seven Cherimoya scripts quoted the same line)."""
    out: list[str] = []
    if conn is None:
        return out
    scripts = []
    try:                       # storyboards first, newest batch first: the batch being written isn't cut off at n
        for r in conn.execute("SELECT items_json FROM video_batches ORDER BY id DESC LIMIT 6"):
            scripts += [json.dumps(i.get("script") or {}) for i in json.loads(r[0] or "[]")
                        if i.get("product_id") == product_id and i.get("script")]
    except Exception:                                # noqa: BLE001  (no batches table in some tests)
        pass
    for r in conn.execute("SELECT copy_json FROM adstudio_videos WHERE product_id=? AND source_kind='styled'"
                          " ORDER BY id DESC LIMIT 12", (product_id,)):
        scripts.append(r[0])
    for raw in scripts:
        try:
            s = json.loads(raw or "{}")
        except ValueError:
            continue
        for ln in s.get("lines") or []:
            t = (ln.get("text") or "").strip()
            if t and ln.get("kind") != "text" and t not in out:
                out.append(t)
    return out[:n]


# Each video of a batch leads with its own angle, so scripts written side by side don't all reach for the same fact
# (feedback 2026-11-03).
ANGLES = ("the taste and texture, in plain words", "how to eat it, step by step", "what it looks like inside",
          "when it's ripe and how to tell", "the first bite, as a reaction", "what makes it unusual to look at",
          "the box arriving and what's in it", "a quote or a surprising approved fact")


def hook_shots(s: dict[str, Any], prefer: list[str]) -> list[str]:
    """One opening shot per hook, each different where the shots allow (Meta treats a new opening picture as a new
    ad; new text on the same picture as the same ad). The first hook keeps the script's own opening shot."""
    first = s["lines"][0]["shot_type"]
    pool = [first] + [t for t in dict.fromkeys(prefer + [ln["shot_type"] for ln in s["lines"]]) if t != first]
    return [pool[i % len(pool)] for i in range(len(s.get("hooks") or []))]


def _clean(s: dict[str, Any], menu: list[dict[str, Any]], style: str, mode: str | None = None,
           n_hooks: int = 3) -> dict[str, Any]:
    valid = [m["type"] for m in menu]
    st = catalog()[style]
    no_voice = st.get("voice") is False
    no_presenter = not st["presenter"] or mode in NO_PRESENTER_MODES
    for ln in s["lines"]:
        if ln["shot_type"] not in valid:
            ln["shot_type"] = valid[0]
        if no_voice:
            ln["kind"] = "text"
        elif style == TABLE or (style in PIP and not no_presenter):
            ln["kind"] = "presenter"                # on camera the whole video (Talking head: in the corner)
        elif no_presenter and ln["kind"] == "presenter":
            ln["kind"] = "narrator"
    if s["lines"]:
        s["lines"][0]["short"] = s["lines"][-1]["short"] = True
        hooks = [h for h in s.get("hooks") or [] if h.strip()]
        first = s["lines"][0]["text"]
        s["hooks"] = ([first] + [h for h in hooks if h != first])[:n_hooks]
    return s


REPAIRS = 2


def claim_problems(claims: dict[str, Any]) -> list[str]:
    """What the claims check found, in words a person can fix: each unapproved claim and each banned term."""
    return ([f"“{u['claim']}”" for u in claims.get("unlisted") or []]
            + [f"“{b['term']}”" + (" (another fruit)" if b.get("rule") == "other_fruit" else
                                   " (a season date: it goes stale)" if b.get("rule") == "season_date" else
                                   " (never say what the video is made of; talk about the fruit)" if b.get("rule") == "medium"
                                   else " (not allowed)")
               for b in claims.get("banned") or []])


# Animated formats never name what they're made of (feedback 2026-11-09: "this clay orange"; the cartoon too).
MEDIUM_WORDS = re.compile(r"\b(?:clay|claymation|plasticine|stop[\s-]?motion|cartoon|animated|animation)\b", re.I)
MEDIUM_STYLES = ("claymation", "fruit_characters")


def medium_hits(style: str, texts: list[str]) -> list[str]:
    if style not in MEDIUM_STYLES:
        return []
    return list(dict.fromkeys(m.group(0).lower() for t in texts for m in MEDIUM_WORDS.finditer(t or "")))


def _check(conn, product: dict[str, Any], s: dict[str, Any]) -> dict[str, Any]:
    from creative_intelligence.adstudio import facts
    items = {"script": " ".join(ln["text"] for ln in s["lines"]),
             "hooks": " ".join(s.get("hooks") or []), "hook": s["hook_headline"], "cta": s["cta"]}
    out = facts.check_copy(conn, product, items, source="styled", source_id=product["id"],
                           allow=facts.MEMORIES if s.get("memories") else "")
    if hits := medium_hits(s.get("style") or "", list(items.values())):
        out["banned"] = list(out.get("banned") or []) + [{"field": "script", "term": h, "rule": "medium"} for h in hits]
        out["status"] = "flagged"
    return out


# ── variants and their cost ─────────────────────────────────────────────────
def variants(s: dict[str, Any], hooks: int = 3, lengths: tuple[str, ...] = ("full", "short"),
             aspects: tuple[str, ...] = ("9:16", "4:5"), captions: tuple[str, ...] = ("bold",)) -> list[dict[str, Any]]:
    n = max(1, min(hooks, len(s.get("hooks") or [s["lines"][0]["text"]])))
    return [{"hook": h, "length": ln, "aspect": a, "captions": c}
            for h, ln, a, c in cartesian(range(n), lengths, aspects, captions)]


def lines_for(s: dict[str, Any], hook: int, length: str) -> list[dict[str, Any]]:
    first = {**s["lines"][0], "text": (s.get("hooks") or [s["lines"][0]["text"]])[hook]}
    if hook < len(s.get("hook_shots") or []):
        first["shot_type"] = s["hook_shots"][hook]                       # each hook opens on its own shot
    body = s["lines"][1:]
    if length == "short" and body:
        body = [ln for ln in body[:-1] if ln.get("short")] + body[-1:]     # the closing line always stays
    return [first] + body


def piece_key(ln: dict[str, Any], voice: str, presenter_id: int | None, method: str) -> str:
    who = f"p{presenter_id}:{method}" if ln["kind"] == "presenter" else voice if ln["kind"] == "narrator" else "text"
    return hashlib.sha1(f"{ln['kind']}|{who}|{ln['text'].strip()}".encode()).hexdigest()[:16]


def estimate(conn, s: dict[str, Any], vs: list[dict[str, Any]], voice: str, presenter_id: int | None,
             method: str = DEFAULT_TALKING) -> dict[str, Any]:
    """What the pieces cost (each made once) plus one word-timing run per hook × length."""
    from creative_intelligence.adstudio import talking
    from creative_intelligence.adstudio.costs import price
    method = one_voice(method)
    v = voices.voice(voice)
    seen, cost, first_cost, presenter_lines = {}, 0.0, 0.0, 0
    combos = sorted({(x["hook"], x["length"]) for x in vs})
    for i, (h, ln) in enumerate(combos):
        for line in lines_for(s, h, ln):
            k = piece_key(line, voice, presenter_id, method)
            if k in seen:
                continue
            c = talking.estimate(conn, method, line["text"]) if line["kind"] == "presenter" else \
                voices.estimate(conn, v, line["text"]) + (talking.lipsync_estimate(conn, line["text"])
                                                          if s.get("character") else 0.0) \
                if line["kind"] == "narrator" else 0.0
            seen[k] = c
            cost += c
            presenter_lines += line["kind"] == "presenter"
            if i == 0:
                first_cost += c
    words = price(conn, config.WORDS_MODEL.split(":")[0]) * len(combos)
    return {"total": round(cost + words + animate.QA_ESTIMATE_USD, 2), "first": round(first_cost + words / len(combos)
            + animate.QA_ESTIMATE_USD, 2), "variants": len(vs), "pieces": len(seen),
            "presenter_lines": presenter_lines, "per_extra_variant": round(max(0.0, cost - first_cost) / max(1, len(vs) - 1), 3)}


# ── create and render ───────────────────────────────────────────────────────
def new_shots(conn, product_id: int, s: dict[str, Any]) -> set[str]:
    """The shot types a script uses (its lines, its hooks' openings, its style's own cards) not in the library yet."""
    from creative_intelligence.webapp.common import _product_dict
    if s.get("style") in NO_OWN_SHOTS:          # the presenter on camera the whole video: no shots are made
        return set()
    used = {ln["shot_type"] for ln in s.get("lines") or []} | set(s.get("hook_shots") or []) | \
        set(style_shots(_product_dict(conn, product_id) or {}, s.get("style") or ""))
    return {t for t in used if t in prompts.shot_cards() and not shots.passing(conn, product_id, t)}


MAX_EXTRA_TAKES = 3     # a format made of its own few shots (clay) gets at most this many more takes of each per video


def repeat_takes(conn, product_id: int, s: dict[str, Any]) -> dict[str, int]:
    """{shot_type: takes to make} so no clip repeats in a video whose lines share a few shot types (2026-11-02,
    Claymation: 7 lines on 3 clay shots played the same clips again and again). Only the formats made of their own
    shots (OWN_SHOTS_ONLY); a character's scene is lip-synced per line, so it is left out."""
    if s.get("style") not in OWN_SHOTS_ONLY:
        return {}
    from collections import Counter
    uses = Counter(ln["shot_type"] for ln in s.get("lines") or [])
    cards = prompts.shot_cards()
    out = {}
    for t, n in uses.items():
        if t not in cards or cards[t].get("character") or shots.keeps_failing(conn, product_id, t):
            continue
        have = len(shots.passing_takes(conn, product_id, t)) + shots.takes(conn, product_id, t)["busy"]
        if (extra := min(MAX_EXTRA_TAKES, n - have)) > 0:
            out[t] = extra
    return out


def make_repeat_takes(conn, product_id: int, s: dict[str, Any]) -> list[int]:
    """Queue repeat_takes; returns the new shot ids (the render waits for them)."""
    ids = []
    for t, n in repeat_takes(conn, product_id, s).items():
        for _ in range(n):
            vid, _, reused = shots.create(conn, product_id, t, extra=True)
            if vid and not reused:
                ids.append(vid)
    return ids


NO_OWN_SHOTS = (TABLE,)            # on camera the whole video, hooks too (feedback 2026-11-02: "only the presenter")


def own_plan(s: dict[str, Any]) -> dict[str, str]:
    """One video's own takes (its opening and two beats) in its format's look (shots.LOOKS), as a batch makes them
    (feedback 2026-10-26: every single video reused the same library shots, so the formats all looked alike)."""
    from creative_intelligence.adstudio import video_batch
    if (s.get("style") or "") in NO_OWN_SHOTS:
        return {}
    return video_batch.own_shots(s, s.get("own_tag") or f"o{int(datetime.utcnow().timestamp() * 1000)}")


def own_shots_usd(conn, product_id: int, s: dict[str, Any]) -> float:
    from creative_intelligence.adstudio import video_batch
    return video_batch._own_cost(conn, product_id, {**s, "own_shots": own_plan(s)})


def new_shots_usd(conn, product_id: int, s: dict[str, Any]) -> float:
    return round(sum(shots.estimate(conn, shots.card(t), True) for t in new_shots(conn, product_id, s)), 2)


def create(conn, product_id: int, s: dict[str, Any], voice: str, presenter_id: int | None = None,
           method: str = DEFAULT_TALKING, vs: list[dict[str, Any]] | None = None, music: str = "",
           template_id: int | None = None, pieces: dict[str, Any] | None = None,
           make_shots: bool = False) -> tuple[int | None, str]:
    """pieces: voice lines and presenter clips already made (a draft's), reused instead of made again.
    make_shots (One video, feedback 2026-10-23): the shots the script uses that aren't in the library yet are queued
    here and the render waits for them, as a batch does; one that fails QA is swapped for another passing shot."""
    from creative_intelligence import budget, jobs
    from creative_intelligence.adstudio import talking
    style = s.get("style") or ""
    if style not in catalog():
        return None, "unknown style"
    if (s.get("character") or {}).get("voice"):         # a fruit character speaks in its own house voice
        voice = s["character"]["voice"]
    s["mode"] = mode_for(style, s.get("mode"))
    if s["mode"] in NO_PRESENTER_MODES:               # narrated / hands only: the narrator says every line
        for ln in s["lines"]:
            if ln["kind"] == "presenter":
                ln["kind"] = "narrator"
    try:
        voices.voice(voice)
    except ValueError as exc:
        return None, str(exc)
    needs_presenter = any(ln["kind"] == "presenter" for ln in s["lines"])
    if needs_presenter:
        if method not in talking.METHODS:
            return None, "unknown talking-head method"
        method = one_voice(method)
        try:
            p, _ = talking.portrait(conn, int(presenter_id or 0))
        except ValueError as exc:
            return None, str(exc)
        if p.get("voice") and not (s.get("character") or {}).get("voice"):
            voice = p["voice"]                          # the narrator speaks in the presenter's own voice
    voice = narrator_voice(voice, p.get("voice") if needs_presenter else "")
    if (s.get("claims") or {}).get("status") == "flagged":
        return None, "The script makes a claim that isn't an approved fact; rewrite it first."
    vs = vs or variants(s)
    est = estimate(conn, s, vs, voice, presenter_id, method)
    try:
        if est["total"] > budget.caps(conn)["video_job"]:
            raise budget.BudgetExceeded(f"About ${est['total']:.2f}: over the video job cap.")
        budget.check(conn, "video", est["total"])
    except budget.BudgetExceeded as exc:
        return None, str(exc)
    waiting = []
    if make_shots:
        for t in sorted(new_shots(conn, product_id, s)):
            if shots.keeps_failing(conn, product_id, t):
                continue                                 # another passing shot stands in at the render
            vid, _, reused = shots.create(conn, product_id, t)
            if vid and not (reused and shots.passing(conn, product_id, t)):   # one already on its way counts too
                waiting.append(t)
        waiting += [f"id:{v}" for v in make_repeat_takes(conn, product_id, s)]
        s["own_shots"] = own_plan(s)
        for t, tag in s["own_shots"].items():           # its own opening and beats, in its format's look
            if shots.keeps_failing(conn, product_id, t):
                continue
            vid, _, _ = shots.create(conn, product_id, t, look=tag, style=style)
            if vid:
                waiting.append(f"{t}@{tag}")
    settings = {**s, "voice": voice, "presenter_id": presenter_id, "method": method, "variants": vs, "music": music,
                "template_id": template_id, "estimate": est, "pieces": dict(pieces or {}), "waiting_shots": waiting}
    with conn:
        vid = conn.execute(
            "INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, duration_s, model, copy_json, voice,"
            " ai_person, prompt_version, created_at, idea_id) VALUES (?, 'styled', ?, ?, 30, 'styled', ?, ?, ?, ?, ?, ?)",
            (product_id, template_id or 0, style, json.dumps(settings, default=str), voice, int(needs_presenter),
             prompts.stamp("story-script-system", style_prompt(style)), datetime.utcnow().isoformat(timespec="seconds"),
             s.get("idea_id") or None)).lastrowid
    jobs.enqueue(conn, "styled", {"video_id": vid}, dedupe_key=f"video:{vid}")
    return vid, ""


def _silence(secs: float, out: Path) -> Path:
    videotools.run(["-f", "lavfi", "-t", f"{secs:.3f}", "-i", "anullsrc=r=48000:cl=mono", str(out)])
    return out


def _wav(src: str | Path, out: Path) -> Path:
    videotools.run(["-i", str(src), "-vn", "-ac", "1", "-ar", "48000", str(out)])
    return out


def _concat(wavs: list[Path], out: Path) -> Path:
    args: list[str] = []
    for w in wavs:
        args += ["-i", str(w)]
    graph = "".join(f"[{i}:a]" for i in range(len(wavs))) + f"concat=n={len(wavs)}:v=0:a=1[a]"
    videotools.run([*args, "-filter_complex", graph, "-map", "[a]", str(out)])
    return out


def script_words(lines: list[dict[str, Any]], spans: list[tuple[float, float]],
                 heard: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Captions in the script's own words (feedback 2026-10-26: "Chill have Skip" for "Chill, halve, skip the seeds"):
    the transcript gives the timings only. 2026-11-03 ("make sure text and voice match"): a line whose heard words
    didn't line up one for one used to be spread evenly over its span, so its captions drifted off the voice. Now
    each script word that was heard takes its heard time, and the words between (a number said as words, a word
    misheard) share the time between their heard neighbours. A line with nothing heard is spread over its span."""
    out: list[dict[str, Any]] = []
    for ln, (a, b) in zip(lines, spans):
        if ln["kind"] == "text":
            continue
        said = ln["text"].split()
        got = [w for w in heard if a - 0.05 <= w["start"] < b]
        if not got:
            out += [{**w, "start": round(w["start"] + a, 3), "end": round(w["end"] + a, 3)}
                    for w in voices.spread_words(" ".join(said), b - a)]
            continue
        out += align_words(said, got, a, b)
    return out


def align_words(said: list[str], got: list[dict[str, Any]], a: float, b: float) -> list[dict[str, Any]]:
    """The script's words on the heard words' timings (difflib on normalised words)."""
    import difflib
    from creative_intelligence.adstudio.voices import norm
    times: list[tuple[float, float] | None] = [None] * len(said)
    sm = difflib.SequenceMatcher(a=[norm(w) for w in said], b=[norm(w["word"]) for w in got], autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal" or (op == "replace" and i2 - i1 == j2 - j1):   # same words, or one-for-one mishearings
            for i, j in zip(range(i1, i2), range(j1, j2)):
                times[i] = (got[j]["start"], got[j]["end"])
        elif op == "replace":                  # "$39" heard as "thirty nine dollars": the run shares those words' time
            t0, t1, n = got[j1]["start"], got[j2 - 1]["end"], i2 - i1
            for k, i in enumerate(range(i1, i2)):
                times[i] = (t0 + (t1 - t0) * k / n, t0 + (t1 - t0) * (k + 1) / n)
    i = 0
    while i < len(said):                       # words never heard: the gap between their heard neighbours
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < len(said) and times[j] is None:
            j += 1
        t0 = times[i - 1][1] if i else max(a, got[0]["start"] - 0.3 * (j - i))
        t1 = times[j][0] if j < len(said) else min(b, max(t0, got[-1]["end"]) + 0.3 * (j - i))
        t1 = max(t1, t0 + 0.05 * (j - i))
        for k in range(i, j):
            times[k] = (t0 + (t1 - t0) * (k - i) / (j - i), t0 + (t1 - t0) * (k - i + 1) / (j - i))
        i = j
    return [{"word": w, "start": round(s, 3), "end": round(e, 3)} for w, (s, e) in zip(said, times)]


def _run_of(lines: list[dict[str, Any]], i: int) -> tuple[int, int]:
    """(where line i sits, how many) in its run of consecutive lines on the same shot type."""
    t, a, b = lines[i]["shot_type"], i, i
    while a > 0 and lines[a - 1]["shot_type"] == t:
        a -= 1
    while b + 1 < len(lines) and lines[b + 1]["shot_type"] == t:
        b += 1
    return i - a, b - a + 1


FACE_AT = (0.2, 0.5, 0.8)    # the scene shot's frames a character's lines start from, so each line looks different


def character_face(conn, video_id: int, vdir: Path, n: int = 1) -> str:
    """A still of the character in its scene shot (frame FACE_AT[n]): the picture a line is lip-synced on."""
    from creative_intelligence.adstudio import characters
    at = FACE_AT[n % len(FACE_AT)]
    out = vdir / f"styled_v{video_id}_character_face{n % len(FACE_AT)}.png"
    if out.exists():
        return str(out)
    pid = conn.execute("SELECT product_id FROM adstudio_videos WHERE id=?", (video_id,)).fetchone()[0]
    shot = shots.passing(conn, pid, characters.SCENE)
    if shot is None:
        raise RuntimeError("The character's scene shot isn't ready: make it in Video › Create › Characters.")
    return str(videotools.frame_at(shot["clip_path"], out, at))


def spoken_context(settings: dict[str, Any], ln: dict[str, Any]) -> tuple[str, str]:
    """The spoken lines just before and after `ln` in its script, for the voice to read it in flow."""
    said = [x for x in settings.get("lines") or [] if x.get("kind") != "text"]
    i = next((n for n, x in enumerate(said) if x is ln or x.get("text") == ln.get("text")), None)
    if i is None:
        return "", ""
    return (said[i - 1]["text"] if i > 0 else ""), (said[i + 1]["text"] if i + 1 < len(said) else "")


def make_piece(conn, video_id: int, ln: dict[str, Any], settings: dict[str, Any], vdir: Path,
               client: Any = None) -> dict[str, Any]:
    """{audio, secs, clip?}: a narrator line's voice-over, a presenter's clip (and its sound), or a silent text beat."""
    from creative_intelligence.adstudio import talking
    key = piece_key(ln, settings["voice"], settings.get("presenter_id"), settings.get("method", DEFAULT_TALKING))
    name = f"styled_v{video_id}_{key}"
    ctx = spoken_context(settings, ln)
    if ln["kind"] == "narrator":
        audio, _ = voices.speak(conn, ln["text"], settings["voice"], vdir / name, "video", video_id, client=client,
                                context=ctx, style=narrator_style(settings))
        wav = _wav(audio, vdir / f"{name}_48k.wav")       # a voice that comes back as .wav already has {name}.wav
        piece = {"audio": str(wav), "secs": videotools.probe_audio(wav)}
        if settings.get("character"):            # the fruit says it, lip-synced (feedback 2026-10-30)
            n = int(key, 16)
            move = talking.CHARACTER_MOVES[n % len(talking.CHARACTER_MOVES)]
            piece["clip"] = talking.lipsync(conn, character_face(conn, video_id, vdir, n), audio, f"{name}_lips", video_id,
                                            f"A cartoon fruit character talking to the camera, its mouth clearly forming "
                                            f"every word, lively expressions; while it talks it {move}.")
        return piece
    if ln["kind"] == "presenter":
        face, action = None, ""
        if settings.get("style") == TABLE:          # at the table: every line from the table scene, showing the fruit
            pid = conn.execute("SELECT product_id FROM adstudio_videos WHERE id=?", (video_id,)).fetchone()[0]
            face = talking.table_scene(conn, int(settings["presenter_id"]), pid)
            action = talking.TABLE_ACTIONS[int(key, 16) % len(talking.TABLE_ACTIONS)]
        elif settings.get("style") in PIP:          # Talking head: on green, keyed out over the shots
            face = talking.cutout_face(conn, int(settings["presenter_id"]))
        else:                                       # any other presenter line: at the counter with THIS fruit only
            # (2026-11-03: Wade's approved look has a crate of mangoes behind him, and a Fruit story opened on it)
            pid = conn.execute("SELECT product_id FROM adstudio_videos WHERE id=?", (video_id,)).fetchone()[0]
            face = talking.table_scene(conn, int(settings["presenter_id"]), pid)
        extra = {"face": face, "action": action} if face else {}
        clip, secs, _ = talking.make(conn, int(settings["presenter_id"]), ln["text"],
                                     scene_method(settings.get("method", DEFAULT_TALKING), settings.get("style")),
                                     name, video_id, client, context=ctx, **extra)
        wav = vdir / f"{name}.wav"
        try:
            _wav(clip, wav)
        except videotools.VideoToolError:
            _silence(secs, wav)
        return {"audio": str(wav), "secs": videotools.probe_audio(wav), "clip": clip,
                **({"cut": True} if settings.get("style") in PIP else {})}
    secs = round(max(STYLE_LOOKS.get(settings.get("style") or "", {}).get("min_s", 1.6),
                     len(ln["text"].split()) / TEXT_WORDS_PER_S + 0.4), 2)
    return {"audio": str(_silence(secs, vdir / f"{name}.wav")), "secs": secs}


def process(video_id: int, db: Callable[[], Any], client: Any = None, words_client: Any = None) -> dict[str, Any]:
    """The styled job: pieces (each made once), then every variant rendered from them."""
    from creative_intelligence import budget
    from creative_intelligence.storage import storage
    conn = db()
    try:
        v = dict(conn.execute("SELECT * FROM adstudio_videos WHERE id=?", (video_id,)).fetchone())
        st = json.loads(v["copy_json"])
        pid = v["product_id"]
        waiting = [w for w in st.get("waiting_shots") or [] if (
            conn.execute("SELECT 1 FROM adstudio_videos WHERE id=? AND status IN ('queued','running')",
                         (int(w[3:]),)).fetchone() if w.startswith("id:")             # an extra take (repeat_takes)
            else not shots.passing(conn, pid, *w.split("@", 1))
            and conn.execute("SELECT 1 FROM adstudio_videos WHERE product_id=? AND style='shot' AND shot_type=?"
                             " AND status IN ('queued','running')", (pid, w.split("@")[0])).fetchone())]
        if st.get("character"):              # the look and its scene shot may still be drawing (made alongside the script)
            from creative_intelligence.adstudio import characters
            prep = characters.prepare(conn, pid)
            if prep["step"] == "failed":
                raise RuntimeError(prep["text"])
            if prep["step"] == "wait" or conn.execute(
                    "SELECT 1 FROM adstudio_videos WHERE product_id=? AND style='shot' AND shot_type=? AND status IN"
                    " ('queued','running')", (pid, characters.SCENE)).fetchone():
                waiting.append(characters.SCENE)
        if waiting and datetime.utcnow() - datetime.fromisoformat(v["created_at"]) < story.WAIT_MAX:
            from creative_intelligence import jobs
            animate._set(conn, video_id, status="queued", step=f"Waiting for {len(waiting)} new shot(s)")
            jobs.enqueue(conn, "styled", {"video_id": video_id}, delay_s=story.WAIT_S)   # this job is still running
            return {"waiting": waiting}
        vdir = storage().area_dir("videos")
        animate.check_stop(conn, video_id)   # a Stop is never undone by a restarted job (2026-11-02)
        animate._set(conn, video_id, status="running", step="Making the pieces")
        combos = sorted({(x["hook"], x["length"]) for x in st["variants"]})
        pieces = st.get("pieces") or {}
        todo: dict[str, dict[str, Any]] = {}
        for h, ln in combos:
            for line in lines_for(st, h, ln):
                k = piece_key(line, st["voice"], st.get("presenter_id"), st.get("method", DEFAULT_TALKING))
                if k not in pieces:
                    todo.setdefault(k, line)
        animate.check_stop(conn, video_id)

        def one(item):                       # each on its own connection (SQLite stays on its thread)
            c = db()
            try:
                animate.check_stop(c, video_id)   # Stop holds every clip not paid for yet
                return item[0], make_piece(c, video_id, item[1], st, vdir, client)
            finally:
                c.close()
        # side by side (2026-10-30: six presenter clips one after another took 20 minutes). Every piece that is made is
        # kept, even when another fails (2026-11-02: one failed clip threw away the paid ones), and a failed one gets
        # one more round before the video fails; a retry of the job then makes only what is still missing.
        failed: dict[str, Exception] = {}
        for _ in range(2):
            with ThreadPoolExecutor(max_workers=PIECES_AT_ONCE) as pool:
                futures = {pool.submit(one, item): item for item in todo.items()}
                failed = {}
                for f in as_completed(futures):
                    k = futures[f][0]
                    try:
                        pieces[k] = f.result()[1]
                    except Exception as exc:          # noqa: BLE001  (one clip; the others go on)
                        failed[k] = exc
                        continue
                    st["pieces"] = pieces
                    with conn:
                        conn.execute("UPDATE adstudio_videos SET copy_json=? WHERE id=?",
                                     (json.dumps(st, default=str), video_id))
            todo = {k: todo[k] for k in failed}
            if not todo:
                break
            animate.check_stop(conn, video_id)
        if any(isinstance(e, animate.Stopped) for e in failed.values()):
            raise animate.Stopped()
        if over := next((e for e in failed.values() if isinstance(e, budget.BudgetExceeded)), None):
            # today's video cap, not a broken clip (2026-11-02): pause, keep what was made, carry on after midnight
            from creative_intelligence import jobs
            animate._set(conn, video_id, status="queued", step="Paused: today's video limit is reached. It continues "
                         "after 00:00 London, or sooner if the limit is raised in Admin and it is retried.")
            jobs.enqueue(conn, "styled", {"video_id": video_id}, delay_s=budget.seconds_to_reset())
            return {"paused": str(over)}
        if failed:
            exc = next(iter(failed.values()))
            raise RuntimeError(f"{len(failed)} of the voice lines or clips failed twice "
                               f"({type(exc).__name__}: {str(exc)[:160]}); retry to make only those.") from exc
        animate.check_stop(conn, video_id)
        kinds = {piece_key(x, st["voice"], st.get("presenter_id"), st.get("method", DEFAULT_TALKING)): x["kind"]
                 for h, ln in combos for x in lines_for(st, h, ln)}
        untimed = [k for k, kind in kinds.items() if kind != "text" and "words" not in pieces[k]]
        if untimed:                          # one timing call for every voiced piece, not one per hook and length
            animate._set(conn, video_id, step="Timing the captions")
            with tempfile.TemporaryDirectory() as td:
                joined = _concat([Path(pieces[k]["audio"]) for k in untimed], Path(td) / "all.wav")
                try:
                    heard = voices.word_timings(conn, joined, "video", video_id, client=words_client or client)
                except Exception as exc:
                    heard = None
                    st["timing_note"] = f"captions timed roughly ({str(exc)[:60]})"
            t0 = 0.0
            for k in untimed:
                a, b = t0, t0 + pieces[k]["secs"]
                pieces[k]["words"] = [{**w, "start": round(w["start"] - a, 3), "end": round(w["end"] - a, 3)}
                                      for w in heard or [] if a - 0.05 <= w["start"] < b]
                t0 = b
            st["pieces"] = pieces
            with conn:
                conn.execute("UPDATE adstudio_videos SET copy_json=? WHERE id=?", (json.dumps(st, default=str), video_id))
        look = STYLE_LOOKS.get(st.get("style") or "", {})
        chosen = st.get("music") or st.get("music_used") or pick_track(
            conn, pid, st.get("style") or "", any(x["kind"] != "text" for x in st.get("lines") or []))
        track = story.pick_music(chosen)
        if track and not st.get("music"):
            st["music_used"] = track.name
            with conn:
                conn.execute("UPDATE adstudio_videos SET copy_json=? WHERE id=?", (json.dumps(st, default=str), video_id))
        renders, notes, used = [], [], set()
        if st.get("timing_note"):
            notes.append(st["timing_note"])
        shots_by_type: dict[str, dict[str, Any]] = {}
        for h, ln in combos:
            lines = lines_for(st, h, ln)
            keys = [piece_key(x, st["voice"], st.get("presenter_id"), st.get("method", DEFAULT_TALKING)) for x in lines]
            with tempfile.TemporaryDirectory() as td:
                voice = _concat([Path(pieces[k]["audio"]) for k in keys], Path(td) / "voice.wav")
                total = videotools.probe_audio(voice)
                has_voice = any(x["kind"] != "text" for x in lines)
                spans, t0 = [], 0.0
                for k in keys:
                    spans.append((round(t0, 3), round(t0 + pieces[k]["secs"], 3)))
                    t0 += pieces[k]["secs"]
                words: list[dict[str, Any]] = []
                if has_voice:                # each piece's own timings, placed where the piece plays
                    words = script_words(lines, spans, [{**w, "start": round(w["start"] + a, 3), "end": round(w["end"] + a, 3)}
                                                        for k, (a, b) in zip(keys, spans)
                                                        for w in pieces[k].get("words") or []])
                clips, texts, played = [], [], {}
                for li, (x, k, (a, b)) in enumerate(zip(lines, keys, spans)):
                    if (x["kind"] == "presenter" and st.get("style") not in PIP) or (
                            st.get("character") and pieces[k].get("clip")):           # the character talking
                        clips.append({"src": pieces[k]["clip"], "label": "presenter",       # the table keeps the fruit in
                                      **({"character": True} if st.get("character") else {}),
                                      "box": [0.1, 0.1, 0.9, 0.95] if st.get("style") == TABLE or st.get("character")
                                      else [0.15, 0.05, 0.85, 0.7]})
                        continue
                    lpid = int(x.get("product_id") or pid)
                    if (lpid, x["shot_type"]) not in shots_by_type:   # the video's own take first (batch looks)
                        own = (st.get("own_shots") or {}) if lpid == pid else {}
                        shots_by_type[(lpid, x["shot_type"])] = (
                            (shots.passing(conn, lpid, x["shot_type"], own[x["shot_type"]]) if x["shot_type"] in own
                             else None)
                            or (shots.best_take(conn, lpid, x["shot_type"], own[x["shot_type"]])     # the model test
                                if st.get("engine") and x["shot_type"] in own else None)
                            or story._shot_for(conn, lpid, x["shot_type"], used)
                            or next((o for t, tag in own.items() if (o := shots.passing(conn, lpid, t, tag))), None))
                    shot = shots_by_type[(lpid, x["shot_type"])]
                    if st.get("engine") and not (shot and "+engine:" in (shot.get("prompt_version") or "")):
                        # the model test: one model for the whole video, never a stand-in from another (2026-11-04)
                        raise RuntimeError(f"The {x['shot_type'].replace('_', ' ')} take on {st['engine']} isn't ready, "
                                           "so this video can't be made on that model alone.")
                    stages = (prompts.shot_cards().get(x["shot_type"]) or {}).get("timelapse_stages")
                    if shot and shot["id"] in used and shot["shot_type"] == x["shot_type"] and not stages:
                        # the type again: another passing take of it, so the same clip doesn't play twice
                        shot = next((o for o in shots.passing_takes(conn, lpid, x["shot_type"]) if o["id"] not in used),
                                    shot)
                    defining = st.get("style") in (prompts.shot_cards().get(x["shot_type"]) or {}).get("styles", [])
                    if defining and (shot is None or shot["shot_type"] != x["shot_type"]):
                        # another of the format's own shots that passed (clay for clay, 2026-10-27) before giving up
                        shot = next((o for t, cc in prompts.shot_cards().items()
                                     if st.get("style") in (cc.get("styles") or []) and cc.get("cast")
                                     and (o := shots.passing(conn, lpid, t))), None)
                    if defining and shot is None:
                        # the format's own shot (sensory hands, the ripening time-lapse, clay, the character) is
                        # never stood in for: a generic shot made every text format look the same (2026-10-26)
                        c = shots.card(x["shot_type"]) or {}
                        raise RuntimeError(f"The {c.get('label', x['shot_type'])} shot isn't ready (not made or it "
                                           "didn't pass QA), and this format needs it: make it again in Video › Shots.")
                    if shot is None:
                        raise RuntimeError("No passing shots in the library for this product.")
                    used.add(shot["id"])
                    if stages:                                  # the ripening time-lapse: one take, one part per line
                        run = _run_of(lines, li)
                        clips.append({"src": shot["clip_path"], "shot_id": shot["id"], "label": shot["shot_type"],
                                      "stage": list(run), "box": json.loads(shot["subject_box"]) if shot.get("subject_box")
                                      else None})
                        if x["kind"] == "text":
                            texts.append({"template": look.get("text", "kinetic"), "headline": x["text"], "start": a, "end": b})
                        continue
                    trim = played.get(shot["id"], 0.0)          # the same shot on the next line plays on, not again
                    played[shot["id"]] = trim + (b - a)
                    clip = {"src": shot["clip_path"], "shot_id": shot["id"], "label": shot["shot_type"],
                            **({"trim_in": round(trim, 3)} if trim else {}),
                            "box": json.loads(shot["subject_box"]) if shot.get("subject_box") else None}
                    sm = (prompts.shot_cards().get(shot["shot_type"]) or {}).get("stop_motion_fps")
                    if sm:
                        clip["stop_motion_fps"] = sm                    # claymation: held frames
                    if x.get("compare"):                                # Us vs supermarket: split screen
                        other = shots.passing(conn, pid, "supermarket_fruit")
                        if other is None:
                            raise RuntimeError("The Supermarket fruit shot is missing or retired: make it in Video › Shots.")
                        used.add(other["id"])
                        clip.update(split=other["clip_path"], split_labels=["Supermarket", _brand_name()])
                    if x["kind"] == "presenter":                        # Talking head: the head over the shot
                        clip["pip"] = pieces[k]["clip"]
                        clip["pip_cut"] = bool(pieces[k].get("cut"))        # older pieces: the circle
                    clips.append(clip)
                    if x["kind"] == "text":
                        texts.append({"template": look.get("text", "kinetic"), "headline": x["text"], "start": a, "end": b})
                if st.get("comment") and clips:            # Comment reply: the real comment, name hidden
                    clips[0]["sticker"] = st["comment"]["text"]
                    clips[0]["sticker_label"] = ("People often ask" if st["comment"].get("kind") == "question"
                                                 else "A customer asked")
                if look.get("steps") and len(lines) > 2:   # How to eat: "Step 1…" chips on the steps, captions stay
                    for n, (a, b) in enumerate(spans[1:-1], 1):
                        texts.append({"template": "day", "headline": f"Step {n}", "start": a, "end": b,
                                      "keep_captions": True})
                # no hook headline card over a spoken first line (feedback 2026-11-09, "text is not matching voice
                # over"): it hid the captions for 3 s and said other words than the voice; the captions say the hook
                # a presenter on screen anywhere: hard cuts (2026-11-04, Fruit story: dissolves ghosted the farmer)
                cut = 0.0 if any(c.get("label") == "presenter" for c in clips) else look.get("transition",
                                                                                       timeline.TRANSITION)
                pic = timeline.picture_plan(spans, clips, cut)
                base = {"version": 1, "fps": 30, "transition": cut, "picture": pic,
                        "voice": {"src": str(voice), "words": words} if has_voice else {"src": str(voice), "words": []},
                        "text": texts, "sfx": [], "music": {"src": str(track), "gain_db": -16 if has_voice else -8} if track else None,
                        "end_card": {"dur": timeline.END_CARD_S, "image": story._hero(conn, pid), "headline": story.v_name(conn, pid),
                                     "cta": st["cta"]}}
                animate.check_stop(conn, video_id)
                animate._set(conn, video_id, step=f"Rendering hook {h + 1} · {'short' if ln == 'short' else 'full'}")
                jobs_here = []
                for var in [x for x in st["variants"] if (x["hook"], x["length"]) == (h, ln)]:
                    label = f"Hook {h + 1} · {'short' if ln == 'short' else 'full'} · {var['aspect']} · {var['captions']} captions"
                    cap = look.get("captions", "bold") if var["captions"] == "bold" else var["captions"]   # bold = the
                    tl = {**base, "aspect": var["aspect"], "captions": {"max_words": look.get("max_words", 3), "style": cap,   # format's own
                                                                      **({"place": "top"} if st.get("style") in CAPTIONS_TOP else {})}}
                    out = vdir / f"styled_v{video_id}_h{h}_{ln}_{var['aspect'].replace(':', 'x')}_{var['captions']}.mp4"
                    jobs_here.append((var, label, tl, out))

                def draw(j):                 # the sizes of one cut side by side (timeline.render caps it app-wide)
                    res = timeline.render(j[2], j[3])
                    return res, videotools.frame_at(j[3], j[3].with_suffix(".jpg"), 1.0, 540)
                with ThreadPoolExecutor(max_workers=max(1, len(jobs_here))) as pool:
                    drawn = list(pool.map(draw, jobs_here))
                for (var, label, tl, out), (res, poster) in zip(jobs_here, drawn):
                    with conn:
                        rid = conn.execute(
                            "INSERT INTO adstudio_video_renders (video_id, product_id, aspect, template, headline, cta, output_path,"
                            " poster_path, status, note, duration_s, created_at) VALUES (?,?,?, ?, ?,?,?,?, 'done', ?, ?, ?)",
                            (video_id, pid, var["aspect"], var["captions"], st["hooks"][h] if st.get("hooks") else st["hook_headline"],
                             st["cta"], str(out), str(poster), f"{label} · {res['duration']:.0f} s", round(res["duration"], 1),
                             datetime.utcnow().isoformat(timespec="seconds"))).lastrowid
                    prelaunch.record_video(conn, rid, res.get("measured"), res.get("export"))
                    renders.append(rid)
                    if not v.get("timeline_json"):
                        v["timeline_json"] = timeline.dumps(tl)
                        with conn:
                            conn.execute("UPDATE adstudio_videos SET timeline_json=?, clip_path=?, duration_s=? WHERE id=?",
                                         (v["timeline_json"], str(out), round(res["duration"]), video_id))
        for sid in used:
            shots.use(conn, sid)
        animate._set(conn, video_id, step="Quality check")
        try:
            from creative_intelligence.adstudio import video_qa
            from creative_intelligence.webapp.common import _primary_photo_uris, _product_dict
            first = conn.execute("SELECT clip_path FROM adstudio_videos WHERE id=?", (video_id,)).fetchone()[0]
            video_qa.score(conn, video_id, first, _primary_photo_uris(conn, pid),
                           _product_dict(conn, pid) or {}, is_montage=True,
                           note=video_qa.note_for(st.get("style") or ""))
        except Exception:
            notes.append("not QA-scored")
        spent = budget.spent_on_video(conn, video_id)
        animate._set(conn, video_id, status="done", step="", finished_at=datetime.utcnow().isoformat(timespec="seconds"),
                     message=f"{len(renders)} variants · {len(pieces)} pieces · ${spent:.2f} new" + (f" · {'; '.join(notes)}" if notes else ""))
        return {"renders": renders, "pieces": len(pieces), "spent_usd": spent}
    except animate.Stopped:
        animate._set(conn, video_id, status="failed", step="", message="Stopped.")
        return {"stopped": True}
    except Exception as exc:
        animate._set(conn, video_id, status="failed", step="", message=str(exc)[:300])
        raise
    finally:
        conn.close()


def _brand_name() -> str:
    from creative_intelligence import brand
    return brand.name()
