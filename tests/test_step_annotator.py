from mise.services.step_annotator import find_mentions


def _hits(text, ingredients):
    """[(text up to the mention end, parts)] — readable assertions."""
    return [(text[:m.end], list(m.parts)) for m in find_mentions(text, ingredients)]


def test_plural_in_step_matches_singular_ingredient():
    assert _hits("Add eggs and stir", [("egg", 4, "ea")]) == [("Add eggs", [(4.0, "ea")])]


def test_singular_in_step_matches_plural_ingredient():
    assert _hits("Slice the carrot", [("carrots", 2, "ea")]) == [("Slice the carrot", [(2.0, "ea")])]


def test_partial_name_matches_when_unambiguous():
    hits = _hits("Put on top of rice", [("cooked rice", 2, "cup"), ("egg", 4, "ea")])
    assert hits == [("Put on top of rice", [(2.0, "cup")])]


def test_ambiguous_partial_is_left_alone():
    ings = [("olive oil", 2, "tbsp"), ("sesame oil", 1, "tsp")]
    assert _hits("Heat the oil", ings) == []
    assert _hits("Heat the sesame oil", ings) == [("Heat the sesame oil", [(1.0, "tsp")])]


def test_full_name_beats_partial_of_longer_name():
    ings = [("rice", 2, "cup"), ("rice vinegar", 1, "tbsp")]
    assert _hits("Add rice vinegar to the rice", ings) == [
        ("Add rice vinegar", [(1.0, "tbsp")]),
        ("Add rice vinegar to the rice", [(2.0, "cup")]),
    ]


def test_longest_phrase_wins():
    ings = [("butter", 1, "tbsp"), ("peanut butter", 0.25, "cup")]
    assert _hits("Whisk the peanut butter", ings) == [("Whisk the peanut butter", [(0.25, "cup")])]


def test_only_first_mention_per_step():
    hits = _hits("Beat the eggs. Pour eggs in.", [("egg", 2, "ea")])
    assert hits == [("Beat the eggs", [(2.0, "ea")])]


def test_skips_when_step_already_states_a_quantity():
    ings = [("egg", 4, "ea"), ("rice", 2, "cup")]
    assert _hits("Crack 2 eggs over 1 cup of rice", ings) == []


def test_number_far_before_mention_does_not_suppress():
    assert _hits("After 5 minutes, stir in the eggs", [("egg", 4, "ea")]) != []


def test_descriptor_alone_never_matches():
    assert _hits("Serve with fresh herbs", [("fresh ginger", 1, "tbsp")]) == []
    assert _hits("Grate the ginger", [("fresh ginger", 1, "tbsp")]) != []


def test_no_substring_matches():
    assert _hits("Cube the eggplant", [("egg", 2, "ea")]) == []


def test_same_name_in_two_sections_shows_both():
    ings = [("salt", 1, "tsp"), ("salt", 0.5, "tsp")]
    assert _hits("Season with salt", ings) == [("Season with salt", [(1.0, "tsp"), (0.5, "tsp")])]


def test_zero_or_missing_qty_ignored():
    assert _hits("Add salt and pepper", [("salt", 0, "ea"), ("pepper", None, None)]) == []


def test_parenthetical_and_prep_in_name_ignored():
    assert _hits("Press the tofu", [("tofu (extra firm), cubed", 1, "block")]) == [
        ("Press the tofu", [(1.0, "block")])
    ]


def test_generic_head_word_alone_never_matches():
    ings = [("soy sauce", 0.25, "cup")]
    assert _hits("Whisk together the sauce", ings) == []
    assert _hits("Add the soy sauce", ings) != []


def test_each_after_quantity_suppresses():
    assert _hits("Add 1 tbsp each sesame oil", [("sesame oil", 1, "tbsp")]) == []


def test_prep_note_carried_on_mention():
    ms = find_mentions("Add the onion and stir", [("onion", 1, "ea", "(finely diced)")])
    assert [m.preps for m in ms] == [("finely diced",)]
    assert find_mentions("Add the onion", [("onion", 1, "ea")])[0].preps == ()


