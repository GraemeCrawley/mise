"""Find ingredient mentions in a method step so cook mode can show the
quantity inline ("add eggs (4)") — no scrolling back to the ingredient list.

Pure + deterministic: token matching only, no LLM. Conservative by design —
a missing bracket is harmless, a wrong one is not, so anything ambiguous
(two oils and the step just says "oil") is left unannotated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ]+")

def _stem(word: str) -> str:
    w = word.lower()
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("oes", "ses", "xes", "ches", "shes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us")):
        return w[:-1]
    return w


# Words that can't stand in for an ingredient on their own — descriptors
# ("fresh", "ground") and generic heads ("the sauce" is usually the mixed
# sauce, not the soy sauce). They only match as part of a longer phrase.
_WEAK = frozenset(_stem(w) for w in """
    a an and or of the to for with in on into from fresh freshly large medium small
    ground dried dry chopped minced sliced diced grated shredded crushed whole
    boneless skinless cooked uncooked raw steamed boiled roasted toasted frozen
    canned black white red green yellow brown dark light extra virgin low sodium
    unsalted salted sweet hot cold warm plain pure fine coarse thick thin lean
    baby mini ripe firm soft hard more less taste optional pre marinated
    all purpose piece pieces any other some
    sauce mixture mix dressing marinade seasoning topping filling glaze batter
    dough spice powder paste flakes seeds leaves
""".split())

# A quantity already written just before the mention ("2 eggs",
# "1 cup of rice") makes the bracket redundant.
_QTY_BEFORE_RE = re.compile(r"[\d¼½¾⅓⅔⅛][\d/.,-]*\s*(?:[A-Za-z.]+\s+)?(?:(?:of|each)\s+)?$")

# Food "head" nouns: "<partial> oil" is an oil, not the partial's ingredient.
_HEADS = frozenset(_stem(w) for w in """
    oil sauce paste powder stock broth milk cream water juice vinegar flakes
    seeds zest butter flour sugar syrup
