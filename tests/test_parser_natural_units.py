from mise.services.ingredient_parser import to_canonical


def test_cup_stays_cup():
    qty, unit, oq, ou = to_canonical(1, "cup")
    assert (qty, unit) == (1, "cup")
    assert oq is None and ou is None


def test_tablespoon_abbreviates_but_keeps_dimension():
    qty, unit, _, _ = to_canonical(2, "Tablespoons")
    assert (qty, unit) == (2, "tbsp")


def test_pounds_stay_pounds():
    qty, unit, _, _ = to_canonical(1.5, "lb")
    assert (qty, unit) == (1.5, "lb")


def test_grams_stay_grams():
    qty, unit, _, _ = to_canonical(200, "g")
    assert (qty, unit) == (200, "g")


def test_unknown_unit_preserved():
    qty, unit, _, _ = to_canonical(1, "smidgen")
    assert unit == "smidgen"
