"""Every recipe list (Library, plan-week sheet, add/swap picker) uses the one
shared search + category chips from _recipe_filter()."""

from datetime import datetime

from mise.models import Household, Recipe


def _seed(session):
    session.add(Household(id=1, name="Test"))
    session.add(Recipe(name_english="Pie", name_original="Pie", servings=4, household_id=1,
                       tags=["Favourites", "Dessert"], reviewed_at=datetime.now()))
    session.add(Recipe(name_english="Stew", name_original="Stew", servings=4, household_id=1,
                       tags=["Instant Pot"], reviewed_at=datetime.now()))
    session.commit()


def test_every_recipe_list_gets_the_same_chips(client, session):
    _seed(session)
    for url, list_id in [("/app/", "lib-list"), ("/app/plan", "swap-list"), ("/app/plan/session", "sh-list")]:
        page = client.get(url).text
        assert f"data-rf='{list_id}'" in page, url
        for tag in ("Favourites", "Dessert", "Instant Pot"):
            assert f"data-tag='{tag}'" in page, (url, tag)
        assert "data-tags='Favourites|Dessert'" in page, url


def test_library_tag_link_starts_on_that_chip(client, session):
    _seed(session)
    page = client.get("/app/?tag=Dessert").text
    assert "data-tag='Dessert' aria-pressed='true'" in page
    # Rows are all sent; the browser hides the ones outside the chip.
    assert "Stew" in page
