"""Seed a fresh Mise instance with made-up sample recipes + a week plan.
Usage: python3 seed.py [base_url]   (default http://localhost:8000)"""
import json
import sys
import urllib.request
from datetime import date, timedelta

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        txt = r.read()
        return json.loads(txt) if txt else None


# (name, servings, prep, cook, emoji, tags, ingredients[(qty, unit, name, category, prep_note)], steps)
RECIPES = [
    ("Weeknight Chili", 4, 15, 40, "🌶️", ["Dinner", "Make Ahead"], [
        (1, "tbsp", "olive oil", "Pantry", None),
        (1, "ea", "yellow onion", "Produce", "diced"),
        (2, "clove", "garlic", "Produce", "minced"),
        (500, "g", "ground beef", "Meat", None),
        (1, "ea", "red bell pepper", "Produce", "chopped"),
        (2, "tbsp", "chili powder", "Spices", None),
        (1, "tsp", "ground cumin", "Spices", None),
        (796, "ml", "crushed tomatoes", "Pantry", None),
        (540, "ml", "kidney beans", "Pantry", "drained and rinsed"),
        (1, "tsp", "salt", "Spices", None),
    ], [
        "Heat the olive oil in a large pot over medium heat. Add the onion and cook until soft, about 5 minutes.",
        "Add the garlic and bell pepper and cook 2 minutes more.",
        "Add the ground beef and cook, breaking it up, until browned.",
        "Stir in the chili powder, cumin and salt, then the crushed tomatoes and kidney beans.",
        "Simmer uncovered for 30 minutes, stirring now and then. Taste and adjust seasoning.",
    ]),
    ("Lemon Herb Chicken", 4, 10, 30, "🍋", ["Dinner", "Quick"], [
        (4, "ea", "chicken thigh", "Meat", "boneless, skinless"),
        (1, "ea", "lemon", "Produce", "zested and juiced"),
        (2, "tbsp", "olive oil", "Pantry", None),
        (2, "clove", "garlic", "Produce", "minced"),
        (1, "tbsp", "fresh thyme", "Herbs", "chopped"),
        (2, "tbsp", "fresh parsley", "Herbs", "chopped"),
        (0.5, "tsp", "salt", "Spices", None),
    ], [
        "Heat the oven to 425°F.",
        "Mix the olive oil, lemon zest, lemon juice, garlic, thyme and salt in a bowl.",
        "Toss the chicken thigh pieces in the mixture and lay them in a baking dish.",
        "Roast for 25 to 30 minutes until cooked through. Scatter the parsley over before serving.",
    ]),
    ("Veggie Fried Rice", 2, 10, 12, "🍚", ["Dinner", "Vegetarian", "Quick"], [
        (2, "cup", "cooked rice", "Pasta/Rice", "day-old"),
        (2, "ea", "egg", "Dairy", "beaten"),
        (1, "cup", "frozen peas and carrots", "Frozen", None),
        (3, "ea", "green onion", "Produce", "sliced"),
        (2, "tbsp", "soy sauce", "Sauces", None),
        (1, "tbsp", "vegetable oil", "Pantry", None),
        (1, "tsp", "sesame oil", "Pantry", None),
    ], [
        "Heat the vegetable oil in a wok or large pan over high heat.",
        "Scramble the egg until just set, then push it to one side.",
        "Add the frozen peas and carrots and cook 2 minutes.",
        "Add the cooked rice and stir-fry until hot and a little crisp.",
        "Stir in the soy sauce, sesame oil and green onion. Serve right away.",
    ]),
    ("Buttermilk Pancakes", 4, 10, 15, "🥞", ["Breakfast", "Weekend"], [
        (2, "cup", "all-purpose flour", "Pantry", None),
        (2, "tbsp", "sugar", "Pantry", None),
        (2, "tsp", "baking powder", "Pantry", None),
        (0.5, "tsp", "baking soda", "Pantry", None),
        (2, "cup", "buttermilk", "Dairy", None),
        (2, "ea", "egg", "Dairy", None),
        (3, "tbsp", "butter", "Dairy", "melted"),
        (0.25, "cup", "maple syrup", "Pantry", "to serve"),
    ], [
        "Whisk the flour, sugar, baking powder and baking soda in a large bowl.",
        "In another bowl, whisk the buttermilk, egg and melted butter.",
        "Pour the wet mix into the dry and stir until just combined. A few lumps are fine.",
        "Cook 1/4 cup portions on a hot greased griddle until bubbles form, then flip.",
        "Serve warm with maple syrup.",
    ]),
    ("Greek Salad", 2, 15, 0, "🥗", ["Lunch", "Vegetarian", "Quick"], [
        (2, "ea", "tomato", "Produce", "cut in wedges"),
        (1, "ea", "cucumber", "Produce", "sliced"),
        (0.5, "ea", "red onion", "Produce", "thinly sliced"),
        (0.5, "cup", "kalamata olives", "Deli", None),
        (150, "g", "feta cheese", "Dairy", "cubed"),
        (3, "tbsp", "olive oil", "Pantry", None),
        (1, "tbsp", "red wine vinegar", "Pantry", None),
        (1, "tsp", "dried oregano", "Spices", None),
    ], [
        "Combine the tomato, cucumber, red onion and kalamata olives in a bowl.",
        "Whisk the olive oil, red wine vinegar and dried oregano together.",
        "Pour the dressing over the vegetables and top with the feta cheese.",
    ]),
    ("Black Bean Tacos", 4, 10, 10, "🌮", ["Dinner", "Vegetarian"], [
        (540, "ml", "black beans", "Pantry", "drained and rinsed"),
        (1, "tsp", "ground cumin", "Spices", None),
        (1, "tsp", "smoked paprika", "Spices", None),
        (8, "ea", "corn tortilla", "Bakery", None),
        (1, "ea", "avocado", "Produce", "sliced"),
        (1, "cup", "shredded cabbage", "Produce", None),
        (1, "ea", "lime", "Produce", "cut in wedges"),
        (0.5, "cup", "salsa", "Sauces", None),
    ], [
        "Warm the black beans in a pan with the cumin, smoked paprika and a splash of water. Mash lightly.",
        "Heat the corn tortillas in a dry skillet until soft.",
        "Fill each tortilla with beans, shredded cabbage, avocado and salsa.",
        "Serve with lime wedges.",
    ]),
]

