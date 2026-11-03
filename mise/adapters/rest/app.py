"""Consumer app — the household's everyday surface.

Mobile-first Fusion design (Tailwind v3 + Fraunces + warm cream). Built
fresh against the v2 mockup direction; this file does NOT inherit from
``preview.py``. The operator preview at ``/preview/`` stays unchanged
and continues to serve data-hygiene workflows (review queue, parse
trace, inline edit, etc.).

Routes:
    GET  /app/                    library — list of reviewed recipes
    GET  /app/recipe/{id}          recipe detail + add-to-plan
    GET  /app/plan                 this week's planned meals
    GET  /app/shop                 shop list for this week (+ extras)
    POST /app/extras               create an ad-hoc shopping item
    PATCH /app/extras/{id}         toggle checked / rename / category
    DELETE /app/extras/{id}        remove
    POST /app/feedback             reuse Feedback model — same backend

Sharing with /preview/:
    - Same SQLModel tables (Recipe, CookingSession, SlotAssignment, …)
    - Same services (planner, shopping_aggregator with new
      aggregate_for_sessions)
    - Same Feedback inbox at /preview/feedback (you triage there)
"""

from __future__ import annotations

import html
import json as _json
import os
import re
from collections import Counter, defaultdict
from datetime import UTC, date as _Date, datetime, timedelta as _Td
from pathlib import Path as _Path
from urllib.parse import urlparse

import pint as _pint
_UREG = _pint.UnitRegistry()

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from mise.db import get_session
from mise.models import (
    CookingSession,
    PlanNote,
    Feedback,
    Household,
    Ingredient,
    IngredientSection,
    Recipe,
    ShopCheck,
    ShopExtra,
    SlotAssignment,
    Step,
    StepSection,
)
from mise.services import planner as planner_service
from mise.services.llm_verify import VALID_CATEGORIES
from mise.services.step_annotator import find_mentions
from mise.services.shopping_aggregator import (
    ShoppingItem,
    aggregate_for_sessions as _aggregate_for_sessions,
)

PREFIX = os.environ.get("MISE_URL_PREFIX", "")
router = APIRouter(tags=["app"])

# Grocery-aisle order for the draft category picker. Walks the store roughly
# perimeter-first; any category in VALID_CATEGORIES we forgot to order is
# appended so the list can never silently drop a choice.
_CATEGORY_ORDER = (
    "Produce", "Meat", "Dairy", "Bakery", "Deli", "Pantry",
    "Pasta/Rice", "Sauces", "Spices", "Frozen", "Snacks", "Beverages", "Other",
)
CATEGORY_CHOICES = (
    *_CATEGORY_ORDER,
    *sorted(VALID_CATEGORIES - set(_CATEGORY_ORDER)),
)

# Words that carry no identity (units, counts, prep descriptors) — stripped
# before measuring how much of a source line the parsed name actually covers.
_DIVERGENCE_NOISE = {
    "cup", "cups", "tbsp", "tsp", "tablespoon", "tablespoons", "teaspoon",
    "teaspoons", "ounce", "ounces", "pound", "pounds", "gram", "grams",
    "kilogram", "milliliter", "liter", "litre", "can", "cans", "jar", "jars",
    "package", "packages", "packet", "bottle", "bottles", "pinch", "dash",
    "fluid", "large", "small", "medium", "fresh", "freshly", "chopped",
    "diced", "minced", "sliced", "ground", "the", "and", "for", "into",
    "with", "plus", "about", "approximately", "such", "your", "any",
}
_WORD_RE = re.compile(r"[a-z]+")
_PAREN_RE = re.compile(r"\([^()]*\)")
# Where the ingredient's "head" ends — the parser dropping the head noun is
# the mis-parse we care about ('Albacore Tuna in olive oil' → 'olive oil').
_HEAD_SEP_RE = re.compile(r",|\b(?:in|with|for|on)\b")


def _norm_word(w: str) -> str:
    return w[:-1] if w.endswith("s") and len(w) > 3 else w


def _parse_divergence(
    source_line: str | None,
    display_name: str | None,
    prep_note: str | None = None,
) -> bool:
    """True when the parsed name shares NONE of the source's 'head' words —
    the words before the first preposition/comma, where the actual ingredient
    usually lives. Catches the parser keeping a trailing modifier and dropping
    the head noun ('… Albacore Tuna in Extra Virgin Olive Oil' → 'olive oil').

    Parentheticals and prep-note words are stripped first (descriptors the
    parser legitimately relocates); singular/plural is normalised so
    'eggs' → 'egg' still matches."""
    def words(s: str | None) -> set[str]:
        return {
            _norm_word(w) for w in _WORD_RE.findall((s or "").lower())
            if len(w) >= 3 and w not in _DIVERGENCE_NOISE
        }
    stripped = source_line or ""
    prev = None
    while prev != stripped:  # collapse nested parentheticals
        prev = stripped
        stripped = _PAREN_RE.sub(" ", stripped)
    stripped = stripped.lower()
    m = _HEAD_SEP_RE.search(stripped)
    head = words(stripped[: m.start()] if m else stripped) - words(prep_note)
    parsed = words(display_name)
    if not head or not parsed:
        return False
    return head.isdisjoint(parsed)

_h = html.escape


def _safe_href(url: str | None) -> str | None:
    # html.escape doesn't block javascript: URIs; only render http(s) links.
    if not url:
        return None
    scheme = urlparse(url).scheme.lower()
    return url if scheme in ("http", "https") else None


# =============================================================================
# Visual tokens — keep in sync with /preview/'s _BASE_CSS where helpful, but
# this file owns the consumer app's look. /preview/ keeps its operator chrome.
# =============================================================================

_THUMB_COLORS = [
    "matte-amber",
    "matte-emerald",
    "matte-rose",
    "matte-violet",
    "matte-orange",
    "matte-sky",
]

# Keyword → emoji map. First match wins; specific terms first.
_KW_EMOJI: list[tuple[str, str]] = [
    ("chimichurri", "🥗"), ("bibimbap", "🍲"), ("matar paneer", "🍛"),
    ("french onion", "🧅"), ("arayes", "🥩"), ("mafe", "🥜"),
    ("guinness", "🥘"), ("mulled wine", "🍷"),
    ("dumpling", "🥟"), ("sandwich", "🥪"), ("pizza", "🍕"),
    ("pasta", "🍝"), ("noodle", "🍜"), ("curry", "🍛"),
    ("burger", "🍔"), ("taco", "🌮"), ("wrap", "🌯"), ("burrito", "🌯"),
    ("salad", "🥗"), ("soup", "🍲"), ("stew", "🥘"), ("chili", "🌶️"),
    ("risotto", "🍚"), ("rice", "🍚"), ("paella", "🥘"),
    ("sushi", "🍣"), ("ramen", "🍜"), ("pho", "🍜"),
    ("chicken", "🍗"), ("beef", "🥩"), ("pork", "🥓"),
    ("fish", "🐟"), ("shrimp", "🦐"), ("salmon", "🐟"),
    ("egg", "🍳"), ("omelet", "🍳"), ("frittata", "🍳"),
    ("pancake", "🥞"), ("waffle", "🧇"), ("bread", "🥖"),
    ("cake", "🍰"), ("cookie", "🍪"), ("pie", "🥧"),
    ("wine", "🍷"), ("cocktail", "🍸"), ("smoothie", "🥤"),
    ("tofu", "🥡"), ("cauliflower", "🥦"), ("brussel", "🥬"),
    ("potato", "🥔"), ("sheppard", "🥧"), ("shepherd", "🥧"),
    ("squash", "🎃"), ("pumpkin", "🎃"),
]


