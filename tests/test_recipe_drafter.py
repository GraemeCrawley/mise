"""recipe_drafter — JSON-LD extraction tests. Pure functions, no DB."""

from __future__ import annotations

import json

from mise.services.recipe_drafter import (
    DraftRecipe,
    _iso_minutes,
    _yield_to_servings,
    extract_jsonld_recipe,
)


def _page(ld: object) -> str:
    return (
        "<html><head><script type=\"application/ld+json\">"
        + json.dumps(ld)
        + "</script></head><body>hi</body></html>"
    )


BASIC = {
    "@context": "https://schema.org",
    "@type": "Recipe",
    "name": "Lemon Chicken",
    "recipeYield": "4 servings",
    "prepTime": "PT15M",
    "cookTime": "PT45M",
    "totalTime": "PT1H",
    "recipeIngredient": ["2 lb chicken thighs", "1 lemon"],
    "recipeInstructions": [
        {"@type": "HowToStep", "text": "Marinate the chicken."},
        {"@type": "HowToStep", "text": "Roast 45 minutes."},
    ],
}


def test_iso_minutes():
    assert _iso_minutes("PT1H30M") == 90
    assert _iso_minutes("PT45M") == 45
    assert _iso_minutes("PT2H") == 120
    assert _iso_minutes("P1DT2H") == 1560
    assert _iso_minutes("P2W") == 20160
    assert _iso_minutes("garbage") is None
    assert _iso_minutes(None) is None


def test_yield_to_servings():
    assert _yield_to_servings("4 servings") == 4
    assert _yield_to_servings(6) == 6
    assert _yield_to_servings("Serves 8") == 8
    assert _yield_to_servings(["12 cookies", "12"]) == 12
    assert _yield_to_servings(None) is None
    assert _yield_to_servings("a crowd") is None


def test_extract_basic_recipe():
    d = extract_jsonld_recipe(_page(BASIC), "https://example.com/lemon")
    assert isinstance(d, DraftRecipe)
    assert d.title == "Lemon Chicken"
    assert d.servings == 4
    assert d.prep_minutes == 15
    assert d.cook_minutes == 45
    assert d.total_minutes == 60
    assert d.ingredients_blob == "2 lb chicken thighs\n1 lemon"
    assert d.instructions_blob == "Marinate the chicken.\nRoast 45 minutes."
    assert d.source_url == "https://example.com/lemon"


def test_extract_from_graph_and_type_list():
    ld = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage", "name": "ignore me"},
        {**BASIC, "@type": ["Recipe", "NewsArticle"]},
    ]}
    d = extract_jsonld_recipe(_page(ld), "https://example.com/x")
    assert d is not None and d.title == "Lemon Chicken"


def test_extract_howto_sections_become_headers():
    ld = {**BASIC, "recipeInstructions": [
        {"@type": "HowToSection", "name": "Sauce", "itemListElement": [
            {"@type": "HowToStep", "text": "Whisk everything."},
        ]},
        {"@type": "HowToSection", "name": "Assembly", "itemListElement": [
            {"@type": "HowToStep", "text": "Combine and serve."},
        ]},
    ]}
    d = extract_jsonld_recipe(_page(ld), "u")
    assert d.instructions_blob == "Sauce:\nWhisk everything.\nAssembly:\nCombine and serve."


def test_extract_plain_string_instructions_and_entities():
    ld = {**BASIC, "name": "Mac &amp; Cheese", "recipeInstructions": "Mix it all. <b>Bake.</b>"}
    d = extract_jsonld_recipe(_page(ld), "u")
    assert d.title == "Mac & Cheese"
    assert "<b>" not in d.instructions_blob
    assert "Bake." in d.instructions_blob


def test_extract_returns_none_without_recipe():
    assert extract_jsonld_recipe(_page({"@type": "WebSite", "name": "x"}), "u") is None
    assert extract_jsonld_recipe("<html><body>no scripts</body></html>", "u") is None
    # Malformed JSON in one block must not blow up the scan.
    html = "<script type='application/ld+json'>{nope</script>" + _page(BASIC)
    assert extract_jsonld_recipe(html, "u").title == "Lemon Chicken"


def test_extract_requires_ingredients():
    ld = {k: v for k, v in BASIC.items() if k != "recipeIngredient"}
    assert extract_jsonld_recipe(_page(ld), "u") is None


