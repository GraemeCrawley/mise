from mise.models import Ingredient
from scripts.migrate_natural_units import plan_row

# plan_row(ing) -> ("overwrite", qty, unit) | ("preserve", reason)


def test_row_with_original_unit_restores_natural():
    ing = Ingredient(section_id=1, order=0, raw_text="1 cup edamame",
                     display_name="edamame", canonical_name="edamame",
                     qty=237, unit="ml", original_qty=1, original_unit="cup",
                     category="Frozen", edited=False)
    assert plan_row(ing) == ("overwrite", 1, "cup")


def test_already_natural_row_untouched():
    ing = Ingredient(section_id=1, order=0, raw_text="200 g tofu",
                     display_name="tofu", canonical_name="tofu",
                     qty=200, unit="g", original_qty=None, original_unit=None,
                     category="Frozen", edited=False)
    assert plan_row(ing) == ("preserve", "already-natural")


def test_edited_row_preserved():
    ing = Ingredient(section_id=1, order=0, raw_text="1 cup edamame",
                     display_name="edamame", canonical_name="edamame",
                     qty=300, unit="ml", original_qty=1, original_unit="cup",
                     category="Frozen", edited=True)
    assert plan_row(ing) == ("preserve", "edited-flag")


def test_divergent_qty_preserved_as_edit():
    # stored qty inconsistent with original*conversion => hand-edited pre-flag
    ing = Ingredient(section_id=1, order=0, raw_text="1 cup edamame",
                     display_name="edamame", canonical_name="edamame",
                     qty=500, unit="ml", original_qty=1, original_unit="cup",
                     category="Frozen", edited=False)
    assert plan_row(ing) == ("preserve", "diverged")
