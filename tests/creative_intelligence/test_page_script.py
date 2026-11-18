"""The studio page is one script: a name declared twice at the top level stops all of it (nothing on the page
responds). Catch that before deploy."""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[2] / "creative_intelligence" / "webapp" / "templates"
TOP = re.compile(r"^(?:const|let|var|async function|function)\s+([A-Za-z_$][\w$]*)", re.M)


def _names(text: str) -> Counter:
    js = "\n".join(re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", text, re.S))
    names = []
    for m in TOP.finditer(js):
        names.append(m.group(1))
        rest = js[m.end():js.find("\n", m.end())]
        if m.group(0).startswith(("const", "let", "var")):        # const a=…, b=… on one line
            names += re.findall(r",\s*([A-Za-z_$][\w$]*)\s*=(?!=)", re.sub(r"\([^()]*\)|\{[^{}]*\}|\[[^\[\]]*\]|'[^']*'|`[^`]*`", "", rest))
    return Counter(names)


def test_no_top_level_name_declared_twice():
    for page in ("adstudio.html", "admin.html"):
        dup = [n for n, k in _names((TEMPLATES / page).read_text()).items() if k > 1]
        assert not dup, f"{page} declares {dup} more than once"