ids = {}
for name, servings, prep, cook, emoji, tags, ings, steps in RECIPES:
    r = call("POST", "/recipes/blank", {
        "name_english": name, "servings": servings,
        "prep_minutes": prep, "cook_minutes": cook, "total_minutes": prep + cook,
    })
    rid = r["id"]
    ids[name] = rid
    for qty, unit, disp, cat, note in ings:
        call("POST", f"/recipes/{rid}/ingredients", {
            "qty": qty, "unit": unit, "display_name": disp,
            "category": cat, "prep_note": note,
        })
    for s in steps:
        call("POST", f"/recipes/{rid}/steps", {"text": s})
    call("PATCH", f"/recipes/{rid}", {"tags": tags, "emoji": emoji})
    call("POST", f"/recipes/{rid}/review/complete")
    print("recipe", rid, name)

today = date.today()
d = lambda n: (today + timedelta(days=n)).isoformat()
PLAN = [
    ("Weeknight Chili", 0, "dinner"),
    ("Veggie Fried Rice", 1, "lunch"),
    ("Greek Salad", 2, "lunch"),
    ("Lemon Herb Chicken", 2, "dinner"),
    ("Black Bean Tacos", 4, "dinner"),
    ("Buttermilk Pancakes", 5, "breakfast"),
]
for name, off, meal in PLAN:
    try:
        s = call("POST", "/plan/place", {"recipe_id": ids[name], "date": d(off), "meal": meal})
        print("placed", name, s["cook_date"], s["cook_meal"])
    except urllib.error.HTTPError as e:
        print("place failed", name, e.code, e.read().decode())