def test_slash_joined_setting_name_is_not_an_ingredient():
    ings = [("chili powder", 2, "tbsp")]
    assert _hits("Turn the pot to the Chili/Beans setting", ings) == []
    assert _hits("Add the chili and stir", ings) != []
    # Capitalised ingredients still match (brands, headings).
    assert _hits("Add Guinness and stir", [("Guinness beer", 1, "can")]) != []


def test_partial_followed_by_another_food_head_is_a_different_ingredient():
    ings = [("coconut milk", 1.5, "cup")]
    assert _hits("until the coconut oil separates", ings) == []
    assert _hits("Add the coconut and stir", ings) != []


def test_shared_word_resolved_when_the_other_is_named_outright():
    ings = [("diced tomatoes", 1, "can"), ("plain tomato sauce", 3, "can")]
    hits = find_mentions("Add the tomatoes, tomato sauce and stir", ings)
    assert [(m.parts, m.labels) for m in hits] == [
        (((1.0, "can"),), ()), (((3.0, "can"),), ()),
    ]


def test_plural_shared_word_lists_every_ingredient():
    ings = [("kidney beans", 1, "can", "drained and rinsed"), ("pinto beans", 1, "can", "(drained and rinsed)")]
    [m] = find_mentions("Add the beans to the pot", ings)
    assert m.parts == ((1.0, "can"), (1.0, "can"))
    assert m.labels == ("kidney", "pinto")
    assert m.preps == ("drained and rinsed",)
    # Singular stays ambiguous, as before.
    assert find_mentions("Add the bean", ings) == []


def test_shared_word_means_the_ingredient_it_is_the_main_noun_of():
    rice = [("Japanese short-grain rice", 1.5, "cup"), ("rice vinegar", 0.5, "tsp")]
    assert _hits("Prepare the rice, then rinse the short-grain rice", rice)[0] == ("Prepare the rice", [(1.5, "cup")])
    pep = [("black pepper", 0.5, "tsp"), ("red pepper flakes", 0.25, "tsp")]
    assert _hits("Add salt and pepper", pep) == [("Add salt and pepper", [(0.5, "tsp")])]
    nuts = [("unsalted peanuts", 1, "cup"), ("peanut butter", 0.25, "cup")]
    assert _hits("Top with peanuts", nuts) == [("Top with peanuts", [(1.0, "cup")])]


def test_fallback_to_named_elsewhere_and_mixed_units_stay_blank():
    ck = [("boneless skinless chicken thigh", 1, "lb"), ("chicken stock", 1, "cup")]
    assert _hits("Add the chicken, then the chicken stock", ck)[0] == ("Add the chicken", [(1.0, "lb")])
    peppers = [("black pepper", 2, "tsp"), ("green bell pepper", 1, "ea"), ("red bell pepper", 1, "ea")]
    assert _hits("Add the sliced peppers", peppers) == []


def test_synonym_crushed_red_pepper_is_the_flakes():
    ings = [("roasted red peppers", 1, "jar"), ("red pepper flakes", 0.25, "tsp")]
    assert _hits("whisk garlic, crushed red pepper and oil", ings) == [
        ("whisk garlic, crushed red pepper", [(0.25, "tsp")])
    ]


def test_unmentioned_lists_ingredients_no_step_covers():
    from mise.services.step_annotator import unmentioned
    ings = [("onion", 1, "ea"), ("coconut milk", 1.5, "cup"), ("fish sauce", 2, "tbsp"),
            ("salt", 0, "ea"), ("bay leaf", 2, "ea")]
    steps = ["Fry the onion.", "Bring 1/2 cup of coconut milk to a boil.", "Season."]
    # Written quantity still counts as covered; zero-qty "to taste" is ignored.
    assert unmentioned(steps, ings) == ["fish sauce", "bay leaf"]


def test_name_with_leading_comma_descriptors():
    ings = [("boneless, skinless chicken thighs", 2, "lb")]
    assert _hits("Add the chicken thighs", ings) == [("Add the chicken thighs", [(2.0, "lb")])]
