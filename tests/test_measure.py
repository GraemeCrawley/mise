"""The shared measurement type: formatting, conversion, combining parts,
and the tappable span every page uses."""

from mise.services.measure import Measure, combine, fmt_number


def test_formats_kitchen_fractions():
    assert fmt_number(0.88) == "⅞" and fmt_number(1.5) == "1½" and fmt_number(2) == "2"
    assert fmt_number(0.3) == "0.3" and fmt_number(236.6) == "237"


def test_converts_within_a_dimension():
    assert Measure.of(1, "cup").to("ml").text() == "237 ml"
    assert Measure.of(3, "tsp").to("tbsp").text() == "1 tbsp"
    assert [m.unit for m in Measure.of(100, "g").alternatives()][:3] == ["kg", "oz", "lb"]


def test_counts_and_odd_units_do_not_convert():
    assert Measure.of(2, "ea").text() == "2" and not Measure.of(2, "ea").convertible
    assert Measure.of(1, "bag").alternatives() == []
    assert "measure-tap" not in Measure.of(1, "bag").html()
    assert "measure-tap" in Measure.of(1, "cups").html()  # alias -> cup


def test_combine_sums_same_dimension_only():
    assert [m.text() for m in combine([Measure.of(2, "tbsp"), Measure.of(1, "tbsp")])] == ["3 tbsp"]
    parts = combine([Measure.of(1, "cup"), Measure.of(250, "g"), Measure.of(2, "tbsp")])
    assert [m.unit for m in parts] == ["cup", "g"] and round(parts[0].qty, 3) == 1.125


def test_volume_and_weight_convert_with_a_known_ingredient():
    milk = Measure.of(0.875, "cup", "sweetened condensed milk")
    assert milk.to("g").text() == "268 g"
    assert "g" in [m.unit for m in milk.alternatives()]
    assert round(Measure.of(120, "g", "all-purpose flour").to("cup").qty, 2) == 1.0
    assert "data-density" in milk.html()


def test_longest_ingredient_name_wins_and_unknown_uses_water():
    from mise.services.measure import density_for
    assert round(density_for("coconut milk") * 236.588) == 226
    assert round(density_for("light brown sugar") * 236.588) == 213
    assert density_for("unsalted butter") == density_for("butter")
    assert density_for("gochujang") is None
    assert Measure.of(1, "cup", "gochujang").to("g").text() == "237 g"
    assert Measure.of(2, "ea", "eggs").density is None
