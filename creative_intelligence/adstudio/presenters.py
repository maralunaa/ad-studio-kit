"""
AI presenters (Video Studio V5): brand narrators with a fixed look and a fixed house voice.

  Rules (the brief): presenters are brand narrators, never posed as real customers or the brand's real staff;
  every line they say comes from approved facts; every video with one is flagged ai_person.

  presets         one presenter per house voice (name, role, voice, look, framing: portrait or phone
                  selfie), editable before approving
  make_looks()    queue portrait options (Nano Banana Pro, 9:16, chest-up, facing the camera, mouth closed:
                  what lip-sync needs) — the "presenter_look" job makes them
  approve_look()  the chosen portrait becomes the presenter's face; the presenter is then approved
  make_sheet()    the character sheet from the approved portrait (same face as the reference): angles
                  and expressions for cutaways and lip-sync
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from creative_intelligence import brand as _brand, prompts

# The brand's presenters (brand.yaml › presenters): name, role, voice, framing, look, setting.
PRESETS = [dict(p) for p in _brand.get("presenters", [])]
FRAMING = {
    "portrait": "Framing: chest-up, centred, facing the camera, eyes to the lens, a relaxed natural smile with the mouth "
                "closed, shoulders square, head and hands fully in frame with space above the head. Soft, even, natural "
                "window light on the face, no hard shadows.",
    "selfie": "Framing: a handheld front-camera selfie at arm's length, a slightly low angle, the near arm reaching "
              "toward the lens at the frame edge; head and shoulders, eyes to the lens, a relaxed natural half-smile with "
              "the mouth closed. Natural room light; feels like a real phone video, not a studio shot.",
}
SHEET = [("three_quarter_left", "Three-quarter view, turned slightly to her or his right, still looking at the camera."),
         ("three_quarter_right", "Three-quarter view, turned slightly to her or his left, still looking at the camera."),
         ("laughing", "A natural, warm laugh, eyes creased, mouth open."),
         ("tasting", "Holding the product up near the face, about to taste or try it, eyes to the camera."),
         ("explaining", "Mid-sentence, explaining something with one open hand raised at chest height, mouth slightly open.")]
LOOKS_PER_RUN = 2


def ensure_presets(conn) -> None:
    """One presenter per house voice: presets missing by name are added; edited ones are left alone."""
    have = {r[0] for r in conn.execute("SELECT name FROM presenters")}
    now = datetime.utcnow().isoformat(timespec="seconds")
    with conn:
        for p in PRESETS:
            if p["name"] not in have:
                conn.execute("INSERT INTO presenters (name, role, voice, look, framing, created_at) VALUES (?,?,?,?,?,?)",
                             (p["name"], p["role"], p["voice"], p["look"] + " || " + p["setting"], p["framing"], now))


def split_look(look: str) -> tuple[str, str]:
    """The stored look is 'person || setting' (setting optional)."""
    person, _, setting = (look or "").partition("||")
    return person.strip(), (setting.strip() or "a warm, softly lit kitchen with exotic fruit")


def listing(conn) -> list[dict[str, Any]]:
    ensure_presets(conn)
    out = []
    order = {p["name"]: i for i, p in enumerate(PRESETS)}
    for p in sorted(conn.execute("SELECT * FROM presenters"), key=lambda r: (order.get(r["name"], 99), r["id"])):
        d = dict(p)
        d["person"], d["setting"] = split_look(d["look"])
        d["looks"] = [dict(r) for r in conn.execute(
            "SELECT id, kind, label, status, error, created_at, path IS NOT NULL AS has_image FROM presenter_looks"
            " WHERE presenter_id=? ORDER BY id DESC", (d["id"],))]
        out.append(d)
    return out


def estimate(conn, n: int) -> float:
    from creative_intelligence.adstudio.costs import price
    from creative_intelligence.providers import model
    return round(price(conn, model("image")) * n, 2)


def _queue(conn, pid: int, items: list[tuple[str, str, str]]) -> list[int]:
    """items: (kind, label, prompt). One job makes them all."""
    from creative_intelligence import budget, jobs
    budget.check(conn, "generation", estimate(conn, len(items)))
    now = datetime.utcnow().isoformat(timespec="seconds")
    ids = []
    with conn:
        for kind, label, prompt in items:
            ids.append(conn.execute("INSERT INTO presenter_looks (presenter_id, kind, label, prompt, created_at) VALUES"
                                    " (?,?,?,?,?)", (pid, kind, label, prompt, now)).lastrowid)
    jobs.enqueue(conn, "presenter_look", {"look_ids": ids})
    return ids


def make_looks(conn, pid: int, n: int = LOOKS_PER_RUN) -> list[int]:
    p = conn.execute("SELECT * FROM presenters WHERE id=?", (pid,)).fetchone()
    if p is None:
        raise ValueError("presenter not found")
    person, setting = split_look(p["look"])
    prompt = prompts.text("presenter-look", look=person, setting=setting, framing=FRAMING.get(p["framing"], FRAMING["portrait"]))
    return _queue(conn, pid, [("portrait", f"Option {i + 1}", prompt) for i in range(max(1, min(n, 4)))])


def _realism() -> str:
    from creative_intelligence.adstudio.shots import realism
    return realism("person")


def make_sheet(conn, pid: int) -> list[int]:
    p = conn.execute("SELECT * FROM presenters WHERE id=?", (pid,)).fetchone()
    if p is None or not p["approved_look_id"]:
        raise ValueError("Approve a look first: the sheet keeps that face.")
    return _queue(conn, pid, [("angle", key, prompts.text("presenter-angle", change=change, realism=_realism())) for key, change in SHEET])


def process(look_ids: list[int], db: Callable[[], Any], generate: Callable[..., str] | None = None) -> dict[str, Any]:
    """The presenter_look job: each look made in turn; a failure is recorded and the rest still run."""
    from creative_intelligence.adstudio import costs
    from creative_intelligence.adstudio.store import to_data_uri
    from creative_intelligence.providers import model, replicate
    from creative_intelligence.storage import storage
    generate = generate or replicate.generate_image
    conn = db()
    made = 0
    try:
        for lid in look_ids:
            row = conn.execute("SELECT * FROM presenter_looks WHERE id=?", (lid,)).fetchone()
            if row is None or row["status"] != "queued":
                continue
            refs = []
            if row["kind"] == "angle":
                face = conn.execute("SELECT l.path FROM presenters p JOIN presenter_looks l ON l.id=p.approved_look_id"
                                    " WHERE p.id=?", (row["presenter_id"],)).fetchone()
                refs = [to_data_uri(face[0])] if face and face[0] else []
            try:
                url = generate(row["prompt"], refs, "9:16")
                path = storage().put_url("presenters", f"presenter{row['presenter_id']}_look{lid}.png", url)
                costs.log_image(conn, model("image"), "presenter", lid)
                with conn:
                    conn.execute("UPDATE presenter_looks SET path=?, status='done' WHERE id=?", (path, lid))
                made += 1
            except Exception as exc:
                with conn:
                    conn.execute("UPDATE presenter_looks SET status='failed', error=? WHERE id=?", (str(exc)[:300], lid))
        return {"made": made}
    finally:
        conn.close()


def set_look_status(conn, lid: int, status: str) -> str:
    """approved: this portrait becomes the presenter's face (the presenter is approved); rejected: hidden."""
    if status not in ("approved", "rejected", "done"):
        return "bad status"
    row = conn.execute("SELECT * FROM presenter_looks WHERE id=?", (lid,)).fetchone()
    if row is None or not row["path"]:
        return "look not found"
    with conn:
        if status == "approved" and row["kind"] == "portrait":
            conn.execute("UPDATE presenter_looks SET status='done' WHERE presenter_id=? AND kind='portrait' AND status='approved'",
                         (row["presenter_id"],))
            conn.execute("UPDATE presenters SET approved_look_id=?, status='approved' WHERE id=?", (lid, row["presenter_id"]))
        conn.execute("UPDATE presenter_looks SET status=? WHERE id=?", (status, lid))
        if status != "approved" and row["kind"] == "portrait":
            conn.execute("UPDATE presenters SET approved_look_id=NULL, status='draft' WHERE id=? AND approved_look_id=?",
                         (row["presenter_id"], lid))
    return ""


