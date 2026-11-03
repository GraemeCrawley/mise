import pytest
from mise.services.buy_units import has_buy_unit, to_buy_unit, rollup, BUY_UNITS

def test_table_is_minimal():
    assert set(BUY_UNITS) == {"garlic", "onion", "tofu", "lemon", "lime"}

def test_bridge_cup_minced_garlic_to_cloves():
    assert to_buy_unit("garlic", 1, "cup") == pytest.approx(48, abs=0.5)

def test_pint_bridge_tbsp_garlic_to_cloves():
    assert to_buy_unit("garlic", 1, "tbsp") == pytest.approx(3, abs=0.5)

def test_block_tofu_to_grams():
    assert to_buy_unit("tofu", 1, "block") == pytest.approx(400, abs=1)

def test_unknown_unit_returns_none():
    assert to_buy_unit("garlic", 1, "furlong") is None

def test_rollup_large_clove_count_to_heads():
    qty, unit = rollup("garlic", 48, "clove")
    assert unit == "head" and qty == pytest.approx(5, abs=0.5)

def test_rollup_below_threshold_stays():
    assert rollup("garlic", 6, "clove") == (6, "clove")

def test_has_buy_unit():
    assert has_buy_unit("garlic") and not has_buy_unit("edamame")

def test_pint_fallback_uses_unit_not_in_bridges():
    # 'ml' is NOT an explicit garlic bridge -> exercises the Pint fallback.
    # 1 tsp ~= 4.93 ml, and 1 tsp == 1 clove, so ~5 ml ~= ~1 clove.
    result = to_buy_unit("garlic", 4.93, "ml")
    assert result == pytest.approx(1.0, abs=0.1)

def test_rollup_at_exact_threshold():
    qty, unit = rollup("garlic", 20, "clove")
    assert unit == "head" and qty == pytest.approx(2.0, abs=0.1)

def test_rollup_none_ingredient_unchanged():
    assert rollup("tofu", 5000, "g") == (5000, "g")