# ---- LLM structuring --------------------------------------------------------

import mise.services.recipe_drafter as drafter_mod
from mise.services.recipe_drafter import structure_text


class _FakeBlock:
    type = "tool_use"
    name = "draft_recipe"

    def __init__(self, payload):
        self.input = payload


class _FakeResp:
    def __init__(self, payload):
        self.content = [_FakeBlock(payload)]


class _FakeMessages:
    def __init__(self, payload):
        self._payload = payload
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeResp(self._payload)


class _FakeAnthropic:
    def __init__(self, payload):
        self.messages = _FakeMessages(payload)


GOOD_PAYLOAD = {
    "title": "Weeknight Dal",
    "servings": 4,
    "prep_minutes": 10,
    "cook_minutes": 30,
    "total_minutes": None,
    "oven_temp_f": None,
    "ingredients_blob": "1 cup red lentils\nDal:\n1 tsp cumin",
    "instructions_blob": "Rinse the lentils.\nSimmer 25 minutes.",
}


def test_structure_text_happy_path(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    fake = _FakeAnthropic(GOOD_PAYLOAD)
    monkeypatch.setattr(drafter_mod.anthropic, "Anthropic", lambda api_key: fake)
    d = structure_text("a rambling dictated dal recipe...", source_url=None)
    assert d.title == "Weeknight Dal"
    assert d.servings == 4
    assert "red lentils" in d.ingredients_blob
    # the raw text must be in the user message verbatim
    sent = fake.messages.last_kwargs["messages"][0]["content"]
    assert "rambling dictated dal recipe" in sent


def test_structure_text_no_key_returns_none(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert structure_text("whatever") is None


def test_structure_text_bad_payload_returns_none(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    fake = _FakeAnthropic({"title": "X"})  # missing required blobs
    monkeypatch.setattr(drafter_mod.anthropic, "Anthropic", lambda api_key: fake)
    assert structure_text("whatever") is None


def test_structure_text_empty_ingredients_returns_none(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    payload = {**GOOD_PAYLOAD, "ingredients_blob": "  "}
    fake = _FakeAnthropic(payload)
    monkeypatch.setattr(drafter_mod.anthropic, "Anthropic", lambda api_key: fake)
    assert structure_text("not a recipe at all") is None


# ---- create_draft orchestrator ----------------------------------------------

import pytest
from sqlmodel import select

from mise.models import Household, Ingredient, IngredientSection, Recipe, Step, StepSection
from mise.services.recipe_drafter import create_draft


@pytest.fixture(autouse=True)
def _household(request):
    """upsert_recipe_from_blobs assumes household_id=1 (single-tenant v1).
    Only seed when the test actually uses the DB."""
    if "session" in request.fixturenames:
        session = request.getfixturevalue("session")
        session.add(Household(id=1, name="Test"))
        session.commit()


DAL_DRAFT = drafter_mod.DraftRecipe(
    title="Weeknight Dal",
    servings=4,
    cook_minutes=30,
    ingredients_blob="1 cup red lentils\n1 tsp cumin\n!!unparseable garbage line!!",
    instructions_blob="Rinse the lentils.\nSimmer 25 minutes.",
)


def test_create_draft_from_text(session, monkeypatch):
    monkeypatch.setattr(drafter_mod, "structure_text", lambda text, source_url=None: DAL_DRAFT)
    # The real strangetom is too forgiving to fail on any plain-text line
    # (it shrugs and returns a name-only parse), so force a hard parser
    # failure for the garbage line to exercise the keep_failed_lines path.
    # _default_strangetom resolves _strangetom_parse at call time.
    import mise.services.ingredient_parser as parser_mod
    real_parse = parser_mod._strangetom_parse

    def exploding_parse(raw, **kwargs):
        if "garbage" in raw:
            raise RuntimeError("simulated hard parser failure")
        return real_parse(raw, **kwargs)

    monkeypatch.setattr(parser_mod, "_strangetom_parse", exploding_parse)
    r = create_draft(session, text="some dictated ramble", llm_verify_mode="off")
    assert r.id is not None
    assert r.name_english == "Weeknight Dal"
    assert r.reviewed_at is None  # lands as a draft
    ings = session.exec(
        select(Ingredient).join(IngredientSection)
        .where(IngredientSection.recipe_id == r.id)
    ).all()
    names = {i.display_name for i in ings}
    # every source line survives — failures become 'unknown' rows
    assert len(ings) == 3
    assert "unknown" in names
    steps = session.exec(
        select(Step).join(StepSection).where(StepSection.recipe_id == r.id)
    ).all()
    assert len(steps) == 2


def test_create_draft_lone_url_in_text_routes_to_url_path(session, monkeypatch):
    seen = {}

    def fake_fetch(url):
        seen["url"] = url
        return "<html></html>"

    monkeypatch.setattr(drafter_mod, "fetch_page", fake_fetch)
    monkeypatch.setattr(drafter_mod, "extract_jsonld_recipe", lambda h, u: DAL_DRAFT)
    r = create_draft(session, text="  https://example.com/dal  ", llm_verify_mode="off")
    assert seen["url"] == "https://example.com/dal"
    assert r.name_english == "Weeknight Dal"


def test_create_draft_url_falls_back_to_llm(session, monkeypatch):
    monkeypatch.setattr(drafter_mod, "fetch_page", lambda u: "<html><p>Dal recipe text</p></html>")
    monkeypatch.setattr(drafter_mod, "extract_jsonld_recipe", lambda h, u: None)
    captured = {}

    def fake_structure(text, source_url=None):
        captured["text"], captured["source_url"] = text, source_url
        return DAL_DRAFT

    monkeypatch.setattr(drafter_mod, "structure_text", fake_structure)
    create_draft(session, url="https://example.com/dal", llm_verify_mode="off")
    assert captured["source_url"] == "https://example.com/dal"
    assert "<p>" not in captured["text"]  # html stripped before LLM


def test_create_draft_title_collision_gets_suffix(session, monkeypatch):
    monkeypatch.setattr(drafter_mod, "structure_text", lambda text, source_url=None: DAL_DRAFT)
    r1 = create_draft(session, text="x", llm_verify_mode="off")
    r2 = create_draft(session, text="x", llm_verify_mode="off")
    assert r1.name_english == "Weeknight Dal"
    assert r2.name_english == "Weeknight Dal (2)"
    assert r1.id != r2.id  # never silently overwrites the existing recipe


def test_create_draft_no_recipe_raises(session, monkeypatch):
    monkeypatch.setattr(drafter_mod, "structure_text", lambda text, source_url=None: None)
    with pytest.raises(ValueError):
        create_draft(session, text="my shopping list: socks", llm_verify_mode="off")


def test_create_draft_rejects_bad_url(session):
    with pytest.raises(ValueError):
        create_draft(session, url="ftp://example.com/x", llm_verify_mode="off")


def test_html_to_text():
    txt = drafter_mod._html_to_text(
        "<html><script>junk()</script><style>.x{}</style>"
        "<h1>Dal</h1><p>1 cup lentils</p><li>cumin</li></html>"
    )
    assert "junk" not in txt and ".x" not in txt
    assert "Dal" in txt and "1 cup lentils" in txt
    assert "\n" in txt  # block tags became newlines


# ---- SSRF guard + grounding ---------------------------------------------------


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/recipe",
    "http://localhost/recipe",
    "http://10.0.0.5/recipe",
    "http://192.168.1.1/recipe",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/recipe",
])
def test_fetch_page_rejects_internal_hosts(url):
    with pytest.raises(ValueError):
        drafter_mod.fetch_page(url)


def test_fetch_page_blocks_redirect_to_internal(monkeypatch):
    def fake_gai(host, port):
        ips = {"example.com": "93.184.216.34"}
        return [(2, 1, 6, "", (ips.get(host, host), 0))]

    monkeypatch.setattr(drafter_mod.socket, "getaddrinfo", fake_gai)

    class _Resp:
        is_redirect = True
        headers = {"location": "http://127.0.0.1/secret"}

    monkeypatch.setattr(drafter_mod.httpx, "get", lambda *a, **k: _Resp())
    with pytest.raises(ValueError):
        drafter_mod.fetch_page("http://example.com/recipe")


def test_structure_text_ungrounded_title_returns_none(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    payload = {**GOOD_PAYLOAD, "title": "Totally Invented Pwned"}
    fake = _FakeAnthropic(payload)
    monkeypatch.setattr(drafter_mod.anthropic, "Anthropic", lambda api_key: fake)
    assert structure_text("a rambling dictated dal recipe...") is None
