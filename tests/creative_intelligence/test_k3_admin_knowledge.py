"""K3: Admin → Knowledge shows the playbook version, the derived files and what uses each value; uploading a
modified playbook gives a section diff and Claude's proposed updates (faked here), and nothing changes until
each is approved. Approved knowledge values go live through an overlay on the volume and can be undone."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from creative_intelligence import knowledge
from creative_intelligence.adstudio import knowledge_updates as KU
from creative_intelligence.db import get_connection, init_db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "data" / "t.db"))
    init_db(tmp_path / "data" / "t.db")
    c = get_connection(tmp_path / "data" / "t.db")
    yield c
    c.close()


def _db(tmp_path):
    return lambda: get_connection(tmp_path / "data" / "t.db")


def _edited() -> str:
    """The playbook with three edits: a revision bump, a CTA rule changed, a section heading renamed."""
    md = knowledge.playbook()
    md = md.replace("doc revision 16", "doc revision 17")
    md = md.replace("**One or two emoji at most.**", "**One emoji at most.**")
    return md.replace("**Field rules**", "**Field lengths**")


class _Claude:
    def __init__(self, proposals):
        self.proposals, self.sent = proposals, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=json.dumps({"proposals": self.proposals}))],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0))


PROPOSALS = [
    {"target": "knowledge", "key": "rules.emoji.max_per_ad", "after": "1", "section": "Copy, CTA and compliance",
     "reason": "One emoji at most now."},
    {"target": "knowledge", "key": "rules.copy_fields.source", "after": "\"Copy, CTA and compliance › Field lengths\"",
     "section": "Copy, CTA and compliance", "reason": "The heading was renamed."},
    {"target": "knowledge", "key": "rules.no_such_value", "after": "3", "section": "x", "reason": "invented"},
    {"target": "prompt", "key": "ai-ad-copy-rules", "after": "Emoji: one at most.", "section": "Copy, CTA and compliance",
     "reason": "Match the new emoji rule."},
]


def _ready(conn, tmp_path, monkeypatch):
    monkeypatch.setattr("creative_intelligence.adstudio.costs.log_tokens", lambda *a, **k: 0.02)
    uid = KU.upload(conn, _edited())
    fake = _Claude(PROPOSALS)
    KU.propose(_db(tmp_path), uid, client=fake)
    return uid, fake


def test_overview_lists_every_value_with_its_source_and_users(conn):
    o = KU.overview()
    assert o["playbook"] == {"exported": "2026-10-20", "revision": 16, "adopted": False} and o["errors"] == []
    vals = {v["key"]: v for f in o["files"] for v in f["values"]}
    assert vals["hooks.types"]["prompts"] == ["hook-university-system"]
    assert "rubric: safe_zone" in vals["rules.safe_zones"]["code"]
    assert vals["rules.on_image_text"]["source"] == "Static ads › Design rules"
    assert vals["styles.fruit_story"]["prompts"] == ["style-fruit-story"] and "styles.catalog()" in vals["styles.fruit_story"]["code"]
    for k in knowledge.CODE_USES:                                        # the registry names real values
        assert knowledge.get(k) if k != "styles" else knowledge.load("styles")


def test_upload_diffs_by_section_and_never_changes_anything(conn, tmp_path, monkeypatch):
    queued = []
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda c, kind, p, **k: queued.append((kind, p)))
    before = knowledge.version()
    uid = KU.upload(conn, _edited())
    u = KU.get(conn, uid)
    assert u["revision"] == 17 and u["status"] == "queued" and queued == [("knowledge_update", {"update_id": uid})]
    changed = {c["section"]: c for c in u["diff"]}
    assert set(changed) == {"Copy, CTA and compliance"} and "+- **One emoji at most.**" in changed["Copy, CTA and compliance"]["diff"]
    assert any(a["where"] == "copy_fields" for a in u["affected"])
    assert knowledge.version() == before and knowledge.playbook_info()["revision"] == 16    # stored, not live
    with pytest.raises(ValueError):
        KU.upload(conn, "just some text")


def test_claude_proposes_one_item_per_change(conn, tmp_path, monkeypatch):
    uid, fake = _ready(conn, tmp_path, monkeypatch)
    u = KU.get(conn, uid)
    assert u["status"] == "ready" and u["cost_usd"] == pytest.approx(0.02)
    assert [p["target"] for p in u["proposals"]] == ["playbook", "knowledge", "knowledge", "prompt"]   # invented key dropped
    assert u["proposals"][1]["before"] == "2" and all(p["status"] == "pending" for p in u["proposals"])
    ask = fake.sent[0]["messages"][0]["content"]
    assert "One emoji at most" in ask and "rules.emoji" in ask and "never propose invented reviews" in fake.sent[0]["system"]


def test_nothing_changes_until_approved_and_related_items_move_together(conn, tmp_path, monkeypatch):
    uid, _ = _ready(conn, tmp_path, monkeypatch)
    v0 = knowledge.version()
    assert knowledge.load("rules")["emoji"]["max_per_ad"] == 2
    # adopting the export alone would orphan rules.copy_fields (its heading was renamed): refused, nothing changes
    r = KU.decide(conn, uid, [0], True)
    assert "Not applied" in r["error"] and "Field rules" in r["error"]
    assert knowledge.playbook_info()["revision"] == 16 and knowledge.version() == v0
    # together they pass the checks and go live at once
    r = KU.decide(conn, uid, [0, 1, 2], True)
    assert r["ok"] and [p["status"] for p in r["update"]["proposals"]] == ["live", "live", "live", "pending"]
    assert knowledge.load("rules")["emoji"]["max_per_ad"] == 1 and knowledge.playbook_info()["revision"] == 17
    assert knowledge.errors() == [] and knowledge.version().startswith("pb17-") and knowledge.version() != v0
    # wording for prompts is approved for the PR, not applied
    r = KU.decide(conn, uid, [3], True)
    assert r["update"]["proposals"][3]["status"] == "approved"
    bundle = KU.export(conn, uid)
    assert "doc revision 17" in bundle["playbook_md"] and len(bundle["live_overrides"]) == 2
    assert [p["key"] for p in bundle["approved_for_pr"]] == ["ai-ad-copy-rules"]
    assert "Nothing left" in KU.decide(conn, uid, [1], False)["error"]


def test_rejected_items_change_nothing_and_approved_values_can_be_undone(conn, tmp_path, monkeypatch):
    uid, _ = _ready(conn, tmp_path, monkeypatch)
    KU.decide(conn, uid, [1], False)
    assert knowledge.load("rules")["emoji"]["max_per_ad"] == 2 and KU.get(conn, uid)["proposals"][1]["status"] == "rejected"
    knowledge.save_overrides([{"file": "rules", "key": "rules.emoji.max_per_ad", "value": 1}])
    assert knowledge.load("rules")["emoji"]["max_per_ad"] == 1
    assert KU.remove_override("rules.emoji.max_per_ad") and knowledge.load("rules")["emoji"]["max_per_ad"] == 2


def test_an_override_that_breaks_validation_is_refused(conn, tmp_path, monkeypatch):
    monkeypatch.setattr("creative_intelligence.adstudio.costs.log_tokens", lambda *a, **k: 0.0)
    uid = KU.upload(conn, _edited())
    KU.propose(_db(tmp_path), uid, client=_Claude([{"target": "knowledge", "key": "rules.emoji.share_of_ads", "after": "3",
                                                    "section": "Copy, CTA and compliance", "reason": "bad"}]))
    r = KU.decide(conn, uid, [1], True)
    assert "share_of_ads must be a fraction" in r["error"] and knowledge.overrides() == []


def test_admin_routes(conn, tmp_path, monkeypatch):
    import creative_intelligence.webapp.app as app_module
    monkeypatch.setattr("creative_intelligence.webapp.common.db", _db(tmp_path))
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda *a, **k: None)
    c = app_module.app.test_client()
    k = c.get("/api/ops/knowledge").get_json()
    assert k["playbook"]["revision"] == 16 and len(k["files"]) == 6 and k["updates"] == []
    u = c.post("/api/ops/knowledge/upload", json={"markdown": _edited()}).get_json()
    assert u["revision"] == 17 and u["diff"]
    assert c.post("/api/ops/knowledge/upload", json={"markdown": "nope"}).status_code == 400
    assert c.post(f"/api/ops/knowledge/{u['id']}/decide", json={"items": [0], "approve": True}).status_code == 400
    assert c.get(f"/api/ops/knowledge/{u['id']}/export").get_json()["playbook_md"] is None
    assert c.get("/healthz").get_json()["checks"]["knowledge"] == "ok"
    assert b"loadKnowledge" in c.get("/admin").data
