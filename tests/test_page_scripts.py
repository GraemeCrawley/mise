"""Inline <script> blocks live inside Python f-strings, where an escape
like '\\n' or "can\\'t" silently turns into broken JavaScript that kills
the whole page's behaviour. Syntax-check every inline script with node."""

import re
import shutil
import subprocess

import pytest

from mise.models import Household

_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_inline_scripts_parse(client, session, monkeypatch, tmp_path):
    session.add(Household(id=1, name="Test"))
    session.commit()
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    rid = client.post("/recipes", json={
        "name_english": "Soup", "name_original": "Soup", "servings": 2,
        "ingredients_blob": "1 onion, diced\n2 cans beans", "instructions_blob": "1. Add the onion\n2. Add beans",
        "llm_verify": "off",
    }).json()["id"]
    pages = ["/app/", f"/app/recipe/{rid}", f"/app/recipe/{rid}/draft", "/app/plan", "/app/shop", "/app/shop?view=recipe"]
    for url in pages:
        html = client.get(url).text
        for n, js in enumerate(_SCRIPT_RE.findall(html)):
            f = tmp_path / f"s{n}.js"
            f.write_text(js)
            r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, f"{url} script #{n}: {r.stderr[:400]}"