""".split())



# Same thing, different words: step phrase -> ingredient name.
_SYNONYMS = {
    tuple(_stem(w) for w in a.split()): tuple(_stem(w) for w in b.split())
    for a, b in [
        ("crushed red pepper", "red pepper flakes"),
        ("chili flakes", "red pepper flakes"),
        ("scallions", "green onions"),
        ("green onions", "scallions"),
        ("cilantro", "coriander"),
    ]
}


@dataclass(frozen=True)
class Mention:
    """An ingredient mention ending at `end` in the step text. `parts` has
    one (qty, unit) per same-named ingredient row (salt in two sections)."""
    end: int
    parts: tuple[tuple[float, str], ...]
    preps: tuple[str, ...] = ()  # "finely diced" — distinct, in row order
    labels: tuple[str, ...] = ()  # per part, when one word covers several ("kidney", "pinto")
    keys: tuple[tuple[str, ...], ...] = ()  # which ingredients this covers


def _name_text(name: str) -> str:
    # Drop parentheticals / trailing prep so "tofu (extra firm), cubed" → tofu,
    # but keep going past leading descriptors: "boneless, skinless chicken".
    parts = re.sub(r"\(.*?\)", " ", name).split(",")
    out = []
    for part in parts:
        out.append(part)
        if any(_stem(t) not in _WEAK for t in _TOKEN_RE.findall(part)):
            break
    return ",".join(out)


def _name_stems(name: str) -> tuple[str, ...]:
    return tuple(_stem(t) for t in _TOKEN_RE.findall(_name_text(name)))


def _head(stems: tuple[str, ...]) -> str:
    """Main noun of a name: its last word ("tomato sauce" is a sauce)."""
    return stems[-1]


def _clean_prep(prep: str | None) -> str:
    p = (prep or "").strip().strip(",").strip()
    if p.startswith("(") and p.endswith(")"):
        p = p[1:-1].strip()
    return p


def find_mentions(text: str, ingredients: list[tuple], *, keep_written: bool = False) -> list[Mention]:
    """`ingredients` is (display_name, qty, unit[, prep_note]) per row.
    Returns the first mention of each ingredient in `text`, in order."""
    # Same-named rows collapse into one entry carrying every quantity.
    entries: dict[tuple[str, ...], list[tuple[float, str]]] = {}
    preps: dict[tuple[str, ...], list[str]] = {}
    words: dict[tuple[str, ...], list[str]] = {}
    for row in ingredients:
        name, qty, unit = row[:3]
        stems = _name_stems(name or "")
        if not stems or not qty:
            continue
        entries.setdefault(stems, []).append((float(qty), unit or ""))
        words.setdefault(stems, _TOKEN_RE.findall(_name_text(name)))
        prep = _clean_prep(row[3] if len(row) > 3 else None)
        if prep and prep not in preps.setdefault(stems, []):
            preps[stems].append(prep)
        preps.setdefault(stems, [])

    # phrase → the entries it could mean. A full name always wins its
    # phrase; a partial ("oil" for "olive oil") may be shared by several.
    partial: dict[tuple[str, ...], list[tuple[str, ...]]] = {}
    for stems in entries:
        for i in range(len(stems)):
            for j in range(i + 1, len(stems) + 1):
                sub = stems[i:j]
                if sub == stems or sub in entries:
                    continue
                if len(sub) == 1 and (sub[0] in _WEAK or len(sub[0]) < 3):
                    continue
                if all(t in _WEAK for t in sub):
                    continue
                if stems not in partial.setdefault(sub, []):
                    partial[sub].append(stems)
    phrases: dict[tuple[str, ...], tuple[tuple[str, ...], ...]] = {s: (s,) for s in entries}
    for sub, owners in partial.items():
        phrases[sub] = tuple(owners)
    for syn, target in _SYNONYMS.items():
        if target in entries:
            phrases[syn] = (target,)
    if not phrases:
        return []
    longest = max(len(p) for p in phrases)

    tokens = [(m.start(), m.end(), _stem(m.group())) for m in _TOKEN_RE.finditer(text)]
    spans: list[tuple[int, int, tuple[str, ...], tuple[tuple[str, ...], ...]]] = []
    i = 0
    while i < len(tokens):
        for n in range(min(longest, len(tokens) - i), 0, -1):
            phrase = tuple(t[2] for t in tokens[i:i + n])
            owners = phrases.get(phrase)
            if owners is None:
                continue
            is_partial = owners != (phrase,)
            # "coconut oil" when the ingredient is coconut milk: the next
            # word names a different thing, so this isn't our ingredient.
            if is_partial and i + n < len(tokens) and tokens[i + n][2] in _HEADS \
                    and all(tokens[i + n][2] not in o for o in owners) \
                    and text[tokens[i + n - 1][1]:tokens[i + n][0]] == " ":
                pass
            # Slash-joined words are appliance settings / options
            # ("the Chili/Beans setting"), not the ingredient.
            elif is_partial and (text[tokens[i + n - 1][1]:tokens[i + n - 1][1] + 1] == "/"
                                 or text[tokens[i][0] - 1:tokens[i][0]] == "/"):
                pass
            else:
                spans.append((i, n, phrase, owners))
            i += n - 1
            break
        i += 1

    # Fallback: an ingredient named outright elsewhere in the step isn't
    # what a shared word means ("the chicken" ≠ the chicken stock).
    claimed = {o[0] for _, _, _, o in spans if len(o) == 1}
    out: list[Mention] = []
    seen: set[tuple[str, ...]] = set()
    for i, n, phrase, owners in spans:
        cands = owners
        if len(owners) > 1:
            # A shared word means the ingredients it's the main noun of:
            # "rice" is the short-grain rice, not the rice vinegar.
            cands = tuple(o for o in owners if _head(o) == phrase[-1]) \
                or tuple(o for o in owners if o not in claimed)
        if not cands:
            continue
        last = text[tokens[i + n - 1][0]:tokens[i + n - 1][1]].lower()
        # Several left: only a plural ("beans") means all of them, and only
        # when they're measured alike (not "peppers" = bell + black pepper).
        if len(cands) > 1 and (last == tokens[i + n - 1][2]
                               or len({u for c in cands for _, u in entries[c]}) > 1):
            continue
        if any(c in seen for c in cands) or (not keep_written and _QTY_BEFORE_RE.search(text[:tokens[i][0]])):
            continue
        seen.update(cands)
        parts = tuple(p for c in cands for p in entries[c])
        labels: tuple[str, ...] = ()
        if len(cands) > 1:
            labels = tuple(
                " ".join(w for w in words[c] if _stem(w) not in phrase and _stem(w) not in _WEAK) or " ".join(words[c])
                for c in cands for _ in entries[c]
            )
        mention_preps = tuple(dict.fromkeys(p for c in cands for p in preps[c]))
        out.append(Mention(end=tokens[i + n - 1][1], parts=parts, preps=mention_preps, labels=labels, keys=cands))
    return out


def unmentioned(steps: list[str], ingredients: list[tuple]) -> list[str]:
    """Ingredients (with an amount) that no step names in a way cook mode
    can match — the review check. A step that writes the amount itself
    ("1/2 cup of coconut milk") counts as covering it."""
    covered = {k for t in steps for m in find_mentions(t, ingredients, keep_written=True) for k in m.keys}
    out: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for row in ingredients:
        stems = _name_stems(row[0] or "")
        if stems and row[1] and stems not in covered and stems not in seen:
            seen.add(stems)
            out.append(row[0])
    return out
