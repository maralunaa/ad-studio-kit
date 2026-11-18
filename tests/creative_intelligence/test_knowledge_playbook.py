"""The Meta Creative Playbook is the app's knowledge base: it is in the repo, says where it comes from and when it
was exported, and keeps the sections the knowledge files are derived from."""
from __future__ import annotations

import re
from pathlib import Path

PLAYBOOK = Path(__file__).resolve().parents[2] / "knowledge" / "meta-creative-playbook.md"


def test_playbook_is_stored_with_its_source_and_sections():
    text = PLAYBOOK.read_text()
    assert "Source of truth:" in text and re.search(r"Exported: \d{4}-\d{2}-\d{2} · doc revision \d+", text)
    for section in ("Hooks", "Static ads", "Video ads", "Copy, CTA and compliance", "Metrics and diagnosis",
                    "Pre-launch checklists", "Styles to test next"):
        assert f"\n## {section}" in text, section
    assert "never claim where the fruit comes from" in text and "invented reviews are never used" in text