def _recipe_emoji(name: str | None, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    n = (name or "").lower()
    for kw, em in _KW_EMOJI:
        if kw in n:
            return em
    return "🍽️"


def _recipe_color(recipe_id: int | None) -> str:
    return _THUMB_COLORS[(recipe_id or 0) % len(_THUMB_COLORS)]


def _fmt_qty(q: float | int | None) -> str:
    """Render a quantity nicely — fractions for halves/quarters/thirds,
    int when integral, otherwise short decimal."""
    if q is None:
        return ""
    qf = float(q)
    if abs(qf - round(qf)) < 0.01:
        return str(int(round(qf)))
    glyph = {0.25: "¼", 0.5: "½", 0.75: "¾", 0.333: "⅓", 0.667: "⅔", 0.125: "⅛"}
    integer = int(qf)
    frac = qf - integer
    for f, g in glyph.items():
        if abs(frac - f) < 0.03:
            return (f"{integer}" if integer else "") + g
    return f"{qf:g}"


def _qty_text(q, unit: str | None) -> str:
    """'2 cup', or just '2' for counts ("ea" is storage, not something to show)."""
    u = "" if (unit or "") == "ea" else (unit or "")
    return f"{_fmt_qty(q)} {u}".strip()


def _step_html(text: str, ingredients: list[tuple]) -> str:
    """Escaped step text with a hidden "(qty unit, prep)" after each ingredient
    mention; cook mode reveals them. The inner spans are `.ing-qty` so the
    serving scaler rescales them along with the ingredient list."""
    out, pos = [], 0
    for m in find_mentions(text, ingredients):
        labels = m.labels or ("",) * len(m.parts)
        parts = " + ".join(
            # Counts read better bare: "eggs (4)", not "eggs (4 ea)".
            f"<span class='ing-qty' data-qty='{q}' data-unit='{_h(u)}'>{_fmt_qty(q)}{' ' + _h(u) if u else ''}</span>"
            + (f" {_h(lab)}" if lab else "")
            for (q, u), lab in zip(((q, "" if u == "ea" else u) for q, u in m.parts), labels)
        )
        prep = f", {_h(', '.join(m.preps))}" if m.preps else ""
        out.append(f"{_h(text[pos:m.end])}<span class='step-qty'> ({parts}{prep})</span>")
        pos = m.end
    out.append(_h(text[pos:]))
    return "".join(out)


# --- shop-list unit conversion (tap qty to cycle) ---------------------------

# Aliases — what Mise stores vs what Pint understands. lb / oz are
# ambiguous in Pint (force-vs-mass); we want cooking-mass semantics.
_PINT_UNIT_ALIAS: dict[str, str] = {
    "tbsp": "tablespoon",
    "tsp":  "teaspoon",
    "cup":  "cup",
    "fl oz": "fluid_ounce",
    "fl_oz": "fluid_ounce",
    "lb":   "pound",
    "lbs":  "pound",
    "oz":   "ounce",
    "g":    "gram",
    "kg":   "kilogram",
    "ml":   "milliliter",
    "l":    "liter",
}
_VOLUME_GROUP = ["cup", "tbsp", "tsp", "fl oz", "ml", "l"]
_MASS_GROUP   = ["lb", "oz", "g", "kg"]


def _round_for_display(value: float) -> str:
    """Round a converted magnitude to a sensible-looking number."""
    if value >= 100:
        return str(int(round(value)))
    if value >= 10:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    if value >= 1:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _unit_group(unit: str) -> list[str] | None:
    u = unit.lower().strip()
    if u in _VOLUME_GROUP: return _VOLUME_GROUP
    if u in _MASS_GROUP:   return _MASS_GROUP
    return None


def _alt_qty_displays(qty: float | int | None, unit: str | None) -> list[str]:
    """Alternative '{n} {unit}' strings for the given quantity, in
    cycle order. Returns [] when the unit isn't a convertible mass /
    volume (e.g. 'pcs', 'each', '')."""
    if qty is None or not unit:
        return []
    group = _unit_group(unit)
    if not group:
        return []
    pint_src = _PINT_UNIT_ALIAS.get(unit.lower().strip())
    if not pint_src:
        return []
    try:
        q = _UREG.Quantity(float(qty), pint_src)
    except Exception:
        return []
    out: list[str] = []
    for tgt in group:
        if tgt == unit.lower().strip():
            continue
        pint_tgt = _PINT_UNIT_ALIAS.get(tgt)
        if not pint_tgt:
            continue
        try:
            converted = q.to(pint_tgt).magnitude
            if converted <= 0:
                continue
            out.append(f"{_round_for_display(converted)} {tgt}")
        except Exception:
            continue
    return out


# =============================================================================
# Shell — every page wraps in this. Mobile-first; bottom tab bar fixed.
# =============================================================================

_APP_CSS = f"""
<link rel="stylesheet" href="{PREFIX}/static/tw.css?v=6">
<link rel="manifest" href="{PREFIX}/static/manifest.json">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="theme-color" content="#FAF7F2">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&display=swap" rel="stylesheet">
<style>
  html, body {{ overflow-x: hidden; max-width: 100vw; }}
  body {{
    font-family: 'Inter', system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
    letter-spacing: -0.012em;
    background: #FAF7F2;
    color: #1c1917;
    padding-bottom: calc(8rem + env(safe-area-inset-bottom));
    overflow-wrap: anywhere;
  }}
  input, select, textarea {{ font-size: 16px; }}
  .serif {{ font-family: 'Fraunces', Georgia, serif; }}
  .card {{
    background: white;
    border-radius: 1.25rem;
    box-shadow: 0 1px 2px rgba(120,80,40,0.04), 0 8px 24px -12px rgba(120,80,40,0.10);
    border: 1px solid #f5f0e8;
    /* Clip inner content (slot rows, swipe action buttons, tap-highlight
       colours) to the rounded shape. Box-shadow renders externally so
       it's unaffected. Solves: (a) square inner corners inside the
       rounded card; (b) swipe-action bg pixels bleeding past the row's
       edge during a swipe. */
    overflow: hidden;
  }}
  .matte-amber   {{ background: #FDE68A; color: #78350F; }}
  .matte-emerald {{ background: #BBF7D0; color: #064E3B; }}
  .matte-rose    {{ background: #FECDD3; color: #881337; }}
  .matte-violet  {{ background: #DDD6FE; color: #4C1D95; }}
  .matte-orange  {{ background: #FED7AA; color: #7C2D12; }}
  .matte-sky     {{ background: #BAE6FD; color: #0C4A6E; }}
  /* Fade-up stagger */
  @keyframes fadeUp {{
    from {{ opacity: 0; transform: translateY(12px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
  }}
  .stagger > * {{
    /* Default opacity:1 is a safety net: if the fadeUp animation
       fails (some iOS Safari + SW timing edge cases left the whole
       main column invisible), the content is still readable. The
       animation still runs from the keyframe's opacity:0 start, but
       a failure no longer hides everything. */
    opacity: 1;
    animation: fadeUp 0.5s cubic-bezier(0.16, 1, 0.3, 1) both;
  }}
  .stagger > *:nth-child(1)  {{ animation-delay: 0.04s; }}
  .stagger > *:nth-child(2)  {{ animation-delay: 0.10s; }}
  .stagger > *:nth-child(3)  {{ animation-delay: 0.16s; }}
  .stagger > *:nth-child(4)  {{ animation-delay: 0.22s; }}
  .stagger > *:nth-child(5)  {{ animation-delay: 0.28s; }}
  .stagger > *:nth-child(6)  {{ animation-delay: 0.34s; }}
  .stagger > *:nth-child(7)  {{ animation-delay: 0.40s; }}
  .stagger > *:nth-child(8)  {{ animation-delay: 0.46s; }}
  .stagger > *:nth-child(9)  {{ animation-delay: 0.52s; }}
  .stagger > *:nth-child(n+10) {{ animation-delay: 0.58s; }}
  @media (prefers-reduced-motion: reduce) {{
    .stagger > * {{ opacity: 1; animation: none; transform: none; }}
  }}
  /* Bottom tab bar — floating pill. Lifted clear of the Safari URL
     bar + iOS gesture zone in browser mode; sits much closer to the
     edge once installed as a PWA (display-mode: standalone) where
     there's no chrome to dodge. */
  .tab-bar {{
    position: fixed; z-index: 30;
    left: 0.75rem; right: 0.75rem;
    bottom: calc(2rem + env(safe-area-inset-bottom));
    background: rgba(250,247,242,0.96); backdrop-filter: blur(16px);
    border: 1px solid #e7e5e4;
    border-radius: 1.5rem;
    box-shadow: 0 12px 32px -8px rgba(120,80,40,0.18);
    padding: 0.55rem 0.5rem;
    display: flex; justify-content: space-around; gap: 0.25rem;
  }}
  .tab-bar a {{ flex: 1; text-align: center; padding: 0.55rem 0.3rem; font-size: 0.85rem; border-radius: 1rem; color: #78716c; }}
  .tab-bar a:hover {{ background: #f5f5f4; color: #1c1917; }}
  .tab-bar a[data-active="1"] {{ font-weight: 600; }}
  .tab-bar a[data-active="1"][data-tab="library"] {{ background: #FEF3C7; color: #78350F; }}
  .tab-bar a[data-active="1"][data-tab="plan"]    {{ background: #EDE9FE; color: #4C1D95; }}
  .tab-bar a[data-active="1"][data-tab="shop"]    {{ background: #FFE4E6; color: #881337; }}
  /* Feedback FAB */
  .fb-fab {{
    position: fixed; left: 1rem; z-index: 90;
    bottom: calc(7rem + env(safe-area-inset-bottom));
    background: #7c3aed; color: white; border: none; border-radius: 9999px;
    width: 3rem; height: 3rem; font-size: 1.25rem;
    box-shadow: 0 8px 24px -8px rgba(124,58,237,0.6);
    display: flex; align-items: center; justify-content: center;
  }}
  /* PWA standalone: no browser chrome at the bottom, so the floating
     bar can hug the safe-area inset. Same for the FAB + body padding. */
  @media (display-mode: standalone) {{
    body {{ padding-bottom: calc(5rem + env(safe-area-inset-bottom)); }}
    .tab-bar {{ bottom: calc(0.4rem + env(safe-area-inset-bottom)); }}
    .fb-fab  {{ bottom: calc(5rem + env(safe-area-inset-bottom)); }}
  }}
  /* ----- Tap feedback ----------------------------------------------
     iOS doesn't fire :hover from touch, so we need explicit :active
     states to confirm a press. Every interactive surface flashes
     amber (or darkens, for primary stone buttons) for the duration
     of the touch. Tap-highlight-color is a backstop for cases I
     don't cover explicitly.
  */
  html {{ -webkit-tap-highlight-color: rgba(252, 191, 73, 0.22); }}
  /* Suppress iOS Copy/Look Up/Translate popup on long-press over
     any interactive surface. Don't apply to text bodies (recipe
     steps, ingredient names, feedback text) — those should still be
     selectable in case the user wants to copy a step or ingredient. */
  button, label, a.card, a[data-tab],
  .slot-add, .swap-pick, .slot-actions, .move-cell, .nav-pill,
  .swipe-row-inner, .swipe-row-inner *,
  .tab-bar a, .fb-fab, .ps-close {{
    -webkit-touch-callout: none;
    -webkit-user-select: none;
    user-select: none;
  }}
  .slot-add, .swap-pick, .slot-actions, .move-cell {{ transition: background 0.12s, transform 0.08s; }}
  /* transform-origin left center on the row keeps the row's left edge
     fixed during the press, so the +-thumb stays aligned with the
     emoji thumbnails on rows above/below. Center-origin shifted the
     thumb ~2px right relative to its neighbours. */
  .slot-add:active {{ background: #FDE68A; transform: scale(0.985); transform-origin: left center; }}
  .slot-add:active .drop-thumb {{
    background: #F59E0B;
    border-style: solid; border-color: #B45309;
    color: white;
    transform: scale(0.92);
  }}
  .drop-thumb {{ transition: background 0.1s, border-color 0.1s, color 0.1s, transform 0.1s; }}
  .swap-pick:active {{ background: #FEF3C7; }}
  .slot-actions:active {{ background: #f5f5f4; }}
  .move-cell:active {{ background: #FEF3C7; border-color: #f59e0b; }}
  /* Primary CTAs (Add to plan, Build shopping list, Send feedback) —
     darken instead of flash since they're already dark stone. */
  .bg-stone-900:active {{ background: #44403c !important; }}
  /* Sheet action rows (View recipe / Swap / Delete) — visible warm
     flash on press. .card .hover:shadow-lg already covers desktop. */
  .ps-sheet .card:active {{ background: #FEF3C7; }}
  /* Recipe cards in library + library row tap feedback. */
  a.card:active {{ box-shadow: 0 1px 2px rgba(120,80,40,0.04), 0 8px 24px -12px rgba(120,80,40,0.10), inset 0 0 0 1px #f59e0b; }}
  /* Bottom tab pills — quick feedback when switching tabs. */
  .tab-bar a:active {{ background: #f5f5f4; }}
  .tab-bar a[data-active="1"]:active {{ filter: brightness(0.94); }}
</style>
"""

_DIALOGS_HTML = """
<!-- Global confirm modal + toast notifier. Lifted into the shell so
     every page can call window.askConfirm / window.notify and get the
     same Mise-styled treatment instead of the native iOS confirm/
     alert dialogs (which clash with the typography + dark-mode
     toggle on the device). -->
<div id="confirm-modal" class="cf-modal" role="alertdialog" aria-modal="true">
  <div class="cf-sheet">
    <p id="cf-msg" class="serif text-lg font-medium tracking-tight mb-1">Are you sure?</p>
    <p id="cf-sub" class="text-sm text-stone-500 mb-4"></p>
    <div class="flex gap-2">
      <button type="button" id="cf-cancel" class="cf-btn cf-btn-cancel">Cancel</button>
      <button type="button" id="cf-ok" class="cf-btn cf-btn-danger">OK</button>
    </div>
  </div>
</div>
<style>
  .cf-modal { display: none; position: fixed; inset: 0; z-index: 200; background: rgba(28,25,23,0.45); align-items: center; justify-content: center; padding: 1.5rem; }
  .cf-modal.is-open { display: flex; }
  .cf-sheet { background: white; border-radius: 1rem; width: 100%; max-width: 22rem; padding: 1.25rem; box-shadow: 0 16px 48px -8px rgba(0,0,0,0.25); }
  .cf-btn { flex: 1; padding: 0.75rem; border-radius: 0.625rem; font-weight: 600; font-size: 14px; border: 0; }
  .cf-btn-cancel { background: #f5f5f4; color: #44403c; }
  .cf-btn-cancel:active { background: #e7e5e4; }
  .cf-btn-danger { background: #e11d48; color: white; }
  .cf-btn-danger:active { background: #be123c; }
  .cf-btn-primary { background: #1c1917; color: #FAF7F2; }
  .cf-btn-primary:active { background: #44403c; }
  /* Toast stack — appears above the tab bar, fades in and out. */
  #toast-stack {
    position: fixed; left: 50%;
    bottom: calc(6rem + env(safe-area-inset-bottom));
    transform: translateX(-50%);
    z-index: 300; display: flex; flex-direction: column; gap: 0.5rem;
    pointer-events: none; max-width: 92vw;
  }
  .toast {
    background: #1c1917; color: #FAF7F2;
    padding: 0.625rem 1rem; border-radius: 9999px;
    font-size: 13px; font-weight: 500;
    box-shadow: 0 8px 24px -4px rgba(0,0,0,0.3);
    opacity: 0; transition: opacity 0.2s, transform 0.2s;
    transform: translateY(0.5rem);
    text-align: center; pointer-events: auto;
  }
  .toast.is-shown { opacity: 1; transform: translateY(0); }
  .toast.tone-error   { background: #7f1d1d; }
  .toast.tone-success { background: #065f46; }
</style>
<script>
  // window.askConfirm({message, sub?, okLabel?, cancelLabel?, danger?})
  // Returns a Promise<boolean>. Replaces window.confirm — same intent,
  // matched typography + colours.
  window.askConfirm = function(opts) {
    opts = opts || {};
    const modal  = document.getElementById('confirm-modal');
    const msg    = document.getElementById('cf-msg');
    const sub    = document.getElementById('cf-sub');
    const cancel = document.getElementById('cf-cancel');
    const ok     = document.getElementById('cf-ok');
    msg.textContent = opts.message || 'Are you sure?';
    sub.textContent = opts.sub || '';
    sub.style.display = opts.sub ? '' : 'none';
    ok.textContent  = opts.okLabel || 'OK';
    cancel.textContent = opts.cancelLabel || 'Cancel';
    ok.classList.toggle('cf-btn-danger',  opts.danger !== false);
    ok.classList.toggle('cf-btn-primary', opts.danger === false);
    return new Promise(resolve => {
      function close(v) {
        modal.classList.remove('is-open');
        ok.removeEventListener('click', onOk);
        cancel.removeEventListener('click', onCancel);
        modal.removeEventListener('click', onBackdrop);
        resolve(v);
      }
      function onOk()      { close(true); }
      function onCancel()  { close(false); }
      function onBackdrop(e) { if (e.target === modal) close(false); }
      ok.addEventListener('click', onOk);
      cancel.addEventListener('click', onCancel);
      modal.addEventListener('click', onBackdrop);
      modal.classList.add('is-open');
    });
  };

  // window.notify(message, {tone, duration}) — toast replacement for
  // alert(). Non-blocking, auto-dismisses.
  window.notify = function(message, opts) {
    opts = opts || {};
    const tone = opts.tone || 'info';
    const duration = opts.duration || (tone === 'error' ? 3600 : 2200);
    let stack = document.getElementById('toast-stack');
    if (!stack) {
      stack = document.createElement('div');
      stack.id = 'toast-stack';
      document.body.appendChild(stack);
    }
    const t = document.createElement('div');
    t.className = 'toast tone-' + tone;
    t.textContent = message;
    stack.appendChild(t);
    requestAnimationFrame(() => t.classList.add('is-shown'));
    setTimeout(() => {
      t.classList.remove('is-shown');
      setTimeout(() => t.remove(), 220);
    }, duration);
  };
</script>
"""


_FEEDBACK_WIDGET_HTML = f"""
<script src="https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js" defer></script>
<button id="fb-fab" type="button" class="fb-fab" aria-label="Leave feedback" title="Leave feedback">💬</button>
<div id="fb-panel" hidden style="position:fixed;left:1rem;bottom:calc(10.5rem + env(safe-area-inset-bottom));z-index:91;width:min(20rem,calc(100vw - 2rem));background:white;border:1px solid #e5e7eb;border-radius:0.9rem;box-shadow:0 24px 48px -12px rgba(0,0,0,0.18);padding:0.9rem;">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:0.5rem;">
    <strong class="serif text-base">What's on your mind?</strong>
    <button id="fb-close" type="button" style="background:none;border:none;color:#9ca3af;font-size:1.4rem;line-height:1;">×</button>
  </div>
  <div style="display:flex;gap:0.3rem;margin-bottom:0.5rem;font-size:0.7rem;" id="fb-kind-row">
    <label style="flex:1;"><input type="radio" name="fb-kind" value="idea" checked style="display:none;"><span class="fb-kind-pill block text-center border border-stone-200 rounded-md py-1">💡 Idea</span></label>
    <label style="flex:1;"><input type="radio" name="fb-kind" value="bug" style="display:none;"><span class="fb-kind-pill block text-center border border-stone-200 rounded-md py-1">🐞 Bug</span></label>
    <label style="flex:1;"><input type="radio" name="fb-kind" value="like" style="display:none;"><span class="fb-kind-pill block text-center border border-stone-200 rounded-md py-1">❤️ Like</span></label>
    <label style="flex:1;"><input type="radio" name="fb-kind" value="note" style="display:none;"><span class="fb-kind-pill block text-center border border-stone-200 rounded-md py-1">📝 Note</span></label>
  </div>
  <textarea id="fb-text" rows="4" placeholder="Type your feedback…" style="width:100%;box-sizing:border-box;border:1px solid #e5e7eb;border-radius:0.45rem;padding:0.55rem 0.7rem;font-size:16px;outline:none;resize:vertical;"></textarea>
  <input id="fb-who" type="text" placeholder="Who is this? (optional)" style="width:100%;box-sizing:border-box;border:1px solid #e5e7eb;border-radius:0.45rem;padding:0.5rem 0.7rem;font-size:16px;margin-top:0.4rem;">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-top:0.55rem;gap:0.5rem;">
    <span id="fb-status" style="font-size:0.72rem;color:#6b7280;"></span>
    <button id="fb-send" type="button" class="bg-stone-900 text-white text-sm font-medium px-4 py-1.5 rounded-lg">Send</button>
  </div>
</div>
<script>
(function() {{
  const fab = document.getElementById('fb-fab');
  const panel = document.getElementById('fb-panel');
  const close = document.getElementById('fb-close');
  const send = document.getElementById('fb-send');
  const text = document.getElementById('fb-text');
  const who = document.getElementById('fb-who');
  const status = document.getElementById('fb-status');
  const pills = document.querySelectorAll('.fb-kind-pill');
  try {{ who.value = localStorage.getItem('fb-who') || ''; }} catch(e) {{}}

  // Screenshot of page state at the moment the user taps the FAB.
  // Captured BEFORE the panel opens so the panel itself isn't in
  // the image. POSTed alongside the feedback. Server stores it,
  // deletes it when the inbox item moves out of "new".
  let pendingShot = null;
  async function captureShot() {{
    if (typeof html2canvas !== 'function') return null;
    try {{
      // Wait for fonts + image decoding before capture — without this,
      // Fraunces serif + the recipe-card matte-color bg can render as
      // blank space because html2canvas walks the DOM faster than the
      // browser finishes layout. Then capture only the current
      // viewport (cropping x/y to scrollY) so the screenshot actually
      // shows what the user was looking at, not the full scrollable
      // body padded out with whitespace.
      if (document.fonts && document.fonts.ready) {{
        await document.fonts.ready;
      }}
      const canvas = await html2canvas(document.body, {{
        scale: 0.5, backgroundColor: '#FAF7F2', logging: false,
        useCORS: true, allowTaint: true,
        width:  window.innerWidth,
        height: window.innerHeight,
        x:      window.scrollX,
        y:      window.scrollY,
        windowWidth:  window.innerWidth,
        windowHeight: window.innerHeight,
      }});
      return canvas.toDataURL('image/jpeg', 0.6);
    }} catch (e) {{ return null; }}
  }}

  fab.addEventListener('click', async () => {{
    if (panel.hidden) {{
      // About to open — snap state first.
      pendingShot = await captureShot();
    }}
    panel.hidden = !panel.hidden;
    if (!panel.hidden) text.focus();
  }});
  close.addEventListener('click', () => {{ panel.hidden = true; }});
  function refresh() {{
    pills.forEach(p => {{
      const cb = p.parentElement.querySelector('input');
      p.style.background = cb.checked ? '#fef3c7' : 'white';
      p.style.color = cb.checked ? '#92400e' : '#6b7280';
      p.style.fontWeight = cb.checked ? '600' : 'normal';
    }});
  }}
  document.querySelectorAll('input[name="fb-kind"]').forEach(i => i.addEventListener('change', refresh));
  refresh();
  send.addEventListener('click', async () => {{
    const content = text.value.trim();
    if (!content) {{ status.textContent = 'Type something first.'; return; }}
    const kind = document.querySelector('input[name="fb-kind"]:checked').value;
    const whoVal = who.value.trim();
    try {{ localStorage.setItem('fb-who', whoVal); }} catch(e) {{}}
    status.textContent = 'Sending…';
    send.disabled = true;
    try {{
      const r = await fetch('{PREFIX}/app/feedback', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{
          path: location.pathname, kind, content, who: whoVal || null,
          screenshot: pendingShot,
        }}),
      }});
      if (!r.ok) throw new Error('HTTP ' + r.status);
      status.textContent = 'Sent. Thank you!';
      text.value = '';
      pendingShot = null;
      setTimeout(() => {{ panel.hidden = true; status.textContent = ''; }}, 1200);
    }} catch (e) {{
      status.textContent = 'Send failed — try again.';
    }} finally {{
      send.disabled = false;
    }}
  }});
}})();
</script>
"""


def _shell(title: str, body: str, active: str = "library") -> str:
    """Wrap an inner page body in the consumer app chrome. Active param
    drives the bottom tab highlight (library / plan / shop / tools)."""
    def tab(name: str, href: str, label: str) -> str:
        a = ' data-active="1"' if name == active else ""
        return f'<a href="{href}" data-tab="{name}"{a}>{label}</a>'
    # Prefetch sibling tabs so the SW cache is warm by the time the
    # user taps. Combined with the SW's stale-while-revalidate this
    # makes tab hops feel instant (no 200–400ms server round-trip on tap).
    tabs_to_prefetch = [
        (f"{PREFIX}/app/",     "library"),
        (f"{PREFIX}/app/plan", "plan"),
        (f"{PREFIX}/app/shop", "shop"),
    ]
    prefetches = "".join(
        f'<link rel="prefetch" href="{href}" as="document">'
        for href, name in tabs_to_prefetch if name != active
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
  <title>{_h(title)} · Mise</title>
  {prefetches}
  {_APP_CSS}
</head>
<body>
  <header class="max-w-2xl mx-auto px-5 pt-5 flex items-center justify-between">
    <a href="{PREFIX}/app/" class="serif text-lg font-medium tracking-tight text-stone-900">🥄 Mise</a>
    <div class="flex items-center gap-3">
      <span id="sync-pill" hidden class="text-[10px] font-semibold uppercase tracking-wider bg-amber-100 text-amber-900 px-2 py-0.5 rounded-full"></span>
      <a href="{PREFIX}/app/tools" class="text-xs text-stone-500 hover:text-stone-900">More ↗</a>
    </div>
  </header>
  <!-- A2HS hint — shown once to iOS Safari visitors so they install. -->
  <div id="a2hs-hint" hidden
       class="max-w-2xl mx-auto px-5 pt-3 text-xs text-amber-900">
    <div class="bg-amber-50 border border-amber-200 rounded-xl px-3 py-2 flex items-center gap-2">
      <span>📲</span>
      <span class="flex-1">Install Mise: tap <strong>Share</strong> then <strong>Add to Home Screen</strong>.</span>
      <button type="button" id="a2hs-dismiss" class="text-amber-700 hover:text-amber-900 text-base leading-none px-1">×</button>
    </div>
  </div>
  <script>
    (function() {{
      // Show the install hint on iOS Safari only, once per device.
      const hint = document.getElementById('a2hs-hint');
      const standalone = window.navigator.standalone === true ||
                          window.matchMedia('(display-mode: standalone)').matches;
      const ua = navigator.userAgent;
      const isIOSSafari = /iPhone|iPad|iPod/.test(ua) && /Safari/.test(ua) && !/CriOS|FxiOS/.test(ua);
      let dismissed = false;
      try {{ dismissed = localStorage.getItem('mise:a2hs:dismissed') === '1'; }} catch (e) {{}}
      if (isIOSSafari && !standalone && !dismissed) {{
        hint.hidden = false;
      }}
      document.getElementById('a2hs-dismiss').addEventListener('click', () => {{
        hint.hidden = true;
        try {{ localStorage.setItem('mise:a2hs:dismissed', '1'); }} catch (e) {{}}
      }});
    }})();

    /* ---- Service worker + online/offline status pill ---- */
    (function() {{
      const pill = document.getElementById('sync-pill');
      function setPill(text, tone) {{
        if (!pill) return;
        if (!text) {{ pill.hidden = true; return; }}
        pill.hidden = false;
        pill.textContent = text;
        pill.className = 'text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 rounded-full ' + (
          tone === 'offline' ? 'bg-rose-100 text-rose-800' :
          tone === 'syncing' ? 'bg-amber-100 text-amber-900' :
          'bg-emerald-100 text-emerald-800'
        );
      }}
      function refresh() {{
        if (!navigator.onLine) setPill('Offline', 'offline');
        else setPill('');   // hide when nothing to say
      }}
      window.addEventListener('online', () => {{
        setPill('Syncing…', 'syncing');
        navigator.serviceWorker?.controller?.postMessage({{ type: 'replay-queue' }});
        setTimeout(refresh, 1200);
      }});
      window.addEventListener('offline', refresh);
      refresh();

      if ('serviceWorker' in navigator) {{
        // Scope = /mise/ because the SW is served at /mise/sw.js
        // (Service-Worker-Allowed: / header lets it claim higher if needed).
        navigator.serviceWorker.register('{PREFIX}/sw.js', {{ scope: '{PREFIX}/' }})
          .catch((e) => console.warn('SW registration failed:', e));
        navigator.serviceWorker.addEventListener('message', (event) => {{
          if (event.data && event.data.type === 'queue-flushed') {{
            const n = event.data.count;
            if (n > 0) {{
              setPill(`Synced ${{n}}`, 'synced');
              setTimeout(refresh, 1800);
            }}
          }}
        }});
      }}
    }})();
  </script>
  <main class="max-w-2xl mx-auto px-5 pt-4 stagger">
    {body}
  </main>
  <nav class="tab-bar" aria-label="Primary">
    {tab("library", PREFIX + "/app/",     "Library")}
    {tab("plan",    PREFIX + "/app/plan", "Plan")}
    {tab("shop",    PREFIX + "/app/shop", "Shop")}
  </nav>
  {_FEEDBACK_WIDGET_HTML}
  {_DIALOGS_HTML}
</body>
</html>"""


# =============================================================================
# Library
# =============================================================================

@router.get("/app", include_in_schema=False)
def _redirect_to_app_slash() -> RedirectResponse:
    return RedirectResponse(url=PREFIX + "/app/", status_code=307)


@router.get("/app/", response_class=HTMLResponse)
def library_page(
    db: Session = Depends(get_session),
    q: str = "",
    tag: str = "",
) -> HTMLResponse:
    all_recipes = db.exec(
        select(Recipe)
        .where(Recipe.archived == False)  # noqa: E712
        .where(Recipe.reviewed_at.is_not(None))
        .order_by(Recipe.name_english)
    ).all()

    unreviewed_count = len(db.exec(
        select(Recipe)
        .where(Recipe.archived == False)  # noqa: E712
        .where(Recipe.reviewed_at.is_(None))
    ).all())

    # Tag counts (for chip row).
    tag_counts: Counter[str] = Counter()
    for r in all_recipes:
        for t in (r.tags or []):
            tag_counts[t] += 1

    recipes = all_recipes
    if tag:
        recipes = [r for r in recipes if tag in (r.tags or [])]
    if q:
        ql = q.lower()
        recipes = [
            r for r in recipes
            if ql in (r.name_english or "").lower()
            or ql in (r.name_original or "").lower()
        ]

    def _row(r: Recipe) -> str:
        thumb = (
            f"<div class='w-14 h-14 rounded-2xl {_recipe_color(r.id)} "
            f"flex items-center justify-center text-2xl shrink-0'>"
            f"{_recipe_emoji(r.name_english or r.name_original, r.emoji)}"
            f"</div>"
        )
        name = r.display_title()
        original = (
            r.name_original
            if r.name_english and r.name_original != r.name_english and r.name_original != name
            else None
        )
        meta_bits = []
        if r.total_minutes:
            meta_bits.append(f"{r.total_minutes}m")
        if r.servings:
            meta_bits.append(f"serves {r.servings}")
        meta = " · ".join(meta_bits)
        star = " <span class='text-amber-500 text-xs'>★</span>" if "Favourites" in (r.tags or []) else ""
        # data-haystack drives the client-side live filter so typing
        # doesn't trigger a full page reload (which jumps the layout
        # under iOS keyboard).
        haystack = _h(((r.name_english or "") + " " + (r.name_original or "")).lower())
        return f"""
          <a href="{PREFIX}/app/recipe/{r.id}" class="library-row card p-3 flex items-center gap-3 hover:shadow-lg transition-shadow" data-haystack="{haystack}">
            {thumb}
            <div class="flex-1 min-w-0">
              <div class="serif text-lg font-medium leading-tight">{_h(name)}{star}</div>
              {f'<div class="text-sm serif italic text-stone-500 mt-0.5 truncate">{_h(original)}</div>' if original else ''}
              <div class="text-sm text-stone-500 mt-1">{_h(meta)}</div>
            </div>
            <div class="text-stone-300 shrink-0">›</div>
          </a>"""

    # Chips: All + top tags.
    chip_qs = f"&q={_h(q)}" if q else ""
    def _chip(label: str, target: str | None) -> str:
        is_active = (target or "") == (tag or "")
        href = (
            f"{PREFIX}/app/?tag={_h(target)}{chip_qs}" if target
            else f"{PREFIX}/app/{('?q=' + _h(q)) if q else ''}"
        )
        cls = (
            "bg-amber-100 text-amber-900 font-medium" if is_active
            else "bg-stone-100 text-stone-600 hover:bg-stone-200"
        )
        return f"<a href='{href}' class='inline-block {cls} text-xs px-3 py-1 rounded-full'>{_h(label)}</a>"

    chips = _chip("All", None) + " "
    fav_count = sum(1 for r in all_recipes if "Favourites" in (r.tags or []))
    if fav_count:
        chips += _chip(f"★ Favourites", "Favourites") + " "
    chips += " ".join(_chip(t, t) for t, _ in tag_counts.most_common(5) if t != "Favourites")

    if recipes:
        rows_html = "".join(_row(r) for r in recipes)
    elif q or tag:
        rows_html = (
            "<div class='card p-8 text-center'>"
            f"  <div class='serif text-2xl text-stone-400 italic mb-2'>Nothing matches.</div>"
            f"  <a href='{PREFIX}/app/' class='text-sm text-amber-700 hover:underline'>Clear filters →</a>"
            "</div>"
        )
    else:
        rows_html = (
            "<div class='card p-8 text-center'>"
            "  <div class='serif text-2xl text-stone-400 italic mb-1'>Empty library.</div>"
            f"  <p class='text-sm text-stone-500 mt-2'>Add your first recipe from the <a href='{PREFIX}/app/add' class='text-amber-700 hover:underline'>Add a recipe</a> page.</p>"
            "</div>"
        )

    review_chip = (
        f" <a href='{PREFIX}/app/review' class='inline-flex items-center gap-1 "
        f"bg-amber-100 hover:bg-amber-200 text-amber-900 text-[10px] font-semibold "
        f"px-2 py-0.5 rounded-full'>{unreviewed_count} awaiting review →</a>"
        if unreviewed_count else ""
    )

    body = f"""
      <div class="mb-1 text-[10px] uppercase tracking-[0.25em] text-stone-400">The kitchen</div>
      <div class="flex justify-between items-end gap-1.5">
        <h1 class="serif text-4xl font-medium tracking-tight leading-none">Recipe <em class="text-stone-500">library</em>.</h1>
        <a href="{PREFIX}/app/add" class="shrink-0 inline-flex items-center text-white text-sm font-semibold px-4 py-2 rounded-full"
           style="background:#047857;">＋ Add</a>
      </div>
      <p class="text-sm text-stone-500 mt-3">{len(all_recipes)} recipes{review_chip}</p>

      <form method="get" action="{PREFIX}/app/" class="mt-5" id="lib-search-form">
        {f'<input type="hidden" name="tag" value="{_h(tag)}">' if tag else ''}
        <input id="lib-search" name="q" value="{_h(q or '')}"
               placeholder="What shall we cook?"
               autocomplete="off"
               class="w-full card px-4 py-2.5 serif italic placeholder:text-stone-400 outline-none">
      </form>

      <div class="flex flex-wrap gap-1.5 mt-3 mb-5">{chips}</div>

      <div id="lib-list" class="space-y-3">{rows_html}</div>
      <div id="lib-empty" class="card p-8 text-center" hidden>
        <div class="serif text-2xl text-stone-400 italic mb-2">Nothing matches.</div>
        <p class="text-sm text-stone-500">Try a different word — or clear the search.</p>
      </div>

      <script>
      (function() {{
        // Live client-side filter. Prevents the form-submit page
        // reload that was causing the layout to jump on every keystroke
        // (server re-render + iOS keyboard reflow).
        const form  = document.getElementById('lib-search-form');
        const input = document.getElementById('lib-search');
        const list  = document.getElementById('lib-list');
        const empty = document.getElementById('lib-empty');
        if (!form || !input) return;
        const rows = list.querySelectorAll('.library-row');
        function apply(q) {{
          const ql = (q || '').toLowerCase().trim();
          let visible = 0;
          rows.forEach(r => {{
            const match = !ql || r.dataset.haystack.includes(ql);
            r.style.display = match ? '' : 'none';
            if (match) visible++;
          }});
          empty.hidden = visible > 0 || rows.length === 0;
        }}
        input.addEventListener('input', () => apply(input.value));
        form.addEventListener('submit', (e) => {{
          e.preventDefault();
          input.blur();   // dismiss the keyboard on Enter
        }});
      }})();
      </script>
    """
    return HTMLResponse(_shell("Library", body, active="library"))


# =============================================================================
# Recipe detail
# =============================================================================


def _tags_by_use(db: Session) -> list[str]:
    """Every tag on a live recipe, most-used first — the recipe page's
    tag picker."""
    counts: Counter[str] = Counter()
    for tags in db.exec(select(Recipe.tags).where(Recipe.archived == False)).all():  # noqa: E712
        counts.update(tags or [])
    return [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))]


def _recipe_share_text(r: Recipe, db: Session) -> str:
    """Plain-text copy of a recipe for the phone's share sheet — the app
    itself usually sits behind a login, so a page link only works for
    the household."""
    out = [r.display_title()]
    meta = [f"Serves {r.servings}" if r.servings else "",
            f"{r.total_minutes} min" if r.total_minutes else ""]
    if any(meta):
        out.append(" · ".join(m for m in meta if m))
    for sec in db.exec(
        select(IngredientSection).where(IngredientSection.recipe_id == r.id).order_by(IngredientSection.order)
    ).all():
        out.append("")
        out.append(sec.name or "Ingredients")
        for i in db.exec(select(Ingredient).where(Ingredient.section_id == sec.id).order_by(Ingredient.order)).all():
            if i.display_name == "unknown":
                continue
            qty = f"{_fmt_qty(i.qty)} {i.unit or ''}".strip() + " " if i.qty else ""
            qty = qty.replace(" ea ", " ")
            line = f"• {qty}{i.display_name}"
            if i.prep_note:
                line += f", {i.prep_note}"
            if i.optional:
                line += " (optional)"
            out.append(line)
    n = 0
    for sec in db.exec(
        select(StepSection).where(StepSection.recipe_id == r.id).order_by(StepSection.order)
    ).all():
        out.append("")
        out.append(sec.name or "Method")
        for st in db.exec(select(Step).where(Step.section_id == sec.id).order_by(Step.order)).all():
            n += 1
            out.append(f"{n}. {st.text}")
    if r.notes_markdown:
        out += ["", r.notes_markdown.strip()]
    if r.source_url:
        out += ["", f"Original: {r.source_url}"]
    return "\n".join(out)

@router.get("/app/recipe/{recipe_id}", response_class=HTMLResponse)
def recipe_page(recipe_id: int, db: Session = Depends(get_session)) -> HTMLResponse:
    r = db.get(Recipe, recipe_id)
    if r is None:
        raise HTTPException(status_code=404)

    # Upcoming slot occupancy for the add-to-plan modal.
    today = _Date.today()
    end = today + _Td(days=6)
    occ_rows = db.exec(
        select(SlotAssignment, CookingSession, Recipe)
        .join(CookingSession, SlotAssignment.session_id == CookingSession.id)
        .join(Recipe, CookingSession.recipe_id == Recipe.id)
        .where(SlotAssignment.date >= today)
        .where(SlotAssignment.date <= end)
        .where(SlotAssignment.position == 1)
    ).all()
    occupied: dict[tuple[str, str], str] = {
        (slot.date.isoformat(), slot.meal): rec.display_title()
        for slot, _, rec in occ_rows
    }
    for n in db.exec(
        select(PlanNote).where(PlanNote.date >= today).where(PlanNote.date <= end)
    ).all():
        occupied[(n.date.isoformat(), n.meal)] = n.text
    _hh = db.exec(select(Household)).first()
    per_slot = (_hh.default_servings_per_meal if _hh else None) or 2

    # Ingredients + steps.
    sect_blocks = []
    step_ings: list[tuple[str, float | None, str | None]] = []
    have_map = _recipe_have_map(r.id, db)  # None = not on the shopping list
    for s in db.exec(
        select(IngredientSection)
        .where(IngredientSection.recipe_id == r.id)
        .order_by(IngredientSection.order)
    ).all():
        ings = db.exec(
            select(Ingredient).where(Ingredient.section_id == s.id).order_by(Ingredient.order)
        ).all()
        ing_rows = []
        for i in ings:
            if i.display_name == "unknown":
                continue
            step_ings.append((i.display_name, i.qty, i.unit, i.prep_note))
            qty = ""
            if not (i.optional and i.qty == 0):
                # data-qty + data-unit drive the serving-scaler JS: when
                # the user bumps the SERVES stepper, JS walks .ing-qty
                # nodes and multiplies by the new factor in place.
                qty = (
                    f"<span class='font-medium text-stone-900 ing-qty' "
                    f"data-qty='{i.qty or 0}' data-unit='{_h('' if i.unit == 'ea' else (i.unit or ''))}'>"
                    f"{_h(_qty_text(i.qty, i.unit))}"
                    f"</span> "
                )
            prep = f", {_h(i.prep_note)}" if i.prep_note else ""
            opt = " <span class='text-stone-400'>(optional)</span>" if i.optional else ""
            cat_pill = (
                f"<span class='text-[10px] uppercase tracking-wider text-stone-400 shrink-0'>{_h(i.category)}</span>"
                if i.category else ""
            )
            # Planned recipe: mark each ingredient bought / still to buy,
            # straight from the shopping-list ticks.
            bullet = "<div class='w-1.5 h-1.5 rounded-full bg-stone-300 shrink-0'></div>"
            if have_map is not None and i.id in have_map:
                if have_map[i.id]:
                    bullet = "<div class='shrink-0 font-semibold have-mark' style='color:#059669;width:1rem;text-align:center;' title='Bought'>✓</div>"
                else:
                    bullet = "<div class='shrink-0 need-mark' style='width:0.7rem;height:0.7rem;border-radius:9999px;border:2px solid #d97706;margin:0 0.15rem;' title='Still to buy'></div>"
                    # Aisle stays — it's what you need standing in the store.
                    cat_pill = (
                        "<div class='shrink-0 need-label' style='text-align:right;line-height:1.35;'>"
                        "<div class='text-[10px] uppercase tracking-wider font-semibold' style='color:#b45309;'>to buy</div>"
                        + (f"<div class='text-[10px] uppercase tracking-wider text-stone-400'>{_h(i.category)}</div>" if i.category else "")
                        + "</div>"
                    )
            ing_rows.append(
                f"<div class='ingredient-row flex items-center gap-3 px-4 py-3 border-b border-stone-100 last:border-0'>"
                f"  {bullet}"
                f"  <div class='text-base flex-1 leading-snug'>{qty}{_h(i.display_name)}{prep}{opt}</div>"
                f"  {cat_pill}"
                f"</div>"
            )
        if ing_rows:
            sect_blocks.append(
                (f"<h3 class='serif text-base font-medium tracking-tight px-1 mt-4 mb-1'>{_h(s.name)}</h3>" if s.name else "")
                + f"<div class='card overflow-hidden'>{''.join(ing_rows)}</div>"
            )

    step_blocks = []
    for s in db.exec(
        select(StepSection).where(StepSection.recipe_id == r.id).order_by(StepSection.order)
    ).all():
        steps = db.exec(
            select(Step).where(Step.section_id == s.id).order_by(Step.order)
        ).all()
        items = []
        for n, st in enumerate(steps, 1):
            # `method-step` class is the cook-mode target; JS walks
            # these to inflate `\\d+ min` matches into tappable timers.
            items.append(
                f"<li class='card p-4 flex gap-4 method-step'>"
                f"  <span class='serif text-3xl font-medium text-stone-300 leading-none shrink-0 step-n'>{n}</span>"
                f"  <p class='text-base leading-relaxed text-stone-800 pt-1 step-text'>{_step_html(st.text, step_ings)}</p>"
                f"</li>"
            )
        if items:
            step_blocks.append(
                (f"<h3 class='serif text-base font-medium tracking-tight px-1 mt-4 mb-1'>{_h(s.name)}</h3>" if s.name else "")
                + f"<ol class='space-y-3'>{''.join(items)}</ol>"
            )

    shop_summary = ""
    if have_map:
        need_n = sum(1 for v in have_map.values() if not v)
        shop_summary = (
            f"<a href='{PREFIX}/app/shop?view=recipe' id='shop-summary' class='block text-sm mb-2 px-1' style='color:#b45309;'>"
            f"Still to buy: {need_n} of {len(have_map)} · open shopping list ›</a>"
            if need_n else
            "<div id='shop-summary' class='text-sm mb-2 px-1' style='color:#059669;'>✓ You have everything on the shopping list for this.</div>"
        )

    name = r.display_title()
    sub = r.name_original if (r.name_english and r.name_original != name) else None
    emoji = _recipe_emoji(name, r.emoji)
    color = _recipe_color(r.id)

    # Metric tiles.
    metric = lambda v, lbl: (
        f"<div class='card p-3 text-center'>"
        f"  <div class='serif text-xl font-medium'>{v}</div>"
        f"  <div class='text-[11px] text-stone-500 mt-0.5 uppercase tracking-wider'>{lbl}</div>"
        f"</div>"
    )

    source_href = _safe_href(r.source_url)
    source_link = (
        f"<a href='{_h(source_href)}' target='_blank' rel='noopener noreferrer' "
        f"   class='inline-flex items-center gap-1 text-amber-700 text-sm hover:underline mt-2'>"
        f"  View original ↗"
        f"</a>"
        if source_href else ""
    )

    is_fav = "Favourites" in (r.tags or [])
    body = f"""
      <div class="mb-3 flex items-center justify-between">
        <a href="{PREFIX}/app/" class="text-sm text-stone-500 hover:text-stone-900">← Library</a>
        <button type="button" id="fav-toggle" aria-label="Toggle favourite"
                aria-pressed="{'true' if is_fav else 'false'}"
                data-recipe-id="{r.id}"
                class="text-2xl leading-none p-1.5 -mr-1.5 hover:scale-110 transition-transform">
          <span id="fav-icon">{'♥' if is_fav else '♡'}</span>
        </button>
      </div>

      <div class="card overflow-hidden">
        <div class="{color} p-6 flex items-center gap-5">
          <div class="text-6xl">{emoji}</div>
          <div class="flex-1 min-w-0">
            <h1 class="serif text-2xl font-medium tracking-tight leading-tight">{_h(name)}</h1>
            {f'<p class="serif italic text-sm opacity-70 mt-1">{_h(sub)}</p>' if sub else ''}
            {source_link}
          </div>
        </div>
      </div>

      <!-- Tag chips + inline editor. Empty by default with a "+ Tag"
           affordance; existing chips get a tiny × to remove. Click the
           tag label to jump to Library filtered by that tag. -->
      <div id="recipe-tags" class="flex flex-wrap gap-1.5 mt-3 items-center"
           style="position:relative;z-index:31;"
           data-recipe-id="{r.id}"
           data-tags="{_h(_json.dumps(r.tags or []))}"
           data-all-tags="{_h(_json.dumps(_tags_by_use(db)))}">
        {''.join(
            f"<span class='tag-chip inline-flex items-center gap-1 bg-stone-100 text-xs font-medium text-stone-700 pl-3 pr-1 py-1 rounded-full' data-tag='{_h(t)}'>"
            f"  <a href='{PREFIX}/app/?tag={_h(t)}' class='hover:text-amber-700'>{_h(t)}</a>"
            f"  <button type='button' class='tag-rm text-stone-400 hover:text-rose-600 w-5 h-5 leading-none' aria-label='Remove tag'>×</button>"
            f"</span>"
            for t in (r.tags or []) if t != "Favourites"
        )}
        <span id="tag-add-wrap" style="position:relative;display:inline-flex;">
          <button type="button" id="tag-add-btn" aria-haspopup="menu" aria-expanded="false"
                  class="inline-flex items-center gap-1 bg-amber-50 border border-amber-200 text-xs font-medium text-amber-800 px-3 py-1 rounded-full hover:bg-amber-100">
            + Tag ▾
          </button>
          <div id="tag-menu" hidden
               style="position:absolute;left:0;top:calc(100% + 6px);z-index:40;width:15rem;background:#fff;border:1px solid #e7e5e4;border-radius:0.9rem;box-shadow:0 10px 30px rgba(0,0,0,0.12);overflow:hidden;">
            <div style="padding:0.5rem;border-bottom:1px solid #f5f5f4;">
              <input type="text" id="tag-input" placeholder="Find or create a tag…" maxlength="40" autocomplete="off"
                     class="w-full border border-stone-300 rounded-lg text-sm px-3 py-2 bg-white outline-none focus:border-amber-500">
            </div>
            <div id="tag-options" style="max-height:15rem;overflow-y:auto;overscroll-behavior:contain;"></div>
          </div>
        </span>
      </div>
      <style>
        #fav-toggle[aria-pressed="true"] #fav-icon {{ color: #be123c; }}
        #fav-toggle[aria-pressed="false"] #fav-icon {{ color: #d6d3d1; }}
      </style>
      <script>
      (function() {{
        const btn = document.getElementById('fav-toggle');
        const icon = document.getElementById('fav-icon');
        btn.addEventListener('click', async () => {{
          const wasFav = btn.getAttribute('aria-pressed') === 'true';
          // Read live tags from the tag-editor's data attribute so we
          // don't clobber any tags someone else added since page load.
          const wrap = document.getElementById('recipe-tags');
          let tags = [];
          if (wrap) {{
            try {{ tags = JSON.parse(wrap.dataset.tags || '[]'); }} catch (e) {{}}
          }}
          const next = wasFav
            ? tags.filter(t => t !== 'Favourites')
            : Array.from(new Set([...tags, 'Favourites']));
          btn.setAttribute('aria-pressed', wasFav ? 'false' : 'true');
          icon.textContent = wasFav ? '♡' : '♥';
          try {{
            const r = await fetch('{PREFIX}/recipes/' + btn.dataset.recipeId, {{
              method: 'PATCH',
              headers: {{ 'Content-Type': 'application/json' }},
              body: JSON.stringify({{ tags: next }}),
            }});
            if (!r.ok) throw new Error('HTTP ' + r.status);
            if (wrap) wrap.dataset.tags = JSON.stringify(next);
          }} catch (e) {{
            // Revert on failure.
            btn.setAttribute('aria-pressed', wasFav ? 'true' : 'false');
            icon.textContent = wasFav ? '♥' : '♡';
            window.notify('Could not save favourite: ' + e.message, {{tone:'error'}});
          }}
        }});
      }})();
      </script>

      <div class="grid grid-cols-4 gap-2 mt-4">
        {metric((r.prep_minutes or '–'), 'Prep')}
        <div class="card p-3 text-center">
          <div class="serif text-xl font-medium">{(r.cook_minutes or '–')}</div>
          <div class="text-[11px] text-stone-500 mt-0.5 uppercase tracking-wider">Cook</div>
          {f'<div class="text-[10px] text-amber-700 font-semibold tracking-tight mt-0.5">{r.oven_temp_f}°F</div>' if r.oven_temp_f else ''}
        </div>
        {metric((r.total_minutes or '–'), 'Total')}
        <div class="card p-2 text-center" id="serves-tile">
          <div class="flex items-center justify-center gap-0.5">
            <button type="button" id="srv-dec" class="text-stone-500 hover:text-stone-900 w-7 h-7 leading-none text-lg" aria-label="Fewer servings">−</button>
            <span id="srv-val" class="serif text-xl font-medium tabular-nums min-w-[1.5rem]">{r.servings or '–'}</span>
            <button type="button" id="srv-inc" class="text-stone-500 hover:text-stone-900 w-7 h-7 leading-none text-lg" aria-label="More servings">+</button>
          </div>
          <div class="text-[11px] text-stone-500 -mt-0.5 uppercase tracking-wider">Serves</div>
        </div>
      </div>
      <div id="scale-indicator" class="text-[11px] text-amber-700 mt-1 ml-1" hidden></div>

      <div class="flex gap-2 mt-4" style="position:relative;z-index:30;">
        <button type="button" id="atp-open"
                class="flex-1 bg-stone-900 hover:bg-stone-700 text-white font-medium py-3 rounded-2xl text-sm">
          + Add to plan
        </button>
        <a href="{PREFIX}/app/recipe/{r.id}/draft"
           class="card px-4 flex items-center justify-center text-sm text-amber-700 hover:bg-stone-50"
           title="Edit ingredients, steps, tags">✎ Edit</a>
        <div style="position:relative;display:flex;">
          <button type="button" id="share-btn" data-text="{_h(_recipe_share_text(r, db))}" data-title="{_h(r.display_title())}"
                  class="card px-4 flex items-center justify-center text-sm text-amber-700 hover:bg-stone-50"
                  aria-haspopup="menu" aria-expanded="false" title="Share this recipe">↑ Share ▾</button>
          <div id="share-menu" role="menu" hidden
               style="position:absolute;right:0;top:calc(100% + 6px);z-index:40;min-width:14rem;background:#fff;border:1px solid #e7e5e4;border-radius:0.9rem;box-shadow:0 10px 30px rgba(0,0,0,0.12);overflow:hidden;">
            <button type="button" role="menuitem" class="share-opt" data-what="full"
                    style="display:block;width:100%;text-align:left;padding:0.75rem 1rem;border-bottom:1px solid #f5f5f4;">
              <div class="text-sm font-medium text-stone-900">Full recipe</div>
              <div class="text-xs text-stone-500">Ingredients, steps, original + Mise links</div>
            </button>
            <button type="button" role="menuitem" class="share-opt" data-what="link"
                    style="display:block;width:100%;text-align:left;padding:0.75rem 1rem;">
              <div class="text-sm font-medium text-stone-900">Just the Mise link</div>
              <div class="text-xs text-stone-500">Opens for anyone who can log in to Mise</div>
            </button>
          </div>
        </div>
      </div>
      <script>
        (function() {{
          const btn = document.getElementById('share-btn');
          const menu = document.getElementById('share-menu');
          const setOpen = (open) => {{ menu.hidden = !open; btn.setAttribute('aria-expanded', open); }};
          btn.addEventListener('click', (e) => {{ e.stopPropagation(); setOpen(menu.hidden); }});
          document.addEventListener('click', (e) => {{ if (!menu.contains(e.target)) setOpen(false); }});
          document.addEventListener('keydown', (e) => {{ if (e.key === 'Escape') setOpen(false); }});
          menu.querySelectorAll('.share-opt').forEach(opt => opt.addEventListener('click', async () => {{
            setOpen(false);
            const link = location.origin + location.pathname;
            const full = opt.dataset.what === 'full';
            const text = full ? btn.dataset.text + '\\n\\nIn Mise: ' + link : link;
            if (navigator.share) {{
              const data = full ? {{ title: btn.dataset.title, text }} : {{ title: btn.dataset.title, url: link }};
              try {{ await navigator.share(data); }} catch (err) {{}}
              return;
            }}
            try {{
              await navigator.clipboard.writeText(text);
              window.notify(full ? 'Recipe copied — paste it anywhere' : 'Link copied', {{tone:'success'}});
            }} catch (err) {{
              window.notify('Could not copy', {{tone:'error'}});
            }}
          }}));
        }})();
      </script>

      <h2 class="serif text-2xl font-medium tracking-tight mt-8 mb-2">Ingredients</h2>
      {shop_summary}
      {''.join(sect_blocks) or '<p class="text-stone-400 italic text-sm">No ingredients filled in yet.</p>'}

      <div class="flex items-baseline justify-between mt-8 mb-2">
        <h2 class="serif text-2xl font-medium tracking-tight">Method</h2>
        <button type="button" id="cook-mode-btn"
                class="text-sm bg-amber-100 hover:bg-amber-200 text-amber-900 font-medium px-3 py-1.5 rounded-lg">
          🍳 Cook mode
        </button>
      </div>
      {''.join(step_blocks) or '<p class="text-stone-400 italic text-sm">No instructions yet.</p>'}

      <div class="flex items-baseline justify-between mt-8 mb-2">
        <h2 class="serif text-2xl font-medium tracking-tight">Notes</h2>
        <span id="notes-status" class="text-[11px] uppercase tracking-wider text-stone-400" hidden></span>
      </div>
      <div class="card p-3">
        <textarea id="recipe-notes" rows="3"
                  placeholder="Tweaks, swaps, what worked last time…"
                  data-recipe-id="{r.id}"
                  class="w-full bg-transparent serif italic placeholder:text-stone-400 outline-none resize-none text-base leading-snug"
                  style="min-height: 5rem;">{_h(r.notes_markdown or '')}</textarea>
      </div>

      {_add_to_plan_modal(r, occupied, per_slot)}

      <!-- Cook mode: scales text + holds the wake lock + turns time
           references in steps into tappable inline countdown timers. -->
      <style>
        body.cook-mode {{ background: #FDFBF6; }}
        body.cook-mode .step-text {{ font-size: 1.25rem; line-height: 1.55; padding-top: 0.4rem; }}
        body.cook-mode .method-step {{ padding: 1rem 1.25rem; }}
        body.cook-mode .step-n {{ font-size: 2.25rem; }}
        /* Inline ingredient quantities — cook mode only. */
        .step-qty {{ display: none; color: #b45309; font-weight: 500; white-space: nowrap; }}
        body.cook-mode .step-qty {{ display: inline; }}
        body.cook-mode .ingredient-row, body.cook-mode #ing-list .ing-row {{ padding: 0.7rem 1rem; font-size: 1.15rem; }}
        .timer-chip {{
          background: #fef3c7; color: #92400e;
          padding: 0.05rem 0.45rem; border-radius: 0.3rem;
          font-weight: 600; cursor: pointer;
          font-variant-numeric: tabular-nums;
        }}
        .timer-chip.running {{ background: #fde68a; animation: pulse 1.4s ease-in-out infinite; }}
        .timer-chip.done {{ background: #fecaca; color: #991b1b; animation: shake 0.4s; }}
        @keyframes pulse {{ 50% {{ opacity: 0.6; }} }}
        @keyframes shake {{ 25% {{ transform: translateX(-3px); }} 75% {{ transform: translateX(3px); }} }}
      </style>
      <script>
      (function() {{
        const btn = document.getElementById('cook-mode-btn');
        let wakeLock = null;

        async function enter() {{
          document.body.classList.add('cook-mode');
          btn.textContent = '✕ Exit cook mode';
          wrapTimers();
          try {{ wakeLock = await navigator.wakeLock.request('screen'); }} catch (e) {{}}
        }}
        async function leave() {{
          document.body.classList.remove('cook-mode');
          btn.textContent = '🍳 Cook mode';
          if (wakeLock) {{ try {{ await wakeLock.release(); }} catch (e) {{}} wakeLock = null; }}
        }}
        btn.addEventListener('click', () =>
          document.body.classList.contains('cook-mode') ? leave() : enter()
        );
        // Re-acquire the wake lock if the page becomes visible again
        // (iOS releases on visibilitychange).
        document.addEventListener('visibilitychange', async () => {{
          if (document.body.classList.contains('cook-mode') &&
              document.visibilityState === 'visible' && !wakeLock) {{
            try {{ wakeLock = await navigator.wakeLock.request('screen'); }} catch (e) {{}}
          }}
        }});

        function wrapTimers() {{
          if (document.body.dataset.timersWrapped === '1') return;
          document.body.dataset.timersWrapped = '1';
          const re = /\\b(\\d+)(?:\\s*[-–]\\s*(\\d+))?\\s*(min(?:ute)?s?|hr|hours?)\\b/gi;
          document.querySelectorAll('.step-text').forEach(p => {{
            p.innerHTML = p.innerHTML.replace(re, (m, a, b, unit) => {{
              const lo = parseInt(a, 10);
              const hi = b ? parseInt(b, 10) : lo;
              const mins = (lo + hi) / 2;
              const secs = unit.toLowerCase().startsWith('h') ? Math.round(mins * 3600) : Math.round(mins * 60);
              return `<span class="timer-chip" data-seconds="${{secs}}">⏱ ${{m}}</span>`;
            }});
          }});
          document.querySelectorAll('.timer-chip').forEach(chip => {{
            chip.addEventListener('click', () => startTimer(chip));
          }});
        }}

        function startTimer(chip) {{
          if (chip.dataset.running === '1') {{
            clearInterval(chip._iv);
            chip.dataset.running = '';
            chip.classList.remove('running', 'done');
            chip.textContent = chip.dataset.label || chip.textContent;
            return;
          }}
          let remaining = parseInt(chip.dataset.seconds, 10);
          chip.dataset.label = chip.textContent;
          chip.dataset.running = '1';
          chip.classList.add('running');
          const tick = () => {{
            if (remaining <= 0) {{
              clearInterval(chip._iv);
              chip.classList.remove('running');
              chip.classList.add('done');
              chip.textContent = '✓ Time!';
              try {{ navigator.vibrate?.([200, 100, 200]); }} catch (e) {{}}
              return;
            }}
            const m = Math.floor(remaining / 60);
            const s = remaining % 60;
            chip.textContent = `⏱ ${{m}}:${{s.toString().padStart(2, '0')}}`;
            remaining--;
          }};
          tick();
          chip._iv = setInterval(tick, 1000);
        }}
      }})();
      </script>

      <script>
      // Tag editor — add a tag via the "+ Tag" pill, remove via the ×
      // on each chip. Saves via PATCH /recipes/{id} with the full
      // tags list (server replaces the list each call).
      (function() {{
        const wrap = document.getElementById('recipe-tags');
        if (!wrap) return;
        const addBtn = document.getElementById('tag-add-btn');
        const input  = document.getElementById('tag-input');
        const recipeId = wrap.dataset.recipeId;
        let tags;
        try {{ tags = JSON.parse(wrap.dataset.tags || '[]'); }}
        catch (e) {{ tags = []; }}

        async function persist() {{
          try {{
            const r = await fetch('{PREFIX}/recipes/' + recipeId, {{
              method: 'PATCH',
              headers: {{ 'Content-Type': 'application/json' }},
              body: JSON.stringify({{ tags }}),
            }});
            if (!r.ok) throw new Error('HTTP ' + r.status);
            wrap.dataset.tags = JSON.stringify(tags);
          }} catch (e) {{
            window.notify('Tag save failed: ' + e.message, {{tone:'error'}});
          }}
        }}

        function makeChip(t) {{
          const span = document.createElement('span');
          span.className = 'tag-chip inline-flex items-center gap-1 bg-stone-100 text-xs font-medium text-stone-700 pl-3 pr-1 py-1 rounded-full';
          span.dataset.tag = t;
          const a = document.createElement('a');
          a.href = '{PREFIX}/app/?tag=' + encodeURIComponent(t);
          a.className = 'hover:text-amber-700';
          a.textContent = t;
          const rm = document.createElement('button');
          rm.type = 'button';
          rm.className = 'tag-rm text-stone-400 hover:text-rose-600 w-5 h-5 leading-none';
          rm.setAttribute('aria-label', 'Remove tag');
          rm.textContent = '×';
          rm.addEventListener('click', () => removeTag(t));
          span.append(a, rm);
          return span;
        }}

        function removeTag(t) {{
          tags = tags.filter(x => x !== t);
          // Keep 'Favourites' in the underlying list (managed by ♥ button)
          // — only the chips visible here are non-Favourites.
          wrap.querySelectorAll('.tag-chip').forEach(c => {{
            if (c.dataset.tag === t) c.remove();
          }});
          persist();
        }}

        wrap.querySelectorAll('.tag-rm').forEach(btn => {{
          const chip = btn.closest('.tag-chip');
          const t = chip.dataset.tag;
          btn.addEventListener('click', () => removeTag(t));
        }});

        // "+ Tag" opens a picker: every tag already in use (most-used
        // first), filtered as you type, plus "Create" for a new one.
        const menu = document.getElementById('tag-menu');
        const opts = document.getElementById('tag-options');
        const addWrap = document.getElementById('tag-add-wrap');
        let allTags;
        try {{ allTags = JSON.parse(wrap.dataset.allTags || '[]'); }}
        catch (e) {{ allTags = []; }}

        function addTag(v) {{
          v = v.trim();
          if (!v) return;
          // Reuse an existing tag's spelling when only the case differs.
          v = allTags.find(t => t.toLowerCase() === v.toLowerCase()) || v;
          if (!tags.includes(v)) {{
            tags = [...tags, v];
            wrap.insertBefore(makeChip(v), addWrap);
            if (!allTags.includes(v)) allTags.push(v);
            persist();
          }}
          setOpen(false);
        }}

        function optBtn(label, onPick, extraStyle) {{
          const b = document.createElement('button');
          b.type = 'button';
          b.className = 'text-sm text-stone-800 hover:bg-stone-50';
          b.style.cssText = 'display:block;width:100%;text-align:left;padding:0.6rem 1rem;border-bottom:1px solid #f5f5f4;' + (extraStyle || '');
          b.textContent = label;
          // mousedown, not click: fires before the input loses focus.
          b.addEventListener('mousedown', (e) => {{ e.preventDefault(); onPick(); }});
          return b;
        }}

        function render() {{
          const q = input.value.trim();
          const ql = q.toLowerCase();
          opts.replaceChildren();
          const avail = allTags.filter(t => t !== 'Favourites' && !tags.includes(t));
          const shown = avail.filter(t => t.toLowerCase().includes(ql));
          if (q && !allTags.some(t => t.toLowerCase() === ql)) {{
            opts.append(optBtn('+ Create “' + q + '”', () => addTag(q), 'color:#b45309;font-weight:500;'));
          }}
          shown.forEach(t => opts.append(optBtn(t, () => addTag(t))));
          if (!opts.children.length) {{
            const p = document.createElement('div');
            p.className = 'text-xs text-stone-400';
            p.style.padding = '0.75rem 1rem';
            p.textContent = q ? 'Already on this recipe.' : 'No other tags yet — type one to create it.';
            opts.append(p);
          }}
        }}

        function setOpen(open) {{
          menu.hidden = !open;
          addBtn.setAttribute('aria-expanded', open);
          if (open) {{
            // Keep the menu on screen when "+ Tag" sits near the right edge.
            const vw = document.documentElement.clientWidth;
            const width = Math.min(240, vw - 32);
            const left = addWrap.getBoundingClientRect().left;
            menu.style.width = width + 'px';
            menu.style.left = Math.min(0, vw - 16 - width - left) + 'px';
            input.value = ''; render(); input.focus();
          }}
        }}

        addBtn.addEventListener('click', (e) => {{ e.stopPropagation(); setOpen(menu.hidden); }});
        document.addEventListener('click', (e) => {{ if (!addWrap.contains(e.target)) setOpen(false); }});
        input.addEventListener('input', render);
        input.addEventListener('keydown', (e) => {{
          if (e.key === 'Enter') {{ e.preventDefault(); addTag(input.value); }}
          else if (e.key === 'Escape') setOpen(false);
        }});
      }})();
      </script>

      <script>
      // Serving-scaler — bump the SERVES stepper and every ingredient
      // quantity rescales in place. Pure JS, no fetch (the recipe's
      // canonical servings stays the same; this is a viewer override).
      (function() {{
        const DEFAULT_SRV = {r.servings or 0};
        if (!DEFAULT_SRV) return;
        let current = DEFAULT_SRV;
        const srvVal = document.getElementById('srv-val');
        const srvDec = document.getElementById('srv-dec');
        const srvInc = document.getElementById('srv-inc');
        const indicator = document.getElementById('scale-indicator');
        if (!srvVal || !srvDec || !srvInc) return;

        // JS mirror of _fmt_qty — fractions for halves/quarters,
        // integer when integral, short decimal otherwise.
        function fmtQty(q) {{
          if (q == null || isNaN(q)) return '';
          if (Math.abs(q - Math.round(q)) < 0.01) return String(Math.round(q));
          const integer = Math.floor(q);
          const frac = q - integer;
          const glyphs = [[0.125,'⅛'],[0.25,'¼'],[0.333,'⅓'],[0.5,'½'],[0.667,'⅔'],[0.75,'¾']];
          for (const [f, g] of glyphs) {{
            if (Math.abs(frac - f) < 0.03) return (integer ? integer : '') + g;
          }}
          // 1 decimal if >= 1, else 2 decimals; strip trailing zeros.
          const fixed = q >= 1 ? q.toFixed(1) : q.toFixed(2);
          return fixed.replace(/\\.?0+$/, '');
        }}

        function rescale() {{
          srvVal.textContent = current;
          const factor = current / DEFAULT_SRV;
          document.querySelectorAll('.ing-qty').forEach(el => {{
            const baseQty = parseFloat(el.dataset.qty || '0');
            if (!baseQty) return;
            const unit = el.dataset.unit || '';
            const scaled = baseQty * factor;
            el.textContent = (unit ? (fmtQty(scaled) + ' ' + unit) : fmtQty(scaled));
          }});
          if (factor === 1) {{
            indicator.hidden = true;
          }} else {{
            indicator.hidden = false;
            const f = factor.toFixed(2).replace(/\\.?0+$/, '');
            indicator.textContent = `× ${{f}} from original ${{DEFAULT_SRV}} srv — taps below scale with you`;
          }}
        }}

        srvDec.addEventListener('click', () => {{
          if (current > 1) {{ current--; rescale(); }}
        }});
        srvInc.addEventListener('click', () => {{
          if (current < 99) {{ current++; rescale(); }}
        }});
      }})();
      </script>

      <script>
      // Sticky notes — debounced auto-save on typing + immediate save
      // on blur. Saves via PATCH /recipes/{id} with notes_markdown.
      // Shows a "Saving…" → "Saved" indicator so you know it took.
      (function() {{
        const ta = document.getElementById('recipe-notes');
        if (!ta) return;
        const status = document.getElementById('notes-status');
        const id = ta.dataset.recipeId;
        let lastSaved = ta.value;
        let timer = null;
        let inflight = false;
        function flash(text, tone) {{
          status.hidden = false;
          status.textContent = text;
          status.classList.remove('text-stone-400', 'text-emerald-700', 'text-rose-700');
          status.classList.add(
            tone === 'ok' ? 'text-emerald-700' :
            tone === 'err' ? 'text-rose-700' :
            'text-stone-400'
          );
        }}
        async function save() {{
          if (inflight) return;
          const val = ta.value;
          if (val === lastSaved) return;
          inflight = true;
          flash('Saving…', 'pending');
          try {{
            const r = await fetch('{PREFIX}/recipes/' + id, {{
              method: 'PATCH',
              headers: {{ 'Content-Type': 'application/json' }},
              body: JSON.stringify({{ notes_markdown: val }}),
            }});
            if (!r.ok) throw new Error('HTTP ' + r.status);
            lastSaved = val;
            flash('Saved', 'ok');
            setTimeout(() => {{ status.hidden = true; }}, 1500);
          }} catch (e) {{
            flash('Save failed', 'err');
          }} finally {{
            inflight = false;
          }}
        }}
        ta.addEventListener('input', () => {{
          if (timer) clearTimeout(timer);
          timer = setTimeout(save, 800);  // debounce 800ms of inactivity
        }});
        ta.addEventListener('blur', () => {{
          if (timer) {{ clearTimeout(timer); timer = null; }}
          save();
        }});
      }})();
      </script>
    """
    return HTMLResponse(_shell(name, body, active="library"))


def _add_to_plan_modal(
    r: Recipe, occupied: dict[tuple[str, str], str], per_slot: int = 2
) -> str:
    """Cook-day picker — pick servings, tap ONE day+meal, and the
    planner auto-fills leftovers forward (cook + leftovers) from the
    servings math. This is the original simple flow; the old
    multi-select 'pick every slot' grid was a wrong turn."""
    today = _Date.today()
    default_srv = r.servings or 4
    day_rows: list[str] = []
    for i in range(7):
        d = today + _Td(days=i)
        iso = d.isoformat()
        label = "Today" if i == 0 else "Tomorrow" if i == 1 else d.strftime("%A")
        sub = d.strftime("%a %b %-d") if i < 2 else d.strftime("%b %-d")
        cells = []
        for meal in ("breakfast", "lunch", "dinner"):
            taken = occupied.get((iso, meal))
            if taken:
                cells.append(
                    f"<button type='button' disabled class='atp-slot atp-taken'>"
                    f"<span class='atp-meal-label'>{meal[0].upper()}</span>"
                    f"<span class='atp-taken-name'>{_h(taken)}</span>"
                    f"</button>"
                )
            else:
                cells.append(
                    f"<button type='button' class='atp-slot atp-open' "
                    f"data-date='{iso}' data-meal='{meal}'>"
                    f"<span class='atp-meal-label'>{meal[0].upper()}</span>"
                    f"<span class='atp-meal-sub'>{meal}</span>"
                    f"</button>"
                )
        day_rows.append(
            f"<div class='atp-day-row'>"
            f"  <div class='atp-day-label'>"
            f"    <div class='atp-day-name'>{label}</div>"
            f"    <div class='atp-day-sub'>{sub}</div>"
            f"  </div>"
            f"  <div class='atp-day-slots'>{''.join(cells)}</div>"
            f"</div>"
        )
    return f"""
<div id="atp-modal" class="atp-modal" role="dialog" aria-modal="true">
  <div class="atp-sheet">
    <div class="flex items-baseline justify-between mb-2">
      <h2 class="serif text-xl font-medium tracking-tight">Add to plan</h2>
      <button type="button" id="atp-close" class="text-stone-400 text-3xl leading-none p-2 -mt-2 -mr-2">×</button>
    </div>
    <div class="flex items-center justify-between gap-3 mb-3">
      <span class="serif italic font-medium text-stone-900 truncate">{_h(r.display_title())}</span>
      <div class="inline-flex items-center border border-stone-200 rounded-lg overflow-hidden bg-white shrink-0">
        <button type="button" id="atp-srv-dec" class="px-3 py-1.5 text-stone-700 hover:bg-stone-100 border-r border-stone-200">−</button>
        <span class="px-3 py-1.5 min-w-[3rem] text-center text-sm">
          <span id="atp-srv-val" class="font-semibold tabular-nums">{default_srv}</span><span class="text-stone-400"> srv</span>
        </span>
        <button type="button" id="atp-srv-inc" class="px-3 py-1.5 text-stone-700 hover:bg-stone-100 border-l border-stone-200">+</button>
      </div>
    </div>
    <div class="text-[11px] font-semibold text-stone-700 uppercase tracking-wider mb-2">
      Pick a day to cook — <span id="atp-fill-hint" class="text-stone-500 normal-case font-normal tracking-normal"></span>
    </div>
    <div class="atp-days">{''.join(day_rows)}</div>
    <p id="atp-status" class="text-sm text-center text-stone-500 mt-3 min-h-[1.25em]"></p>
  </div>
</div>
<style>
  #atp-modal {{ display: none; position: fixed; inset: 0; z-index: 100; background: rgba(28,25,23,0.45); align-items: flex-end; justify-content: center; }}
  #atp-modal.is-open {{ display: flex; }}
  #atp-modal .atp-sheet {{ background: white; border-radius: 1.25rem 1.25rem 0 0; width: 100%; max-width: 28rem; max-height: 90vh; overflow-y: auto; padding: 1.25rem 1.25rem calc(1.5rem + env(safe-area-inset-bottom)); box-shadow: 0 -16px 48px -8px rgba(0,0,0,0.22); }}
  #atp-modal .atp-days {{ display: flex; flex-direction: column; gap: 0.5rem; }}
  #atp-modal .atp-day-row {{ display: flex; align-items: stretch; gap: 0.6rem; }}
  #atp-modal .atp-day-label {{ width: 5.5rem; flex-shrink: 0; display: flex; flex-direction: column; justify-content: center; }}
  #atp-modal .atp-day-name {{ font-family: 'Fraunces', Georgia, serif; font-weight: 500; font-size: 0.95rem; line-height: 1.1; }}
  #atp-modal .atp-day-sub {{ font-size: 0.7rem; color: #a8a29e; margin-top: 0.05rem; }}
  #atp-modal .atp-day-slots {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0.35rem; flex: 1; min-width: 0; }}
  #atp-modal .atp-slot {{ text-align: center; padding: 0.45rem 0.4rem; border-radius: 0.55rem; border: 1px solid #e7e5e4; background: white; cursor: pointer; display: flex; flex-direction: column; align-items: center; gap: 0.05rem; font-size: 13px; min-width: 0; }}
  #atp-modal .atp-slot.atp-open:hover, #atp-modal .atp-slot.atp-open:active {{ background: #fffbeb; border-color: #fcd34d; }}
  /* Selected = amber filled — multi-select pattern, not immediate submit. */
  #atp-modal .atp-slot.is-picked {{ background: #FDE68A; border-color: #F59E0B; }}
  #atp-modal .atp-slot.is-picked .atp-meal-label {{ color: #78350F; }}
  #atp-modal .atp-meal-label {{ font-weight: 700; color: #1c1917; font-size: 13px; letter-spacing: 0.05em; }}
  #atp-modal .atp-meal-sub {{ font-size: 0.6rem; color: #a8a29e; text-transform: uppercase; letter-spacing: 0.08em; }}
  #atp-modal .atp-taken {{ background: #f5f5f4; cursor: not-allowed; opacity: 0.65; overflow: hidden; }}
  #atp-modal .atp-taken .atp-meal-label {{ color: #78716c; }}
  #atp-modal .atp-taken-name {{ font-family: 'Fraunces', Georgia, serif; font-style: italic; font-size: 0.65rem; color: #57534e; line-height: 1.1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }}
</style>
<script>
(function() {{
  const RECIPE_ID = {r.id};
  const openBtn = document.getElementById('atp-open');
  const modal = document.getElementById('atp-modal');
  const closeBtn = document.getElementById('atp-close');
  const status = document.getElementById('atp-status');
  const srvVal = document.getElementById('atp-srv-val');
  const srvDec = document.getElementById('atp-srv-dec');
  const srvInc = document.getElementById('atp-srv-inc');
  const fab = document.getElementById('fb-fab');
  let servings = {default_srv};
  const PER_SLOT = {per_slot};
  const fillHint = document.getElementById('atp-fill-hint');
  function fillsFor(s) {{ return Math.max(1, Math.ceil(s / PER_SLOT)); }}
  function refreshHint() {{
    const n = fillsFor(servings);
    fillHint.textContent = `fills ${{n}} ${{n === 1 ? 'meal' : 'meals'}} (${{PER_SLOT}}/meal)`;
  }}
  function show() {{ modal.classList.add('is-open'); if (fab) fab.style.display = 'none'; status.textContent = ''; refreshHint(); }}
  function hide() {{ modal.classList.remove('is-open'); if (fab) fab.style.display = ''; }}
  openBtn.addEventListener('click', show);
  closeBtn.addEventListener('click', hide);
  modal.addEventListener('click', (e) => {{ if (e.target === modal) hide(); }});
  document.addEventListener('keydown', (e) => {{ if (e.key === 'Escape') hide(); }});
  srvDec.addEventListener('click', () => {{ servings = Math.max(1, servings - 1); srvVal.textContent = servings; refreshHint(); }});
  srvInc.addEventListener('click', () => {{ servings = Math.min(40, servings + 1); srvVal.textContent = servings; refreshHint(); }});
  // Single-pick: tap ONE day+meal = the cook slot. The planner
  // auto-fills leftovers forward from the servings math (/plan/place).
  let placing = false;
  document.querySelectorAll('#atp-modal .atp-slot.atp-open').forEach(btn => {{
    btn.addEventListener('click', async () => {{
      if (placing) return;
      placing = true;
      document.querySelectorAll('#atp-modal .atp-slot.is-picked').forEach(b => b.classList.remove('is-picked'));
      btn.classList.add('is-picked');
      const n = fillsFor(servings);
      status.textContent = `Adding — fills ${{n}} ${{n === 1 ? 'meal' : 'meals'}}…`;
      try {{
        const r = await fetch('{PREFIX}/plan/place', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            recipe_id: RECIPE_ID, date: btn.dataset.date,
            meal: btn.dataset.meal, servings,
          }}),
        }});
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
        status.textContent = 'Added!';
        setTimeout(() => {{ location.href = '{PREFIX}/app/plan'; }}, 400);
      }} catch (e) {{
        status.textContent = 'Failed: ' + e.message;
        btn.classList.remove('is-picked');
        placing = false;
      }}
    }});
  }});
}})();
</script>
"""


# =============================================================================
# Draft review
# =============================================================================

@router.get("/app/recipe/{recipe_id}/draft", response_class=HTMLResponse)
def recipe_draft_page(recipe_id: int, db: Session = Depends(get_session)) -> HTMLResponse:
    r = db.get(Recipe, recipe_id)
    if r is None:
        raise HTTPException(status_code=404, detail="recipe not found")
    if r.archived:
        # Discarded — a stale draft URL must not let Save resurrect it.
        raise HTTPException(status_code=404, detail="recipe not found")
    # Saved recipes reach this screen via "✎ Edit": same editor, but no
    # Discard (that would archive a live recipe) and edit-flavoured labels.
    editing = r.reviewed_at is not None

    ing_sections = db.exec(
        select(IngredientSection)
        .where(IngredientSection.recipe_id == recipe_id)
        .order_by(IngredientSection.order)
    ).all()
    step_sections = db.exec(
        select(StepSection)
        .where(StepSection.recipe_id == recipe_id)
        .order_by(StepSection.order)
    ).all()

    def _num(v) -> str:
        return "" if v is None else str(v)

    ing_html = []
    for sect in ing_sections:
        ings = db.exec(
            select(Ingredient)
            .where(Ingredient.section_id == sect.id)
            .order_by(Ingredient.order)
        ).all()
        if not ings:
            continue
        if sect.name:
            ing_html.append(
                f"<div class='text-[11px] uppercase tracking-wider text-stone-500 font-semibold mt-4 mb-1.5'>{_h(sect.name)}</div>"
            )
        for i in ings:
            failed = i.display_name == "unknown"
            name_val = _h(i.source_line or i.raw_text or "") if failed else _h(i.display_name or "")
            row_style = (
                "background:#FFFBEB;border:1px solid #FDE68A;" if failed
                else "background:#fff;border:1px solid #f5f5f4;"
            )
            flag = (
                "<div class='text-[10px] text-amber-700 font-semibold mt-1'>⚠ Couldn't parse — fix the name, amount and unit</div>"
                if failed else ""
            )
            # The original scraped/dictated line, so mis-parses are
            # spottable at a glance. Amber rows already show it in the
            # name field — no point repeating it.
            src = (i.source_line or "").strip()
            prep = (i.prep_note or "").strip().strip("()")
            # Parser sometimes keeps the prep word in the name too
            # ("steamed jasmine rice" + prep "steamed") — skip the
            # chip when it adds nothing.
            # Parser sometimes echoes the prep word in the name too
            # ("steamed jasmine rice" + prep "steamed") — don't preload a
            # redundant note; the user can still add one.
            prep_redundant = bool(prep) and prep.lower() in (i.display_name or "").lower()
            prep_attr = "" if (failed or prep_redundant) else prep
            # Likely mis-parse: parsed name barely overlaps the source line.
            divergent = (not failed) and _parse_divergence(
                i.source_line, i.display_name, i.prep_note
            )
            # The prep + optional chips are rendered/edited entirely in JS
            # from these data-* attrs, so an edit can re-render in place.
            meta_div = (
                "<div class='ing-meta flex flex-wrap items-center gap-1' "
                f"data-prep=\"{_h(prep_attr)}\" "
                f"data-optional='{1 if i.optional else 0}' "
                f"data-divergent='{1 if divergent else 0}' "
                f"data-failed='{1 if failed else 0}'></div>"
            )
            # Grocery category — a Mise-styled chip button (shrinks to the
            # word) that opens a shared bottom sheet, not the iOS picker.
            cur_cat = i.category or "Other"
            cat_chip = (
                f"<button type='button' class='ing-cat inline-flex items-center gap-1 "
                "bg-stone-100 text-stone-600 rounded-full shrink-0' "
                f"data-cat=\"{_h(cur_cat)}\" "
                "style=\"font-size:10px;font-weight:500;line-height:1;padding:4px 9px;"
                "border:1px solid #e7e5e4;\">"
                f"<span class='ing-cat-label'>{_h(cur_cat)}</span>"
                "<span style='font-size:7px;opacity:0.55;'>▾</span></button>"
            )
            chips_html = (
                "<div class='flex items-center justify-between gap-1 mt-1.5 px-1'>"
                f"{meta_div}{cat_chip}</div>"
            )
            source_caption = (
                f"<div class='text-[11px] text-stone-400 mb-1.5 px-1 leading-snug break-words'>{_h(src)}</div>"
                if src and not failed else ""
            )
            ing_html.append(f"""
              <div class="rounded-xl px-3 py-2.5 mb-1.5 ing-row" style="{row_style}" data-ing-id="{i.id}" data-failed="{1 if failed else 0}">
                {source_caption}
                <div class="flex items-center gap-2">
                  <input data-f="qty" type="number" inputmode="decimal" step="0.25" min="0" value="{_num(i.qty)}"
                         class="w-16 border border-stone-200 rounded-lg px-2 py-1.5 text-sm bg-white text-right" placeholder="qty">
                  <input data-f="unit" type="text" value="{_h(i.unit or '')}"
                         class="w-16 border border-stone-200 rounded-lg px-2 py-1.5 text-sm bg-white" placeholder="unit">
                  <input data-f="display_name" type="text" value="{name_val}"
                         class="flex-1 min-w-0 border border-stone-200 rounded-lg px-2 py-1.5 text-sm bg-white" placeholder="ingredient">
                  <button type="button" class="ing-del text-stone-300 hover:text-rose-600 text-lg px-1 shrink-0">×</button>
                </div>
                {chips_html}{flag}
              </div>
            """)

    step_html = []
    step_counter = 0
    for sect in step_sections:
        steps = db.exec(
            select(Step).where(Step.section_id == sect.id).order_by(Step.order)
        ).all()
        if not steps:
            continue
        if sect.name:
            step_html.append(
                f"<div class='text-[11px] uppercase tracking-wider text-stone-500 font-semibold mt-4 mb-1.5'>{_h(sect.name)}</div>"
            )
        for s in steps:
            step_counter += 1
            step_html.append(f"""
              <div class="rounded-xl px-3 py-2.5 mb-1.5 flex gap-2.5 items-start" style="background:#fff;border:1px solid #f5f5f4;">
                <div class="serif text-stone-400 text-sm pt-1.5 w-5 text-right shrink-0">{step_counter}</div>
                <textarea data-step-id="{s.id}" rows="2"
                  class="flex-1 min-w-0 border-0 bg-transparent text-sm leading-relaxed p-0 pt-1"
                  style="resize:none;outline:none;">{_h(s.text)}</textarea>
                <button type="button" class="step-del text-stone-300 hover:text-rose-600 text-lg px-1 shrink-0">×</button>
              </div>
            """)

    src_href = _safe_href(r.source_url)
    src = (
        f"<a href='{_h(src_href)}' target='_blank' rel='noopener noreferrer' class='text-xs text-stone-400 hover:text-amber-700 underline'>view original ↗</a>"
        if src_href else ""
    )
    discard_btn = "" if editing else (
        '<button id="d-discard" type="button" class="px-4 py-3 rounded-xl text-sm text-rose-700 font-medium" '
        'style="background:#fff;border:1px solid #fecdd3;">Discard</button>'
    )
    body = f"""
      <style>.tab-bar {{ display: none; }}</style>
      <div class="mb-1 text-[10px] uppercase tracking-[0.25em] text-amber-700 font-semibold">{"Editing — changes save as you go" if editing else "Draft — review before saving"}</div>
      <input id="d-title" value="{_h(r.display_title())}"
        class="serif text-3xl font-medium tracking-tight leading-tight w-full bg-transparent border-0 border-b border-dashed border-stone-300"
        style="outline:none;">
      <div class="mt-1">{src}</div>

      <div class="grid grid-cols-5 gap-2 mt-4">
        {"".join(
          f'''<label class="card p-2 text-center block">
                <input data-meta="{field}" type="number" min="0" value="{_num(getattr(r, field))}"
                  class="serif text-lg font-medium w-full text-center bg-transparent border-0" style="outline:none;">
                <div class="text-[10px] text-stone-500 uppercase tracking-wider">{label}</div>
              </label>'''
          for field, label in [
              ("servings", "Serves"), ("prep_minutes", "Prep"),
              ("cook_minutes", "Cook"), ("total_minutes", "Total"), ("oven_temp_f", "°F"),
          ]
        )}
      </div>

      <div class="card p-4 mt-5" style="background:#FAF7F2;">
        <div class="text-[11px] uppercase tracking-[0.2em] text-stone-400 font-semibold mb-2">Ingredients</div>
        {"".join(ing_html) or "<p class='text-sm text-stone-400 serif italic'>None parsed.</p>"}
      </div>

      <div class="card p-4 mt-4" style="background:#FAF7F2;">
        <div class="text-[11px] uppercase tracking-[0.2em] text-stone-400 font-semibold mb-2">Steps</div>
        {"".join(step_html) or "<p class='text-sm text-stone-400 serif italic'>None parsed.</p>"}
      </div>

      <div id="cook-check" class="card p-4 mt-4" style="background:#FAF7F2;" hidden>
        <div class="text-[11px] uppercase tracking-[0.2em] text-stone-400 font-semibold mb-2">Cook mode check</div>
        <div id="cook-check-body" class="text-sm text-stone-700 leading-relaxed"></div>
      </div>

      <div class="h-24"></div>
      <div class="fixed bottom-0 left-0 right-0 p-3" style="background:linear-gradient(transparent, #FAF7F2 35%);z-index:30;padding-bottom:calc(0.75rem + env(safe-area-inset-bottom));">
        <div class="max-w-xl mx-auto flex items-center gap-3">
          <button id="d-save" type="button" class="flex-1 text-white font-medium py-3 rounded-xl text-sm shadow-lg"
            style="background:#047857;">{"Done" if editing else "Save recipe"}</button>
          {discard_btn}
        </div>
      </div>

      <div id="cat-sheet" class="fixed hidden" style="top:0;left:0;right:0;bottom:0;z-index:60;">
        <div id="cat-backdrop" class="absolute" style="top:0;left:0;right:0;bottom:0;background:rgba(28,25,23,0.45);"></div>
        <div class="absolute left-0 right-0 bottom-0 shadow-lg" style="background:#FAF7F2;border-radius:1.25rem 1.25rem 0 0;max-height:72vh;overflow-y:auto;padding-bottom:env(safe-area-inset-bottom);">
          <div class="px-5 pt-4 pb-1 text-[11px] uppercase tracking-[0.2em] text-stone-400 font-semibold">Grocery category</div>
          <div class="px-3 pb-4">
            {"".join(f'<button type="button" class="cat-opt block w-full text-left px-4 py-3 rounded-xl text-sm text-stone-700 hover:bg-stone-100" data-cat="{_h(c)}">{_h(c)}</button>' for c in CATEGORY_CHOICES)}
          </div>
        </div>
      </div>

      <script>
        (function() {{
          const RID = {r.id};

          async function patch(url, body) {{
            const resp = await fetch(url, {{
              method: 'PATCH',
              headers: {{ 'Content-Type': 'application/json' }},
              body: JSON.stringify(body),
            }});
            if (!resp.ok) window.notify('Save failed (HTTP ' + resp.status + ')', {{tone:'error'}});
            else scheduleCookCheck();
            return resp.ok;
          }}

          // Cook mode check: which ingredients no step names, so cook mode
          // can't show their amounts. Refreshes after every save.
          let cookCheckTimer = null;
          function scheduleCookCheck() {{
            clearTimeout(cookCheckTimer);
            cookCheckTimer = setTimeout(runCookCheck, 400);
          }}
          async function runCookCheck() {{
            const box = document.getElementById('cook-check');
            const body = document.getElementById('cook-check-body');
            try {{
              const r = await fetch('{PREFIX}/recipes/' + RID + '/cook-check', {{ cache: 'no-store' }});
              if (!r.ok) return;
              const d = await r.json();
              if (!d.steps) {{ box.hidden = true; return; }}
              box.hidden = false;
              body.replaceChildren();
              if (!d.unmentioned.length) {{
                body.style.color = '#047857';
                body.textContent = '✓ Every ingredient shows up in the steps — cook mode can show all the amounts.';
                return;
              }}
              body.style.color = '';
              const p = document.createElement('p');
              p.textContent = 'Cook mode can’t place these amounts — no step names them:';
              const ul = document.createElement('ul');
              ul.style.cssText = 'margin:0.4rem 0 0.4rem 1.1rem;list-style:disc;color:#92400e;';
              d.unmentioned.forEach(n => {{ const li = document.createElement('li'); li.textContent = n; ul.append(li); }});
              const tip = document.createElement('p');
              tip.className = 'text-xs text-stone-500';
              tip.textContent = 'Name them in a step (e.g. "add the kidney beans") or leave it — it only affects cook mode.';
              body.append(p, ul, tip);
            }} catch (e) {{}}
          }}
          runCookCheck();
          document.addEventListener('click', (e) => {{
            if (e.target.closest('.ing-del, .step-del')) setTimeout(scheduleCookCheck, 600);
          }});

          const slug = s => (s || '').toLowerCase().replace(/[^a-z0-9\\s]/g, '').trim().replace(/\\s+/g, '_') || 'unknown';

          // Prep + optional chips: rendered from the row's data-* attrs so an
          // edit re-renders in place. Tapping prep edits it inline (empty
          // clears); the optional chip toggles; both PATCH the ingredient.
          const CHIP = 'inline-flex items-center rounded-full';
          function chipBtn(text, style) {{
            const b = document.createElement('button');
            b.type = 'button'; b.className = CHIP; b.style.cssText = style; b.textContent = text;
            return b;
          }}
          function editPrep(meta, id, btn) {{
            const inp = document.createElement('input');
            inp.type = 'text'; inp.value = meta.dataset.prep || ''; inp.placeholder = 'prep note';
            inp.style.cssText = 'font-size:11px;border:1px solid #d6d3d1;border-radius:9999px;padding:2px 9px;min-width:130px;background:#fff;';
            btn.replaceWith(inp); inp.focus();
            let done = false;
            const commit = () => {{
              if (done) return; done = true;
              const v = inp.value.trim();
              meta.dataset.prep = v;
              patch('{PREFIX}/recipes/ingredient/' + id, {{ prep_note: v || null }});
              renderMeta(meta, id);
            }};
            inp.addEventListener('blur', commit);
            inp.addEventListener('keydown', e => {{ if (e.key === 'Enter') {{ e.preventDefault(); inp.blur(); }} }});
          }}
          function renderMeta(meta, id) {{
            const prep = meta.dataset.prep || '';
            const optional = meta.dataset.optional === '1';
            const divergent = meta.dataset.divergent === '1';
            const failed = meta.dataset.failed === '1';
            meta.innerHTML = '';
            if (failed) return;  // amber rows: fix name/qty/unit first
            const prepBtn = prep
              ? chipBtn('✂ ' + prep, 'font-size:10px;background:#f5f5f4;color:#57534e;padding:3px 9px;')
              : chipBtn('✂ note', 'font-size:10px;background:transparent;color:#a8a29e;border:1px dashed #d6d3d1;padding:2px 8px;');
            prepBtn.addEventListener('click', () => editPrep(meta, id, prepBtn));
            meta.appendChild(prepBtn);
            const optBtn = optional
              ? chipBtn('optional', 'font-size:10px;background:#fffbeb;color:#92400e;border:1px solid #fde68a;padding:2px 8px;')
              : chipBtn('+ optional', 'font-size:10px;background:transparent;color:#a8a29e;border:1px dashed #d6d3d1;padding:2px 8px;');
            optBtn.addEventListener('click', () => {{
              const now = meta.dataset.optional === '1';
              meta.dataset.optional = now ? '0' : '1';
              patch('{PREFIX}/recipes/ingredient/' + id, {{ optional: !now }});
              renderMeta(meta, id);
            }});
            meta.appendChild(optBtn);
            if (divergent) {{
              const c = chipBtn('⚠ check', 'font-size:10px;font-weight:600;background:#fef3c7;color:#78350f;padding:3px 9px;');
              c.disabled = true; c.style.cursor = 'default';
              meta.appendChild(c);
            }}
          }}

          // Title + header metrics → PATCH /recipes/{{id}} on blur/change.
          document.getElementById('d-title').addEventListener('blur', (e) => {{
            const v = e.target.value.trim();
            if (v) patch('{PREFIX}/recipes/' + RID, {{ name_english: v }});
          }});
          document.querySelectorAll('[data-meta]').forEach(inp => {{
            inp.addEventListener('blur', () => {{
              const v = inp.value.trim();
              patch('{PREFIX}/recipes/' + RID, {{ [inp.dataset.meta]: v === '' ? null : parseInt(v, 10) }});
            }});
          }});

          // Ingredient rows → PATCH /recipes/ingredient/{{id}} on blur.
          document.querySelectorAll('.ing-row').forEach(row => {{
            const id = row.dataset.ingId;
            row.querySelectorAll('input').forEach(inp => {{
              inp.addEventListener('blur', async () => {{
                const get = f => row.querySelector('[data-f="' + f + '"]').value.trim();
                const body = {{
                  qty: get('qty') === '' ? null : parseFloat(get('qty')),
                  unit: get('unit') || null,
                  display_name: get('display_name'),
                }};
                if (row.dataset.failed === '1' && body.display_name && body.display_name !== 'unknown') {{
                  // Hand-fixed amber row: give it a real aggregation key so
                  // it shows up on shopping lists. patch_ingredient mirrors
                  // this into IngredientAlias as a manual (sacred) entry.
                  body.canonical_name = slug(body.display_name);
                  if (await patch('{PREFIX}/recipes/ingredient/' + id, body)) {{
                    row.dataset.failed = '0';
                    row.style.background = '#fff';
                    row.style.border = '1px solid #f5f5f4';
                    const flag = row.querySelector('.text-amber-700');
                    if (flag) flag.remove();
                    const m = row.querySelector('.ing-meta');
                    if (m) {{ m.dataset.failed = '0'; renderMeta(m, id); }}
                  }}
                  return;
                }}
                patch('{PREFIX}/recipes/ingredient/' + id, body);
              }});
            }});
            const meta = row.querySelector('.ing-meta');
            if (meta) renderMeta(meta, id);
            const catBtn = row.querySelector('.ing-cat');
            if (catBtn) catBtn.addEventListener('click', () => openCatSheet(id, catBtn));
            row.querySelector('.ing-del').addEventListener('click', async () => {{
              const resp = await fetch('{PREFIX}/recipes/ingredient/' + id, {{ method: 'DELETE' }});
              if (resp.ok) row.remove(); else window.notify('Delete failed', {{tone:'error'}});
            }});
          }});

          // Shared category bottom sheet — one sheet, set the row that opened it.
          const catSheet = document.getElementById('cat-sheet');
          let catTarget = null;  // {{ id, btn }}
          function openCatSheet(id, btn) {{
            catTarget = {{ id, btn }};
            const cur = btn.dataset.cat;
            catSheet.querySelectorAll('.cat-opt').forEach(o => {{
              const on = o.dataset.cat === cur;
              o.style.background = on ? '#e7e5e4' : '';
              o.style.fontWeight = on ? '600' : '';
            }});
            catSheet.classList.remove('hidden');
          }}
          function closeCatSheet() {{ catSheet.classList.add('hidden'); catTarget = null; }}
          document.getElementById('cat-backdrop').addEventListener('click', closeCatSheet);
          catSheet.querySelectorAll('.cat-opt').forEach(opt => {{
            opt.addEventListener('click', () => {{
              if (!catTarget) return;
              const cat = opt.dataset.cat;
              catTarget.btn.dataset.cat = cat;
              catTarget.btn.querySelector('.ing-cat-label').textContent = cat;
              patch('{PREFIX}/recipes/ingredient/' + catTarget.id, {{ category: cat }});
              closeCatSheet();
            }});
          }});

          // Steps → PATCH /recipes/step/{{id}} on blur; × deletes.
          // Auto-grow so the full step text is readable — this is a review screen.
          const fit = ta => {{ ta.style.height = 'auto'; ta.style.height = ta.scrollHeight + 'px'; }};
          document.querySelectorAll('[data-step-id]').forEach(ta => {{
            fit(ta);
            ta.addEventListener('input', () => fit(ta));
            ta.addEventListener('blur', () => {{
              const v = ta.value.trim();
              if (v) patch('{PREFIX}/recipes/step/' + ta.dataset.stepId, {{ text: v }});
            }});
            ta.closest('div').querySelector('.step-del').addEventListener('click', async () => {{
              const resp = await fetch('{PREFIX}/recipes/step/' + ta.dataset.stepId, {{ method: 'DELETE' }});
              if (resp.ok) ta.closest('div').remove(); else window.notify('Delete failed', {{tone:'error'}});
            }});
          }});

          document.getElementById('d-save').addEventListener('click', async () => {{
            // Flush any focused edit first.
            if (document.activeElement) document.activeElement.blur();
            const resp = await fetch('{PREFIX}/recipes/' + RID + '/review/complete', {{ method: 'POST' }});
            if (!resp.ok) {{ window.notify('Save failed (HTTP ' + resp.status + ')', {{tone:'error'}}); return; }}
            window.location = '{PREFIX}/app/recipe/' + RID;
          }});

          document.getElementById('d-discard')?.addEventListener('click', async () => {{
            const ok = await window.askConfirm({{
              message: 'Discard this draft?',
              sub: 'It moves to the rejected backlog — you can restore it from Tools later.',
              okLabel: 'Discard', danger: true,
            }});
            if (!ok) return;
            const resp = await fetch('{PREFIX}/recipes/' + RID, {{
              method: 'PATCH',
              headers: {{ 'Content-Type': 'application/json' }},
              body: JSON.stringify({{ archived: true, archived_reason: 'Draft discarded' }}),
            }});
            if (resp.ok) window.location = '{PREFIX}/app/';
            else window.notify('Discard failed', {{tone:'error'}});
          }});
        }})();
      </script>
    """
    return HTMLResponse(_shell("Edit" if editing else "Draft", body, active="library"))


# =============================================================================
# Plan
# =============================================================================

@router.get("/app/plan", response_class=HTMLResponse)
def plan_page(
    db: Session = Depends(get_session),
    start: str = "",
) -> HTMLResponse:
    if start:
        try:
            start_date = _Date.fromisoformat(start)
        except ValueError:
            start_date = _Date.today()
    else:
        start_date = _Date.today()

    # One extra day so the last card can still show "make tomorrow's lunch".
    view = planner_service.get_view(start_date, 8, db)
    next_day = {a.date: b for a, b in zip(view, view[1:])}
    view = view[:7]

    day_blocks: list[str] = []
    for d in view:
        slot_rows = []
        for s in d.slots:
            if s.note:
                # Free-text entry — no recipe behind it, so no swap/move;
                # just the text and a remove button.
                slot_rows.append(
                    f"<div class='slot-note flex items-center gap-3 px-5 py-2 border-b border-stone-100 last:border-0'>"
                    f"  <div class='shrink-0 w-10 h-10 rounded-lg bg-stone-100 flex items-center justify-center text-lg'>📝</div>"
                    f"  <span class='uppercase text-stone-400 font-semibold shrink-0' style='font-size:10px;letter-spacing:0.1em;width:4.25rem;white-space:nowrap;'>{_h(s.meal)}</span>"
                    f"  <div class='flex-1 min-w-0'><span class='serif text-lg font-medium'>{_h(s.note)}</span></div>"
                    f"  <button type='button' class='note-del text-stone-300 hover:text-rose-600 px-2 text-xl leading-none shrink-0' "
                    f"data-date='{s.date.isoformat()}' data-meal='{s.meal}' data-text='{_h(s.note)}' aria-label='Remove'>×</button>"
                    f"</div>"
                )
                continue
            if s.recipe_id is None:
                thumb = (
                    "<div class='shrink-0 w-10 h-10 rounded-lg border-2 border-dashed border-amber-300 "
                    "flex items-center justify-center text-amber-700 text-lg font-medium drop-thumb hover:border-amber-500 transition-colors'>+</div>"
                )
                cell = (
                    "<span class='text-amber-800 serif italic text-base font-medium'>Add a meal</span>"
                )
                actions = ""
            else:
                emoji = _recipe_emoji(s.recipe_name)
                color = _recipe_color(s.recipe_id)
                thumb = (
                    f"<div class='shrink-0 w-10 h-10 rounded-lg {color} "
                    f"flex items-center justify-center text-lg'>{emoji}</div>"
                )
                is_lead = (s.position or 1) == 1 and (s.total_slots or 0) > 1
                lead_html = (
                    "<span class='inline-block bg-amber-100 text-amber-900 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider mr-1'>⚓ Leads</span>"
                    if is_lead else ""
                )
                if (s.position or 1) > 1:
                    note = f"<div class='text-xs text-stone-400 mt-0.5'>leftover {s.position}/{s.total_slots}</div>"
                elif s.recipe_servings and s.total_slots:
                    note = f"<div class='text-xs text-stone-400 mt-0.5'>{s.recipe_servings} srv · fills {s.total_slots} {'meal' if s.total_slots == 1 else 'meals'}</div>"
                else:
                    note = ""
                cell = (
                    f"<div>"
                    f"  {lead_html}"
                    f"  <a href='{PREFIX}/app/recipe/{s.recipe_id}' class='serif text-lg font-medium hover:text-amber-800'>{_h(s.recipe_name)}</a>"
                    f"  {note}"
                    f"</div>"
                )
                # Per-slot actions trigger — opens the sheet with this
                # slot's context. Only on cook slots; leftover positions
                # share fate with their parent cook session.
                if (s.position or 1) == 1:
                    actions = (
                        f"<button type='button' class='slot-actions text-stone-300 hover:text-stone-700 px-2 text-xl leading-none shrink-0' "
                        f"data-date='{s.date.isoformat()}' data-meal='{s.meal}' "
                        f"data-recipe-id='{s.recipe_id}' data-recipe-name='{_h(s.recipe_name)}' "
                        f"aria-label='Slot actions'>···</button>"
                    )
                else:
                    actions = ""
            # Wrap each row in a swipe-shell — swipe-left reveals quick
            # Swap + Delete actions underneath (household request A).
            # Only cook slots get the swipe gesture; empty/leftover
            # rows just render flat.
            if s.recipe_id is not None and (s.position or 1) == 1:
                row_inner = (
                    f"<div class='swipe-row-inner flex items-center gap-3 px-5 py-2 bg-[#FAF7F2]' "
                    f"data-date='{s.date.isoformat()}' data-meal='{s.meal}' "
                    f"data-recipe-id='{s.recipe_id}' data-recipe-name='{_h(s.recipe_name)}'>"
                    f"  {thumb}"
                    f"  <span class='uppercase text-stone-400 font-semibold shrink-0' style='font-size:10px;letter-spacing:0.1em;width:4.25rem;white-space:nowrap;'>{_h(s.meal)}</span>"
                    f"  <div class='flex-1 min-w-0'>{cell}</div>"
                    f"  {actions}"
                    f"</div>"
                )
                slot_rows.append(
                    f"<div class='swipe-row relative overflow-hidden border-b border-stone-100 last:border-0'>"
                    f"  <div class='swipe-actions absolute right-0 top-0 bottom-0 flex items-stretch'>"
                    f"    <button type='button' class='swipe-swap bg-amber-500 text-white px-4 text-sm font-medium'>Swap</button>"
                    f"    <button type='button' class='swipe-del bg-rose-600 text-white px-4 text-sm font-medium'>Delete</button>"
                    f"  </div>"
                    f"  {row_inner}"
                    f"</div>"
                )
            else:
                # Empty slots become drop targets AND tappable add-
                # affordances (open the recipe picker in 'add' mode).
                # Leftover slots render flat — not draggable, not
                # droppable, share fate with the cook session.
                is_empty = s.recipe_id is None
                if is_empty:
                    slot_rows.append(
                        f"<button type='button' class='slot-add w-full flex items-center gap-3 px-5 py-2 border-b border-stone-100 last:border-0 drop-target text-left' "
                        f"data-empty='1' data-date='{s.date.isoformat()}' data-meal='{s.meal}'>"
                        f"  {thumb}"
                        f"  <span class='uppercase text-stone-400 font-semibold shrink-0' style='font-size:10px;letter-spacing:0.1em;width:4.25rem;white-space:nowrap;'>{_h(s.meal)}</span>"
                        f"  <div class='flex-1 min-w-0'>{cell}</div>"
                        f"  <span class='text-amber-400 shrink-0 text-lg leading-none' aria-hidden='true'>›</span>"
                        f"</button>"
                    )
                else:
                    # Leftover row — also a valid drop target so a session
                    # can be moved onto a slot currently occupied by its
                    # own leftover (e.g. moving the cook forward to the
                    # next day where the leftover lives). The backend's
                    # move() short-circuits when dst.session_id ==
                    # src.session_id; cross-session drops still raise
                    # cleanly. Without this class the drag-handler can't
                    # see the slot as droppable at all.
                    slot_rows.append(
                        f"<div class='slot-leftover flex items-center gap-3 px-5 py-2 border-b border-stone-100 last:border-0 drop-target' "
                        f"data-date='{s.date.isoformat()}' data-meal='{s.meal}'>"
                        f"  {thumb}"
                        f"  <span class='uppercase text-stone-400 font-semibold shrink-0' style='font-size:10px;letter-spacing:0.1em;width:4.25rem;white-space:nowrap;'>{_h(s.meal)}</span>"
                        f"  <div class='flex-1 min-w-0'>{cell}</div>"
                        f"  {actions}"
                        f"</div>"
                    )

        # A freshly-cooked lunch (not a leftover) gets made the evening
        # before — it may be eaten at the office — so the reminder sits
        # under that evening's dinner.
        for nxt in next_day[d.date].slots:
            if nxt.meal == "lunch" and nxt.recipe_id is not None and nxt.position == 1:
                slot_rows.append(
                    f"<a href='{PREFIX}/app/recipe/{nxt.recipe_id}' class='make-ahead flex items-center gap-3 px-5 py-2 border-b border-stone-100 last:border-0' "
                    f"style='background:#FFFBEB;'>"
                    f"  <div class='shrink-0 w-10 h-10 rounded-lg border-2 border-dashed border-amber-300 flex items-center justify-center text-lg'>🥡</div>"
                    f"  <span class='text-[10px] uppercase tracking-[0.15em] text-amber-700 w-12 font-semibold shrink-0'>Make</span>"
                    f"  <div class='flex-1 min-w-0'>"
                    f"    <span class='serif text-lg font-medium'>{_h(nxt.recipe_name)}</span>"
                    f"    <div class='text-xs text-stone-500 mt-0.5'>tonight, for {nxt.date.strftime('%A')}’s lunch</div>"
                    f"  </div>"
                    f"  <span class='text-amber-400 shrink-0 text-lg leading-none' aria-hidden='true'>›</span>"
                    f"</a>"
                )

        is_today = d.date == _Date.today()
        today_badge = (
            " <span class='text-[10px] uppercase tracking-wider bg-amber-100 text-amber-800 font-semibold px-2 py-0.5 rounded-full'>Today</span>"
            if is_today else ""
        )
        day_blocks.append(f"""
          <div class="card plan-day py-4">
            <div class="flex items-baseline justify-between mb-1.5 px-5">
              <div class="flex items-baseline gap-2">
                <span class="serif text-xl font-medium tracking-tight">{d.date.strftime('%A')}</span>
                <span class="text-xs text-stone-400">{d.date.strftime('%b %-d')}</span>
              </div>
              {today_badge}
            </div>
            {''.join(slot_rows)}
          </div>
        """)

    # Reviewed recipes for the swap picker — same gate as the library.
    pickable = db.exec(
        select(Recipe)
        .where(Recipe.archived == False)  # noqa: E712
        .where(Recipe.reviewed_at.is_not(None))
        .order_by(Recipe.name_english)
    ).all()
    pick_rows = "".join(
        f"<button type='button' class='swap-pick flex items-center gap-3 w-full px-3 py-2 border-b border-stone-100 last:border-0 hover:bg-stone-50 text-left' "
        f"data-recipe-id='{pr.id}' data-haystack='{_h(((pr.name_english or '') + ' ' + (pr.name_original or '')).lower())}'>"
        f"  <div class='w-9 h-9 rounded-lg {_recipe_color(pr.id)} flex items-center justify-center text-base shrink-0'>"
        f"    {_recipe_emoji(pr.name_english or pr.name_original, pr.emoji)}"
        f"  </div>"
        f"  <span class='serif text-sm font-medium flex-1 truncate'>{_h(pr.display_title())}</span>"
        f"  <span class='text-stone-300 text-xs'>›</span>"
        f"</button>"
        for pr in pickable
    )

    prev_iso = (start_date - _Td(days=7)).isoformat()
    next_iso = (start_date + _Td(days=7)).isoformat()
    today_iso = _Date.today().isoformat()

    body = f"""
      <div class="mb-1 text-[10px] uppercase tracking-[0.25em] text-stone-400">This week</div>
      <div class="flex items-end justify-between gap-3">
        <h1 class="serif text-4xl font-medium tracking-tight leading-none">The <em class="text-stone-500">plan</em>.</h1>
        <div class="flex items-center gap-1 text-sm">
          <a href="{PREFIX}/app/plan?start={prev_iso}" class="w-8 h-8 rounded-lg border border-stone-200 bg-white text-stone-500 hover:bg-stone-100 inline-flex items-center justify-center">←</a>
          <a href="{PREFIX}/app/plan?start={today_iso}" class="px-3 h-8 rounded-lg border border-stone-200 bg-white text-stone-700 hover:bg-stone-100 inline-flex items-center text-sm">today</a>
          <a href="{PREFIX}/app/plan?start={next_iso}" class="w-8 h-8 rounded-lg border border-stone-200 bg-white text-stone-500 hover:bg-stone-100 inline-flex items-center justify-center">→</a>
        </div>
      </div>
      <p class="text-sm text-stone-500 mt-2">{start_date.strftime('%b %-d')} — {(start_date + _Td(days=6)).strftime('%b %-d')}</p>

      <div class="space-y-3 mt-6">
        {''.join(day_blocks)}
      </div>

      {f'<div class="card p-8 text-center mt-6"><div class="serif text-2xl text-stone-400 italic mb-1">Nothing planned.</div><p class="text-sm text-stone-500 mt-2">Tap a recipe from the <a href="{PREFIX}/app/" class="text-amber-700 hover:underline">Library</a> and use Add to plan to start the week.</p></div>' if all(s.recipe_id is None and not s.note for d in view for s in d.slots) else ''}

      <a href="{PREFIX}/app/shop" class="mt-6 block card text-center py-4 serif italic text-lg matte-emerald hover:shadow-lg transition-shadow">
        Build the shopping list →
      </a>

      <!-- Slot actions sheet: tap ··· on a planned meal → opens this.
           Three views: actions / swap-picker / move-picker. -->
      <div id="slot-sheet" class="ps-modal" role="dialog" aria-modal="true">
        <div class="ps-sheet">
          <div data-view="actions">
            <div class="flex items-baseline justify-between mb-3">
              <h2 class="serif text-xl font-medium tracking-tight">
                <span id="slot-title">Meal</span>
              </h2>
              <button type="button" class="ps-close text-stone-400 text-3xl leading-none p-2 -mt-2 -mr-2">×</button>
            </div>
            <div class="space-y-2">
              <a id="slot-view-link" href="#" class="card p-3 flex items-center gap-3 hover:shadow-lg transition-shadow">
                <span class="serif text-base font-medium flex-1">View recipe</span>
                <span class="text-stone-300">›</span>
              </a>
              <button type="button" id="slot-swap-btn" class="card p-3 w-full flex items-center gap-3 hover:shadow-lg transition-shadow text-left">
                <span class="serif text-base font-medium flex-1">Swap for another recipe</span>
                <span class="text-stone-300">›</span>
              </button>
              <button type="button" id="slot-move-btn" class="card p-3 w-full flex items-center gap-3 hover:shadow-lg transition-shadow text-left">
                <span class="serif text-base font-medium flex-1">Move to another day</span>
                <span class="text-stone-300">›</span>
              </button>
              <button type="button" id="slot-del-btn" class="card p-3 w-full flex items-center gap-3 hover:bg-rose-50 text-left text-rose-700">
                <span class="serif text-base font-medium flex-1">Delete from plan</span>
              </button>
            </div>
            <p id="slot-status" class="text-sm text-center text-stone-500 mt-3 min-h-[1.25em]"></p>
          </div>
          <div data-view="swap" hidden>
            <div class="swap-header">
              <div class="flex items-center justify-between mb-2">
                <button type="button" class="ps-close text-stone-500 -ml-2 p-2 leading-none flex items-center gap-1 text-base" aria-label="Close">
                  <span class="text-xl">←</span><span>Back</span>
                </button>
                <h2 id="swap-title" class="serif text-lg font-medium tracking-tight">Swap for…</h2>
                <span class="w-12"></span>
              </div>
              <input id="swap-filter" type="text" placeholder="Search recipes…" autocomplete="off"
                     class="w-full border border-stone-200 rounded-lg px-3 py-2 bg-white">
              <!-- Free-text entry (add mode only): leftovers, eating out… -->
              <form id="note-form" class="flex gap-2 mt-2" hidden>
                <input id="note-text" type="text" maxlength="200" autocomplete="off"
                       placeholder="Or just write it — leftovers, eating out…"
                       class="flex-1 min-w-0 border border-stone-200 rounded-lg px-3 py-2 bg-white">
                <button type="submit" class="bg-stone-900 text-white text-sm font-medium px-4 rounded-lg shrink-0">Add</button>
              </form>
            </div>
            <div id="swap-list" class="swap-list">
              {pick_rows}
            </div>
          </div>
          <div data-view="move" hidden>
            <div class="flex items-baseline justify-between mb-3">
              <h2 class="serif text-xl font-medium tracking-tight">Move to…</h2>
              <button type="button" class="ps-close text-stone-400 text-3xl leading-none p-2 -mt-2 -mr-2">×</button>
            </div>
            <p class="text-sm text-stone-500 mb-3">Pick a new day + meal slot.</p>
            <div id="move-grid" class="grid grid-cols-3 gap-2"></div>
          </div>
        </div>
      </div>

      <style>
        .ps-modal {{ display: none; position: fixed; inset: 0; z-index: 100; background: rgba(28,25,23,0.45); align-items: flex-end; justify-content: center; }}
        .ps-modal.is-open {{ display: flex; }}
        .ps-sheet {{ background: white; border-radius: 1.25rem 1.25rem 0 0; width: 100%; max-width: 28rem; max-height: 90vh; overflow-y: auto; padding: 1.25rem 1.25rem calc(1.5rem + env(safe-area-inset-bottom)); box-shadow: 0 -16px 48px -8px rgba(0,0,0,0.22); }}
        /* Fullscreen swap view: when the user opens the recipe picker
           we make the sheet fill the screen so iOS keyboard pops up
           inside a stable layout (no jumping) and the list can scroll
           independently of the search input. */
        .ps-modal.is-fullscreen {{ background: white; align-items: stretch; }}
        .ps-modal.is-fullscreen .ps-sheet {{
          max-width: 100%; max-height: none;
          border-radius: 0; padding: 0;
          box-shadow: none;
          display: flex; flex-direction: column;
          overflow: hidden;  /* swap-list scrolls instead */
        }}
        /* Sized by flex all the way down, never by height:100% — iOS
           Safari treats a % height inside a stretched flex item as auto,
           so the list grew to its full length, got clipped, and touches
           scrolled the page behind instead. */
        .ps-modal.is-fullscreen [data-view="swap"] {{
          display: flex !important; flex-direction: column;
          flex: 1 1 auto; min-height: 0;
        }}
        .swap-header {{
          background: white;
          padding: calc(0.75rem + env(safe-area-inset-top)) 1rem 0.75rem;
          border-bottom: 1px solid #f5f0e8;
          flex-shrink: 0;
        }}
        .swap-list {{
          flex: 1 1 0; min-height: 0; overflow-y: auto;
          overscroll-behavior: contain;
          -webkit-overflow-scrolling: touch;
          background: white;
          padding-bottom: env(safe-area-inset-bottom);
        }}
        /* Swipe-row mechanics — the inner content slides left, revealing
           the absolutely-positioned action buttons underneath. The
           inner gets an explicit positioning context + z-index so it
           paints firmly over the actions; without it, sub-pixel render
           on iOS leaks a hairline of the amber/rose action bg at the
           row's bottom edge. */
        .swipe-row {{ background: white; }}
        .swipe-row-inner {{
          position: relative; z-index: 1;
          background: white;
          transition: transform 0.18s ease; will-change: transform;
          touch-action: pan-y;
          -webkit-touch-callout: none; -webkit-user-select: none; user-select: none;
          cursor: pointer;
        }}
        /* Style C — cream day card + white pill rows. (Picked from
           /app/plan-styles after side-by-side tests.) The card is the
           tonal section, rows are inset white pills with their own
           rounded corners. */
        .plan-day {{
          background: #FAF7F2;
          padding-left: 0.5rem;
          padding-right: 0.5rem;
          padding-bottom: 0.5rem;
        }}
        .plan-day > .flex {{ padding-left: 1rem; padding-right: 1rem; }}
        .plan-day .slot-add,
        .plan-day .swipe-row,
        .plan-day .slot-leftover {{
          background: white;
          border-radius: 0.875rem;
          border-bottom: 0 !important;
          margin: 0 0 0.375rem 0;
        }}
        .plan-day .swipe-row-inner {{ background: white; border-radius: 0.875rem; }}
        .plan-day .swipe-row {{ overflow: hidden; }}
        /* Children too — otherwise long-press over the recipe name <a>
           pops the iOS Copy/Look Up bubble before our drag handler. */
        .swipe-row-inner * {{ -webkit-touch-callout: none !important; -webkit-user-select: none; user-select: none; }}
        .swipe-row.is-swiping .swipe-row-inner {{ transition: none; }}
        .swipe-row.is-open .swipe-row-inner {{ transform: translateX(-160px); }}
        /* Hidden by default — only displayed when the user is actively
           swiping or the row is in the open state. Eliminates the
           sub-pixel bleed at the row's bottom edge entirely (actions
           don't paint, can't leak). Insets top/bottom 1px so even when
           painted, a tiny gap shows the row's cream bg — the action
           bg can't visually overlap the row's separator. */
        /* Actions track spans the full row width, sitting BENEATH the
           inner. Swap (amber) takes the leftover space — its bg
           extends under the recipe so the recipe's rounded right
           corner reveals amber as it slides. Delete stays 80px on the
           right. iOS Mail / iMessage pattern. */
        .swipe-actions {{
          display: none !important;
          left: 0 !important; right: 0 !important;
          width: auto !important;
          top: 1px !important; bottom: 1px !important;
        }}
        .swipe-actions > .swipe-swap {{
          flex: 1 1 auto; min-width: 0;
          text-align: right; padding-right: 1.25rem;
        }}
        .swipe-actions > .swipe-del  {{ flex: 0 0 80px; min-width: 0; }}
        .swipe-row.is-open .swipe-actions,
        .swipe-row.is-swiping .swipe-actions {{ display: flex !important; }}
        /* Long-press drag-to-move (household request B). Source row
           dims; empty drop targets pulse amber; hovered target gets
           a solid amber outline. */
        body.is-dragging {{ overflow: hidden; }}
        .swipe-row.dragging .swipe-row-inner {{ opacity: 0.25; }}
        /* Hide the swipe action buttons during drag — they sit behind
           the inner content via overflow-hidden, but the 25% opacity
           drag-state makes them bleed through. */
        .swipe-row.dragging .swipe-actions {{ display: none; }}
        body.is-dragging .swipe-actions {{ display: none; }}
        body.is-dragging .drop-target {{ background: #fef3c7; outline: 1px dashed #f59e0b; outline-offset: -2px; transition: background 0.15s; }}
        body.is-dragging .drop-target.drop-hover {{ background: #fde68a; outline: 2px solid #d97706; outline-offset: -2px; }}
        /* white-space: nowrap prevents iOS from re-wrapping the chip
           into a 1-character-per-line column when the touch approaches
           the viewport edge. */
        .drag-chip {{
          position: fixed; z-index: 200; pointer-events: none;
          background: white; color: #1c1917;
          padding: 0.5rem 0.85rem; border-radius: 9999px;
          box-shadow: 0 12px 32px -4px rgba(0,0,0,0.25), 0 0 0 2px #d97706;
          font-family: 'Fraunces', Georgia, serif; font-weight: 500;
          display: flex; align-items: center; gap: 0.5rem;
          transform: translate(-50%, -120%);
          font-size: 0.95rem;
          white-space: nowrap;
        }}
        .drag-chip .drag-chip-thumb {{
          width: 1.75rem; height: 1.75rem;
          border-radius: 0.5rem;
          display: flex; align-items: center; justify-content: center;
          font-size: 1.1rem;
        }}
      </style>
      <script>
      // Kill iOS magnifier / loupe / context menu on long-press of any
      // element. CSS user-select:none isn't enough on iOS 17+; the
      // selectstart preventDefault is what actually stops the loupe.
      document.addEventListener('selectstart', (e) => {{
        // Allow text selection inside actual inputs (recipe filter, etc.).
        if (e.target.matches('input, textarea, [contenteditable]')) return;
        e.preventDefault();
      }});
      document.addEventListener('contextmenu', (e) => e.preventDefault());

      (function() {{
        const sheet = document.getElementById('slot-sheet');
        const fab = document.getElementById('fb-fab');
        const title = document.getElementById('slot-title');
        const viewLink = document.getElementById('slot-view-link');
        const swapBtn = document.getElementById('slot-swap-btn');
        const delBtn = document.getElementById('slot-del-btn');
        const status = document.getElementById('slot-status');
        const filter = document.getElementById('swap-filter');
        const actionsView = sheet.querySelector('[data-view="actions"]');
        const swapView = sheet.querySelector('[data-view="swap"]');
        let ctx = {{}};

        // The page behind must not scroll while the sheet is up.
        function show() {{ sheet.classList.add('is-open'); document.documentElement.style.overflow = 'hidden'; if (fab) fab.style.display = 'none'; status.textContent = ''; }}
        function hide() {{
          sheet.classList.remove('is-open');
          document.documentElement.style.overflow = '';
          sheet.classList.remove('is-fullscreen');
          if (fab) fab.style.display = '';
          actionsView.hidden = false;
          swapView.hidden = true;
          const mv = sheet.querySelector('[data-view="move"]');
          if (mv) mv.hidden = true;
        }}
        // Promote sheet to fullscreen when showing the recipe picker
        // — gives the search input a stable viewport (keyboard doesn't
        // squeeze the modal) and lets the list scroll independently.
        function openSwapFullscreen() {{
          actionsView.hidden = true;
          swapView.hidden = false;
          sheet.classList.add('is-fullscreen');
        }}
        // iOS pattern: scrolling the list dismisses the keyboard.
        // Tapping the input again re-summons it (default behaviour).
        const swapList = document.getElementById('swap-list');
        if (swapList) {{
          swapList.addEventListener('touchstart', () => {{
            if (document.activeElement === filter) filter.blur();
          }}, {{ passive: true }});
        }}
        sheet.querySelectorAll('.ps-close').forEach(b => b.addEventListener('click', hide));
        sheet.addEventListener('click', (e) => {{ if (e.target === sheet) hide(); }});
        document.addEventListener('keydown', (e) => {{ if (e.key === 'Escape') hide(); }});

        document.querySelectorAll('.slot-actions').forEach(btn => {{
          btn.addEventListener('click', () => {{
            ctx = {{
              date: btn.dataset.date,
              meal: btn.dataset.meal,
              recipeId: btn.dataset.recipeId,
              recipeName: btn.dataset.recipeName,
            }};
            title.textContent = ctx.recipeName;
            viewLink.href = '{PREFIX}/app/recipe/' + ctx.recipeId;
            actionsView.hidden = false;
            swapView.hidden = true;
            show();
          }});
        }});

        const swapTitle = document.getElementById('swap-title');
        const noteForm = document.getElementById('note-form');
        const noteText = document.getElementById('note-text');
        noteForm.addEventListener('submit', async (e) => {{
          e.preventDefault();
          const text = noteText.value.trim();
          if (!text) {{ noteText.focus(); return; }}
          try {{
            const r = await fetch('{PREFIX}/plan/note', {{
              method: 'POST',
              headers: {{ 'Content-Type': 'application/json' }},
              body: JSON.stringify({{ date: ctx.date, meal: ctx.meal, text }}),
            }});
            if (!r.ok) {{ const d = await r.json(); throw new Error(d.detail || r.status); }}
            location.reload();
          }} catch (err) {{
            window.notify('Add failed: ' + err.message, {{tone:'error'}});
          }}
        }});
        document.querySelectorAll('.note-del').forEach(btn => {{
          btn.addEventListener('click', async () => {{
            const ok = await window.askConfirm({{
              message: 'Remove from plan?',
              sub: btn.dataset.text + ' · ' + btn.dataset.meal + ' · ' + btn.dataset.date,
              okLabel: 'Remove',
            }});
            if (!ok) return;
            try {{
              const r = await fetch('{PREFIX}/plan/' + btn.dataset.date + '/' + btn.dataset.meal, {{ method: 'DELETE' }});
              if (!r.ok && r.status !== 204) throw new Error('HTTP ' + r.status);
              location.reload();
            }} catch (err) {{
              window.notify('Remove failed: ' + err.message, {{tone:'error'}});
            }}
          }});
        }});
        swapBtn.addEventListener('click', () => {{
          openSwapFullscreen();
          noteForm.hidden = true;
          swapTitle.textContent = 'Swap for…';
          filter.value = '';
          filterList('');
          setTimeout(() => filter.focus(), 50);
        }});

        // Move action — render a 7-day × 2-meal grid of target slots.
        const moveBtn = document.getElementById('slot-move-btn');
        const moveView = sheet.querySelector('[data-view="move"]');
        const moveGrid = document.getElementById('move-grid');
        moveBtn.addEventListener('click', () => {{
          actionsView.hidden = true;
          moveView.hidden = false;
          renderMoveGrid();
        }});

        function renderMoveGrid() {{
          const today = new Date();
          const cells = [];
          for (let i = 0; i < 7; i++) {{
            const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() + i);
            const iso = d.toISOString().slice(0, 10);
            const label = i === 0 ? 'Today' : i === 1 ? 'Tomorrow' :
              d.toLocaleDateString(undefined, {{ weekday: 'short', month: 'short', day: 'numeric' }});
            for (const meal of ['breakfast', 'lunch', 'dinner']) {{
              const isSelf = iso === ctx.date && meal === ctx.meal;
              cells.push(
                `<button type="button" class="move-cell border border-stone-200 rounded-lg p-2 text-left bg-white hover:bg-amber-50 hover:border-amber-300 ${{isSelf ? 'opacity-40 cursor-not-allowed' : ''}}" `
                + `data-date="${{iso}}" data-meal="${{meal}}" ${{isSelf ? 'disabled' : ''}}>`
                + `<div class="uppercase tracking-wider text-stone-400 font-semibold" style="font-size:10px;white-space:nowrap;">${{meal}}</div>`
                + `<div class="text-xs font-medium text-stone-800">${{label}}</div>`
                + `</button>`
              );
            }}
          }}
          moveGrid.innerHTML = cells.join('');
          moveGrid.querySelectorAll('.move-cell').forEach(btn => {{
            btn.addEventListener('click', async () => {{
              btn.disabled = true;
              try {{
                const r = await fetch('{PREFIX}/plan/move', {{
                  method: 'POST',
                  headers: {{ 'Content-Type': 'application/json' }},
                  body: JSON.stringify({{
                    from_date: ctx.date, from_meal: ctx.meal,
                    to_date: btn.dataset.date, to_meal: btn.dataset.meal,
                  }}),
                }});
                if (!r.ok) {{ const d = await r.json(); throw new Error(d.detail || r.status); }}
                location.reload();
              }} catch (e) {{
                btn.disabled = false;
                window.notify('Move failed: ' + e.message, {{tone:'error'}});
              }}
            }});
          }});
        }}

        function filterList(q) {{
          const ql = q.toLowerCase();
          swapView.querySelectorAll('.swap-pick').forEach(b => {{
            b.style.display = (!ql || b.dataset.haystack.includes(ql)) ? '' : 'none';
          }});
        }}
        filter.addEventListener('input', () => filterList(filter.value));

        swapView.querySelectorAll('.swap-pick').forEach(btn => {{
          btn.addEventListener('click', async () => {{
            btn.disabled = true;
            // ctx.mode === 'add' for empty-slot tap; otherwise swap.
            const url = ctx.mode === 'add' ? '{PREFIX}/plan/place' : '{PREFIX}/plan/swap';
            const body = ctx.mode === 'add'
              ? {{ recipe_id: parseInt(btn.dataset.recipeId, 10), date: ctx.date, meal: ctx.meal }}
              : {{ date: ctx.date, meal: ctx.meal, recipe_id: parseInt(btn.dataset.recipeId, 10) }};
            try {{
              const r = await fetch(url, {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(body),
              }});
              if (!r.ok) {{ const d = await r.json(); throw new Error(d.detail || r.status); }}
              location.reload();
            }} catch (e) {{
              btn.disabled = false;
              window.notify((ctx.mode === 'add' ? 'Add' : 'Swap') + ' failed: ' + e.message, {{tone:'error'}});
            }}
          }});
        }});

        // Empty-slot tap → open picker in 'add' mode.
        document.querySelectorAll('.slot-add').forEach(btn => {{
          btn.addEventListener('click', () => {{
            // Best-effort haptic feedback. iOS Safari ignores; Android
            // gives a short blip. Either way no harm.
            try {{ navigator.vibrate?.(8); }} catch (e) {{}}
            ctx = {{ date: btn.dataset.date, meal: btn.dataset.meal, mode: 'add' }};
            // Capitalise the meal name for the heading: "Add a Lunch"
            const m = btn.dataset.meal;
            const mealCap = m.charAt(0).toUpperCase() + m.slice(1);
            swapTitle.textContent = `Add a ${{mealCap}}…`;
            const mv = sheet.querySelector('[data-view="move"]');
            if (mv) mv.hidden = true;
            openSwapFullscreen();
            noteForm.hidden = false;
            noteText.value = '';
            filter.value = '';
            filterList('');
            show();
            setTimeout(() => filter.focus(), 50);
          }});
        }});

        delBtn.addEventListener('click', async () => {{
          const ok = await window.askConfirm({{
            message: 'Remove from plan?',
            sub: ctx.recipeName + ' · ' + ctx.meal + ' · ' + ctx.date,
            okLabel: 'Remove',
          }});
          if (!ok) return;
          delBtn.disabled = true;
          status.textContent = 'Deleting…';
          try {{
            const r = await fetch('{PREFIX}/plan/' + ctx.date + '/' + ctx.meal, {{ method: 'DELETE' }});
            if (!r.ok && r.status !== 204) throw new Error('HTTP ' + r.status);
            location.reload();
          }} catch (e) {{
            delBtn.disabled = false;
            status.textContent = 'Delete failed: ' + e.message;
          }}
        }});

        /* ----- Swipe-left actions (A) + long-press-to-drag (B) ----- */
        // Each .swipe-row supports two gestures, axis-disambiguated:
        //   - horizontal swipe-left → reveal Swap + Delete buttons
        //   - long-press (300ms, no movement) → enter drag-to-move
        // Vertical scrolls pass through to the browser. Timing + threshold
        // values picked from /app/plan-styles tuning (300ms, easy/40px).
        const SWIPE_THRESHOLD = 40;
        const SWIPE_OFFSET = 160;
        const LONG_PRESS_MS = 300;
        const MOVE_TOLERANCE = 8;   // px — beyond this cancels long-press
        let openRow = null;
        let dragChip = null;
        let dragCtx = null;

        function cancelDrag() {{
          document.body.classList.remove('is-dragging');
          document.querySelectorAll('.swipe-row.dragging').forEach(r => r.classList.remove('dragging'));
          document.querySelectorAll('.drop-target.drop-hover').forEach(t => t.classList.remove('drop-hover'));
          if (dragChip) {{ dragChip.remove(); dragChip = null; }}
          dragCtx = null;
        }}

        function startDrag(row, x, y) {{
          // Defensive: close any open swipe state — including this row's,
          // so the Swap/Delete buttons can't peek through the drag dim.
          if (openRow) {{
            openRow.classList.remove('is-open');
            const oi = openRow.querySelector('.swipe-row-inner');
            if (oi) oi.style.transform = '';
            openRow = null;
          }}
          row.classList.remove('is-open');
          const t = row.querySelector('.swipe-row-inner');
          if (t) t.style.transform = '';
          dragCtx = {{
            row,
            fromDate: t.dataset.date, fromMeal: t.dataset.meal,
            recipeName: t.dataset.recipeName, recipeId: t.dataset.recipeId,
          }};
          row.classList.add('dragging');
          document.body.classList.add('is-dragging');
          dragChip = document.createElement('div');
          dragChip.className = 'drag-chip';
          const thumb = row.querySelector('.swipe-row-inner > div:first-child');
          dragChip.innerHTML =
            (thumb ? `<div class="drag-chip-thumb ${{thumb.className.match(/matte-\\w+/)?.[0] || ''}}">${{thumb.textContent.trim()}}</div>` : '')
            + `<span>Move <em>${{dragCtx.recipeName}}</em></span>`;
          document.body.appendChild(dragChip);
          updateChip(x, y);
          try {{ navigator.vibrate?.(15); }} catch (e) {{}}
        }}

        function updateChip(x, y) {{
          if (!dragChip) return;
          dragChip.style.left = x + 'px';
          dragChip.style.top = y + 'px';
          // Highlight the drop target under the finger.
          dragChip.style.display = 'none';
          const under = document.elementFromPoint(x, y);
          dragChip.style.display = '';
          const target = under ? under.closest('.drop-target') : null;
          document.querySelectorAll('.drop-target.drop-hover').forEach(t => {{
            if (t !== target) t.classList.remove('drop-hover');
          }});
          if (target) target.classList.add('drop-hover');
        }}

        async function dropDrag(x, y) {{
          if (!dragCtx) return;
          dragChip.style.display = 'none';
          const under = document.elementFromPoint(x, y);
          dragChip.style.display = '';
          const target = under ? under.closest('.drop-target') : null;
          const ctx = dragCtx;
          cancelDrag();
          if (!target) return;
          const toDate = target.dataset.date;
          const toMeal = target.dataset.meal;
          if (toDate === ctx.fromDate && toMeal === ctx.fromMeal) return;
          try {{
            const r = await fetch('{PREFIX}/plan/move', {{
              method: 'POST',
              headers: {{ 'Content-Type': 'application/json' }},
              body: JSON.stringify({{
                from_date: ctx.fromDate, from_meal: ctx.fromMeal,
                to_date: toDate, to_meal: toMeal,
              }}),
            }});
            if (!r.ok) {{ const d = await r.json(); throw new Error(d.detail || r.status); }}
            location.reload();
          }} catch (e) {{
            window.notify('Move failed: ' + e.message, {{tone:'error'}});
          }}
        }}

        document.querySelectorAll('.swipe-row').forEach(row => {{
          const inner = row.querySelector('.swipe-row-inner');
          let startX = 0, startY = 0, dragging = false, dx = 0;
          let longPressTimer = null;
          let longPressing = false;

          inner.addEventListener('touchstart', (e) => {{
            if (e.touches.length !== 1) return;
            startX = e.touches[0].clientX;
            startY = e.touches[0].clientY;
            dragging = false;
            dx = row.classList.contains('is-open') ? -SWIPE_OFFSET : 0;
            // Arm long-press: if no meaningful movement after 500ms,
            // start drag mode.
            longPressing = false;
            longPressTimer = setTimeout(() => {{
              longPressTimer = null;
              longPressing = true;
              startDrag(row, startX, startY);
            }}, LONG_PRESS_MS);
          }}, {{ passive: true }});

          inner.addEventListener('touchmove', (e) => {{
            const tx = e.touches[0].clientX;
            const ty = e.touches[0].clientY;
            const dxRaw = tx - startX;
            const dyRaw = ty - startY;

            // If we're in drag mode, route to drag handler.
            if (longPressing) {{
              e.preventDefault();
              updateChip(tx, ty);
              return;
            }}

            // Past the movement tolerance → cancel the long-press timer.
            if (longPressTimer && (Math.abs(dxRaw) > MOVE_TOLERANCE || Math.abs(dyRaw) > MOVE_TOLERANCE)) {{
              clearTimeout(longPressTimer);
              longPressTimer = null;
            }}

            if (!dragging) {{
              if (Math.abs(dyRaw) > 8 && Math.abs(dyRaw) > Math.abs(dxRaw)) return;
              if (Math.abs(dxRaw) > 8) {{
                dragging = true;
                row.classList.add('is-swiping');
                if (openRow && openRow !== row) {{
                  openRow.classList.remove('is-open');
                  openRow.querySelector('.swipe-row-inner').style.transform = '';
                  openRow = null;
                }}
              }}
            }}
            if (dragging) {{
              const base = row.classList.contains('is-open') ? -SWIPE_OFFSET : 0;
              dx = Math.max(-SWIPE_OFFSET, Math.min(0, base + dxRaw));
              inner.style.transform = `translateX(${{dx}}px)`;
              e.preventDefault();
            }}
          }}, {{ passive: false }});

          inner.addEventListener('touchend', (e) => {{
            // Long-press drag finishing?
            if (longPressing) {{
              const t = e.changedTouches[0];
              dropDrag(t.clientX, t.clientY);
              longPressing = false;
              return;
            }}
            if (longPressTimer) {{
              clearTimeout(longPressTimer);
              longPressTimer = null;
            }}
            row.classList.remove('is-swiping');
            if (!dragging) return;
            if (dx <= -SWIPE_THRESHOLD) {{
              row.classList.add('is-open');
              inner.style.transform = '';
              openRow = row;
            }} else {{
              row.classList.remove('is-open');
              inner.style.transform = '';
              if (openRow === row) openRow = null;
            }}
            dragging = false;
          }});

          inner.addEventListener('touchcancel', () => {{
            if (longPressTimer) clearTimeout(longPressTimer);
            longPressTimer = null;
            if (longPressing) {{ cancelDrag(); longPressing = false; }}
            row.classList.remove('is-swiping');
            inner.style.transform = row.classList.contains('is-open') ? '' : '';
          }});

          // Tap the row (anywhere — not just the recipe-name link) to
          // open the recipe. Mirrors Library cards. Skip when the
          // click was on a link/button child (their own handlers run)
          // or while the row is in a gesture state (swipe open, drag).
          inner.addEventListener('click', (e) => {{
            if (e.target.closest('a, button')) return;
            if (row.classList.contains('is-open')) return;
            if (row.classList.contains('is-swiping')) return;
            if (document.body.classList.contains('is-dragging')) return;
            const rid = inner.dataset.recipeId;
            if (rid) location.href = '{PREFIX}/app/recipe/' + rid;
          }});

          // Suppress the iOS context menu / text-callout during long-press.
          inner.addEventListener('contextmenu', (e) => e.preventDefault());

          // Close on tap-elsewhere.
          document.addEventListener('click', (e) => {{
            if (openRow === row && !row.contains(e.target)) {{
              row.classList.remove('is-open');
              openRow = null;
            }}
          }});

          // Swipe action buttons.
          row.querySelector('.swipe-swap').addEventListener('click', () => {{
            const t = row.querySelector('.swipe-row-inner');
            ctx = {{ date: t.dataset.date, meal: t.dataset.meal, recipeId: t.dataset.recipeId, recipeName: t.dataset.recipeName }};
            title.textContent = ctx.recipeName;
            viewLink.href = '{PREFIX}/app/recipe/' + ctx.recipeId;
            openSwapFullscreen();
            filter.value = ''; filterList('');
            show();
            row.classList.remove('is-open');
            openRow = null;
            setTimeout(() => filter.focus(), 50);
          }});
          row.querySelector('.swipe-del').addEventListener('click', async () => {{
            const t = row.querySelector('.swipe-row-inner');
            const ok = await window.askConfirm({{
              message: 'Remove from plan?',
              sub: t.dataset.recipeName + ' · ' + t.dataset.meal + ' · ' + t.dataset.date,
              okLabel: 'Remove',
            }});
            if (!ok) return;
            try {{
              const r = await fetch('{PREFIX}/plan/' + t.dataset.date + '/' + t.dataset.meal, {{ method: 'DELETE' }});
              if (!r.ok && r.status !== 204) throw new Error('HTTP ' + r.status);
              location.reload();
            }} catch (e) {{
              window.notify('Delete failed: ' + e.message, {{tone:'error'}});
            }}
          }});
        }});

        // Leftover rows are flat divs (no swipe gesture), but the
        // whole card should still navigate to the recipe — match the
        // Library card behaviour the household expects. The recipe name is
        // already an <a>; this adds a row-wide click handler that
        // follows that link if you tap anywhere else on the row.
        document.querySelectorAll('.slot-leftover').forEach(row => {{
          row.style.cursor = 'pointer';
          row.addEventListener('click', (e) => {{
            if (e.target.closest('a, button')) return;
            if (document.body.classList.contains('is-dragging')) return;
            const link = row.querySelector('a[href*="/app/recipe/"]');
            if (link) location.href = link.href;
          }});
        }});
      }})();
      </script>
    """
    return HTMLResponse(_shell("Plan", body, active="plan"))


# =============================================================================
# Plan-style exploration (test page)
# =============================================================================

def _demo_day_card(suffix: str, label: str, sub: str, badge: str = "") -> str:
    """Self-contained day card with one empty slot + one swipeable cook
    slot + one leftover. Used by /app/plan-styles to render the same
    structure under 5 different scoped CSS treatments."""
    return f"""
      <div class="card py-4">
        <div class="flex items-baseline justify-between mb-1.5 px-5">
          <div class="flex items-baseline gap-2">
            <span class="serif text-xl font-medium tracking-tight">{label}</span>
            <span class="text-xs text-stone-400">{sub}</span>
          </div>
          {badge}
        </div>
        <button type="button" class="slot-add w-full flex items-center gap-3 px-5 py-2 border-b border-stone-100 last:border-0 drop-target text-left"
                data-empty="1" data-date="demo-{suffix}" data-meal="brk">
          <div class="shrink-0 w-10 h-10 rounded-lg border-2 border-dashed border-amber-300 flex items-center justify-center text-amber-700 text-lg font-medium drop-thumb">+</div>
          <span class="text-[10px] uppercase tracking-[0.15em] text-stone-400 w-12 font-semibold shrink-0">brk</span>
          <div class="flex-1 min-w-0"><span class="text-amber-800 serif italic text-base font-medium">Add a meal</span></div>
          <span class="text-amber-400 shrink-0 text-lg leading-none">›</span>
        </button>
        <div class="swipe-row relative overflow-hidden border-b border-stone-100 last:border-0">
          <div class="swipe-actions absolute right-0 top-0 bottom-0 flex items-stretch">
            <button type="button" class="swipe-swap bg-amber-500 text-white px-4 text-sm font-medium">Swap</button>
            <button type="button" class="swipe-del bg-rose-600 text-white px-4 text-sm font-medium">Delete</button>
          </div>
          <div class="swipe-row-inner flex items-center gap-3 px-5 py-2"
               data-date="demo-{suffix}" data-meal="lun" data-recipe-name="Azorian Beef Stew" data-recipe-id="0">
            <div class="shrink-0 w-10 h-10 rounded-lg matte-emerald flex items-center justify-center text-lg">🍲</div>
            <span class="text-[10px] uppercase tracking-[0.15em] text-stone-400 w-12 font-semibold shrink-0">lun</span>
            <div class="flex-1 min-w-0">
              <div>
                <span class="inline-block bg-amber-100 text-amber-900 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider mr-1">⚓ Leads</span>
                <span class="serif text-lg font-medium">Azorian Beef Stew</span>
                <div class="text-xs text-stone-400 mt-0.5">4 srv · fills 2 meals</div>
              </div>
            </div>
            <button type="button" class="slot-actions text-stone-300 px-2 text-xl leading-none shrink-0" aria-label="Slot actions">···</button>
          </div>
        </div>
        <div class="leftover-row flex items-center gap-3 px-5 py-2 border-b border-stone-100 last:border-0">
          <div class="shrink-0 w-10 h-10 rounded-lg matte-emerald flex items-center justify-center text-lg">🍲</div>
          <span class="text-[10px] uppercase tracking-[0.15em] text-stone-400 w-12 font-semibold shrink-0">din</span>
          <div class="flex-1 min-w-0">
            <div>
              <span class="serif text-lg font-medium">Azorian Beef Stew</span>
              <div class="text-xs text-stone-400 mt-0.5">leftover 2/2</div>
            </div>
          </div>
        </div>
      </div>
    """


_PLAN_STYLES_CSS = """
<style>
  /* ----- Base swipe + drag CSS (mirrors plan_page) ---------------- */
  /* Without these the action buttons sit visible on top of every row
     because their default display: flex isn't gated to is-open. */
  .swipe-row { background: #FAF7F2; }
  .swipe-row-inner {
    position: relative; z-index: 1;
    background: #FAF7F2;
    transition: transform 0.18s ease; will-change: transform;
    touch-action: pan-y;
    -webkit-touch-callout: none; -webkit-user-select: none; user-select: none;
  }
  .swipe-row-inner * { -webkit-touch-callout: none !important; -webkit-user-select: none; user-select: none; }
  .swipe-row.is-swiping .swipe-row-inner { transition: none; }
  .swipe-row.is-open .swipe-row-inner { transform: translateX(-160px); }
  /* !important wins over the Tailwind .flex utility on the actions div
     — otherwise both rules tie at (0,1,0) specificity and the cascade
     order leaks a sliver of the Delete bg at every row's right edge. */
  /* The actions track spans the FULL row width, sitting beneath the
     inner (which has z-index: 1). Swap takes all the leftover space
     to the LEFT of Delete — its amber bg extends under the recipe so
     when the inner slides, the recipe's rounded right corner is
     revealed against the amber. Delete stays at 80px on the right.
     iOS Mail / iMessage swipe pattern: action bg fills behind the
     content as the content slides off. */
  .swipe-actions {
    display: none !important;
    left: 0 !important; right: 0 !important;
    width: auto !important;
    top: 1px !important; bottom: 1px !important;
  }
  /* Swap label sits at the right edge — centred text would land
     under the recipe (invisible) now that the Swap area is wide. */
  .swipe-actions > .swipe-swap {
    flex: 1 1 auto; min-width: 0;
    text-align: right; padding-right: 1.25rem;
  }
  .swipe-actions > .swipe-del  { flex: 0 0 80px; min-width: 0; }
  .swipe-row.is-open .swipe-actions,
  .swipe-row.is-swiping .swipe-actions { display: flex !important; }
  body.is-dragging { overflow: hidden; }
  .swipe-row.dragging .swipe-row-inner { opacity: 0.25; }
  .swipe-row.dragging .swipe-actions { display: none !important; }
  body.is-dragging .swipe-actions { display: none !important; }
  body.is-dragging .drop-target { background: #fef3c7; outline: 1px dashed #f59e0b; outline-offset: -2px; transition: background 0.15s; }
  body.is-dragging .drop-target.drop-hover { background: #fde68a; outline: 2px solid #d97706; outline-offset: -2px; }
  /* white-space: nowrap prevents iOS from re-wrapping the chip into a
     1-character-per-line column when the touch nears the viewport edge. */
  .drag-chip {
    position: fixed; z-index: 200; pointer-events: none;
    background: white; color: #1c1917;
    padding: 0.5rem 0.85rem; border-radius: 9999px;
    box-shadow: 0 12px 32px -4px rgba(0,0,0,0.25), 0 0 0 2px #d97706;
    font-family: 'Fraunces', Georgia, serif; font-weight: 500;
    display: flex; align-items: center; gap: 0.5rem;
    transform: translate(-50%, -120%);
    font-size: 0.95rem;
    white-space: nowrap;
  }
  /* Lock down text selection + iOS magnifier across the entire
     plan-styles page. CSS alone isn't enough on iOS — the magnifier
     (loupe) loop appears on any long-press even with user-select:none.
     The JS handler below adds selectstart + contextmenu suppression
     to actually kill it. */
  html, body, body * {
    -webkit-touch-callout: none !important;
    -webkit-user-select: none !important;
    user-select: none !important;
    -webkit-tap-highlight-color: transparent;
  }
  .drag-chip .drag-chip-thumb {
    width: 1.75rem; height: 1.75rem;
    border-radius: 0.5rem;
    display: flex; align-items: center; justify-content: center;
    font-size: 1.1rem;
  }

  /* ----- Per-style scoping -------------------------------------- */
  /* Each .style-X scopes the day-card treatment so swiping a row in
     style A doesn't affect the same structure in style B. Differences
     live in the wrapper class; the underlying HTML is identical. */

  /* ----- Style A: white rows, hairline dividers ------------------- */
  /* Rows share the card's white bg — only the hairline + content
     distinguishes them. Eliminates row-bg-vs-card-bg contrast
     (= no visible square row corners). */
  .style-a .swipe-row,
  .style-a .swipe-row-inner { background: white; }

  /* ----- Style B: cream pill rows inside white card --------------- */
  /* Each row is its own rounded pill with its own corners — the
     card's clipping is irrelevant because each row rounds itself.
     We inset the rows via padding on the card, NOT margin on the
     rows: rows have w-full (= 100%) and margins on top would push
     the layout past the card's right edge, getting clipped. */
  .style-b .card { padding-left: 0.5rem; padding-right: 0.5rem; padding-bottom: 0.5rem; }
  .style-b .slot-add,
  .style-b .swipe-row,
  .style-b .leftover-row {
    background: #FAF7F2;
    border-radius: 0.875rem;
    border-bottom: 0 !important;
    margin: 0 0 0.375rem 0;
  }
  .style-b .swipe-row-inner { background: #FAF7F2; border-radius: 0.875rem; }
  .style-b .swipe-row { overflow: hidden; }

  /* ----- Style C: cream card + white pill rows ------------------- */
  /* Inverted tonal sheet (Apple-list-style). Card is the tonal
     section, rows are inset cards within it. */
  .style-c .card { background: #FAF7F2; padding-left: 0.5rem; padding-right: 0.5rem; padding-bottom: 0.5rem; }
  .style-c .slot-add,
  .style-c .swipe-row,
  .style-c .leftover-row {
    background: white;
    border-radius: 0.875rem;
    border-bottom: 0 !important;
    margin: 0 0 0.375rem 0;
  }
  .style-c .swipe-row-inner { background: white; border-radius: 0.875rem; }
  .style-c .swipe-row { overflow: hidden; }

  /* ----- Style D: no day card, flat list ------------------------- */
  /* No card wrapper at all — day header floats above its rows. Rows
     get hairlines that span the screen width. */
  .style-d .card {
    background: transparent !important; border: 0 !important;
    box-shadow: none !important; border-radius: 0;
    padding-left: 0; padding-right: 0; overflow: visible;
  }
  .style-d .swipe-row,
  .style-d .swipe-row-inner { background: #FAF7F2; }

  /* ----- Style G: Telegram-style — pills GROW with swipe ------- */
  /* Like F (floating pills at right) but the pills' scale tracks the
     swipe distance — they grow from 0 to 1 as the row slides past
     SWIPE_OFFSET. Driven by --swipe-progress CSS variable, set on
     the row by the touchmove JS handler. */
  .style-g .card {
    background: transparent !important; border: 0 !important;
    box-shadow: none !important; border-radius: 0;
    padding: 0; overflow: visible;
  }
  .style-g .card > div:first-child { padding: 0.25rem 0 0.5rem; }
  .style-g .slot-add,
  .style-g .swipe-row,
  .style-g .leftover-row {
    background: white;
    border-radius: 1rem;
    border-bottom: 0 !important;
    margin: 0 0 0.5rem 0;
    box-shadow: 0 1px 2px rgba(120,80,40,0.04), 0 4px 12px -8px rgba(120,80,40,0.10);
    border: 1px solid #f5f0e8;
  }
  .style-g .swipe-row-inner { background: white; border-radius: 1rem; }
  .style-g .swipe-row { overflow: hidden; }
  .style-g .swipe-actions {
    background: transparent !important;
    border-radius: 0 !important;
    overflow: visible !important;
    top: 0 !important; bottom: 0 !important;
    padding-right: 0.5rem;
    gap: 0.5rem;
    align-items: center;
    justify-content: flex-end;
  }
  .style-g .swipe-actions > .swipe-swap,
  .style-g .swipe-actions > .swipe-del {
    flex: 0 0 44px !important;
    width: 44px; height: 44px;
    min-width: 0; padding: 0;
    border-radius: 9999px;
    font-size: 0;
    position: relative;
    box-shadow: 0 2px 6px -1px rgba(0,0,0,0.18);
    /* Pills track the swipe distance. transform-origin: right means
       they grow from where they're anchored (the row's right edge). */
    transform: scale(var(--swipe-progress, 0));
    transform-origin: right center;
    transition: transform 0.18s ease;
  }
  .style-g .swipe-row.is-swiping .swipe-actions > .swipe-swap,
  .style-g .swipe-row.is-swiping .swipe-actions > .swipe-del {
    transition: none;  /* follow the finger directly mid-swipe */
  }
  .style-g .swipe-row.is-open { --swipe-progress: 1; }
  .style-g .swipe-actions > .swipe-swap::after,
  .style-g .swipe-actions > .swipe-del::after {
    position: absolute; inset: 0;
    display: flex; align-items: center; justify-content: center;
    color: white; font-size: 18px;
  }
  .style-g .swipe-actions > .swipe-swap::after { content: '⇄'; font-weight: 700; }
  .style-g .swipe-actions > .swipe-del::after  { content: '🗑'; font-size: 17px; }

  /* ----- Style F: iMessage-style floating pill actions ---------- */
  /* Two circular icon pills float in their own track to the right of
     the row, with a transparent gap between them and the recipe
     content. Mirrors iOS Messages: secondary (mute) on left, primary
     (delete) on right; both fully rounded; no connected button block.
     The row itself is a floating card like Style E. */
  .style-f .card {
    background: transparent !important; border: 0 !important;
    box-shadow: none !important; border-radius: 0;
    padding: 0; overflow: visible;
  }
  .style-f .card > div:first-child { padding: 0.25rem 0 0.5rem; }
  .style-f .slot-add,
  .style-f .swipe-row,
  .style-f .leftover-row {
    background: white;
    border-radius: 1rem;
    border-bottom: 0 !important;
    margin: 0 0 0.5rem 0;
    box-shadow: 0 1px 2px rgba(120,80,40,0.04), 0 4px 12px -8px rgba(120,80,40,0.10);
    border: 1px solid #f5f0e8;
  }
  .style-f .swipe-row-inner { background: white; border-radius: 1rem; }
  .style-f .swipe-row { overflow: hidden; }
  /* iMessage-style action track: transparent container, pills hugging
     the right edge with a gap from the recipe content. */
  .style-f .swipe-actions {
    background: transparent !important;
    border-radius: 0 !important;
    overflow: visible !important;
    top: 0 !important; bottom: 0 !important;
    padding-right: 0.5rem;
    gap: 0.5rem;
    align-items: center;
    justify-content: flex-end;
  }
  /* Faithful iMessage: secondary (Swap) stays a small circle, primary
     (Delete) ELONGATES horizontally with swipe progress. The pill is
     wider than tall at full swipe — that's iMessage's tell. */
  .style-f .swipe-actions > .swipe-swap {
    flex: 0 0 36px !important;
    width: 36px; height: 36px;
    min-width: 0; padding: 0;
    border-radius: 9999px;
    font-size: 0;
    position: relative;
    box-shadow: 0 2px 6px -1px rgba(0,0,0,0.18);
  }
  .style-f .swipe-actions > .swipe-del {
    flex: 0 0 auto !important;
    /* Grows from 44px → ~96px wide as swipe progresses. Height fixed
       at 44 so the shape transitions from circle → horizontal pill. */
    width: calc(44px + var(--swipe-progress, 0) * 52px);
    height: 44px;
    min-width: 0; padding: 0;
    border-radius: 9999px;
    font-size: 0;
    position: relative;
    box-shadow: 0 2px 6px -1px rgba(0,0,0,0.18);
    transition: width 0.18s ease;
  }
  .style-f .swipe-row.is-swiping .swipe-actions > .swipe-del { transition: none; }
  .style-f .swipe-actions > .swipe-swap::after,
  .style-f .swipe-actions > .swipe-del::after {
    position: absolute; inset: 0;
    display: flex; align-items: center; justify-content: center;
    color: white; font-size: 18px;
  }
  .style-f .swipe-actions > .swipe-swap::after { content: '⇄'; font-weight: 700; font-size: 16px; }
  .style-f .swipe-actions > .swipe-del::after  { content: '🗑'; font-size: 17px; }

  /* ----- Style E: floating individual rows ---------------------- */
  /* No day card; every row is its own elevated card. Heaviest visual
     weight but each row is structurally independent — no nesting.
     overflow visible on the card so the row shadows don't clip. */
  .style-e .card {
    background: transparent !important; border: 0 !important;
    box-shadow: none !important; border-radius: 0;
    padding: 0; overflow: visible;
  }
  .style-e .card > div:first-child { padding: 0.25rem 0 0.5rem; }
  .style-e .slot-add,
  .style-e .swipe-row,
  .style-e .leftover-row {
    background: white;
    border-radius: 1rem;
    border-bottom: 0 !important;
    margin: 0 0 0.5rem 0;
    box-shadow: 0 1px 2px rgba(120,80,40,0.04), 0 4px 12px -8px rgba(120,80,40,0.10);
    border: 1px solid #f5f0e8;
  }
  .style-e .swipe-row-inner { background: white; border-radius: 1rem; }
  .style-e .swipe-row { overflow: hidden; }

  /* ---------- Add-a-meal treatment variations ---------- */
  /* Three distinct add-a-meal looks, mixed across the 7 styles so the
     test page surfaces variation in BOTH the swipe action AND the
     empty-slot affordance. */

  /* Variant α — SOLID amber thumb, bold (no italic). Used in B + E. */
  .style-b .slot-add .drop-thumb,
  .style-e .slot-add .drop-thumb {
    border-style: solid !important;
    border-color: #B45309 !important;
    background: #F59E0B;
    color: white;
  }
  .style-b .slot-add .text-amber-800,
  .style-e .slot-add .text-amber-800 {
    font-style: normal !important;
    font-weight: 600;
  }

  /* Variant β — NO thumb, "+ Add a meal" inline, no chevron, centered.
     Used in D + F. */
  .style-d .slot-add .drop-thumb,
  .style-f .slot-add .drop-thumb {
    display: none;
  }
  .style-d .slot-add > span:last-child,
  .style-f .slot-add > span:last-child {
    display: none;
  }
  .style-d .slot-add .text-amber-800::before,
  .style-f .slot-add .text-amber-800::before {
    content: '+ ';
    font-style: normal;
    font-weight: 700;
    margin-right: 2px;
  }
  .style-d .slot-add { justify-content: flex-start; }

  /* Variant γ — Bigger thumb, ghosted "no meal" cue. Used in G. */
  .style-g .slot-add { padding-top: 0.75rem; padding-bottom: 0.75rem; }
  .style-g .slot-add .drop-thumb {
    width: 2.75rem; height: 2.75rem;
    border-color: #FCD34D !important;
  }
  .style-g .slot-add .text-amber-800 {
    font-style: normal !important;
    color: #78350F;
    font-weight: 500;
  }

  /* Scoped picker pill */
  .style-pick {
    display: inline-flex; align-items: center; gap: 6px;
    background: #FDE68A; color: #78350F;
    border-radius: 9999px; padding: 4px 12px;
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em;
    font-weight: 600;
  }

  /* Dial controls (top of page) — let the tester tune timings + threshold
     + haptic strength in-place without redeploying. */
  .dials {
    background: white; border: 1px solid #f5f0e8;
    border-radius: 1rem; padding: 0.875rem 1rem;
    box-shadow: 0 1px 2px rgba(120,80,40,0.04);
  }
  .dial { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.5rem; }
  .dial:last-of-type { margin-bottom: 0; }
  .dial-label {
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em;
    color: #57534e; font-weight: 600;
    flex: 0 0 5.5rem;
  }
  .dial-buttons { display: flex; gap: 0.25rem; flex: 1; }
  .dial-buttons > button {
    flex: 1; padding: 6px 4px;
    font-size: 11px; font-weight: 500;
    border-radius: 0.5rem;
    background: #f5f5f4; color: #44403c;
    border: 1px solid transparent;
    transition: background 0.1s, transform 0.06s;
    white-space: nowrap; min-width: 0;
  }
  .dial-buttons > button.active {
    background: #FDE68A; color: #78350F;
    border-color: #F59E0B;
  }
  .dial-buttons > button:active { transform: scale(0.95); }
</style>
"""


@router.get("/app/plan-styles", response_class=HTMLResponse)
def plan_styles_page() -> HTMLResponse:
    """Side-by-side row-treatment exploration. 5 styles, same DOM,
    scoped CSS. Stubbed interactions — swipe + long-press-drag work
    but don't hit the API. Lets the household pick a style on-device."""

    styles = [
        ("a", "Style A — White rows, hairlines",
         "Rows blend with the white card. Only a 1px hairline + the content separates them. Most subtle."),
        ("b", "Style B — Cream pill rows",
         "Each row is its own rounded cream pill inside the white card. Pills have their own corners — square inner corners impossible by construction."),
        ("c", "Style C — Cream card, white pill rows",
         "Inverted: the card is the tonal sheet, rows are white pills inside it. Apple settings-list pattern."),
        ("d", "Style D — Flat list, no day card",
         "No day-card wrapper at all. Day header floats; rows span the full screen with hairlines."),
        ("e", "Style E — Floating individual rows",
         "Every row is its own elevated card. No nesting whatsoever — each row stands alone."),
        ("f", "Style F — iMessage-style pill actions",
         "Floating rows (like E), but swipe-left reveals two circular icon pills (⇄ swap, 🗑 delete) in their own track to the right of the row. Mirrors iOS Messages."),
        ("g", "Style G — Telegram: pills grow with swipe",
         "Like F, but the pills SCALE from 0→1 with swipe distance. Feels heavier — your finger is dragging the pills into existence, not just revealing them."),
    ]

    today_badge = '<span class="text-[10px] uppercase tracking-wider bg-amber-100 text-amber-800 font-semibold px-2 py-0.5 rounded-full">Today</span>'

    sections = []
    for slug, title, desc in styles:
        sections.append(f"""
          <section class="mt-8">
            <div class="mb-3 px-1">
              <span class="style-pick">{slug.upper()}</span>
              <h2 class="serif text-2xl font-medium tracking-tight mt-2">{title}</h2>
              <p class="text-sm text-stone-500 mt-1">{desc}</p>
            </div>
            <div class="style-{slug}">
              {_demo_day_card(slug, "Saturday", "Jun 6", today_badge)}
            </div>
          </section>
        """)

    body = f"""
      {_PLAN_STYLES_CSS}
      <div class="mb-1 text-[10px] uppercase tracking-[0.25em] text-stone-400">Exploration</div>
      <h1 class="serif text-4xl font-medium tracking-tight leading-none">Pick a <em class="text-stone-500">style</em>.</h1>
      <p class="text-sm text-stone-500 mt-2">Same data, 7 different row treatments. Tap, swipe-left, and long-press on a row to see how it behaves in each. Actions are stubbed — nothing here mutates the real plan.</p>

      <div class="dials mt-4">
        <div class="dial">
          <span class="dial-label">Hold time</span>
          <div class="dial-buttons" data-set="hold">
            <button data-val="300">300ms</button>
            <button data-val="500" class="active">500ms</button>
            <button data-val="800">800ms</button>
            <button data-val="1200">1.2s</button>
          </div>
        </div>
        <div class="dial">
          <span class="dial-label">Swipe threshold</span>
          <div class="dial-buttons" data-set="swipe">
            <button data-val="40">Easy</button>
            <button data-val="60" class="active">Medium</button>
            <button data-val="90">Firm</button>
          </div>
        </div>
        <div class="dial">
          <span class="dial-label">Haptics</span>
          <div class="dial-buttons" data-set="haptic">
            <button data-val="0">Off</button>
            <button data-val="8">Light</button>
            <button data-val="15" class="active">Med</button>
            <button data-val="30">Heavy</button>
          </div>
        </div>
        <p class="text-[11px] text-stone-400 mt-2 leading-snug">Haptics need Android/desktop Chrome — iOS Safari ignores <code>navigator.vibrate</code>.</p>
      </div>

      <script>
      (function() {{
        document.querySelectorAll('.dial-buttons').forEach(group => {{
          const setter = group.dataset.set;
          group.querySelectorAll('button').forEach(btn => {{
            btn.addEventListener('click', () => {{
              group.querySelectorAll('button').forEach(b => b.classList.remove('active'));
              btn.classList.add('active');
              const val = parseInt(btn.dataset.val, 10);
              // The gesture IIFE below exposes setters on window.__planStyles.
              const api = window.__planStyles;
              if (!api) return;
              if (setter === 'hold')   api.setHold(val);
              if (setter === 'swipe')  api.setSwipe(val);
              if (setter === 'haptic') api.setHaptic(val);
              if (setter === 'haptic' && val > 0 && navigator.vibrate) {{
                navigator.vibrate(val);  // immediate preview of the chosen strength
              }}
            }});
          }});
        }});
      }})();
      </script>

      {''.join(sections)}

      <div class="mt-12 mb-8 text-center">
        <a href="{PREFIX}/app/plan" class="serif italic text-lg text-stone-500 hover:text-amber-800">← back to the real plan</a>
      </div>

      <script>
      // Page-wide: kill iOS magnifier / loupe / context menu on
      // long-press of any element. CSS user-select:none alone doesn't
      // fully suppress the magnifier on iOS 17+; cancelling
      // `selectstart` does. preventDefault on contextmenu kills the
      // long-press callout for completeness.
      document.addEventListener('selectstart', (e) => e.preventDefault());
      document.addEventListener('contextmenu', (e) => e.preventDefault());

      (function() {{
        // Slimmed JS — just the gestures, no mutation calls. Lets
        // the household *feel* how each style behaves under tap/swipe/drag
        // without altering the real plan data. Several values below
        // are mutable (let) so the dial controls at the top of the
        // page can tune them live.
        let SWIPE_THRESHOLD = 60;
        const SWIPE_OFFSET = 160;
        let LONG_PRESS_MS = 500;
        let HAPTIC_LEVEL = 15;  // ms vibrate; 0 = off
        const MOVE_TOLERANCE = 8;

        // Expose setters so the dial buttons can mutate these.
        window.__planStyles = {{
          setHold(v) {{ LONG_PRESS_MS = v; }},
          setSwipe(v) {{ SWIPE_THRESHOLD = v; }},
          setHaptic(v) {{ HAPTIC_LEVEL = v; }},
        }};

        function buzz(scale = 1) {{
          if (HAPTIC_LEVEL > 0) {{
            try {{ navigator.vibrate?.(HAPTIC_LEVEL * scale); }} catch (e) {{}}
          }}
        }}
        let openRow = null;
        let dragChip = null;
        let dragCtx = null;

        function cancelDrag() {{
          document.body.classList.remove('is-dragging');
          document.querySelectorAll('.swipe-row.dragging').forEach(r => r.classList.remove('dragging'));
          document.querySelectorAll('.drop-target.drop-hover').forEach(t => t.classList.remove('drop-hover'));
          if (dragChip) {{ dragChip.remove(); dragChip = null; }}
          dragCtx = null;
        }}

        function startDrag(row, x, y) {{
          if (openRow) {{
            openRow.classList.remove('is-open');
            const oi = openRow.querySelector('.swipe-row-inner');
            if (oi) oi.style.transform = '';
            openRow = null;
          }}
          row.classList.remove('is-open');
          const t = row.querySelector('.swipe-row-inner');
          if (t) t.style.transform = '';
          dragCtx = {{ row, recipeName: t.dataset.recipeName }};
          row.classList.add('dragging');
          document.body.classList.add('is-dragging');
          dragChip = document.createElement('div');
          dragChip.className = 'drag-chip';
          const thumb = row.querySelector('.swipe-row-inner > div:first-child');
          dragChip.innerHTML =
            (thumb ? `<div class="drag-chip-thumb ${{thumb.className.match(/matte-\\w+/)?.[0] || ''}}">${{thumb.textContent.trim()}}</div>` : '')
            + `<span>Move <em>${{dragCtx.recipeName}}</em></span>`;
          document.body.appendChild(dragChip);
          updateChip(x, y);
          buzz(2);  // medium pulse on drag-armed
        }}

        function updateChip(x, y) {{
          if (!dragChip) return;
          dragChip.style.left = x + 'px';
          dragChip.style.top = y + 'px';
          dragChip.style.display = 'none';
          const under = document.elementFromPoint(x, y);
          dragChip.style.display = '';
          const target = under ? under.closest('.drop-target') : null;
          document.querySelectorAll('.drop-target.drop-hover').forEach(t => {{
            if (t !== target) t.classList.remove('drop-hover');
          }});
          if (target) target.classList.add('drop-hover');
        }}

        document.querySelectorAll('.swipe-row').forEach(row => {{
          const inner = row.querySelector('.swipe-row-inner');
          let startX = 0, startY = 0, dragging = false, dx = 0;
          let longPressTimer = null;
          let longPressing = false;

          inner.addEventListener('touchstart', (e) => {{
            if (e.touches.length !== 1) return;
            startX = e.touches[0].clientX;
            startY = e.touches[0].clientY;
            dragging = false;
            dx = row.classList.contains('is-open') ? -SWIPE_OFFSET : 0;
            longPressing = false;
            longPressTimer = setTimeout(() => {{
              longPressTimer = null;
              longPressing = true;
              startDrag(row, startX, startY);
            }}, LONG_PRESS_MS);
          }}, {{ passive: true }});

          inner.addEventListener('touchmove', (e) => {{
            const tx = e.touches[0].clientX;
            const ty = e.touches[0].clientY;
            const dxRaw = tx - startX;
            const dyRaw = ty - startY;
            if (longPressing) {{ e.preventDefault(); updateChip(tx, ty); return; }}
            if (longPressTimer && (Math.abs(dxRaw) > MOVE_TOLERANCE || Math.abs(dyRaw) > MOVE_TOLERANCE)) {{
              clearTimeout(longPressTimer);
              longPressTimer = null;
            }}
            if (!dragging) {{
              if (Math.abs(dyRaw) > 8 && Math.abs(dyRaw) > Math.abs(dxRaw)) return;
              if (Math.abs(dxRaw) > 8) {{
                dragging = true;
                row.classList.add('is-swiping');
                if (openRow && openRow !== row) {{
                  openRow.classList.remove('is-open');
                  openRow.querySelector('.swipe-row-inner').style.transform = '';
                  openRow = null;
                }}
              }}
            }}
            if (dragging) {{
              const base = row.classList.contains('is-open') ? -SWIPE_OFFSET : 0;
              dx = Math.max(-SWIPE_OFFSET, Math.min(0, base + dxRaw));
              inner.style.transform = `translateX(${{dx}}px)`;
              // Style G reads this to scale the pills with the swipe.
              row.style.setProperty('--swipe-progress', Math.abs(dx) / SWIPE_OFFSET);
              e.preventDefault();
            }}
          }}, {{ passive: false }});

          inner.addEventListener('touchend', () => {{
            if (longPressing) {{ cancelDrag(); longPressing = false; return; }}
            if (longPressTimer) {{ clearTimeout(longPressTimer); longPressTimer = null; }}
            row.classList.remove('is-swiping');
            if (!dragging) return;
            if (dx <= -SWIPE_THRESHOLD) {{
              row.classList.add('is-open');
              inner.style.transform = '';
              row.style.setProperty('--swipe-progress', 1);
              openRow = row;
              buzz(1);  // light pulse on lock-open
            }} else {{
              row.classList.remove('is-open');
              inner.style.transform = '';
              row.style.setProperty('--swipe-progress', 0);
              if (openRow === row) openRow = null;
            }}
            dragging = false;
          }});

          inner.addEventListener('touchcancel', () => {{
            if (longPressTimer) clearTimeout(longPressTimer);
            longPressTimer = null;
            if (longPressing) {{ cancelDrag(); longPressing = false; }}
            row.classList.remove('is-swiping');
          }});

          inner.addEventListener('contextmenu', (e) => e.preventDefault());

          document.addEventListener('click', (e) => {{
            if (openRow === row && !row.contains(e.target)) {{
              row.classList.remove('is-open');
              openRow = null;
            }}
          }});

          // Stub swap/delete — close the row, flash a hint.
          const stub = (label) => {{
            row.classList.remove('is-open');
            openRow = null;
            const hint = document.createElement('div');
            hint.textContent = label + ' (demo, not wired)';
            hint.style.cssText = 'position:fixed;left:50%;bottom:90px;transform:translateX(-50%);background:#1c1917;color:#FAF7F2;padding:8px 14px;border-radius:9999px;font-size:13px;z-index:200;opacity:0;transition:opacity .15s';
            document.body.appendChild(hint);
            requestAnimationFrame(() => hint.style.opacity = '1');
            setTimeout(() => {{ hint.style.opacity = '0'; setTimeout(() => hint.remove(), 200); }}, 1400);
          }};
          row.querySelector('.swipe-swap').addEventListener('click', () => stub('Swap'));
          row.querySelector('.swipe-del').addEventListener('click', () => stub('Delete'));
        }});

        document.querySelectorAll('.slot-add').forEach(b => {{
          b.addEventListener('click', (e) => {{ e.preventDefault(); }});
        }});

        // Cancel drag if user lifts finger outside any handler.
        document.addEventListener('touchend', (e) => {{
          if (dragCtx) {{
            // No mutation in demo — just clean up.
            cancelDrag();
          }}
        }});
      }})();
      </script>
    """
    return HTMLResponse(_shell("Plan styles", body, active="plan"))


# =============================================================================
# Shop
# =============================================================================

def _household_id(db: Session) -> int:
    """Single-household assumption for now — return the first household
    or create one. Matches the existing convention in /preview/."""
    hh = db.exec(select(Household).order_by(Household.id)).first()
    if hh is None:
        hh = Household(name="Default", grocery_categories=["Produce", "Pantry", "Other"])
        db.add(hh)
        db.commit()
        db.refresh(hh)
    return hh.id  # type: ignore[return-value]


def _shop_window_sessions(db: Session) -> list[CookingSession]:
    """Cook sessions the shopping list covers (today + 13 days), shopped
    or not. Shared by the shop page and the recipe page's have/need marks
    so both always agree on what "on the list" means."""
    today = _Date.today()
    return list(db.exec(
        select(CookingSession)
        .join(SlotAssignment, SlotAssignment.session_id == CookingSession.id)
        .where(SlotAssignment.date >= today)
        .where(SlotAssignment.date <= today + _Td(days=13))
        .where(SlotAssignment.position == 1)
        .distinct()
    ).all())


def _shop_checked_keys(db: Session) -> set[str]:
    return set(db.exec(
        select(ShopCheck.item_key)
        .where(ShopCheck.household_id == _household_id(db))
        .where(ShopCheck.list_key == "week")
    ).all())


def _recipe_have_map(recipe_id: int, db: Session) -> dict[int, bool] | None:
    """ingredient_id -> already bought?, for a recipe that is on the
    current shopping list. None when the recipe isn't planned (no marks).
    "Bought" = its shopping-list line is ticked, or the whole session was
    marked shopped."""
    mine = [s for s in _shop_window_sessions(db) if s.recipe_id == recipe_id]
    if not mine:
        return None
    pending = [s.id for s in _shop_window_sessions(db) if s.shopped_at is None and s.id is not None]
    out: dict[int, bool] = {}
    if any(s.shopped_at is None for s in mine):
        checked = _shop_checked_keys(db)
        for it in _aggregate_for_sessions(pending, db):
            for src in it.sources:
                if src.recipe_id == recipe_id and src.ingredient_id is not None:
                    out[src.ingredient_id] = f"{it.canonical_name}|{it.unit}" in checked
        return out
    # Every planned session of it is already marked shopped.
    for ing, _sec in db.exec(
        select(Ingredient, IngredientSection)
        .join(IngredientSection, Ingredient.section_id == IngredientSection.id)
        .where(IngredientSection.recipe_id == recipe_id)
    ).all():
        if not ing.optional and ing.qty and ing.display_name != "unknown":
            out[ing.id] = True
    return out


@router.get("/app/shop", response_class=HTMLResponse)
def shop_page(db: Session = Depends(get_session), view: str = "") -> HTMLResponse:
    """Shop list for the current week's planned sessions, with extras.
    `view=recipe` groups the same lines under each recipe instead of by
    aisle (ticks are shared: one line, shown under every recipe using it)."""
    by_recipe = view == "recipe"
    today = _Date.today()
    end = today + _Td(days=13)

    # Position==1 only — cook slots, not leftovers. A meal whose cook
    # already happened doesn't need re-shopping; the leftover days
    # share fate with the cook session and were bought for at the
    # same trip. Shop list semantics: "what to buy" (strict), not
    # "what's planned" (forgiving).
    #
    # ALSO exclude sessions where shopped_at is set — those have been
    # explicitly "marked shopped" by the user. They live in a separate
    # shopped_sessions list so the empty state can offer to unshop.
    all_sessions_in_window = _shop_window_sessions(db)
    sessions = [s for s in all_sessions_in_window if s.shopped_at is None]
    shopped_sessions = [s for s in all_sessions_in_window if s.shopped_at is not None]
    session_ids = [s.id for s in sessions if s.id is not None]

    items = _aggregate_for_sessions(session_ids, db) if session_ids else []
    by_category: dict[str, list[ShoppingItem]] = defaultdict(list)
    for it in items:
        by_category[it.category].append(it)

    # Extras grouped by category — household-scoped, list_key='week'.
    hh_id = _household_id(db)
    extras = db.exec(
        select(ShopExtra)
        .where(ShopExtra.household_id == hh_id)
        .where(ShopExtra.list_key == "week")
        .order_by(ShopExtra.created_at)
    ).all()
    extras_by_category: dict[str, list[ShopExtra]] = defaultdict(list)
    for e in extras:
        extras_by_category[e.category].append(e)

    # Household-shared check-off state for aggregated rows. Lets one
    # cook tick "carrots" and the other cook see the strike-through
    # after a refresh — the old localStorage stopgap was per-device.
    checked_keys = _shop_checked_keys(db)

    # Recipe chip row — what's contributing. Deduped by recipe_id with
    # a "×N" suffix when a recipe is planned more than once that week,
    # so a 3-batch smoothie shows once instead of three identical chips.
    counts: Counter[int] = Counter()
    titles: dict[int, str] = {}
    for s in sessions:
        rec = db.get(Recipe, s.recipe_id)
        if rec and rec.id is not None:
            counts[rec.id] += 1
            titles[rec.id] = rec.display_title()
    # Tapping a chip checks off the whole recipe (marks its cooks
    # shopped, so its amounts leave the list). Checked-off recipes keep
    # a struck chip so one tap brings them back.
    done_titles: dict[int, str] = {}
    for s in shopped_sessions:
        if s.recipe_id not in titles:
            rec = db.get(Recipe, s.recipe_id)
            if rec and rec.id is not None:
                done_titles[rec.id] = rec.display_title()

    def _chip(rid: int, title: str, done: bool) -> str:
        style = (
            "background:#ecfdf5;color:#78716c;text-decoration:line-through;"
            if done else "background:#f5f5f4;color:#44403c;"
        )
        mark = (
            "<span style='color:#059669;font-weight:600;'>✓</span>" if done else
            "<span style='display:inline-block;width:0.75rem;height:0.75rem;border-radius:9999px;border:1.5px solid #a8a29e;'></span>"
        )
        return (
            f"<button type='button' class='recipe-chip inline-flex items-center gap-1.5 text-xs px-3 py-1 rounded-full' "
            f"style='{style}' data-recipe='{rid}' data-shopped='{1 if done else 0}' "
            f"aria-pressed='{'true' if done else 'false'}'>{mark}"
            f"<span>{_h(title)}{f' ×{counts[rid]}' if counts[rid] > 1 else ''}</span></button>"
        )
    chips = "".join(_chip(rid, titles[rid], False) for rid in titles) + "".join(
        _chip(rid, t, True) for rid, t in done_titles.items()
    )

    def _aggregated_row(it: ShoppingItem, own_qty: str | None = None, also: str = "") -> str:
        # own_qty/also: by-recipe view — show this recipe's share of the
        # line and which other recipes need it too. Same key either way,
        # so a tick is one tick everywhere.
        key = f"{it.canonical_name}|{it.unit}"
        is_checked = key in checked_keys
        chk = " checked" if is_checked else ""
        struck = " line-through text-stone-400" if is_checked else ""
        # Multi-part lines (rule 3, e.g. "2 cup + 250 g") have no single
        # unit to offer conversions for — skip the alt-qty menu.
        alts = [] if (own_qty or " + " in (it.display_qty or "")) else _alt_qty_displays(it.qty, it.unit)
        qty_orig = _h(own_qty or it.display_qty or _qty_text(it.qty, it.unit))
        if alts:
            # Dropdown of unit conversions — tap the qty to open a menu
            # of alternatives, tap one to set. (Was a cycle-on-tap; menu
            # lets you jump straight to the unit you want.)
            opts_html = "".join(
                f"<button type='button' class='qty-opt{' is-current' if i == 0 else ''}'>{_h(opt)}</button>"
                for i, opt in enumerate([qty_orig] + alts)
            )
            qty_html = (
                f"<span class='qty-wrap shrink-0'>"
                f"  <button type='button' class='row-qty qty-tap serif text-base font-medium w-24 text-right text-stone-700 tabular-nums{struck}'>{qty_orig}</button>"
                f"  <div class='qty-menu' hidden>{opts_html}</div>"
                f"</span>"
            )
        else:
            qty_html = (
                f"<span class='row-qty serif text-base font-medium w-24 text-right text-stone-700 tabular-nums shrink-0{struck}'>{qty_orig}</span>"
            )
        # Every row can show where it came from: tap the ▾ on the right
        # to expand one line per contributing recipe (linked). Rows fed
        # by several recipes also show the count (×N).
        src_lines = "".join(
            f"<div>{_h(_qty_text(s.qty, s.unit))} · "
            + (
                f"<a href='{PREFIX}/app/recipe/{s.recipe_id}' class='text-amber-700 hover:underline'>{_h(s.recipe_name)}</a>"
                if s.recipe_id else _h(s.recipe_name)
            )
            + "</div>"
            for s in it.sources
        )
        if own_qty:
            sources = ""
            count_badge = ""
        elif src_lines:
            sources = (
                f"<div class='shop-sources text-xs text-stone-500 pb-3 space-y-1' style='padding-left:3.25rem;padding-right:1rem;' hidden>"
                f"{src_lines}</div>"
            )
            count_badge = (
                f"<button type='button' class='shop-src-toggle text-xs text-stone-400 hover:text-stone-900 shrink-0 px-2 py-1 rounded hover:bg-stone-100' "
                f"aria-label='Show which recipes this is for'>{f'×{it.source_count} ' if it.source_count > 1 else ''}▾</button>"
            )
        else:
            sources = ""
            count_badge = ""
        return (
            f"<div class='shop-row-wrap border-b border-stone-100 last:border-0'>"
            f"  <label class='shop-row flex items-center gap-4 px-4 py-3' data-key='{_h(key)}'>"
            f"    <input type='checkbox' class='shop-check h-5 w-5 accent-amber-700 cursor-pointer shrink-0'{chk}>"
            f"    {qty_html}"
            f"    <span class='flex-1 min-w-0'>"
            f"      <span class='row-name text-base leading-snug{struck}'>{_h(it.display_name)}</span>"
            + (f"<span class='block text-xs text-stone-400'>also for {_h(also)}</span>" if also else "")
            + f"    </span>"
            f"    {count_badge}"
            f"  </label>"
            f"  {sources}"
            f"</div>"
        )

    def _extra_row(e: ShopExtra) -> str:
        chk = "checked" if e.checked else ""
        struck = "line-through text-stone-400" if e.checked else ""
        return (
            f"<div class='extras-row flex items-center gap-4 px-4 py-3 border-b border-stone-100 last:border-0' data-id='{e.id}'>"
            f"  <input type='checkbox' class='ex-check h-5 w-5 accent-amber-700 cursor-pointer shrink-0' {chk}>"
            f"  <span class='ex-qty serif text-base font-medium w-24 text-right text-stone-700 tabular-nums shrink-0'>{_h(e.qty or '')}</span>"
            f"  <span class='ex-name text-base flex-1 leading-snug {struck}'>{_h(e.name)}</span>"
            f"  <button type='button' class='ex-del text-stone-300 hover:text-rose-500 shrink-0 text-lg leading-none' aria-label='Remove'>×</button>"
            f"</div>"
        )

    sections_html = []
    done_sections_html = []  # fully-ticked aisles sink below the rest

    def _add_section(title_html: str, cat: str, rows: str, total: int, done: int) -> None:
        pct = round(100 * done / total) if total else 0
        progress_html = (
            f"<div class='cat-progress flex items-center gap-2 mb-2 text-[11px] tabular-nums' data-cat='{_h(cat)}'>"
            f"  <div class='flex-1 h-1 bg-stone-200 rounded-full overflow-hidden'>"
            f"    <div class='cat-progress-bar h-full bg-emerald-500 transition-all duration-200' style='width:{pct}%'></div>"
            f"  </div>"
            f"  <span class='cat-progress-text text-stone-500 shrink-0 w-12 text-right'>"
            f"    <span class='cat-done'>{done}</span><span class='text-stone-300'>/</span><span class='cat-total'>{total}</span>"
            f"  </span>"
            f"</div>"
        ) if total else ""
        (done_sections_html if total and done == total else sections_html).append(
            f"<section class='mt-6 shop-section' data-order='{len(sections_html) + len(done_sections_html)}'>"
            f"  <h3 class='serif text-xl font-medium tracking-tight mb-1'>{title_html}</h3>"
            f"  {progress_html}"
            f"  <div class='card overflow-hidden' data-cat='{_h(cat)}'>{rows}</div>"
            f"</section>"
        )

    if by_recipe:
        # One section per planned recipe, in cook order; a line shared by
        # two recipes appears under both with that recipe's own amount.
        first_cook: dict[int, _Date] = {}
        for sess in sessions:
            if sess.cook_date >= today:
                first_cook[sess.recipe_id] = min(first_cook.get(sess.recipe_id, sess.cook_date), sess.cook_date)
            else:
                first_cook.setdefault(sess.recipe_id, sess.cook_date)
        for rid in sorted(first_cook, key=lambda r: (first_cook[r], titles.get(r, ""))):
            mine: list[tuple[bool, str]] = []
            for it in items:
                own = [s for s in it.sources if s.recipe_id == rid]
                if not own:
                    continue
                own_qty = " + ".join(_qty_text(s.qty, s.unit) for s in own)
                others = sorted({s.recipe_name for s in it.sources if s.recipe_id != rid})
                is_checked = f"{it.canonical_name}|{it.unit}" in checked_keys
                mine.append((is_checked, _aggregated_row(it, own_qty=own_qty, also=", ".join(others))))
            if not mine:
                continue
            mine.sort(key=lambda t: t[0])  # stable: unticked first
            title = (
                f"<a href='{PREFIX}/app/recipe/{rid}' class='hover:text-amber-800'>{_h(titles.get(rid, 'Recipe'))}</a>"
                f" <span class='text-xs text-stone-400 font-normal' style='font-family:Inter,system-ui,sans-serif;'>{first_cook[rid].strftime('%a %b %-d')}</span>"
            )
            _add_section(title, f"recipe-{rid}", "".join(r for _, r in mine), len(mine), sum(1 for c, _ in mine if c))
        by_category = defaultdict(list)  # extras still group by aisle below

    all_categories = sorted(set(list(by_category.keys()) + list(extras_by_category.keys())))
    for cat in all_categories:
        # Checked items fall to the bottom of their category — keeps
        # the unchecked-still-to-buy stuff at the top where the eye
        # lands first. JS replicates this on toggle without a reload.
        agg_items = by_category.get(cat, [])
        ex_items = extras_by_category.get(cat, [])
        agg_unchecked = [it for it in agg_items if f"{it.canonical_name}|{it.unit}" not in checked_keys]
        agg_checked   = [it for it in agg_items if f"{it.canonical_name}|{it.unit}" in checked_keys]
        ex_unchecked  = [e for e in ex_items if not e.checked]
        ex_checked    = [e for e in ex_items if e.checked]
        rows = "".join(_aggregated_row(it) for it in agg_unchecked)
        rows += "".join(_extra_row(e) for e in ex_unchecked)
        rows += "".join(_aggregated_row(it) for it in agg_checked)
        rows += "".join(_extra_row(e) for e in ex_checked)
        # Per-section progress — visible at-a-glance check on how much
        # of each aisle is done. The JS sync updates these counters in
        # place on toggle without a full reload.
        total_in_cat = len(agg_items) + len(ex_items)
        done_in_cat  = len(agg_checked) + len(ex_checked)
        if rows:
            _add_section(_h(cat), cat, rows, total_in_cat, done_in_cat)

    sections_html += done_sections_html
    if not sections_html and not extras:
        # Check whether there are upcoming leftover slots (i.e., the
        # plan has meals but the cooks already happened). That's a
        # legit empty list — already shopped — not a "do nothing" state.
        leftover_ahead = db.exec(
            select(SlotAssignment)
            .where(SlotAssignment.date >= today)
            .where(SlotAssignment.date <= end)
            .where(SlotAssignment.position > 1)
            .limit(1)
        ).first()
        if leftover_ahead:
            sections_html.append(
                "<div class='card p-8 text-center mt-6'>"
                "  <div class='serif text-2xl text-stone-400 italic mb-1'>All shopped.</div>"
                "  <p class='text-sm text-stone-500 mt-2'>Upcoming meals are leftovers from cooks you already shopped for. Nothing new to buy this week.</p>"
                "</div>"
            )
        else:
            sections_html.append(
                "<div class='card p-8 text-center mt-6'>"
                "  <div class='serif text-2xl text-stone-400 italic mb-1'>Nothing here yet.</div>"
                f"  <p class='text-sm text-stone-500 mt-2'>Plan some meals on the <a href='{PREFIX}/app/plan' class='text-amber-700 hover:underline'>Plan</a> tab — or add ad-hoc items below.</p>"
                "</div>"
            )

    # Categories for the extras form selector.
    hh = db.get(Household, hh_id)
    cat_options = (hh.grocery_categories if hh else ["Pantry"]) or ["Pantry", "Other"]
    cat_opts_html = "".join(f"<option value='{_h(c)}'>{_h(c)}</option>" for c in cat_options)

    # Global progress: across all aggregated rows + all extras.
    total_items = len(items) + len(extras)
    done_items = (
        sum(1 for it in items if f"{it.canonical_name}|{it.unit}" in checked_keys)
        + sum(1 for e in extras if e.checked)
    )
    all_done = (total_items > 0 and done_items == total_items)
    global_pct = round(100 * done_items / total_items) if total_items else 0

    def _seg(label: str, href: str, on: bool) -> str:
        style = "background:#1c1917;color:#fff;" if on else "background:#fff;color:#57534e;"
        return (
            f"<a href='{href}' class='flex-1 text-center text-sm font-medium py-2 rounded-lg' "
            f"style='{style}'{' aria-current=\"page\"' if on else ''}>{label}</a>"
        )
    view_switch = (
        "<div id='shop-view' class='flex gap-1 mt-3 p-1 rounded-xl border border-stone-200 bg-white'>"
        + _seg("By aisle", f"{PREFIX}/app/shop", not by_recipe)
        + _seg("By recipe", f"{PREFIX}/app/shop?view=recipe", by_recipe)
        + "</div>"
    ) if items else ""

    body = f"""
      <div class="mb-1 text-[10px] uppercase tracking-[0.25em] text-stone-400">Marketing list</div>
      <h1 class="serif text-4xl font-medium tracking-tight leading-none">The <em class="text-stone-500">shop</em>.</h1>
      <p class="text-sm text-stone-500 mt-2">
        {len(items)} aggregated item{'s' if len(items) != 1 else ''} from {len(sessions)} planned meal{'s' if len(sessions) != 1 else ''}{f' · {len(extras)} extra{chr(115) if len(extras) != 1 else ""}' if extras else ''}
      </p>

      {view_switch}

      {f'''
      <div class="mt-3 flex items-center gap-3" id="global-progress">
        <div class="flex-1 h-1.5 bg-stone-200 rounded-full overflow-hidden">
          <div id="gp-bar" class="h-full bg-emerald-500 transition-all duration-300" style="width:{global_pct}%"></div>
        </div>
        <span class="text-xs text-stone-500 tabular-nums shrink-0">
          <span id="gp-done">{done_items}</span><span class="text-stone-300">/</span><span id="gp-total">{total_items}</span>
        </span>
        <button type="button" id="shop-reset"
                class="text-[11px] uppercase tracking-wider font-semibold text-stone-500 hover:text-rose-700 px-2 py-1 rounded-md"
                {'' if done_items > 0 else 'hidden'}>
          Reset
        </button>
      </div>
      ''' if total_items else ''}

      <div id="celebration" class="card p-6 text-center mt-5 bg-emerald-50 border border-emerald-200" {'' if all_done else 'hidden'}>
        <div class="text-3xl mb-2">🎉</div>
        <div class="serif text-2xl font-medium text-emerald-900">Shopping done!</div>
        <p class="text-sm text-emerald-700 mt-1">Everything on the list is in the cart.</p>
        <button type="button" id="mark-shopped-btn"
                style="background:#047857;color:white;"
                class="mt-4 font-medium px-5 py-2.5 rounded-xl text-sm">
          Mark these meals as shopped
        </button>
      </div>

      {(
        '<div class="card p-5 text-center mt-5 bg-stone-50 border border-stone-200">'
        '  <div class="text-2xl mb-1">✓</div>'
        f'  <div class="serif text-xl font-medium text-stone-800">Shopped for the week</div>'
        f'  <p class="text-sm text-stone-500 mt-1">{len(shopped_sessions)} meal{"s" if len(shopped_sessions) != 1 else ""} already covered. Plan more to build a new list.</p>'
        '  <button type="button" id="unshop-btn" class="mt-3 text-xs uppercase tracking-wider font-semibold text-stone-500 hover:text-rose-700 px-3 py-1.5 rounded-md">Unshop</button>'
        '</div>'
      ) if shopped_sessions and not session_ids else ''}

      {f'<div class="flex flex-wrap gap-1.5 mt-3" id="recipe-chips">{chips}</div><p class="text-xs text-stone-400 mt-1.5">Tap a recipe to check it all off.</p>' if chips else ''}

      <div id="shop-sections">{''.join(sections_html)}</div>

      <section class="mt-8">
        <h3 class="serif text-xl font-medium tracking-tight mb-2">Add an item</h3>
        <form id="extras-form" class="card p-4 space-y-2">
          <div class="flex gap-2">
            <input id="ex-qty" type="text" placeholder="qty" class="w-20 border border-stone-200 rounded-lg px-3 py-2 bg-white">
            <input id="ex-name" type="text" placeholder="What do you need?" autocomplete="off"
                   class="flex-1 border border-stone-200 rounded-lg px-3 py-2 bg-white">
          </div>
          <div class="flex gap-2">
            <select id="ex-cat" class="flex-1 border border-stone-200 rounded-lg px-3 py-2 bg-white">{cat_opts_html}</select>
            <button type="submit" class="bg-stone-900 hover:bg-stone-700 text-white text-sm font-medium px-5 py-2 rounded-lg">Add</button>
          </div>
        </form>
      </section>

      <style>
        /* Tap-to-convert qty cell. The dotted underline (only on
           tappable rows — non-convertible units stay plain spans)
           tells you the cell is interactive. Tap opens a dropdown of
           alternative units; tap one to pick. */
        .qty-wrap {{ position: relative; display: inline-block; }}
        button.row-qty.qty-tap {{
          background: transparent; border: 0;
          cursor: pointer;
          text-decoration: underline dotted #d6d3d1;
          text-underline-offset: 3px;
        }}
        button.row-qty.qty-tap:active {{ color: #b45309; }}
        /* Fixed, not absolute: the category card is overflow-hidden
           (rounded corners), which clipped the menu on a card's last
           rows. JS moves it to <body> and places it from the button's
           viewport rect. */
        .qty-menu {{
          position: fixed;
          background: white; border: 1px solid #e7e5e4;
          border-radius: 0.625rem;
          box-shadow: 0 8px 24px -4px rgba(0,0,0,0.15), 0 0 0 1px rgba(0,0,0,0.03);
          min-width: 7.5rem; z-index: 30;
          padding: 0.25rem;
        }}
        .qty-menu[hidden] {{ display: none; }}
        .qty-opt {{
          display: block; width: 100%; text-align: right;
          padding: 6px 10px; font-size: 13px;
          border-radius: 0.4rem;
          font-variant-numeric: tabular-nums;
          color: #44403c; background: transparent; border: 0;
        }}
        .qty-opt:active {{ background: #FEF3C7; }}
        .qty-opt.is-current {{ background: #FDE68A; color: #78350F; font-weight: 600; }}
      </style>
      <script>
        // Unit-conversion dropdown. Tap the qty → menu of alternative
        // units pops below. Tap an option → qty displays that unit.
        // preventDefault + stopPropagation on every click stops the
        // surrounding <label> from toggling the checkbox.
        document.querySelectorAll('.qty-wrap').forEach(wrap => {{
          const btn  = wrap.querySelector('button.row-qty.qty-tap');
          const menu = wrap.querySelector('.qty-menu');
          if (!btn || !menu) return;
          btn.addEventListener('click', (e) => {{
            e.preventDefault();
            e.stopPropagation();
            document.querySelectorAll('.qty-menu:not([hidden])').forEach(m => {{
              if (m !== menu) m.hidden = true;
            }});
            // Lift the menu out to <body>: the category <section> keeps a
            // transform from its entry animation, which would re-anchor
            // (and clip) a fixed child.
            if (menu.parentElement !== document.body) document.body.appendChild(menu);
            menu.hidden = !menu.hidden;
            if (menu.hidden) return;
            // Below the qty, flipping above when it would run off-screen.
            const r = btn.getBoundingClientRect();
            const mh = menu.offsetHeight, mw = menu.offsetWidth;
            const below = r.bottom + 4;
            const top = (below + mh > window.innerHeight - 8) ? r.top - 4 - mh : below;
            menu.style.top = Math.max(8, top) + 'px';
            menu.style.left = Math.max(8, Math.min(r.right - mw, window.innerWidth - mw - 8)) + 'px';
          }});
          menu.querySelectorAll('.qty-opt').forEach(opt => {{
            opt.addEventListener('click', (e) => {{
              e.preventDefault();
              e.stopPropagation();
              btn.textContent = opt.textContent;
              menu.querySelectorAll('.qty-opt').forEach(o => o.classList.remove('is-current'));
              opt.classList.add('is-current');
              menu.hidden = true;
            }});
          }});
        }});
        // Outside-click, scroll + Escape close any open menu (a fixed
        // menu would otherwise float away from its row on scroll).
        const closeQtyMenus = () =>
          document.querySelectorAll('.qty-menu:not([hidden])').forEach(m => m.hidden = true);
        document.addEventListener('click', closeQtyMenus);
        window.addEventListener('scroll', closeQtyMenus, {{ passive: true }});
        document.addEventListener('keydown', (e) => {{
          if (e.key === 'Escape') {{
            document.querySelectorAll('.qty-menu:not([hidden])').forEach(m => m.hidden = true);
          }}
        }});

        // "Reset" — wipe all check-offs for this week. Confirms first
        // (destructive-ish). The 3s sync poll will replicate this to
        // any other open device.
        (function() {{
          const btn = document.getElementById('shop-reset');
          if (!btn) return;
          btn.addEventListener('click', async () => {{
            const ok = await window.askConfirm({{
              message: 'Reset shopping list?',
              sub: 'Clears every check-off across all categories.',
              okLabel: 'Reset',
            }});
            if (!ok) return;
            try {{
              const r = await fetch('{PREFIX}/app/shop/checks/clear', {{ method: 'POST' }});
              if (!r.ok) throw new Error('HTTP ' + r.status);
              location.reload();
            }} catch (e) {{
              window.notify('Reset failed: ' + e.message, {{tone:'error'}});
            }}
          }});
        }})();

        // "Mark these meals as shopped" — promotes the celebration into
        // a permanent state change. All current-week sessions get
        // shopped_at = now; their ingredients drop out of the
        // aggregation. New sessions added later still show up.
        (function() {{
          const btn = document.getElementById('mark-shopped-btn');
          if (!btn) return;
          btn.addEventListener('click', async () => {{
            const ok = await window.askConfirm({{
              message: 'Mark these meals as shopped?',
              sub: 'Hides this week\\'s ingredients and clears any extras you added. You can unshop later.',
              okLabel: 'Mark shopped',
              danger: false,
            }});
            if (!ok) return;
            try {{
              const r = await fetch('{PREFIX}/app/shop/sessions/mark-shopped', {{ method: 'POST' }});
              if (!r.ok) throw new Error('HTTP ' + r.status);
              window.notify('Shopped for the week ✓', {{tone:'success'}});
              setTimeout(() => location.reload(), 400);
            }} catch (e) {{
              window.notify('Mark-shopped failed: ' + e.message, {{tone:'error'}});
            }}
          }});
        }})();

        // Recipe chips: check off / bring back a whole recipe.
        document.querySelectorAll('.recipe-chip').forEach(chip => {{
          chip.addEventListener('click', async () => {{
            const shopped = chip.dataset.shopped !== '1';
            chip.disabled = true;
            try {{
              const r = await fetch('{PREFIX}/app/shop/recipe/' + chip.dataset.recipe + '/shopped', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ shopped }}),
              }});
              if (!r.ok) throw new Error('HTTP ' + r.status);
              location.reload();
            }} catch (e) {{
              chip.disabled = false;
              window.notify('Could not update: ' + e.message, {{tone:'error'}});
            }}
          }});
        }});

        // "Unshop" — revert the shopped-for-the-week banner.
        (function() {{
          const btn = document.getElementById('unshop-btn');
          if (!btn) return;
          btn.addEventListener('click', async () => {{
            const ok = await window.askConfirm({{
              message: 'Unshop these meals?',
              sub: 'They\\'ll re-appear in the shop list with all items unchecked.',
              okLabel: 'Unshop',
            }});
            if (!ok) return;
            try {{
              const r = await fetch('{PREFIX}/app/shop/sessions/unmark-shopped', {{ method: 'POST' }});
              if (!r.ok) throw new Error('HTTP ' + r.status);
              location.reload();
            }} catch (e) {{
              window.notify('Unshop failed: ' + e.message, {{tone:'error'}});
            }}
          }});
        }})();

        // Tap the ▾ on a row to toggle which recipes it comes from.
        // Pure DOM, no fetch.
        document.querySelectorAll('.shop-src-toggle').forEach(btn => {{
          btn.addEventListener('click', (e) => {{
            e.preventDefault();
            e.stopPropagation();  // inside the row <label>: don't tick the box
            const sources = btn.closest('.shop-row-wrap').querySelector('.shop-sources');
            if (!sources) return;
            const open = !sources.hidden;
            sources.hidden = open;
            btn.textContent = btn.textContent.replace(open ? '▴' : '▾', open ? '▾' : '▴');
          }});
        }});

        // Recompute & paint per-section + global progress counters.
        // Called on every toggle (local OR sync-driven) so the bars
        // and counts stay live without a reload. Also flips the
        // 'Shopping done!' celebration banner when everything's done.
        function updateAllProgress() {{
          let totalAll = 0, doneAll = 0;
          // By-recipe view shows a shared line under each recipe: count
          // it once in the overall total.
          const seen = new Set();
          document.querySelectorAll('section .card[data-cat]').forEach(card => {{
            const cat = card.dataset.cat;
            const rows = card.querySelectorAll('.shop-check, .ex-check');
            const total = rows.length;
            let done = 0;
            rows.forEach(cb => {{
              if (cb.checked) done++;
              const key = cb.closest('.shop-row')?.dataset.key;
              if (key && seen.has(key)) return;
              if (key) seen.add(key);
              totalAll++; if (cb.checked) doneAll++;
            }});
            const wrap = document.querySelector(`.cat-progress[data-cat="${{CSS.escape(cat)}}"]`);
            if (wrap) {{
              wrap.querySelector('.cat-progress-bar').style.width = (total ? (100 * done / total) : 0) + '%';
              wrap.querySelector('.cat-done').textContent  = done;
              wrap.querySelector('.cat-total').textContent = total;
            }}
          }});
          // Finished aisles sink below the unfinished ones (same idea as
          // ticked rows inside an aisle); unticking floats one back up
          // to its alphabetical place.
          const holder = document.getElementById('shop-sections');
          if (holder) {{
            const secs = [...holder.querySelectorAll('.shop-section')];
            const isDone = sec => {{
              const cbs = sec.querySelectorAll('.shop-check, .ex-check');
              return cbs.length > 0 && [...cbs].every(cb => cb.checked);
            }};
            const want = secs.slice().sort((a, b) =>
              (isDone(a) - isDone(b)) || (a.dataset.order - b.dataset.order));
            if (want.some((sec, i) => sec !== secs[i])) want.forEach(sec => holder.appendChild(sec));
          }}
          const bar = document.getElementById('gp-bar');
          const doneEl  = document.getElementById('gp-done');
          const totalEl = document.getElementById('gp-total');
          if (bar) bar.style.width = (totalAll ? (100 * doneAll / totalAll) : 0) + '%';
          if (doneEl)  doneEl.textContent  = doneAll;
          if (totalEl) totalEl.textContent = totalAll;
          const celeb = document.getElementById('celebration');
          if (celeb) {{
            const finished = totalAll > 0 && doneAll === totalAll;
            const wasHidden = celeb.hidden;
            celeb.hidden = !finished;
            // Light haptic blip on the transition into "done".
            if (finished && wasHidden) {{
              try {{ navigator.vibrate?.([20, 40, 20]); }} catch (e) {{}}
            }}
          }}
        }}

        // Reorder helper: checked items fall to the bottom of their
        // category card, unchecked rise to the top. Server-side sort
        // does this on initial render; this keeps the ordering live
        // when a checkbox flips without forcing a reload.
        function reorderShopRow(rowOuter, isChecked) {{
          const card = rowOuter.parentElement;
          if (!card) return;
          if (isChecked) {{
            card.appendChild(rowOuter);
          }} else {{
            // Insert before the first row in this card whose box is
            // checked — keeps unchecked items grouped at the top.
            const firstChecked = Array.from(card.children).find(r => {{
              if (r === rowOuter) return false;
              const cb = r.querySelector('.shop-check, .ex-check');
              return cb && cb.checked;
            }});
            if (firstChecked) card.insertBefore(rowOuter, firstChecked);
            else card.appendChild(rowOuter);
          }}
        }}

        // Aggregated rows — household-shared check-off persisted to the
        // backend so the other cook sees the strike-through after a
        // refresh / when the page reopens. Initial checked state comes
        // from the server render (no JS load on first paint).
        (function() {{
          document.querySelectorAll('.shop-row').forEach(row => {{
            const key = row.dataset.key;
            const cb = row.querySelector('.shop-check');
            const wrap = row.closest('.shop-row-wrap');
            const apply = () => {{
              row.querySelector('.row-qty').classList.toggle('line-through', cb.checked);
              row.querySelector('.row-qty').classList.toggle('text-stone-400', cb.checked);
              row.querySelector('.row-name').classList.toggle('line-through', cb.checked);
              row.querySelector('.row-name').classList.toggle('text-stone-400', cb.checked);
            }};
            cb.addEventListener('change', async () => {{
              apply();
              reorderShopRow(wrap, cb.checked);
              // Same line under another recipe (by-recipe view): keep in step.
              document.querySelectorAll('.shop-row').forEach(twin => {{
                if (twin === row || twin.dataset.key !== key) return;
                const tcb = twin.querySelector('.shop-check');
                tcb.checked = cb.checked;
                ['.row-qty', '.row-name'].forEach(sel => {{
                  twin.querySelector(sel).classList.toggle('line-through', cb.checked);
                  twin.querySelector(sel).classList.toggle('text-stone-400', cb.checked);
                }});
                reorderShopRow(twin.closest('.shop-row-wrap'), cb.checked);
              }});
              updateAllProgress();
              try {{
                await fetch('{PREFIX}/app/shop/check', {{
                  method: 'POST',
                  headers: {{ 'Content-Type': 'application/json' }},
                  body: JSON.stringify({{ item_key: key, checked: cb.checked }}),
                }});
              }} catch (e) {{
                // Revert on network failure (SW will queue too if available).
                cb.checked = !cb.checked; apply();
                reorderShopRow(wrap, cb.checked);
                updateAllProgress();
              }}
            }});
          }});
        }})();

        // Real-time-ish sync: poll the server every 3s and surgically
        // update only the checkboxes that changed — no full page
        // reload, no scroll-position loss. Each cook sees the other's
        // ticks within a few seconds without lifting a finger.
        // cache: 'no-store' on the fetch bypasses any SW caching of
        // the checks endpoint.
        (function() {{
          let inflight = false;
          async function syncAgg() {{
            if (inflight) return;
            if (document.visibilityState !== 'visible') return;
            // Don't yank state out from under an active touch.
            if (document.body.classList.contains('is-dragging')) return;
            inflight = true;
            try {{
              const r = await fetch('{PREFIX}/app/shop/checks', {{ cache: 'no-store' }});
              if (!r.ok) return;
              const data = await r.json();
              const server = new Set(data.checked || []);
              let changed = false;
              document.querySelectorAll('.shop-row').forEach(row => {{
                const key = row.dataset.key;
                const cb = row.querySelector('.shop-check');
                if (!cb) return;
                const shouldBe = server.has(key);
                if (cb.checked === shouldBe) return;
                changed = true;
                cb.checked = shouldBe;
                row.querySelector('.row-qty').classList.toggle('line-through', shouldBe);
                row.querySelector('.row-qty').classList.toggle('text-stone-400', shouldBe);
                row.querySelector('.row-name').classList.toggle('line-through', shouldBe);
                row.querySelector('.row-name').classList.toggle('text-stone-400', shouldBe);
                const wrap = row.closest('.shop-row-wrap');
                if (wrap) reorderShopRow(wrap, shouldBe);
              }});
              if (changed) updateAllProgress();
            }} catch (e) {{}}
            finally {{ inflight = false; }}
          }}
          // Run on focus + every 3s + on visibility-change.
          setInterval(syncAgg, 3000);
          window.addEventListener('focus', syncAgg);
          document.addEventListener('visibilitychange', () => {{
            if (document.visibilityState === 'visible') syncAgg();
          }});
          // Initial sync ~500ms after load so a fast device gets a
          // first reconciliation even if the page was rendered against
          // stale data.
          setTimeout(syncAgg, 500);
        }})();

        // Pull-to-refresh — drag the top of the page down ≥80px and
        // release to reload. Native iOS Safari already has this in
        // the browser chrome but the PWA install loses it.
        (function() {{
          let startY = 0, pulling = false;
          const indicator = document.createElement('div');
          indicator.style.cssText = 'position:fixed;left:50%;top:0;transform:translateX(-50%) translateY(-100%);background:#FDE68A;color:#78350F;padding:10px 18px;border-radius:0 0 12px 12px;font-size:13px;font-weight:600;z-index:200;transition:transform 0.18s ease;pointer-events:none;';
          indicator.textContent = '↓ Pull to refresh';
          document.body.appendChild(indicator);
          document.addEventListener('touchstart', (e) => {{
            if (window.scrollY > 0) {{ pulling = false; return; }}
            startY = e.touches[0].clientY;
            pulling = true;
          }}, {{ passive: true }});
          document.addEventListener('touchmove', (e) => {{
            if (!pulling) return;
            const dy = e.touches[0].clientY - startY;
            if (dy > 0) {{
              const p = Math.min(1, dy / 80);
              indicator.style.transform = `translateX(-50%) translateY(${{(p * 100 - 100)}}%)`;
              indicator.textContent = p >= 1 ? '↻ Release to refresh' : '↓ Pull to refresh';
            }}
          }}, {{ passive: true }});
          document.addEventListener('touchend', (e) => {{
            if (!pulling) return;
            pulling = false;
            const dy = (e.changedTouches[0]?.clientY ?? startY) - startY;
            if (dy >= 80 && window.scrollY === 0) {{
              indicator.textContent = '↻ Refreshing…';
              location.reload();
            }} else {{
              indicator.style.transform = 'translateX(-50%) translateY(-100%)';
            }}
          }}, {{ passive: true }});
        }})();

        // Extras CRUD against the server.
        (function() {{
          const form = document.getElementById('extras-form');
          form.addEventListener('submit', async (e) => {{
            e.preventDefault();
            const name = document.getElementById('ex-name').value.trim();
            if (!name) return;
            const qty = document.getElementById('ex-qty').value.trim();
            const category = document.getElementById('ex-cat').value;
            const r = await fetch('{PREFIX}/app/extras', {{
              method: 'POST',
              headers: {{ 'Content-Type': 'application/json' }},
              body: JSON.stringify({{ name, qty: qty || null, category }}),
            }});
            if (r.ok) location.reload();
            else window.notify('Add failed', {{tone:'error'}});
          }});

          document.querySelectorAll('.extras-row').forEach(row => {{
            const id = row.dataset.id;
            const cb = row.querySelector('.ex-check');
            // Checking an extra DELETES it — extras are per-trip
            // ad-hocs; ticking off means "got it, don't need it
            // listed anymore". Matches the household's mental model (the
            // strike-through was confusing — she expected it to go).
            cb.addEventListener('change', async () => {{
              if (!cb.checked) return;
              row.style.transition = 'opacity 0.2s, transform 0.2s';
              row.style.opacity = '0';
              row.style.transform = 'translateX(40px)';
              try {{
                await fetch('{PREFIX}/app/extras/' + id, {{ method: 'DELETE' }});
                setTimeout(() => {{ row.remove(); updateAllProgress(); }}, 200);
              }} catch (e) {{
                cb.checked = false;
                row.style.opacity = ''; row.style.transform = '';
                window.notify('Failed to remove: ' + e.message, {{tone:'error'}});
              }}
            }});
            row.querySelector('.ex-del').addEventListener('click', async () => {{
              await fetch('{PREFIX}/app/extras/' + id, {{ method: 'DELETE' }});
              row.remove();
            }});
          }});
        }})();
      </script>
    """
    return HTMLResponse(_shell("Shop", body, active="shop"))


# =============================================================================
# Shop check-off (household-shared, real-time-sync via refresh)
# =============================================================================

@router.get("/app/shop/checks")
def shop_checks_list(db: Session = Depends(get_session)) -> dict:
    """Return the set of currently-checked item_keys for this week,
    so the page-poller can detect when the other cook ticked something
    off and trigger a silent reload."""
    hh_id = _household_id(db)
    keys = db.exec(
        select(ShopCheck.item_key)
        .where(ShopCheck.household_id == hh_id)
        .where(ShopCheck.list_key == "week")
    ).all()
    return {"checked": list(keys)}


def _current_week_sessions(db: Session) -> list[CookingSession]:
    """All position-1 sessions in the current 2-week shopping window
    — same query the shop page uses. Reused by the mark-shopped /
    unmark endpoints below."""
    today = _Date.today()
    end = today + _Td(days=13)
    return db.exec(
        select(CookingSession)
        .join(SlotAssignment, SlotAssignment.session_id == CookingSession.id)
        .where(SlotAssignment.date >= today)
        .where(SlotAssignment.date <= end)
        .where(SlotAssignment.position == 1)
        .distinct()
    ).all()


@router.post("/app/shop/sessions/mark-shopped")
def shop_mark_shopped(db: Session = Depends(get_session)) -> dict:
    """Stamp every current-week cooking session as 'shopped' so its
    ingredients drop out of the aggregated shop list. Also clears any
    per-item check-offs AND deletes all extras (checked or not) —
    extras are per-trip ad-hoc adds, they belong to the shopping run
    that's now complete."""
    now = datetime.now(UTC)
    n = 0
    for s in _current_week_sessions(db):
        if s.shopped_at is None:
            s.shopped_at = now
            n += 1
    hh_id = _household_id(db)
    for row in db.exec(
        select(ShopCheck)
        .where(ShopCheck.household_id == hh_id)
        .where(ShopCheck.list_key == "week")
    ).all():
        db.delete(row)
    extras_deleted = 0
    for ex in db.exec(
        select(ShopExtra)
        .where(ShopExtra.household_id == hh_id)
        .where(ShopExtra.list_key == "week")
    ).all():
        db.delete(ex)
        extras_deleted += 1
    db.commit()
    return {"marked": n, "extras_deleted": extras_deleted}


@router.post("/app/shop/sessions/unmark-shopped")
def shop_unmark_shopped(db: Session = Depends(get_session)) -> dict:
    """Clear shopped_at on every current-week session so its
    ingredients come back into the shop list. Doesn't restore the
    per-item check-offs (those got cleared at mark time)."""
    n = 0
    for s in _current_week_sessions(db):
        if s.shopped_at is not None:
            s.shopped_at = None
            n += 1
    db.commit()
    return {"unmarked": n}


@router.post("/app/shop/recipe/{recipe_id}/shopped")
async def shop_recipe_shopped(recipe_id: int, req: Request, db: Session = Depends(get_session)) -> dict:
    """Check off (or bring back) one whole recipe: stamp/clear
    shopped_at on its cooks in the shopping window."""
    shopped = bool((await req.json()).get("shopped", True))
    mine = [s for s in _shop_window_sessions(db) if s.recipe_id == recipe_id]
    if not mine:
        raise HTTPException(status_code=404, detail="recipe not on the shopping list")
    now = datetime.now(UTC)
    changed = 0
    for s in mine:
        if (s.shopped_at is not None) != shopped:
            s.shopped_at = now if shopped else None
            changed += 1
    db.commit()
    return {"status": "ok", "changed": changed}


@router.post("/app/shop/checks/clear")
def shop_checks_clear(db: Session = Depends(get_session)) -> dict:
    """Wipe all ShopCheck rows for this household + week — the
    'leaving the store' / 'fresh week' reset. Also unchecks any
    extras that are currently checked, since they're part of the
    same logical list."""
    hh_id = _household_id(db)
    cleared = 0
    for row in db.exec(
        select(ShopCheck)
        .where(ShopCheck.household_id == hh_id)
        .where(ShopCheck.list_key == "week")
    ).all():
        db.delete(row)
        cleared += 1
    # Reset checked extras too — they're visually part of the same list.
    extras_reset = 0
    for ex in db.exec(
        select(ShopExtra)
        .where(ShopExtra.household_id == hh_id)
        .where(ShopExtra.list_key == "week")
        .where(ShopExtra.checked == True)  # noqa: E712
    ).all():
        ex.checked = False
        extras_reset += 1
    db.commit()
    return {"cleared": cleared, "extras_reset": extras_reset}


@router.post("/app/shop/check")
async def shop_check_toggle(req: Request, db: Session = Depends(get_session)) -> dict:
    """Toggle a shop-list item's checked state. Idempotent — same key
    can be toggled repeatedly without creating duplicates."""
    body = await req.json()
    key = (body.get("item_key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="item_key required")
    if len(key) > 300:
        raise HTTPException(status_code=400, detail="item_key too long")
    checked = bool(body.get("checked", False))
    hh_id = _household_id(db)
    # Delete any existing rows, then insert if checked=true (idempotent).
    for existing in db.exec(
        select(ShopCheck)
        .where(ShopCheck.household_id == hh_id)
        .where(ShopCheck.list_key == "week")
        .where(ShopCheck.item_key == key)
    ).all():
        db.delete(existing)
    if checked:
        db.add(ShopCheck(household_id=hh_id, list_key="week", item_key=key))
    db.commit()
    return {"status": "ok", "checked": checked}


# =============================================================================
# Extras CRUD
# =============================================================================

@router.post("/app/extras")
async def add_extra(req: Request, db: Session = Depends(get_session)) -> dict:
    body = await req.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    if len(name) > 200:
        raise HTTPException(status_code=400, detail="name too long")
    e = ShopExtra(
        household_id=_household_id(db),
        list_key="week",
        name=name,
        qty=(body.get("qty") or None),
        category=(body.get("category") or "Other"),
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    return {"id": e.id, "status": "ok"}


@router.patch("/app/extras/{extra_id}")
async def patch_extra(extra_id: int, req: Request, db: Session = Depends(get_session)) -> dict:
    body = await req.json()
    e = db.get(ShopExtra, extra_id)
    if e is None:
        raise HTTPException(status_code=404)
    if "checked" in body:
        e.checked = bool(body["checked"])
    if "name" in body:
        e.name = str(body["name"]).strip()
    if "qty" in body:
        e.qty = body["qty"] or None
    if "category" in body:
        e.category = str(body["category"])
    db.add(e)
    db.commit()
    return {"status": "ok"}


@router.delete("/app/extras/{extra_id}", status_code=204)
def delete_extra(extra_id: int, db: Session = Depends(get_session)) -> None:
    e = db.get(ShopExtra, extra_id)
    if e is None:
        raise HTTPException(status_code=404)
    db.delete(e)
    db.commit()


# =============================================================================
# Tools — links to deeper-tool pages. Same Fusion shell as the rest.
# Some links still point at /preview/* until those pages are migrated;
# this page is the explicit "this is where deep tools live" surface.
# =============================================================================

@router.get("/app/tools", response_class=HTMLResponse)
def tools_page() -> HTMLResponse:
    items = [
        ("Review queue",   f"{PREFIX}/app/review",
         "Recipes pending verification — new imports land here."),
        ("Rejected backlog", f"{PREFIX}/app/rejected",
         "Recipes marked rejected during review."),
        ("New recipe",     f"{PREFIX}/app/add",
         "Paste, dictate, or link a recipe — one box does it all."),
        ("Feedback inbox", f"{PREFIX}/app/inbox",
         "Notes from the floating button."),
        ("Parse trace",    f"{PREFIX}/app/tools/parse-trace",
         "Step-by-step parser output for a single ingredient line."),
        ("Operator preview", f"{PREFIX}/preview/",
         "Old desktop-style preview UI — still works for data hygiene."),
    ]
    rows = "".join(
        f"<a href='{href}' class='card p-4 flex items-center gap-3 hover:shadow-lg transition-shadow'>"
        f"  <div class='flex-1 min-w-0'>"
        f"    <div class='serif text-base font-medium'>{_h(label)}</div>"
        f"    <div class='text-xs text-stone-500 mt-0.5'>{_h(desc)}</div>"
        f"  </div>"
        f"  <div class='text-stone-300 shrink-0'>›</div>"
        f"</a>"
        for label, href, desc in items
    )
    body = f"""
      <div class="mb-1 text-[10px] uppercase tracking-[0.25em] text-stone-400">Deep tools</div>
      <h1 class="serif text-4xl font-medium tracking-tight leading-none">More.</h1>
      <p class="text-sm text-stone-500 mt-2">Operator-grade affordances. Everything in the app is for both of us.</p>
      <div class="space-y-2 mt-6">{rows}</div>
    """
    return HTMLResponse(_shell("More", body, active="tools"))


# =============================================================================
# Feedback inbox — migrated from /preview/feedback (GET). The POST
# endpoint at /app/feedback is below; the operator route at
# /preview/feedback still exists and reads from the same Feedback table.
# =============================================================================

@router.get("/app/inbox", response_class=HTMLResponse)
def inbox_page(
    db: Session = Depends(get_session),
    status: str = "new",
) -> HTMLResponse:
    q = select(Feedback).order_by(Feedback.created_at.desc())
    if status != "all":
        q = q.where(Feedback.status == status)
    rows = db.exec(q).all()

    kind_emoji = {"idea": "💡", "bug": "🐞", "like": "❤️", "note": "📝"}
    status_cls = {
        "new":      "bg-violet-100 text-violet-700",
        "reviewed": "bg-amber-100 text-amber-700",
        "done":     "bg-emerald-100 text-emerald-700",
        "wontfix":  "bg-stone-100 text-stone-500",
    }

    def _row(fb: Feedback) -> str:
        when = fb.created_at.strftime("%b %-d · %H:%M") if fb.created_at else ""
        path_link = (
            f"<a href='{_h(fb.path)}' class='text-amber-700 hover:underline'>{_h(fb.path)}</a>"
            if fb.path and fb.path.startswith("/") else _h(fb.path or "")
        )
        who_str = f" · <span class='text-stone-500'>{_h(fb.who)}</span>" if fb.who else ""
        actions = " ".join(
            f"<button type='button' data-id='{fb.id}' data-status='{s}' "
            f"class='fb-action px-2 py-0.5 rounded border border-stone-200 text-xs hover:bg-stone-100'>{lbl}</button>"
            for s, lbl in (
                ("reviewed", "Reviewed"),
                ("done", "Done"),
                ("wontfix", "Won't fix"),
                ("new", "Reopen"),
            )
            if s != fb.status
        )
        # Thumbnail of the user's screen at submit time — deleted once
        # the item moves out of "new". Click to view full size.
        shot_html = (
            f"<a href='{PREFIX}/app/feedback/shot/{fb.id}' target='_blank' rel='noopener' "
            f"class='block mt-3 rounded-lg overflow-hidden border border-stone-200 max-w-xs'>"
            f"  <img src='{PREFIX}/app/feedback/shot/{fb.id}' alt='Screen at submit time' "
            f"       class='w-full h-auto block' loading='lazy'>"
            f"</a>"
            if fb.screenshot_path else ""
        )
        return f"""
<li class='card p-4'>
  <div class='flex items-baseline gap-2 text-xs text-stone-500 mb-2'>
    <span class='text-base'>{kind_emoji.get(fb.kind, "📝")}</span>
    <span class='font-mono'>{when}</span>
    <span>·</span>
    <span>{path_link}</span>
    {who_str}
    <span class='ml-auto px-2 py-0.5 rounded-full text-[10px] font-semibold uppercase tracking-wider {status_cls.get(fb.status, "")}'>{_h(fb.status)}</span>
  </div>
  <div class='whitespace-pre-wrap text-base text-stone-900 leading-snug'>{_h(fb.content)}</div>
  {shot_html}
  <div class='flex gap-1.5 mt-3'>{actions}</div>
</li>"""

    body_rows = "\n".join(_row(fb) for fb in rows) or (
        "<li class='text-stone-400 italic text-sm py-8 text-center'>"
        f"No feedback {_h(status)}.</li>"
    )
    def tab_link(s: str, label: str) -> str:
        cls = ("bg-amber-100 text-amber-900 font-semibold" if status == s
               else "text-stone-500 hover:bg-stone-100")
        return f"<a href='{PREFIX}/app/inbox?status={s}' class='px-3 py-1.5 rounded-lg text-sm {cls}'>{_h(label)}</a>"
    body = f"""
      <div class="mb-1 text-[10px] uppercase tracking-[0.25em] text-stone-400">From the floating button</div>
      <h1 class="serif text-4xl font-medium tracking-tight leading-none">Feedback.</h1>
      <div class='flex gap-1 text-sm mt-4 flex-wrap'>
        {tab_link("new", "New")} {tab_link("reviewed", "Reviewed")} {tab_link("done", "Done")} {tab_link("wontfix", "Won't fix")} {tab_link("all", "All")}
      </div>
      <ul class='space-y-3 mt-4'>{body_rows}</ul>
      <script>
      document.querySelectorAll('.fb-action').forEach(btn => {{
        btn.addEventListener('click', async () => {{
          const id = btn.dataset.id;
          const s = btn.dataset.status;
          btn.disabled = true;
          const r = await fetch('{PREFIX}/preview/feedback/' + id + '/status', {{
            method: 'PATCH',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ status: s }}),
          }});
          if (r.ok) location.reload();
          else {{ btn.disabled = false; window.notify('Update failed', {{tone:'error'}}); }}
        }});
      }});
      </script>
    """
    return HTMLResponse(_shell("Feedback inbox", body, active="tools"))


# =============================================================================
# Review surface — entry into the per-recipe review/edit screen.
# /app/review/{id} 307s to the app-native draft screen
# (/app/recipe/{id}/draft), now the default door for every unreviewed
# recipe. The legacy /preview/review editor stays available as the
# power/debug tool but is no longer linked from the app surfaces.
# =============================================================================

@router.get("/app/review", response_class=HTMLResponse)
def review_queue(db: Session = Depends(get_session)) -> HTMLResponse:
    """List of recipes pending verification — Fusion-shelled queue.
    Selecting one opens the app-native draft/review screen."""
    pending = db.exec(
        select(Recipe)
        .where(Recipe.archived == False)  # noqa: E712
        .where(Recipe.reviewed_at.is_(None))
        .order_by(Recipe.id)
    ).all()
    if not pending:
        body = """
          <div class="mb-1 text-[10px] uppercase tracking-[0.25em] text-stone-400">Review queue</div>
          <h1 class="serif text-4xl font-medium tracking-tight leading-none">All clear.</h1>
          <p class="text-sm text-stone-500 mt-3 serif italic">No recipes awaiting review.</p>
        """
        return HTMLResponse(_shell("Review", body, active="tools"))

    rows = "".join(
        f"<a href='{PREFIX}/app/review/{r.id}' class='card p-3 flex items-center gap-3 hover:shadow-lg transition-shadow'>"
        f"  <div class='w-12 h-12 rounded-xl {_recipe_color(r.id)} flex items-center justify-center text-xl shrink-0'>"
        f"  {_recipe_emoji(r.name_english or r.name_original, r.emoji)}"
        f"  </div>"
        f"  <div class='flex-1 min-w-0'>"
        f"    <div class='serif text-lg font-medium leading-tight truncate'>{_h(r.display_title())}</div>"
        f"    <div class='text-xs text-stone-500 mt-0.5'>"
        f"      Imported {r.created_at.strftime('%b %-d') if r.created_at else '—'}"
        f"    </div>"
        f"  </div>"
        f"  <div class='text-stone-300 shrink-0'>›</div>"
        f"</a>"
        for r in pending
    )
    body = f"""
      <div class="mb-1 text-[10px] uppercase tracking-[0.25em] text-stone-400">Review queue</div>
      <h1 class="serif text-4xl font-medium tracking-tight leading-none">Awaiting <em class="text-stone-500">review</em>.</h1>
      <p class="text-sm text-stone-500 mt-3">{len(pending)} recipe{'s' if len(pending) != 1 else ''} pending. Tap to verify parsed data.</p>
      <div class="space-y-2 mt-5">{rows}</div>
    """
    return HTMLResponse(_shell("Review", body, active="tools"))


@router.get("/app/review/{recipe_id}", include_in_schema=False)
def review_recipe_redirect(recipe_id: int) -> RedirectResponse:
    """Open the app-native draft/review screen. This is now the review
    surface for every unreviewed recipe — the legacy /preview/review
    editor stays available as the power/debug tool but is no longer the
    default door from the queue."""
    return RedirectResponse(url=f"{PREFIX}/app/recipe/{recipe_id}/draft", status_code=307)


# =============================================================================
# Rejected backlog
# =============================================================================

@router.get("/app/rejected", response_class=HTMLResponse)
def rejected_page(db: Session = Depends(get_session)) -> HTMLResponse:
    rows = db.exec(
        select(Recipe)
        .where(Recipe.archived == True)  # noqa: E712
        .order_by(Recipe.updated_at.desc())
    ).all()
    if not rows:
        body = """
          <div class="mb-1 text-[10px] uppercase tracking-[0.25em] text-stone-400">Rejected backlog</div>
          <h1 class="serif text-4xl font-medium tracking-tight leading-none">All clear.</h1>
          <p class="text-sm text-stone-500 mt-3 serif italic">No archived recipes.</p>
        """
        return HTMLResponse(_shell("Rejected", body, active="tools"))

    items = []
    for r in rows:
        name = r.display_title()
        reason = r.archived_reason or "(no reason)"
        updated = r.updated_at.strftime("%b %-d") if r.updated_at else "—"
        items.append(
            f"<li class='card p-4 flex items-center justify-between gap-3'>"
            f"  <div class='min-w-0 flex-1'>"
            f"    <a href='{PREFIX}/app/review/{r.id}' class='serif text-lg font-medium hover:text-amber-800'>{_h(name)}</a>"
            f"    <div class='text-xs text-stone-500 mt-0.5'>{_h(reason)} · {updated}</div>"
            f"  </div>"
            f"  <button type='button' class='restore-btn text-sm text-amber-700 hover:text-amber-900 hover:underline whitespace-nowrap shrink-0' data-recipe-id='{r.id}'>↻ Restore</button>"
            f"</li>"
        )
    body = f"""
      <div class="mb-1 text-[10px] uppercase tracking-[0.25em] text-stone-400">Rejected backlog</div>
      <h1 class="serif text-4xl font-medium tracking-tight leading-none">Archived.</h1>
      <p class="text-sm text-stone-500 mt-3">
        {len(rows)} archived recipe{'s' if len(rows) != 1 else ''}. Restore to put back on the review queue.
      </p>
      <ul class="space-y-2 mt-5">{''.join(items)}</ul>
      <script>
        document.querySelectorAll('.restore-btn').forEach(btn => {{
          btn.addEventListener('click', async () => {{
            btn.disabled = true;
            btn.textContent = '…';
            try {{
              const r = await fetch('{PREFIX}/recipes/' + btn.dataset.recipeId, {{
                method: 'PATCH',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ archived: false, archived_reason: null }}),
              }});
              if (!r.ok) throw new Error('HTTP ' + r.status);
              btn.closest('li').remove();
            }} catch (e) {{
              btn.disabled = false;
              btn.textContent = '↻ Restore';
              window.notify('Restore failed: ' + e.message, {{tone:'error'}});
            }}
          }});
        }});
      </script>
    """
    return HTMLResponse(_shell("Rejected", body, active="tools"))


# =============================================================================
# Add recipe (capture page — paste / dictate / link in one box; ?url= share entry)
# =============================================================================

@router.get("/app/new", include_in_schema=False)
def new_recipe_redirect() -> RedirectResponse:
    return RedirectResponse(url=f"{PREFIX}/app/add", status_code=307)


@router.get("/app/add", response_class=HTMLResponse)
def add_recipe_page() -> HTMLResponse:
    body = f"""
      <style>
        .cap-spin {{ display:inline-block; width:14px; height:14px; border:2px solid #e7e5e4;
          border-top-color:#1c1917; border-radius:50%; animation:cap-rot 0.7s linear infinite; }}
        @keyframes cap-rot {{ to {{ transform:rotate(360deg); }} }}
      </style>
      <div class="mb-1 text-[10px] uppercase tracking-[0.25em] text-stone-400">Add a recipe</div>
      <h1 class="serif text-4xl font-medium tracking-tight leading-none">New <em class="text-stone-500">recipe</em>.</h1>
      <p class="text-sm text-stone-500 mt-3">Paste a recipe, paste a link, or tap the box and use the
        <span class="whitespace-nowrap">keyboard mic 🎙</span> to just say it — title, servings, ingredients, steps.</p>

      <div class="card p-4 mt-6">
        <textarea id="cap-input" rows="9" placeholder="Paste or dictate a recipe here — or drop in a link…"
          class="block w-full border border-stone-200 rounded-xl px-3 py-3 bg-white text-[16px] leading-relaxed"
          style="resize: vertical; min-height: 11rem;"></textarea>
        <div class="flex items-center gap-3 mt-3">
          <button id="cap-go" type="button"
            class="text-white font-medium px-5 py-2.5 rounded-lg text-sm"
            style="background:#1c1917;">Make recipe</button>
          <a href="{PREFIX}/app/" class="text-sm text-stone-500 hover:text-stone-900">Cancel</a>
          <span id="cap-status" class="text-sm text-stone-500 hidden inline-flex items-center gap-2">
            <span class="cap-spin" aria-hidden="true"></span><span id="cap-phrase"></span>
          </span>
        </div>
        <div id="cap-error" class="hidden mt-3 text-sm text-rose-700 bg-rose-50 border border-rose-100 rounded-lg px-3 py-2"></div>
      </div>

      <details class="mt-6 text-sm text-stone-500">
        <summary class="cursor-pointer font-medium text-stone-600">Add Mise to your iPhone share sheet</summary>
        <ol class="list-decimal ml-5 mt-2 space-y-1">
          <li>Open the <strong>Shortcuts</strong> app → <strong>+</strong> to create a new shortcut.</li>
          <li>Add the action <strong>Open URLs</strong>.</li>
          <li>Set the URL to <code class="bg-stone-100 px-1 rounded"><span class="sc-origin">https://your-mise-host</span>{PREFIX}/app/add?url=</code><script>document.querySelectorAll(".sc-origin").forEach(function(e){{e.textContent=location.origin;}});</script> and tap the blue token area to insert the <strong>Shortcut Input</strong> variable right after <code>url=</code>.</li>
          <li>Tap the info (ⓘ) panel → enable <strong>Show in Share Sheet</strong> → set accepted types to <strong>URLs</strong>.</li>
          <li>Name it <strong>Send to Mise</strong>. Now share any recipe page → Send to Mise.</li>
        </ol>
      </details>

      <script>
        (function() {{
          const input = document.getElementById('cap-input');
          const go = document.getElementById('cap-go');
          const status = document.getElementById('cap-status');
          const phrase = document.getElementById('cap-phrase');
          const errBox = document.getElementById('cap-error');
          let busy = false;

          const PHRASES = ['Reading the recipe…', 'Splitting ingredients…', 'Tidying the steps…', 'Almost there…'];
          let phraseTimer = null;

          function setBusy(b) {{
            busy = b;
            go.disabled = b;
            go.style.opacity = b ? '0.5' : '1';
            status.classList.toggle('hidden', !b);
            if (b) {{
              let i = 0;
              phrase.textContent = PHRASES[0];
              phraseTimer = setInterval(() => {{
                i = (i + 1) % PHRASES.length;
                phrase.textContent = PHRASES[i];
              }}, 3500);
            }} else if (phraseTimer) {{
              clearInterval(phraseTimer);
              phraseTimer = null;
            }}
          }}

          async function makeRecipe() {{
            if (busy) return;
            const value = input.value.trim();
            errBox.classList.add('hidden');
            if (!value) {{
              errBox.textContent = 'Paste a recipe or a link first.';
              errBox.classList.remove('hidden');
              return;
            }}
            setBusy(true);
            try {{
              const r = await fetch('{PREFIX}/recipes/draft', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ text: value }}),
              }});
              const data = await r.json().catch(() => ({{}}));
              if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
              window.location = '{PREFIX}/app/recipe/' + data.id + '/draft';
            }} catch (e) {{
              setBusy(false);
              errBox.textContent = e.message;
              errBox.classList.remove('hidden');
            }}
          }}

          go.addEventListener('click', makeRecipe);

          // Share-sheet / deep-link entry: ?url=… auto-starts the import.
          const shared = new URLSearchParams(location.search).get('url');
          if (shared) {{
            input.value = shared;
            makeRecipe();
          }}

          // share_target text that isn't a URL: prefill, let the user confirm.
          const sharedText = new URLSearchParams(location.search).get('text');
          if (sharedText && !shared) input.value = sharedText;
        }})();
      </script>
    """
    return HTMLResponse(_shell("Add recipe", body, active="tools"))


# =============================================================================
# Parse trace — operator debug surface, bridged to legacy for now.
# Sits under /app/tools/ to flag "deep tool" affordance.
# =============================================================================

@router.get("/app/tools/parse-trace", include_in_schema=False)
def parse_trace_redirect(blob: str = "") -> RedirectResponse:
    """The parse-trace page is a heavy debug surface — 380+ lines of
    diagnostic rendering. Keeping it at /preview/parse-trace for now;
    deep-tool, low-frequency. Migration to /app/tools/parse-trace UI is
    a follow-up. Query string passes through verbatim."""
    qs = f"?blob={blob}" if blob else ""
    return RedirectResponse(url=f"{PREFIX}/preview/parse-trace{qs}", status_code=307)


# =============================================================================
# Feedback (reuse the same model the operator-side route writes to)
# =============================================================================

_FEEDBACK_SHOTS_DIR = _Path("/data/feedback-shots")


def _decode_screenshot(data_url: str, fb_id: int) -> str | None:
    """Decode a data:image/jpeg;base64,... URL, write to disk, return
    the relative path. Caps size at ~2MB so a runaway client can't
    fill the disk."""
    import base64
    if not data_url or not data_url.startswith("data:image/"):
        return None
    try:
        _hdr, b64 = data_url.split(",", 1)
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return None
    if len(raw) > 2 * 1024 * 1024:
        return None
    ext = "jpg" if "jpeg" in data_url[:30] else "png"
    _FEEDBACK_SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{fb_id}.{ext}"
    (_FEEDBACK_SHOTS_DIR / name).write_bytes(raw)
    return name


@router.post("/app/feedback")
async def app_feedback(req: Request, db: Session = Depends(get_session)) -> dict:
    body = await req.json()
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content required")
    if len(content) > 4000:
        raise HTTPException(status_code=400, detail="content too long")
    kind = body.get("kind", "note")
    if kind not in {"idea", "bug", "like", "note"}:
        kind = "note"
    who = body.get("who")
    if who is not None:
        who = str(who).strip()[:60] or None
    fb = Feedback(
        path=(body.get("path") or "")[:500],
        kind=kind,
        content=content,
        who=who,
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)
    # Decode + persist the screenshot AFTER we have an id (used in the
    # filename). Failure to decode is non-fatal — feedback is more
    # important than the screenshot.
    shot = body.get("screenshot")
    if shot and isinstance(shot, str) and fb.id is not None:
        path = _decode_screenshot(shot, fb.id)
        if path:
            fb.screenshot_path = path
            db.add(fb)
            db.commit()
    return {"id": fb.id, "status": "ok"}


@router.get("/app/feedback/shot/{fb_id}", include_in_schema=False)
def feedback_shot(fb_id: int, db: Session = Depends(get_session)):
    from fastapi.responses import FileResponse as _FR
    fb = db.get(Feedback, fb_id)
    if not fb or not fb.screenshot_path:
        raise HTTPException(status_code=404)
    target = (_FEEDBACK_SHOTS_DIR / fb.screenshot_path).resolve()
    if not str(target).startswith(str(_FEEDBACK_SHOTS_DIR.resolve())) or not target.is_file():
        raise HTTPException(status_code=404)
    return _FR(target, media_type="image/jpeg")