def update(conn, pid: int, d: dict[str, Any]) -> str:
    """Edit name, role, voice or look (person and setting). A changed look sends the presenter back to draft."""
    from creative_intelligence.adstudio import voices
    p = conn.execute("SELECT * FROM presenters WHERE id=?", (pid,)).fetchone()
    if p is None:
        return "presenter not found"
    if d.get("voice"):
        try:
            voices.voice(d["voice"])
        except ValueError as exc:
            return str(exc)
    person, setting = split_look(p["look"])
    look = (d.get("person", person) or "").strip() + " || " + (d.get("setting", setting) or "").strip()
    changed = look != p["look"]
    with conn:
        conn.execute("UPDATE presenters SET name=?, role=?, voice=?, look=?, status=CASE WHEN ? THEN 'draft' ELSE status END,"
                     " approved_look_id=CASE WHEN ? THEN NULL ELSE approved_look_id END, brief=?, memories=? WHERE id=?",
                     ((d.get("name") or p["name"]).strip(), (d.get("role") or p["role"]).strip(), d.get("voice") or p["voice"],
                      look, int(changed), int(changed), (d["brief"] if "brief" in d else p["brief"] or "").strip(),
                      int(bool(d["memories"])) if "memories" in d else p["memories"], pid))
    return ""


def tells_memories(conn, presenter_id: int | None) -> bool:
    """The owner lets this persona tell first-person memories (2026-11-04: the Grandparent)."""
    if not presenter_id:
        return False
    r = conn.execute("SELECT memories FROM presenters WHERE id=?", (int(presenter_id),)).fetchone()
    return bool(r and r["memories"])


def brief_block(conn, presenter_id: int | None) -> str:
    """The presenter's persona for the script writer ('' when there is none)."""
    if not presenter_id:
        return ""
    p = conn.execute("SELECT name, role, brief, memories FROM presenters WHERE id=?", (int(presenter_id),)).fetchone()
    if p is None or not (p["brief"] or "").strip():
        return ""
    return (f"\nTHE PRESENTER ON CAMERA: {p['name']}, {p['role']}. {p['brief'].strip()}\nWrite every presenter line the "
            "way this person talks, leaning on the topics they cover when the format allows. Their persona never adds a "
            "claim: facts only from the approved list, never a medical claim, and they are never presented as a real "
            "customer or a named employee of the brand.\n"
            + ("They may tell first-person memories and feelings (the owner allows it for this persona); a fact about "
               "the fruit inside a memory still comes only from the approved list.\n" if p["memories"] else ""))
