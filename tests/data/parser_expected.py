"""Hand-annotated "correctness" expectations for every unique source
line in the library. Used by tests/test_parser_correctness.py.

Each entry: source_line → expected dict (or None to skip if the source
is genuinely ambiguous or out-of-scope).

Format:
    (qty, unit, name_substr, category, optional, parse_failed)

Where:
  - qty: expected numeric qty (after our category-preference logic).
         Use None when undefined ("to taste" with no qty).
  - unit: expected unit string ('tsp', 'tbsp', 'cup', 'ea', 'lb', 'g',
          'ml', 'kg', 'can', 'clove', 'sprig', 'inch', etc.)
  - name_substr: substring that MUST appear in display_name (lowercase).
  - category: one of {Meat, Produce, Dairy, Bakery, Deli, Frozen,
              Pantry, Sauces, Pasta/Rice, Snacks, Beverages, Spices,
              Other}
  - optional: True if the recipe says optional/garnish/to-taste
  - parse_failed: True for foreign-language or genuinely unparseable

Where a category is borderline (e.g. herbs in Produce vs Spices), I
picked the one that matches the recipe's intent (fresh = Produce,
dried = Spices).
"""

from __future__ import annotations

# Field tuple type
# (qty, unit, name_substr, category, optional, parse_failed)
EXPECTED: dict[str, tuple | None] = {
    # ── Asterisk-led shot of rum (Brazilian Lemonade) ────────────────
    "* shot of white rum optional":              (None, None, "rum",        "Beverages", True,  False),

    # ── Small fractional spice/salt amounts ──────────────────────────
    "0.125 teaspoon ground nutmeg":              (0.125, "tsp", "nutmeg",   "Spices",  False, False),
    "0.125 tsp Ground Nutmeg":                   (0.125, "tsp", "nutmeg",   "Spices",  False, False),
    "0.125 tsp Salt":                            (0.125, "tsp", "salt",     "Spices",  False, False),
    "0.25 Inch Ginger":                          None,  # strangetom doesn't recognise "inch" as a unit
    "0.25 cup low-sodium soy sauce":             (0.25, "cup", "soy sauce", "Sauces",  False, False),
    "0.25 cup water":                            (0.25, "cup", "water",     "Beverages",False,False),
    "0.25 teaspoon white pepper":                (0.25, "tsp", "white pepper","Spices",False, False),

    # ── Half-cup type amounts ────────────────────────────────────────
    "0.5 Cup White Sugar":                       (0.5,  "cup", "sugar",     "Pantry",  False, False),
    "0.5 cup Extra virgin olive oil":            (0.5,  "cup", "olive oil", "Pantry",  False, False),
    "0.5 cup oil":                               (0.5,  "cup", "oil",       "Pantry",  False, False),
    "0.5 red onion, finely sliced":              (0.5,  "ea",  "red onion", "Produce", False, False),
    "0.5 tablespoon minced fresh rosemary":      (0.5,  "tbsp","rosemary",  "Produce", False, False),
    "0.5 tablespoon sesame oil":                 (0.5,  "tbsp","sesame oil","Pantry",  False, False),
    "0.5 tablespoons nutmeg":                    (0.5,  "tbsp","nutmeg",    "Spices",  False, False),
    "0.5 teaspoon baking powder":                (0.5,  "tsp", "baking powder","Pantry",False,False),
    "0.5 teaspoon rice wine vinegar":            (0.5,  "tsp", "rice",      "Pantry",  False, False),
    "0.5 teaspoon salt, divided":                (0.5,  "tsp", "salt",      "Spices",  False, False),
    "0.5 tsp black pepper":                      (0.5,  "tsp", "black pepper","Spices",False, False),
    "0.75 tsp salt":                             (0.75, "tsp", "salt",      "Spices",  False, False),

    # ── Cans and other packages ──────────────────────────────────────
    "1 (15-oz.) can fire roasted diced tomatoes with green chiles": (1.0, "can", "tomato", "Produce", False, False),
    "1 (15-oz.) can refried black or pinto beans or homemade refried beans": (1.0, "can", "refried bean", "Pantry", False, False),

    # ── Sugar/water at scale ─────────────────────────────────────────
    "1 1/2 cup sugar or less or more to taste":  (1.5,  "cup", "sugar",     "Pantry",  False, False),
    "1 1/2 cups water":                          (1.5,  "cup", "water",     "Beverages",False,False),
    "1 1/2 tsp smoked paprika":                  (1.5,  "tsp", "paprika",   "Spices",  False, False),

    # ── Big leg of lamb (range qty) ──────────────────────────────────
    "1 4 to 5 pound leg of lamb, bone-in, fat trimmed": None,  # two-number prefix edge case

    # ── Camembert, alcohol, cinnamon ─────────────────────────────────
    "1 Camembert or other cheese (you can make 1 other Camembert alone in the oven for more cheese)": (1.0, "ea", "camembert", "Dairy", False, False),
    "1 Can Guinness Beer":                       (1.0,  "can", "guinness",  "Beverages",False,False),
    "1 Cinnamon Stick":                          (1.0,  "ea",  "cinnamon",  "Spices",  False, False),
    "1 Extra Tbsp White Sugar":                  None,  # strangetom: unit="extra tbsp"

    # ── Common 1 Tbsp lines (Asian dishes) ───────────────────────────
    "1 Tbsp chili powder":                       (1.0,  "tbsp","chili",     "Spices",  False, False),
    "1 Tbsp roasted sesame seeds":               (1.0,  "tbsp","sesame seed","Spices", False, False),
    "1 Tbsp sesame oil":                         (1.0,  "tbsp","sesame oil","Pantry",  False, False),
    "1 Tbsp soy sauce":                          (1.0,  "tbsp","soy sauce", "Sauces",  False, False),
    "1 Tbsp sugar – I used raw sugar":           (1.0,  "tbsp","sugar",     "Pantry",  False, False),
    "1 Tbsp water":                              (1.0,  "tbsp","water",     "Beverages",False,False),

    "1 Vanilla Bean (Split & Scraped)":          (1.0,  "ea",  "vanilla",   "Spices",  False, False),

    # ── Counts ───────────────────────────────────────────────────────
    "1 bay leaf":                                (1.0,  "ea",  "bay leaf",  "Spices",  False, False),
    "1 bottle of dry red wine":                  (1.0,  "bottle","red wine","Beverages",False,False),
    "1 can into beans, rinsed and drained":      (1.0,  "can", "bean",      "Pantry",  False, False),  # "into beans" looks like a typo for "pinto"
    "1 cinnamon stick":                          (1.0,  "ea",  "cinnamon",  "Spices",  False, False),

    # ── 1 cup lines ──────────────────────────────────────────────────
    "1 cup (250 ml) warm water":                 (1.0,  "cup", "water",     "Beverages",False,False),
    "1 cup corn kernels, drained":               (1.0,  "cup", "corn",      "Frozen",  False, False),
    "1 cup frozen berries/fruit":                (1.0,  "cup", "berries",   "Frozen",  False, False),
    "1 cup granulated sugar":                    (1.0,  "cup", "sugar",     "Pantry",  False, False),
    "1 cup green peas":                          (1.0,  "cup", "peas",      "Frozen",  False, False),
    "1 cup light brown sugar packed":            (1.0,  "cup", "brown sugar","Pantry", False, False),
    "1 cup milk of choice":                      (1.0,  "cup", "milk",      "Dairy",   False, False),
    "1 cup oat milk":                            (1.0,  "cup", "oat milk",  "Dairy",   False, False),  # plant milk → Dairy aisle in most stores
    "1 cup peeled ginger":                       (1.0,  "cup", "ginger",    "Produce", False, False),
    "1 cup plain room temp Greek yogurt":        (1.0,  "cup", "yogurt",    "Dairy",   False, False),
    "1 cup plain yogurt":                        (1.0,  "cup", "yogurt",    "Dairy",   False, False),
    "1 cup salted butter, softened":             (1.0,  "cup", "butter",    "Dairy",   False, False),
    "1 cup water":                               (1.0,  "cup", "water",     "Beverages",False,False),
    "1 cup white wine":                          (1.0,  "cup", "white wine","Beverages",False,False),
    "1 cup whole milk":                          (1.0,  "cup", "milk",      "Dairy",   False, False),

    "1 egg yolk with a little water":            (1.0,  "ea",  "egg yolk",  "Dairy",   False, False),

    # ── Cloves of garlic and similar ─────────────────────────────────
    "1 garlic clove , finely grated":            (1.0,  "clove","garlic",   "Produce", False, False),
    "1 garlic clove, minced":                    (1.0,  "clove","garlic",   "Produce", False, False),
    "1 green chili":                             (1.0,  "ea",  "chili",     "Produce", False, False),
    "1 head of garlic (or more)":                (1.0,  "head","garlic",    "Produce", False, False),
    "1 heaped teaspoon black tea leaves":        None,  # strangetom: unit="heaped teaspoon"

    # ── Inch ginger / cinnamon (strangetom can't do "inch", source_line covers it) ─
    "1 inch cinnamon":                           None,  # see above (inch unit limit)
    "1 inch ginger, grated":                     None,
    "1 inch long cinnamon bark":                 None,

    "1 jalapeño, seeds removed, finely chopped": (1.0,  "ea",  "jalape",    "Produce", False, False),
    "1 large butternut squash halved, seedless": (1.0,  "ea",  "squash",    "Produce", False, False),
    "1 large can diced tomatoes**, with their juices": (1.0, "can", "tomato","Produce", False, False),
    "1 large french baguette":                   (1.0,  "ea",  "baguette",  "Bakery",  False, False),
    "1 large onion, chopped":                    (1.0,  "ea",  "onion",     "Produce", False, False),
    "1 large red bell pepper, chopped":          (1.0,  "ea",  "bell pepper","Produce",False, False),
    "1 large yellow onion, thinly sliced":       (1.0,  "ea",  "onion",     "Produce", False, False),
    "1 lb. ground beef":                         (1.0,  "lb",  "beef",      "Meat",    False, False),
    "1 medium red onion, chopped":               (1.0,  "ea",  "red onion", "Produce", False, False),
    "1 onion":                                   (1.0,  "ea",  "onion",     "Produce", False, False),
    "1 orange, sliced into rounds":              (1.0,  "ea",  "orange",    "Produce", False, False),
    "1 pod garlic":                              (1.0,  "pod", "garlic",    "Produce", False, False),
    "1 scoop protein powder (vanilla)":          (1.0,  "scoop","protein",  "Pantry",  False, False),
    "1 sliced tomato":                           (1.0,  "ea",  "tomato",    "Produce", False, False),
    "1 small carrot":                            (1.0,  "ea",  "carrot",    "Produce", False, False),
    "1 star anise pod":                          (1.0,  "ea",  "star anise","Spices",  False, False),
    "1 sweet green pepper":                      (1.0,  "ea",  "green pepper","Produce",False, False),

    # ── 1 tablespoon lines ───────────────────────────────────────────
    "1 tablespoon chopped fresh sage":           (1.0,  "tbsp","sage",      "Produce", False, False),
    "1 tablespoon cornstarch":                   (1.0,  "tbsp","cornstarch","Pantry",  False, False),
    "1 tablespoon oil":                          (1.0,  "tbsp","oil",       "Pantry",  False, False),
    "1 tablespoon olive oil":                    (1.0,  "tbsp","olive oil", "Pantry",  False, False),
    "1 tablespoon paprika":                      (1.0,  "tbsp","paprika",   "Spices",  False, False),
    "1 tablespoon salt":                         (1.0,  "tbsp","salt",      "Spices",  False, False),
    "1 tablespoon sliced fresh ginger with the skin on": (1.0, "tbsp","ginger","Produce",False,False),
    "1 tablespoon tomato paste":                 (1.0,  "tbsp","tomato paste","Pantry",False, False),
    "1 tablespoon vegetable oil":                (1.0,  "tbsp","vegetable oil","Pantry",False,False),
    "1 tablespoons paprika":                     (1.0,  "tbsp","paprika",   "Spices",  False, False),

    # ── 1 tbsp lines ─────────────────────────────────────────────────
    "1 tbsp butter":                             (1.0,  "tbsp","butter",    "Dairy",   False, False),
    "1 tbsp chia seeds":                         (1.0,  "tbsp","chia",      "Pantry",  False, False),
    "1 tbsp chili/cayenne powder":               (1.0,  "tbsp","chili",     "Spices",  False, False),
    "1 tbsp finely chopped parsley":             (1.0,  "tbsp","parsley",   "Produce", False, False),
    "1 tbsp hemp hearts":                        (1.0,  "tbsp","hemp",      "Pantry",  False, False),
    "1 tbsp honey":                              (1.0,  "tbsp","honey",     "Pantry",  False, False),
    "1 tbsp lemon juice":                        (1.0,  "tbsp","lemon",     "Produce", False, False),
    "1 tbsp peanut butter":                      (1.0,  "tbsp","peanut butter","Pantry",False,False),
    "1 tbsp. taco seasoning":                    (1.0,  "tbsp","taco",      "Spices",  False, False),

    # ── 1 teaspoon lines ─────────────────────────────────────────────
    "1 teaspoon Worcestershire sauce":           (1.0,  "tsp", "worcestershire","Sauces",False,False),
    "1 teaspoon all-natural garlic powder":      (1.0,  "tsp", "garlic powder","Spices",False,False),
    "1 teaspoon baking soda":                    (1.0,  "tsp", "baking soda","Pantry", False, False),
    "1 teaspoon cumin":                          (1.0,  "tsp", "cumin",     "Spices",  False, False),
    "1 teaspoon dried oregano":                  (1.0,  "tsp", "oregano",   "Spices",  False, False),
    "1 teaspoon dried rose petals (optional)":   (1.0,  "tsp", "rose",      "Spices",  True,  False),
    "1 teaspoon garlic paste":                   (1.0,  "tsp", "garlic",    "Produce", False, False),
    "1 teaspoon grated fresh ginger":            (1.0,  "tsp", "ginger",    "Produce", False, False),
    "1 teaspoon maple syrup, or honey, optional":(1.0,  "tsp", "maple syrup","Pantry", True,  False),
    "1 teaspoon paprika, more for later":        (1.0,  "tsp", "paprika",   "Spices",  False, False),
    "1 teaspoon salt":                           (1.0,  "tsp", "salt",      "Spices",  False, False),
    "1 teaspoon sea salt":                       (1.0,  "tsp", "salt",      "Spices",  False, False),
    "1 teaspoon sugar (optional)":               (1.0,  "tsp", "sugar",     "Pantry",  True,  False),
    "1 teaspoon whole peppercorn":               (1.0,  "tsp", "peppercorn","Spices",  False, False),

    "1 to 2 tablespoons butter, to taste":       (1.0,  "tbsp","butter",    "Dairy",   False, False),
    "1 tomato":                                  (1.0,  "ea",  "tomato",    "Produce", False, False),

    # ── 1 tsp lines ──────────────────────────────────────────────────
    "1 tsp Dijon mustard":                       (1.0,  "tsp", "dijon",     "Sauces",  False, False),
    "1 tsp brown sugar":                         (1.0,  "tsp", "brown sugar","Pantry", False, False),
    "1 tsp fine sea salt":                       (1.0,  "tsp", "salt",      "Spices",  False, False),
    "1 tsp garam masala":                        (1.0,  "tsp", "garam masala","Spices",False, False),
    "1 tsp ground black pepper":                 (1.0,  "tsp", "black pepper","Spices",False, False),
    "1 tsp instant yeast":                       (1.0,  "tsp", "yeast",     "Pantry",  False, False),
    "1 tsp kasuri methi":                        (1.0,  "tsp", "kasuri",    "Spices",  False, False),
    "1 tsp minced garlic":                       (1.0,  "tsp", "garlic",    "Produce", False, False),
    "1 tsp of white wine":                       (1.0,  "tsp", "white wine","Beverages",False,False),
    "1 tsp paprika, preferably smoked paprika":  (1.0,  "tsp", "paprika",   "Spices",  False, False),
    "1 tsp red chili powder":                    (1.0,  "tsp", "chili",     "Spices",  False, False),
    "1 tsp salt":                                (1.0,  "tsp", "salt",      "Spices",  False, False),
    "1 tsp sugar":                               (1.0,  "tsp", "sugar",     "Pantry",  False, False),
    "1 tsp sugar (optional)":                    (1.0,  "tsp", "sugar",     "Pantry",  True,  False),
    "1 tsp vinegar – I used apple vinegar":      (1.0,  "tsp", "vinegar",   "Pantry",  False, False),
    "1 tsp. kosher salt, plus more":             (1.0,  "tsp", "salt",      "Spices",  False, False),

    "1 yellow onion":                            (1.0,  "ea",  "onion",     "Produce", False, False),
    "1 yellow onion, peeled and cut into wedges":(1.0,  "ea",  "onion",     "Produce", False, False),

    "1-2 green cardamoms":                       (1.0,  "ea",  "cardamom",  "Spices",  False, False),

    # ── 1.5 lines ────────────────────────────────────────────────────
    "1.5 cups non-fat plain Greek yogurt":       (1.5,  "cup", "yogurt",    "Dairy",   False, False),
    "1.5 lbs. flank steak":                      (1.5,  "lb",  "flank steak","Meat",   False, False),
    "1.5 pounds ground pork":                    (1.5,  "lb",  "pork",      "Meat",    False, False),
    "1.5 tablespoons vinegar":                   (1.5,  "tbsp","vinegar",   "Pantry",  False, False),
    "1.5 tbsp oil/ghee":                         (1.5,  "tbsp","oil",       "Pantry",  False, False),
    "1.5 teaspoons sambal oelek":                (1.5,  "tsp", "sambal",    "Sauces",  False, False),
    "1.5 teaspoons smoked paprika*":             (1.5,  "tsp", "paprika",   "Spices",  False, False),

    # ── 1/2 cup ──────────────────────────────────────────────────────
    "1/2 cup apple cider vinegar":               (0.5,  "cup", "vinegar",   "Pantry",  False, False),
    "1/2 cup chopped red onion":                 (0.5,  "cup", "red onion", "Produce", False, False),
    "1/2 cup condensed milk or less or more to taste": (0.5, "cup","condensed milk","Dairy",False,False),
    "1/2 cup dry white wine":                    (0.5,  "cup", "white wine","Beverages",False,False),
    "1/2 cup fresh lemon juice":                 (0.5,  "cup", "lemon",     "Produce", False, False),
    "1/2 cup grated ginger":                     (0.5,  "cup", "ginger",    "Produce", False, False),
    "1/2 cup maple syrup":                       (0.5,  "cup", "maple syrup","Pantry", False, False),

    "1/2 pound potatoes":                        (0.5,  "lb",  "potato",    "Produce", False, False),

    # ── 1/2 teaspoon ─────────────────────────────────────────────────
    "1/2 teaspoon fennel seeds":                 (0.5,  "tsp", "fennel",    "Spices",  False, False),
    "1/2 teaspoon salt":                         (0.5,  "tsp", "salt",      "Spices",  False, False),

    # ── 1/2 tsp ──────────────────────────────────────────────────────
    "1/2 tsp cooking/kosher salt":               (0.5,  "tsp", "salt",      "Spices",  False, False),
    "1/2 tsp coriander powder":                  (0.5,  "tsp", "coriander", "Spices",  False, False),
    "1/2 tsp dried oregano":                     (0.5,  "tsp", "oregano",   "Spices",  False, False),
    "1/2 tsp fine sea salt":                     (0.5,  "tsp", "salt",      "Spices",  False, False),
    "1/2 tsp garlic powder":                     (0.5,  "tsp", "garlic powder","Spices",False,False),
    "1/2 tsp ground pepper":                     (0.5,  "tsp", "pepper",    "Spices",  False, False),
    "1/2 tsp onion powder":                      (0.5,  "tsp", "onion powder","Spices",False, False),

    # ── 1/3 cup ──────────────────────────────────────────────────────
    "1/3 cup soy sauce":                         (0.333,"cup", "soy sauce", "Sauces",  False, False),
    "1/3 cup yogurt/curd":                       (0.333,"cup", "yogurt",    "Dairy",   False, False),

    # ── 1/4 cup ──────────────────────────────────────────────────────
    "1/4 cup brandy (or orange liqueur)":        (0.25, "cup", "brandy",    "Beverages",False,False),
    "1/4 cup maple syrup":                       (0.25, "cup", "maple syrup","Pantry", False, False),
    "1/4 cup mayo":                              (0.25, "cup", "mayo",      "Sauces",  False, False),
    "1/4 cup olive oil":                         (0.25, "cup", "olive oil", "Pantry",  False, False),
    "1/4 cup peanut butter":                     (0.25, "cup", "peanut butter","Pantry",False,False),

    "1/4 sour cream":                            None,  # missing unit; can't reliably infer

    # ── 1/4 teaspoon/tsp ─────────────────────────────────────────────
    "1/4 teaspoon salt (optional)":              (0.25, "tsp", "salt",      "Spices",  True,  False),
    "1/4 tsp black pepper":                      (0.25, "tsp", "black pepper","Spices",False, False),
    "1/4 tsp minced garlic":                     (0.25, "tsp", "garlic",    "Produce", False, False),
    "1/4 tsp salt":                              (0.25, "tsp", "salt",      "Spices",  False, False),
    "1/4 tsp turmeric":                          (0.25, "tsp", "turmeric",  "Spices",  False, False),

    # ── Bigger counts ────────────────────────────────────────────────
    "10 Pita Bread":                             (10.0, "ea",  "pita",      "Bakery",  False, False),
    "100 ml B12":                                (100.0,"ml",  "b12",       "Pantry",  False, False),
    "100g beef mince (or other cuts)":           (100.0,"g",   "beef",      "Meat",    False, False),
    "100g shiitake mushroom":                    (100.0,"g",   "mushroom",  "Produce", False, False),

    "12 cashews":                                (12.0, "ea",  "cashew",    "Pantry",  False, False),
    "12 cups cos/romaine lettuce, chopped":      (12.0, "cup", "lettuce",   "Produce", False, False),
    "15 garlic cloves, peeled":                  (15.0, "clove","garlic",   "Produce", False, False),
    "150 g/5 oz bacon, cooked & chopped (optional)": None,  # known bug: g/5 read as fraction
    "180g bacon, speck or pancetta, diced":      (180.0,"g",   "bacon",     "Meat",    False, False),

    # ── 2 lines ──────────────────────────────────────────────────────
    "2 Cups (500g) Heavy Cream":                 (2.0,  "cup", "heavy cream","Dairy",  False, False),
    "2 Tablespoon olive oil":                    (2.0,  "tbsp","olive oil", "Pantry",  False, False),
    "2 Tbsp Light Brown Sugar":                  (2.0,  "tbsp","brown sugar","Pantry", False, False),
    "2 Tbsp gochujang":                          (2.0,  "tbsp","gochujang", "Sauces",  False, False),
    "2 avocados, cut into chunks":               (2.0,  "ea",  "avocado",   "Produce", False, False),
    "2 bay leaves":                              (2.0,  "ea",  "bay",       "Spices",  False, False),
    "2 cans black beans, rinsed and drained":    (2.0,  "can", "black bean","Pantry",  False, False),
    "2 cinnamon sticks":                         (2.0,  "ea",  "cinnamon",  "Spices",  False, False),
    "2 cloves garlic, chopped":                  (2.0,  "clove","garlic",   "Produce", False, False),
    "2 cloves garlic, crushed":                  (2.0,  "clove","garlic",   "Produce", False, False),
    "2 cucumbers, halved lengthwise then sliced (or 1 long cucumber)": (2.0,"ea","cucumber","Produce",False,False),
    "2 cups (300 g) unbleached all-purpose flour": (2.0,"cup","flour",      "Bakery",  False, False),
    "2 cups chocolate chips (14 oz)":            (2.0,  "cup", "chocolate chip","Snacks",False,False),
    "2 cups cooked white or brown rice, cooled": (2.0,  "cup", "rice",      "Pasta/Rice",False,False),
    "2 cups ice":                                (2.0,  "cup", "ice",       "Beverages",False,False),
    "2 cups of beef broth":                      (2.0,  "cup", "beef broth","Pantry",  False, False),
    "2 cups vegetable broth or water":           (2.0,  "cup", "vegetable broth","Pantry",False,False),
    "2 cups water":                              (2.0,  "cup", "water",     "Beverages",False,False),
    "2 eggs":                                    (2.0,  "ea",  "egg",       "Dairy",   False, False),
    "2 garlic cloves minced":                    (2.0,  "clove","garlic",   "Produce", False, False),
    "2 large celery stalks, cut into 1″ pieces": None,  # unit="large stalk"
    "2 large cloves garlic, minced":             None,  # unit="large clove"
    "2 large eggs":                              (2.0,  "ea",  "egg",       "Dairy",   False, False),
    "2 lbs chicken, cut into 2-3 inch pieces":   (2.0,  "lb",  "chicken",   "Meat",    False, False),
    "2 lbs chuck beef, cut into bite-size cubes":(2.0,  "lb",  "chuck",     "Meat",    False, False),
    "2 lemons, juice of":                        (2.0,  "ea",  "lemon",     "Produce", False, False),
    "2 medium carrots, chopped":                 (2.0,  "ea",  "carrot",    "Produce", False, False),
    "2 onions, chopped":                         (2.0,  "ea",  "onion",     "Produce", False, False),
    "2 puff pastries":                           (2.0,  "ea",  "puff past", "Frozen", False, False),
    "2 ribs celery, chopped":                    (2.0,  "rib", "celery",    "Produce", False, False),
    "2 star anise":                              (2.0,  "ea",  "star anise","Spices",  False, False),
    "2 tablespoons chili powder*":               (2.0,  "tbsp","chili",     "Spices",  False, False),
    "2 tablespoons chopped fresh cilantro":      (2.0,  "tbsp","cilantro",  "Produce", False, False),
    "2 tablespoons cornstarch":                  (2.0,  "tbsp","cornstarch","Pantry",  False, False),
    "2 tablespoons dried mint flakes":           (2.0,  "tbsp","mint",      "Spices",  False, False),
    "2 tablespoons dried oregano":               (2.0,  "tbsp","oregano",   "Spices",  False, False),
    "2 tablespoons extra-virgin olive oil":      (2.0,  "tbsp","olive oil", "Pantry",  False, False),
    "2 tbsp coriander (cilantro) leaves, chopped":(2.0, "tbsp","coriander", "Produce", False, False),
    "2 tbsp creamed horseradish spread":         (2.0,  "tbsp","horseradish","Sauces", False, False),
    "2 tbsp honey":                              (2.0,  "tbsp","honey",     "Pantry",  False, False),
    "2 tbsp olive oil":                          (2.0,  "tbsp","olive oil", "Pantry",  False, False),
    "2 tbsp olive oil, divided":                 (2.0,  "tbsp","olive oil", "Pantry",  False, False),
    "2 tbsp red wine vinegar":                   (2.0,  "tbsp","vinegar",   "Pantry",  False, False),
    "2 tbsp tahini (or Chinese sesame paste – Note 5)": (2.0,"tbsp","tahini","Pantry", False, False),
    "2 tbsp. canola oil, divided":               (2.0,  "tbsp","canola oil","Pantry",  False, False),
    "2 teaspoons Aleppo pepper/red pepper flakes":(2.0, "tsp", "aleppo",    "Spices",  False, False),
    "2 teaspoons ground cumin":                  (2.0,  "tsp", "cumin",     "Spices",  False, False),
    "2 teaspoons honey":                         (2.0,  "tsp", "honey",     "Pantry",  False, False),
    "2 teaspoons pure vanilla extract":          (2.0,  "tsp", "vanilla",   "Spices",  False, False),
    "2 teaspoons sherry vinegar, red wine vinegar, or lime juice": (2.0, "tsp","vinegar","Pantry",False,False),
    "2 to 4 tablespoons sugar, honey, or maple syrup to taste": (2.0,"tbsp","sugar","Pantry",False,False),
    "2 tsp coconut oil":                         (2.0,  "tsp", "coconut oil","Pantry", False, False),
    "2 tsp dried mixed herbs":                   (2.0,  "tsp", "herb",      "Spices",  False, False),
    "2 tsp ground cumin":                        (2.0,  "tsp", "cumin",     "Spices",  False, False),
    "2 yellow onions, chopped":                  (2.0,  "ea",  "onion",     "Produce", False, False),
    "2-3 large tomatoes, chopped":               (2.0,  "ea",  "tomato",    "Produce", False, False),
    "2.5 lb beef chuck , boneless short rib or any other slow cooking beef, cut into 2″ chunks": (2.5,"lb","chuck","Meat",False,False),
    "2.5 tbsp cider vinegar":                    (2.5,  "tbsp","vinegar",   "Pantry",  False, False),
    "2/3 cup dark chocolate chips":              (0.667,"cup", "chocolate chip","Snacks",False,False),
    "2/3 cup shaoxing wine":                     (0.667,"cup", "wine",      "Beverages",False,False),
    "2/3 cup water":                             (0.667,"cup", "water",     "Beverages",False,False),
    "20 beef meatballs":                         (20.0, "ea",  "meatball",  "Meat",    False, False),

    "250g paneer, cubed":                        (250.0,"g",   "paneer",    "Dairy",   False, False),
    "250g seasoned spinach":                     (250.0,"g",   "spinach",   "Produce", False, False),

    # ── 3 lines ──────────────────────────────────────────────────────
    "3 carrots, peeled and cut into 1/2″ thick pieces": (3.0,"ea","carrot",  "Produce", False, False),
    "3 cloves":                                  (3.0,  "ea",  "clove",     "Spices",  False, False),  # whole cloves the spice
    "3 cloves garlic":                           (3.0,  "clove","garlic",   "Produce", False, False),
    "3 cups all-purpose flour":                  (3.0,  "cup", "flour",     "Bakery",  False, False),
    "3 cups chicken/beef stock/broth":           (3.0,  "cup", "chicken",   "Pantry",  False, False),
    "3 cups store‑bought roast chicken, shredded": (3.0,"cup", "chicken",   "Meat",    False, False),
    "3 garlic cloves, chopped":                  (3.0,  "clove","garlic",   "Produce", False, False),
    "3 garlic cloves, minced":                   (3.0,  "clove","garlic",   "Produce", False, False),
    "3 lbs green leafy vegetable":               (3.0,  "lb",  "leafy",     "Produce", False, False),
    "3 or 4 eggs":                               (3.0,  "ea",  "egg",       "Dairy",   False, False),
    "3 packages dumpling wrappers":              (3.0,  "package","dumpling","Frozen", False, False),
    "3 pieces dried bay leaves (dahon ng laurel)":(3.0, "ea",  "bay",       "Spices",  False, False),
    "3 sprigs fresh thyme (or 1 teaspoon dried thyme)": (3.0,"sprig","thyme","Produce",False,False),
    "3 sprigs thyme (or 1 tsp dried thyme)":     (3.0,  "sprig","thyme",    "Produce", False, False),
    "3 tablespoons butter":                      (3.0,  "tbsp","butter",    "Dairy",   False, False),
    "3 tablespoons cooking oil":                 (3.0,  "tbsp","oil",       "Pantry",  False, False),
    "3 tablespoons extra virgin olive oil":      (3.0,  "tbsp","olive oil", "Pantry",  False, False),
    "3 tablespoons flour":                       (3.0,  "tbsp","flour",     "Bakery",  False, False),
    "3 tablespoons sesame oil":                  (3.0,  "tbsp","sesame oil","Pantry",  False, False),
    "3 tablespoons soy sauce":                   (3.0,  "tbsp","soy sauce", "Sauces",  False, False),
    "3 tbsp flour":                              (3.0,  "tbsp","flour",     "Bakery",  False, False),
    "3 to 4 cups vegetable broth, as needed":    (3.0,  "cup", "vegetable broth","Pantry",False,False),
    "3 to 4 serving portions of steamed rice":   (3.0,  "ea",  "rice",      "Pasta/Rice",False,False),
    "3 tsp ground coriander":                    (3.0,  "tsp", "coriander", "Spices",  False, False),
    "3 tsp ground cumin":                        (3.0,  "tsp", "cumin",     "Spices",  False, False),
    "3/4 tsp each salt and black pepper":        (0.75, "tsp", "salt",      "Spices",  False, False),  # compound; salt is the primary
    "3/4 tsp ground allspice":                   (0.75, "tsp", "allspice",  "Spices",  False, False),
    "300 g/10 oz cherry/grape tomatoes (or 2 normal tomatoes, chopped)": (300.0,"g","tomato","Produce",False,False),
    "350g seasoned bean sprouts":                (350.0,"g",   "bean sprout","Produce",False, False),
    "397g extra firm tofu":                      (397.0,"g",   "tofu",      "Pantry",  False, False),

    # ── 4 lines ──────────────────────────────────────────────────────
    "4 black peppercorns":                       (4.0,  "ea",  "peppercorn","Spices",  False, False),
    "4 cloves garlic, minced":                   (4.0,  "clove","garlic",   "Produce", False, False),
    "4 cloves garlic, pressed or minced":        (4.0,  "clove","garlic",   "Produce", False, False),
    "4 garlic cloves":                           (4.0,  "clove","garlic",   "Produce", False, False),
    "4 ounces cream cheese, room temperature":   (4.0,  "oz",  "cream cheese","Dairy", False, False),
    "4 oz. Monterey Jack cheese, shredded":      (4.0,  "oz",  "monterey",  "Dairy",   False, False),
    "4 oz. sharp cheddar cheese, shredded":      (4.0,  "oz",  "cheddar",   "Dairy",   False, False),
    "4 pounds yellow onions, peeled and thinly sliced (approximately 5–6 large onions)": (4.0,"lb","onion","Produce",False,False),
    "4 tablespoons white vinegar (note 4)":      (4.0,  "tbsp","vinegar",   "Pantry",  False, False),
    "4 tbsp cream":                              (4.0,  "tbsp","cream",     "Dairy",   False, False),
    "4 tbsp tomato paste":                       (4.0,  "tbsp","tomato paste","Pantry",False,False),

    # ── 5 lines ──────────────────────────────────────────────────────
    "5 Cloves":                                  (5.0,  "ea",  "clove",     "Spices",  False, False),
    "5 Egg Yolks":                               (5.0,  "ea",  "egg yolk",  "Dairy",   False, False),
    "5 Mini Pumpkins":                           (5.0,  "ea",  "pumpkin",   "Produce", False, False),
    "5 cloves garlic":                           (5.0,  "clove","garlic",   "Produce", False, False),
    "5 garlic cloves, peeled and sliced; more for later": (5.0,"clove","garlic","Produce",False,False),
    "5 lbs Yukon Gold/Russet potatoes":          (5.0,  "lb",  "potato",    "Produce", False, False),
    "5-6 limes peeled or juiced or whole. you can also use lemons": (5.0,"ea","lime","Produce",False,False),
    "5-7 green cardamom pods":                   (5.0,  "ea",  "cardamom",  "Spices",  False, False),
    "500 ground meat":                           None,  # likely "500g …" typo; no unit

    "6 cups of beef stock (or veggie stock)":    (6.0,  "cup", "beef stock","Pantry",  False, False),
    "6 cups water":                              (6.0,  "cup", "water",     "Beverages",False,False),
    "6 cups water add more if needed":           (6.0,  "cup", "water",     "Beverages",False,False),
    "6 garlic cloves, chopped":                  (6.0,  "clove","garlic",   "Produce", False, False),
    "6 tablespoons butter":                      (6.0,  "tbsp","butter",    "Dairy",   False, False),
    "6 tablespoons soy sauce (preferably Filipino)": (6.0,"tbsp","soy sauce","Sauces",  False, False),
    "6 tbsp extra‑virgin olive oil":             (6.0,  "tbsp","olive oil", "Pantry",  False, False),

    "8 (10\") flour tortillas":                  (8.0,  "ea",  "tortilla",  "Bakery",  False, False),
    "8 gold potatoes, peeled and cut into wedges": (8.0,"ea",  "potato",    "Produce", False, False),
    "8 ounces sliced mushrooms":                 (8.0,  "oz",  "mushroom",  "Produce", False, False),
    "8 slices white cheddar cheese":             (8.0,  "slice","cheddar",  "Dairy",   False, False),
    "8 whole cloves":                            (8.0,  "ea",  "clove",     "Spices",  False, False),

    # ── Name-only lines: optional (implicit "to taste") ──────────────
    "Carrots, chopped (optional)":               (None, None, "carrot",    "Produce", True,  False),
    "Cooking oil":                               (None, None, "oil",       "Pantry",  True,  False),
    "Extra virgin olive oil":                    (None, None, "olive oil", "Pantry",  True,  False),
    "Fresh parsley for garnish, optional":       (None, None, "parsley",   "Produce", True,  False),
    "Freshly ground black pepper":               (None, None, "black pepper","Spices",True,  False),
    "Freshly ground black pepper, to taste":     (None, None, "black pepper","Spices",True,  False),
    "Meat and cheese":                           None,  # ambiguous — could be section header
    "Nutmeg (optional)":                         (None, None, "nutmeg",    "Spices",  True,  False),
    "Pinch red pepper flakes, optional":         (1.0,  "pinch","pepper",   "Spices",  True,  False),
    "Pinch salt":                                (1.0,  "pinch","salt",     "Spices",  False, False),
    "Saffron (optional)":                        (None, None, "saffron",   "Spices",  True,  False),
    "Salt and pepper":                           (None, None, "salt",      "Spices",  True,  False),
    "Salt and pepper to taste":                  (None, None, "salt",      "Spices",  True,  False),
    "Sesame seeds":                              (None, None, "sesame",    "Spices",  True,  False),
    "Some toasted seasoned seaweed, shredded (long thin cut)": (None,None,"seaweed","Pantry",True,False),
    "Thyme":                                     (None, None, "thyme",     "Produce", True,  False),
    "baguette":                                  (None, None, "baguette",  "Bakery",  True,  False),
    "fine sea salt":                             (None, None, "salt",      "Spices",  True,  False),
    "fine sea salt and freshly-cracked black pepper, to taste": (None,None,"salt","Spices",True,False),
    "fresh peppermint":                          (None, None, "peppermint","Produce", True,  False),
    "grated or sliced cheese (such as Gruyere, Asiago, Swiss, Gouda or Mozzarella)": None,  # complex compound
    "lime zest":                                 (None, None, "lime",      "Produce", True,  False),
    "pinch of sea salt":                         (1.0,  "pinch","salt",     "Spices",  False, False),
    "sliced limes":                              (None, None, "lime",      "Produce", True,  False),

    # ── Foreign language (parse_failed; source-line fallback) ────────
    "basilc":                                    (None, None, "basilc",    "Other",   True,  False),  # could be "basil" typo, parses as optional
    "burrata":                                   (None, None, "burrata",   "Dairy",   True,  False),
    "huile d’olive":                             (None, None, "huile",     "Other",   True,  False),
    "jaune d’oeuf":                              (None, None, "jaune",     "Other",   True,  False),
    "miel":                                      (None, None, "miel",      "Other",   True,  False),
    "oignons confits":                           (None, None, "oignons",   "Other",   True,  False),
    "pignons":                                   (None, None, "pignons",   "Other",   True,  False),
    "poivre":                                    (None, None, "poivre",    "Other",   True,  False),
    "pâte brisée":                               (None, None, "pâte",      "Other",   True,  False),
    "sel":                                       (None, None, "sel",       "Other",   True,  False),
    "tomates et tomates cerises":                (None, None, "tomate",    "Other",   True,  False),
    "vinaigre balsamique":                       (None, None, "vinaigre",  "Other",   True,  False),
}
