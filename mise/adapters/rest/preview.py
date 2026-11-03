"""Read-only preview UI for Phase 0 — let an operator (or the household)
browse the imported library and decide whether the data is usable
before we layer Phase 1's planner + form on top.

No mutations here. Adds happen via POST /recipes (curl-only this
phase); the proper mobile form is Phase 1.

Tailwind via CDN — single-page Phase-0 evaluation surface, not the
real frontend, not worth a build pipeline yet.
"""

from __future__ import annotations

import html
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlmodel import Session, select

# Path prefix when behind Traefik strip-prefix. Empty for dev (uvicorn
# direct); "/mise" in prod compose. All HTML hrefs prepend this.
PREFIX = os.environ.get("MISE_URL_PREFIX", "")

from mise.db import get_session
from mise.models import (
    CookingSession,
    Household,
    Ingredient,
    IngredientSection,
    Recipe,
    SlotAssignment,
    Step,
    StepSection,
)
from mise.services.recipe_exporter import _serialise_recipe

router = APIRouter(tags=["preview"])

_BASE_CSS = f"""
<link rel="stylesheet" href="{PREFIX}/static/tw.css?v=6">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&display=swap" rel="stylesheet">
<style>
  /* Fusion base — warm cream, Inter body, tighter tracking. Matches the
     fusion mockup direction; the household's primary surface from now on. */
  body {{
    font-family: 'Inter', system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
    letter-spacing: -0.012em;
    background: #FAF7F2;
    color: #1c1917;
    /* Bottom-padding so content doesn't hide under the floating tab bar.
       Larger on mobile to account for the bar being lifted above the
       iOS gesture/Siri zone at the bottom edge. */
    padding-bottom: calc(8rem + env(safe-area-inset-bottom));
  }}
  @media (min-width: 768px) {{ body {{ padding-bottom: 0; }} }}
  .serif {{ font-family: 'Fraunces', Georgia, serif; }}
  .card {{
    background: white;
    border-radius: 1.25rem;
    box-shadow: 0 1px 2px rgba(120,80,40,0.04), 0 8px 24px -12px rgba(120,80,40,0.10);
    border: 1px solid #f5f0e8;
  }}
  /* Matte solid card tints — same palette as the fusion mockups. */
  .matte-amber {{ background: #FDE68A; color: #78350F; }}
  .matte-emerald {{ background: #BBF7D0; color: #064E3B; }}
  .matte-rose {{ background: #FECDD3; color: #881337; }}
  .matte-violet {{ background: #DDD6FE; color: #4C1D95; }}
  .matte-sky {{ background: #BAE6FD; color: #0C4A6E; }}
  /* Global iOS-zoom guard. */
  input, select, textarea {{ font-size: 16px; }}
  /* Fade-up entrance — Linear/Vercel pattern. .stagger > * children
     cascade in with 60ms delays. CSS-only; honours reduced-motion. */
  @keyframes fadeUp {{
    from {{ opacity: 0; transform: translateY(12px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
  }}
  .stagger > * {{
    opacity: 0;
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
  /* Leads pill — visible nod to the planner's anchor session. */
  .leads-pill {{
    display: inline-flex; align-items: center; gap: 0.2rem;
    background: #FEF3C7; color: #92400E; border-radius: 9999px;
    padding: 0.1rem 0.5rem; font-size: 0.65rem; font-weight: 600;
    letter-spacing: 0.04em; text-transform: uppercase;
  }}
  /* Operator-only affordances — inline-edit ⋮ menus, source sub-lines,
     parse-failure markers. Hidden by default on the household surface;
     visible when body has .op-mode (toggled by the "Edit" button). */
  .op-only {{ display: none; }}
  .op-mode .op-only {{ display: revert; }}
  /* Legacy carry-overs: keep .editable inline-edit affordance intact. */
  .raw-link {{ color: #c2410c; }}
  pre {{ white-space: pre-wrap; word-wrap: break-word; }}
  .editable {{ cursor: pointer; border-bottom: 1px dotted #d6d3d1; }}
  .editable:hover {{ background: #fef3c7; }}
  .saving {{ opacity: 0.5; }}
  .save-error {{ background: #fee2e2 !important; }}
  /* Top + bottom nav pill state. */
  .nav-pill {{ padding: 0.4rem 0.9rem; border-radius: 9999px; font-size: 0.875rem; color: #78716c; transition: background 0.15s, color 0.15s; }}
  .nav-pill:hover {{ background: #f5f5f4; color: #1c1917; }}
  .nav-pill[data-active="1"] {{ font-weight: 600; }}
  .nav-pill[data-active="1"][data-tab="library"]  {{ background: #FEF3C7; color: #78350F; }}
  .nav-pill[data-active="1"][data-tab="plan"]     {{ background: #EDE9FE; color: #4C1D95; }}
  .nav-pill[data-active="1"][data-tab="review"]   {{ background: #D1FAE5; color: #064E3B; }}
  .nav-pill[data-active="1"][data-tab="shop"]     {{ background: #FFE4E6; color: #881337; }}
  .nav-pill[data-active="1"][data-tab="feedback"] {{ background: #fff7ed; color: #7c2d12; }}
  .nav-pill[data-active="1"][data-tab="other"]    {{ background: #f5f5f4; color: #1c1917; }}
  /* Bottom tab bar — floating pill bar, lifted ABOVE the iOS home-
     indicator / Siri-swipe zone so tapping the leftmost tab doesn't
     trigger the system gesture. The bar floats with margin on all
     sides + rounded corners — visually distinct from the screen edge. */
  .tab-bar {{
    position: fixed; z-index: 30;
    left: 0.75rem; right: 0.75rem;
    /* Lift well clear of the iOS home indicator / gesture zone. */
    bottom: calc(2rem + env(safe-area-inset-bottom));
    background: rgba(250,247,242,0.96); backdrop-filter: blur(16px);
    border: 1px solid #e7e5e4;
    border-radius: 1.5rem;
    box-shadow: 0 12px 32px -8px rgba(120,80,40,0.18);
    padding: 0.55rem 0.5rem;
    display: flex; justify-content: space-around; gap: 0.25rem;
  }}
  @media (min-width: 768px) {{ .tab-bar {{ display: none; }} }}
  .tab-bar .nav-pill {{ flex: 1; text-align: center; padding: 0.55rem 0.3rem; font-size: 0.78rem; border-radius: 1rem; }}
  /* Float the feedback FAB clear of the tab bar on mobile. */
  .fb-root-pos {{ bottom: calc(7rem + env(safe-area-inset-bottom)); }}
  @media (min-width: 768px) {{ .fb-root-pos {{ bottom: 1rem; }} }}
</style>
<script>
  // Inline edit: tap an .editable span -> swap to input -> save on
  // blur/enter, revert on Escape. Input type from data-type:
  //   'number'   → numeric input
  //   'select'   → <select> with data-options="A,B,C"
  //   'datalist' → text input + datalist suggestions via data-list="<id>"
  //                 (lets the user pick an existing canonical OR type a new one)
  //   (default)  → plain text
  document.addEventListener('click', async (e) => {{
    const el = e.target.closest('.editable');
    if (!el || el.querySelector('input, select')) return;
    const original = el.dataset.value ?? el.textContent.trim();
    const type = el.dataset.type || 'text';
    const isNumber = type === 'number';

    let input;
    if (type === 'select') {{
      input = document.createElement('select');
      input.className = 'border px-1 py-0 w-full text-xs';
      const opts = (el.dataset.options || '').split(',').filter(Boolean);
      for (const o of opts) {{
        const opt = document.createElement('option');
        opt.value = o; opt.textContent = o;
        if (o === original) opt.selected = true;
        input.appendChild(opt);
      }}
    }} else {{
      input = document.createElement('input');
      input.value = original;
      input.type = isNumber ? 'number' : 'text';
      if (isNumber) input.step = 'any';
      input.className = 'border px-1 py-0 w-full';
    }}
    el.textContent = '';
    el.style.position = 'relative';
    el.appendChild(input);
    input.focus();
    if (input.select) input.select();

    // Custom autocomplete popover for type='datalist'. iOS Safari's
    // native <datalist> rendering is unreliable so we build our own
    // filterable suggestion list. Options come from the page-level
    // <datalist> referenced by data-list — the same source the native
    // attribute would use, just rendered as a tappable popover.
    if (type === 'datalist' && el.dataset.list) {{
      const optsHost = document.getElementById(el.dataset.list);
      const options = optsHost
        ? [...optsHost.querySelectorAll('option')].map(o => o.value)
        : [];
      const suggest = document.createElement('div');
      suggest.className =
        'absolute z-50 bg-white border border-stone-300 rounded shadow-lg ' +
        'max-h-56 overflow-y-auto text-sm';
      suggest.style.position = 'absolute';
      suggest.style.top = '100%';
      suggest.style.left = '0';
      suggest.style.minWidth = '220px';
      suggest.style.marginTop = '2px';
      suggest.style.webkitOverflowScrolling = 'touch';
      el.appendChild(suggest);

      let lockBlur = false;
      const render = (q) => {{
        const ql = (q || '').toLowerCase();
        const matches = options
          .filter(o => o.toLowerCase().includes(ql))
          .slice(0, 40);
        if (matches.length === 0) {{
          suggest.innerHTML =
            `<div class='px-2 py-1 text-stone-500 italic'>` +
            `type to add new: <span class='font-mono'>${{esc(q)}}</span></div>`;
          return;
        }}
        suggest.innerHTML = matches.map(o =>
          `<div class='sg-item px-2 py-1 hover:bg-amber-50 cursor-pointer ` +
          `border-b border-stone-100' data-val='${{esc(o).replace(/'/g, "&#39;")}}'>` +
          `${{esc(o)}}</div>`
        ).join('');
        suggest.querySelectorAll('.sg-item').forEach(item => {{
          // mousedown + touchstart fire BEFORE blur — preventDefault
          // keeps the input focused so we can set its value and call
          // commit() ourselves.
          const pick = (ev) => {{
            ev.preventDefault();
            lockBlur = true;
            input.value = item.dataset.val;
            commit();
          }};
          item.addEventListener('mousedown', pick);
          item.addEventListener('touchstart', pick, {{ passive: false }});
        }});
      }};
      // local helper inside this scope so `esc` resolves before commit
      // is defined (declared via const further down).
      function esc(s) {{
        return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
                        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
      }}
      render(input.value);
      input.addEventListener('input', () => render(input.value));
      // If blur fires AFTER pick, the suggestion already committed —
      // skip the normal blur commit so we don't double-fire.
      input.addEventListener('blur', (ev) => {{
        if (lockBlur) ev.stopImmediatePropagation();
      }}, true);
    }}
    const cleanup = () => {{ el.classList.remove('saving', 'save-error'); }};
    const revert = () => {{ el.textContent = original; cleanup(); }};
    const commit = async () => {{
      let newValue = (input.value || '').trim();
      // canonical_name is a slug: lowercase + spaces → underscores.
      // Matches _slug_canonical in recipes.py so typing 'all purpose
      // flour' lands as 'all_purpose_flour' without extra effort.
      if (el.dataset.field === 'canonical_name') {{
        newValue = newValue.toLowerCase().replace(/\\s+/g, '_');
      }}
      if (newValue === original) {{ revert(); return; }}
      el.classList.add('saving');
      try {{
        const endpoint = '{PREFIX}' + el.dataset.endpoint;
        const field = el.dataset.field;
        // Synthetic field: tags-csv splits the typed text on commas
        // and sends as the real `tags` list (server title-cases +
        // dedupes). Keeps the UI a single text input.
        let body;
        if (field === 'tags-csv') {{
          const tags = newValue
            ? newValue.split(',').map(s => s.trim()).filter(Boolean)
            : [];
          body = {{ tags: tags }};
        }} else if (
          newValue === '' &&
          (field === 'prep_note' || field === 'size_descriptor' ||
           field === 'alternatives_note' || field === 'source_url' ||
           field === 'notes_markdown' || field === 'emoji' ||
           field === 'language' || field === 'name_original')
        ) {{
          // Nullable string fields: empty input → null (clears the
          // value), not an empty string. Keeps DB clean.
          body = {{ [field]: null }};
        }} else {{
          body = {{ [field]: isNumber ? parseFloat(newValue) : newValue }};
        }}
        const r = await fetch(endpoint, {{
          method: 'PATCH',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(body),
        }});
        if (!r.ok) throw new Error('HTTP ' + r.status);
        el.textContent = newValue;
        el.dataset.value = newValue;
        cleanup();
        // Notify ancestors (review page hooks this to re-color the row).
        el.dispatchEvent(new CustomEvent('valueChanged', {{ bubbles: true }}));
      }} catch (err) {{
        console.error(err);
        el.classList.remove('saving');
        el.classList.add('save-error');
        el.textContent = original;
      }}
    }};
    input.addEventListener('keydown', (ev) => {{
      if (ev.key === 'Escape') {{ revert(); ev.preventDefault(); }}
      if (ev.key === 'Enter') {{ input.blur(); ev.preventDefault(); }}
    }});
    input.addEventListener('blur', commit);
    if (type === 'select') input.addEventListener('change', commit);
  }});
</script>
"""


def _h(s: str | None) -> str:
    """HTML-escape; tolerate None."""
    return html.escape(s or "")


# Glyphs for the common cooking fractions. Anything not within `tol` of
# a key here falls through to the regular `{:g}` format.
_FRAC_GLYPHS = {
    0.125: "⅛", 0.25: "¼", 1/3: "⅓", 0.375: "⅜",
    0.5: "½", 0.625: "⅝", 2/3: "⅔", 0.75: "¾", 0.875: "⅞",
}


def _fmt_qty(q: float, tol: float = 0.02) -> str:
    """Render a recipe quantity. Integers stay integers ('2');
    integer + clean fraction renders with a glyph ('1½' for 1.5,
    '¼' for 0.25); everything else falls back to {:g}."""
    if q == 0:
        return "0"
    integer = int(q)
    frac = q - integer
    for f, g in _FRAC_GLYPHS.items():
        if abs(frac - f) <= tol:
            return (f"{integer}" if integer else "") + g
    # Whole-number-with-tiny-decimal — round to int (1.97 → 2)
    if abs(frac) <= tol or abs(frac - 1) <= tol:
        return f"{round(q)}"
    return f"{q:g}"


_THUMB_COLORS = ["matte-amber", "matte-emerald", "matte-rose", "matte-violet", "matte-orange", "matte-sky"]

# Keyword → emoji map for recipes without an explicit emoji set. First
# matching keyword wins, so put more-specific terms first (e.g. "rice
# bowl" before "rice"). Generic fallback at the bottom of the function.
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


def _recipe_emoji(r: Recipe) -> str:
    """Lookup emoji for a recipe — explicit field first, then keyword
    heuristic on the name. Default is the plate icon."""
    if r.emoji:
        return r.emoji
    name = ((r.name_english or "") + " " + (r.name_original or "")).lower()
    for kw, em in _KW_EMOJI:
        if kw in name:
            return em
    return "🍽️"


def _recipe_thumb_color(r: Recipe) -> str:
    """Deterministic matte tint by id — same recipe always gets the
    same colour, but the library reads as varied without curation."""
    return _THUMB_COLORS[(r.id or 0) % len(_THUMB_COLORS)]


def _shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_h(title)} · Mise</title>
  {_BASE_CSS}
</head>
<body>
  <header class="sticky top-0 z-40 bg-[#FAF7F2]/90 backdrop-blur border-b border-stone-200">
    <div class="max-w-5xl mx-auto px-4 sm:px-6 py-3 flex items-center justify-between gap-3">
      <a href="{PREFIX}/preview/" class="flex items-center gap-2 shrink-0">
        <span class="text-lg">🥄</span>
        <span class="serif text-xl font-semibold tracking-tight">Mise</span>
      </a>
      <nav class="hidden md:flex items-center gap-1 text-sm">
        <a href="{PREFIX}/app/"              data-tab="other"    class="nav-pill"><strong>App →</strong></a>
        <a href="{PREFIX}/preview/"          data-tab="library"  class="nav-pill">Library</a>
        <a href="{PREFIX}/preview/plan"      data-tab="plan"     class="nav-pill">Plan</a>
        <a href="{PREFIX}/preview/review"    data-tab="review"   class="nav-pill">Review</a>
        <a href="{PREFIX}/preview/feedback"  data-tab="feedback" class="nav-pill">Feedback</a>
        <a href="{PREFIX}/mockups/"          data-tab="other"    class="nav-pill">V2 mockups</a>
        <a href="{PREFIX}/docs"              data-tab="other"    class="nav-pill">API</a>
      </nav>
      <div class="md:hidden serif italic text-sm text-stone-500 truncate">{_h(title)}</div>
    </div>
  </header>
  <main class="max-w-5xl mx-auto px-4 sm:px-6 py-6 stagger">
    {body}
  </main>
  <footer class="max-w-5xl mx-auto px-6 py-6 text-xs text-stone-400 serif italic">
    Mise — household kitchen helper.
  </footer>
  <nav class="tab-bar" aria-label="Primary">
    <a href="{PREFIX}/preview/"         data-tab="library"  class="nav-pill">Library</a>
    <a href="{PREFIX}/preview/plan"     data-tab="plan"     class="nav-pill">Plan</a>
    <a href="{PREFIX}/preview/review"   data-tab="review"   class="nav-pill">Review</a>
    <a href="{PREFIX}/preview/feedback" data-tab="feedback" class="nav-pill">Inbox</a>
  </nav>
  <script>
    // Active tab — match by URL prefix on the data-tab routes.
    (function() {{
      const path = location.pathname.replace(/\\/$/, '');
      const norm = p => p.replace(/\\/$/, '');
      const map = [
        ['plan',     ['{PREFIX}/preview/plan']],
        ['review',   ['{PREFIX}/preview/review', '{PREFIX}/preview/rejected']],
        ['feedback', ['{PREFIX}/preview/feedback']],
        ['shop',     ['{PREFIX}/shopping']],
        ['library',  ['{PREFIX}/preview']],
      ];
      let active = '';
      for (const [tab, prefixes] of map) {{
        if (prefixes.some(p => path === norm(p) || path.startsWith(p + '/'))) {{
          active = tab; break;
        }}
      }}
      document.querySelectorAll('.nav-pill').forEach(el => {{
        if (el.dataset.tab === active) el.dataset.active = '1';
      }});
    }})();
  </script>
  {_FEEDBACK_WIDGET}
</body>
</html>"""


# Floating feedback button — present on every preview page so the household can
# leave real-time notes without leaving the surface she's commenting on.
# Posts to /preview/feedback (same router) which writes the Feedback row.
_FEEDBACK_WIDGET = f"""
<div id="fb-root" class="fb-root-pos" style="position:fixed;left:1rem;z-index:90;font-family:system-ui,sans-serif;">
  <button id="fb-fab" type="button" aria-label="Leave feedback" title="Leave feedback"
          style="background:#7c3aed;color:white;border:none;border-radius:9999px;width:3rem;height:3rem;font-size:1.25rem;box-shadow:0 8px 24px -8px rgba(124,58,237,0.6);cursor:pointer;display:flex;align-items:center;justify-content:center;">
    💬
  </button>
  <div id="fb-panel" hidden
       style="position:absolute;left:0;bottom:3.2rem;width:min(20rem,calc(100vw - 2rem));background:white;border:1px solid #e5e7eb;border-radius:0.75rem;box-shadow:0 24px 48px -12px rgba(0,0,0,0.18);padding:0.9rem;">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:0.5rem;">
      <strong style="font-size:0.85rem;">What's on your mind?</strong>
      <button id="fb-close" type="button" aria-label="Close"
              style="background:none;border:none;color:#9ca3af;cursor:pointer;font-size:1.1rem;line-height:1;">×</button>
    </div>
    <div style="display:flex;gap:0.3rem;margin-bottom:0.5rem;font-size:0.7rem;" id="fb-kind-row">
      <label style="flex:1;cursor:pointer;"><input type="radio" name="fb-kind" value="idea" checked style="display:none;"><span class="fb-kind-pill" style="display:block;text-align:center;border:1px solid #e5e7eb;border-radius:0.4rem;padding:0.3rem;background:#f5f3ff;color:#6d28d9;font-weight:600;">💡 Idea</span></label>
      <label style="flex:1;cursor:pointer;"><input type="radio" name="fb-kind" value="bug" style="display:none;"><span class="fb-kind-pill" style="display:block;text-align:center;border:1px solid #e5e7eb;border-radius:0.4rem;padding:0.3rem;color:#6b7280;">🐞 Bug</span></label>
      <label style="flex:1;cursor:pointer;"><input type="radio" name="fb-kind" value="like" style="display:none;"><span class="fb-kind-pill" style="display:block;text-align:center;border:1px solid #e5e7eb;border-radius:0.4rem;padding:0.3rem;color:#6b7280;">❤️ Like</span></label>
      <label style="flex:1;cursor:pointer;"><input type="radio" name="fb-kind" value="note" style="display:none;"><span class="fb-kind-pill" style="display:block;text-align:center;border:1px solid #e5e7eb;border-radius:0.4rem;padding:0.3rem;color:#6b7280;">📝 Note</span></label>
    </div>
    <textarea id="fb-text" rows="4" placeholder="Type your feedback…"
              style="width:100%;box-sizing:border-box;border:1px solid #e5e7eb;border-radius:0.4rem;padding:0.5rem 0.6rem;font-family:inherit;font-size:16px;outline:none;resize:vertical;"></textarea>
    <input id="fb-who" type="text" placeholder="Who is this? (optional)"
           style="width:100%;box-sizing:border-box;border:1px solid #e5e7eb;border-radius:0.4rem;padding:0.5rem 0.6rem;font-family:inherit;font-size:16px;outline:none;margin-top:0.4rem;">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-top:0.55rem;gap:0.5rem;">
      <span id="fb-status" style="font-size:0.7rem;color:#6b7280;"></span>
      <button id="fb-send" type="button"
              style="background:#7c3aed;color:white;border:none;border-radius:0.4rem;padding:0.4rem 0.85rem;font-size:0.8rem;font-weight:600;cursor:pointer;">Send</button>
    </div>
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
  const kindLabels = document.querySelectorAll('#fb-kind-row label');

  // Remember last "who" so testers don't have to retype their name every time.
  try {{ who.value = localStorage.getItem('fb-who') || ''; }} catch(e) {{}}

  fab.addEventListener('click', () => {{ panel.hidden = !panel.hidden; if (!panel.hidden) text.focus(); }});
  close.addEventListener('click', () => {{ panel.hidden = true; }});

  // Custom radio styling — highlight the selected kind pill.
  function refreshKind() {{
    kindLabels.forEach(l => {{
      const input = l.querySelector('input');
      const pill = l.querySelector('.fb-kind-pill');
      if (input.checked) {{
        pill.style.background = '#f5f3ff';
        pill.style.color = '#6d28d9';
        pill.style.fontWeight = '600';
        pill.style.borderColor = '#c4b5fd';
      }} else {{
        pill.style.background = 'white';
        pill.style.color = '#6b7280';
        pill.style.fontWeight = 'normal';
        pill.style.borderColor = '#e5e7eb';
      }}
    }});
  }}
  kindLabels.forEach(l => l.addEventListener('change', refreshKind));

  send.addEventListener('click', async () => {{
    const content = text.value.trim();
    if (!content) {{ status.textContent = 'Type something first.'; return; }}
    const kind = document.querySelector('input[name="fb-kind"]:checked').value;
    const whoVal = who.value.trim();
    try {{ localStorage.setItem('fb-who', whoVal); }} catch(e) {{}}
    status.textContent = 'Sending…';
    send.disabled = true;
    try {{
      const r = await fetch('{PREFIX}/preview/feedback', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{
          path: location.pathname,
          kind, content,
          who: whoVal || null,
        }}),
      }});
      if (!r.ok) throw new Error('HTTP ' + r.status);
      status.textContent = 'Sent. Thank you!';
      text.value = '';
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


@router.post("/preview/feedback")
async def submit_feedback(req: Request, db: Session = Depends(get_session)) -> dict[str, int | str]:
    """Floating-button submissions land here. Keep it cheap (no auth, no
    rate limiting) — anyone who can reach the preview can leave a note."""
    from mise.models import Feedback as _Feedback
    body = await req.json()
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content required")
    if len(content) > 4000:
        raise HTTPException(status_code=400, detail="content too long")
    kind = body.get("kind", "note")
    if kind not in {"idea", "bug", "like", "note"}:
        kind = "note"
    path = (body.get("path") or "")[:500]
    who = body.get("who")
    if who is not None:
        who = str(who).strip()[:60] or None
    fb = _Feedback(path=path, kind=kind, content=content, who=who)
    db.add(fb)
    db.commit()
    db.refresh(fb)
    return {"id": fb.id, "status": "ok"}


@router.get("/preview/feedback", response_class=HTMLResponse)
def feedback_inbox(
    db: Session = Depends(get_session),
    status: str = "new",
) -> HTMLResponse:
    """Migrated to /app/inbox."""
    from fastapi.responses import RedirectResponse as _RR
    qs = f"?status={status}" if status != "new" else ""
    return _RR(url=f"{PREFIX}/app/inbox{qs}", status_code=307)


@router.patch("/preview/feedback/{fb_id}/status")
async def update_feedback_status(
    fb_id: int, req: Request, db: Session = Depends(get_session)
) -> dict[str, str]:
    from mise.models import Feedback as _Feedback
    from pathlib import Path as _Path
    body = await req.json()
    new_status = body.get("status", "")
    if new_status not in {"new", "reviewed", "done", "wontfix"}:
        raise HTTPException(status_code=400, detail="invalid status")
    fb = db.get(_Feedback, fb_id)
    if not fb:
        raise HTTPException(status_code=404)
    # Transitioning out of "new" means the operator has triaged the
    # feedback — the screenshot is no longer needed. Delete the file
    # and null the path so disk doesn't accumulate stale captures.
    if fb.status == "new" and new_status != "new" and fb.screenshot_path:
        shot_dir = _Path("/data/feedback-shots")
        target = (shot_dir / fb.screenshot_path).resolve()
        if str(target).startswith(str(shot_dir.resolve())) and target.is_file():
            try:
                target.unlink()
            except OSError:
                pass
        fb.screenshot_path = None
    fb.status = new_status
    db.add(fb)
    db.commit()
    return {"status": "ok"}


@router.get("/", response_class=HTMLResponse)
def root_redirect() -> HTMLResponse:
    """Default landing is now the consumer app at /app/. /preview/* still
    works for the deep operator tools (review queue, parse trace,
    rejected backlog) until those are migrated to /app/tools/*. Meta-
    refresh rather than 302 because Traefik strip-prefix can confuse
    direct router redirects on root."""
    return HTMLResponse(f'<meta http-equiv="refresh" content="0;url={PREFIX}/app/">')


@router.get("/preview/", response_class=HTMLResponse)
def preview_index(
    tag: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """Library — migrated to /app/. Redirect, preserving any tag / q
    query params so old bookmarks still surface the right filter."""
    from fastapi.responses import RedirectResponse as _RR
    parts = []
    if q: parts.append(f"q={q}")
    if tag: parts.append(f"tag={tag}")
    qs = ("?" + "&".join(parts)) if parts else ""
    return _RR(url=f"{PREFIX}/app/{qs}", status_code=307)
    # Dead code below kept until /app/ stabilises; remove on cleanup.
    """Library grid. `tag` filters by single tag; `q` is a substring
    search over names (English + original). Tag chips above the grid
    are ordered by usage count desc — so the most-used tag (typically
    'favourite' once that's seeded) lands left."""
    from collections import Counter

    # Library shows ONLY reviewed recipes — unreviewed rows live in
    # the review queue and aren't pickable for the planner/shopping
    # list until they've been eyeballed. Avoids planning a meal based
    # on parser drift the operator hasn't sanity-checked yet.
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

    # Tag usage counter across the library.
    tag_counts: Counter[str] = Counter()
    for r in all_recipes:
        for t in (r.tags or []):
            tag_counts[t] += 1

    # Apply filters.
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

    rows = []
    for r in recipes:
        all_ings = db.exec(
            select(Ingredient)
            .join(IngredientSection, Ingredient.section_id == IngredientSection.id)
            .where(IngredientSection.recipe_id == r.id)
        ).all()
        ing_count = len(all_ings)
        unparsed_in_recipe = sum(1 for i in all_ings if i.display_name == "unknown")
        name = r.display_title()
        original = r.name_original if r.name_english and r.name_original != r.name_english and r.name_original != name else None
        servings = r.servings
        # Stub recipes: the xlsx row has a name but no ingredients/
        # instructions filled in. Visually distinguish so the operator
        # can spot the backfill backlog at a glance.
        is_stub = ing_count == 0
        card_classes = "block card p-3 hover:shadow-lg transition-shadow flex items-center gap-3"
        if is_stub:
            card_classes += " opacity-50"
        if is_stub:
            meta = "<span class='text-amber-600'>stub — needs filling</span>"
        else:
            badge = (
                f" · <span class='text-amber-600'>{unparsed_in_recipe} unparsed</span>"
                if unparsed_in_recipe else ""
            )
            meta = f"{servings or '?'} servings · {ing_count} ingredients{badge}"
        # Card layout: a generously-sized tap target in the top-right
        # for shopping-list selection + the rest of the card links to
        # detail. The selection target is a <label> wrapping the
        # checkbox so taps anywhere in the 44pt+ zone toggle it (and
        # the label intercepts the tap so the underlying card link
        # doesn't fire). Stub recipes have nothing to aggregate — skip.
        checkbox_html = "" if is_stub else (
            f"<label class='shop-pick-target absolute top-0 right-0 z-10 p-3 "
            f"cursor-pointer select-none' "
            f"onclick='event.stopPropagation()'>"
            f"  <input type='checkbox' class='shop-pick h-7 w-7 accent-amber-700 cursor-pointer' "
            f"  data-recipe-id='{r.id}'>"
            f"</label>"
        )
        # Tiny reviewed tick — every card in the library is reviewed
        # by construction (we filter unreviewed out), but the check
        # makes the "library = vetted" contract visible at a glance.
        reviewed_tick = (
            "<span class='text-emerald-600 mr-1' title='Reviewed'>✓</span>"
        )
        # Haystack for client-side filter — covers both name fields +
        # tags so typing "araye", "carne", or "favourite" all surface
        # the right cards as the user types.
        haystack = " ".join(filter(None, [
            (r.name_english or "").lower(),
            (r.name_original or "").lower(),
            " ".join((r.tags or [])).lower(),
        ]))
        thumb_color = _recipe_thumb_color(r)
        emoji = _recipe_emoji(r)
        rows.append(f"""
          <div class="relative recipe-card" data-haystack="{_h(haystack)}">
            {checkbox_html}
            <a href="{PREFIX}/preview/{r.id}" class="{card_classes}">
              <div class="w-14 h-14 rounded-2xl {thumb_color} flex items-center justify-center text-2xl shrink-0">{emoji}</div>
              <div class="flex-1 min-w-0 pr-10">
                <div class="serif text-base font-medium leading-tight truncate">{reviewed_tick}{_h(name)}</div>
                {f'<div class="text-xs serif italic text-stone-500 mt-0.5 truncate">{_h(original)}</div>' if original else ''}
                <div class="text-[11px] text-stone-400 mt-1">{meta}</div>
              </div>
            </a>
          </div>
        """)

    # Tag chips, sorted by usage. Build URLs that preserve the `q`
    # query if set. Active chip gets a darker style; an "All" chip
    # clears the filter.
    chip_qs = f"&q={_h(q)}" if q else ""
    def _chip(label: str, target_tag: str | None, count: int | None = None) -> str:
        is_active = (target_tag or "") == (tag or "")
        href = (
            f"{PREFIX}/preview/?tag={_h(target_tag)}{chip_qs}"
            if target_tag else f"{PREFIX}/preview/{('?q=' + _h(q)) if q else ''}"
        )
        classes = (
            "bg-amber-200 text-amber-900 font-medium" if is_active
            else "bg-stone-100 text-stone-600 hover:bg-stone-200"
        )
        count_span = (
            f" <span class='text-xs opacity-60'>{count}</span>" if count else ""
        )
        return (
            f"<a href='{href}' class='inline-block {classes} text-sm px-3 py-1 "
            f"rounded-full'>{_h(label)}{count_span}</a>"
        )

    chips_html = _chip("All", None)
    for t, c in tag_counts.most_common():
        chips_html += _chip(t, t, c)

    filtered_note = ""
    if tag or q:
        bits = []
        if tag: bits.append(f"tag: <strong>{_h(tag)}</strong>")
        if q: bits.append(f"search: <strong>{_h(q)}</strong>")
        filtered_note = (
            f"<div class='text-sm text-stone-500 mt-2'>"
            f"  {len(recipes)} of {len(all_recipes)} · {' · '.join(bits)}"
            f"</div>"
        )

    # Inline review chip — household feedback: don't shove a big banner
    # in the way, surface the count as a tappable chip next to the
    # subtitle. Replaces the old amber banner.
    review_chip = (
        f"<a href='{PREFIX}/preview/review' "
        f"   class='inline-flex items-center gap-1 bg-amber-100 hover:bg-amber-200 "
        f"          text-amber-900 text-xs font-semibold px-2 py-0.5 rounded-full ml-1 "
        f"          no-underline'>"
        f"{unreviewed_count} awaiting review →"
        f"</a>"
        if unreviewed_count else ""
    )
    body = f"""
      <div class="mb-6 flex items-start justify-between gap-3">
        <div class="flex-1 min-w-0">
          <div class="text-[10px] uppercase tracking-[0.25em] text-stone-400 mb-1">The kitchen</div>
          <h1 class="serif text-4xl font-medium tracking-tight leading-none">Recipe <em class="text-stone-500">library</em>.</h1>
          <p class="text-sm text-stone-500 mt-3">
            {len(all_recipes)} reviewed recipes{review_chip} · Tap any to view. Tick a
            checkbox to add it to a multi-recipe shopping list.
          </p>
          {filtered_note}
        </div>
        <a href="{PREFIX}/preview/new"
           class="shrink-0 bg-stone-900 hover:bg-stone-700 text-white text-sm font-medium px-4 py-2 rounded-lg">
          + New recipe
        </a>
      </div>

      <!-- Search input — filters cards client-side as you type. The
           form wrapper still allows Enter-to-submit for shareable URL
           state (?q=…). -->
      <form method="get" action="{PREFIX}/preview/" class="mb-3">
        {f'<input type="hidden" name="tag" value="{_h(tag)}">' if tag else ''}
        <input id="lib-search" name="q" value="{_h(q or '')}"
               placeholder="Search recipes (name, original, tag)…"
               autocomplete="off"
               class="w-full border rounded px-3 py-2 text-sm">
      </form>

      <div class="flex flex-wrap gap-2 mb-4">
        {chips_html}
      </div>

      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {''.join(rows) or '<p class="text-stone-400 italic col-span-full">No recipes match.</p>'}
      </div>

      <a id="build-list-btn"
         href="#"
         class="hidden fixed bottom-6 right-6 bg-stone-900 hover:bg-stone-700 text-white font-semibold px-5 py-3 rounded-full shadow-lg">
        Build list (<span id="picked-count">0</span>)
      </a>
      <script>
        // Live filter — every keystroke hides cards whose haystack
        // doesn't contain the query. Avoids the "type → press search →
        // see results" round-trip the bare GET form required.
        const libSearch = document.getElementById('lib-search');
        if (libSearch) {{
          const cards = document.querySelectorAll('.recipe-card');
          const applyFilter = () => {{
            const q = libSearch.value.trim().toLowerCase();
            cards.forEach(c => {{
              const hit = !q || (c.dataset.haystack || '').includes(q);
              c.classList.toggle('hidden', !hit);
            }});
          }};
          libSearch.addEventListener('input', applyFilter);
          // Run once on load so a ?q=... URL state takes effect even
          // before the user touches the input.
          applyFilter();
        }}

        // Sync checkbox → floating button.
        const btn = document.getElementById('build-list-btn');
        const countSpan = document.getElementById('picked-count');
        document.addEventListener('change', (e) => {{
          if (!e.target.classList.contains('shop-pick')) return;
          const picked = [...document.querySelectorAll('.shop-pick:checked')]
            .map(c => c.dataset.recipeId);
          countSpan.textContent = picked.length;
          btn.classList.toggle('hidden', picked.length === 0);
          btn.href = '{PREFIX}/shopping?ids=' + picked.join(',');
        }});
      </script>
    """
    return HTMLResponse(_shell("Library", body))


@router.get("/preview/review", response_class=HTMLResponse)
def review_redirect(db: Session = Depends(get_session)) -> HTMLResponse:
    """Migrated to /app/review."""
    from fastapi.responses import RedirectResponse as _RR
    return _RR(url=f"{PREFIX}/app/review", status_code=307)


@router.get("/preview/rejected", response_class=HTMLResponse)
def rejected_backlog(db: Session = Depends(get_session)) -> HTMLResponse:
    """Migrated to /app/rejected."""
    from fastapi.responses import RedirectResponse as _RR
    return _RR(url=f"{PREFIX}/app/rejected", status_code=307)


@router.get("/preview/review/{recipe_id}", response_class=HTMLResponse)
def review_recipe(
    recipe_id: int,
    show: str = "unreviewed",
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """Side-by-side review surface — original source on the left,
    parsed structure on the right with severity dots and inline trace.

    Goal: walk through every recipe, fix the obvious mis-parses, move
    on. Each parsed row reuses the existing PATCH /recipes/ingredient
    endpoint via click-to-edit (inherited from the detail page).
    """
    from mise.services.parse_trace import _trace_line, all_words, content_words, stem_word

    def _diff_color(text: str, kept_stems: set[str]) -> str:
        """Highlight matching/dropped tokens with a continuous bg.
        Tokens: words (\\w+, includes Unicode + digits) get classified
        green/red by stem-membership. Whitespace between same-class
        words inherits the class so 'Toss the meat' renders as one
        unbroken green stripe. Punctuation and class-boundary
        whitespace pass through plain."""
        import re as _re
        if not text:
            return ""
        tokens = _re.split(r"(\W+)", text)
        if tokens and tokens[0] == "":
            tokens = tokens[1:]

        # Classify each token
        classes: list[str] = []
        for tok in tokens:
            if not tok:
                classes.append("neutral")
            elif _re.fullmatch(r"\w+", tok):
                stem = stem_word(tok.lower())
                classes.append("green" if stem in kept_stems else "red")
            else:
                classes.append("neutral")

        # Promote ANY neutral token between two same-class words —
        # keeps a green / red run unbroken across punctuation, hyphens,
        # commas, periods, colons, parens. Mirrors the JS coloring used
        # on the Ingredients table; the Steps table is server-rendered
        # only, so this is where the fix has to live.
        for i in range(len(tokens)):
            if classes[i] != "neutral" or not tokens[i]:
                continue
            prev_cls = classes[i - 1] if i > 0 else "neutral"
            next_cls = classes[i + 1] if i < len(tokens) - 1 else "neutral"
            if prev_cls == next_cls and prev_cls in ("green", "red"):
                classes[i] = prev_cls

        # Merge contiguous same-class runs
        out: list[str] = []
        i = 0
        while i < len(tokens):
            cls = classes[i]
            chunk = tokens[i]
            j = i + 1
            while j < len(tokens) and classes[j] == cls:
                chunk += tokens[j]
                j += 1
            if cls == "green":
                out.append(f"<mark class='bg-green-200 rounded-sm'>{_h(chunk)}</mark>")
            elif cls == "red":
                out.append(f"<mark class='bg-red-200 rounded-sm'>{_h(chunk)}</mark>")
            else:
                out.append(_h(chunk))
            i = j
        return "".join(out)

    r = db.get(Recipe, recipe_id)
    if r is None:
        raise HTTPException(status_code=404, detail="recipe not found")

    # Prev / next for nav. Default: skip already-reviewed recipes so
    # the queue walk stays focused. `?show=all` includes everything.
    # The current recipe is always included even if filtered out, so
    # you can navigate FROM a reviewed recipe without dead-ending.
    show_all = show == "all"
    base_q = select(Recipe).where(Recipe.archived == False)  # noqa: E712
    if not show_all:
        base_q = base_q.where(
            (Recipe.reviewed_at.is_(None)) | (Recipe.id == recipe_id)
        )
    all_ids = [x.id for x in db.exec(base_q.order_by(Recipe.id)).all()]
    pos = all_ids.index(recipe_id) if recipe_id in all_ids else 0
    prev_id = all_ids[pos - 1] if pos > 0 else None
    next_id = all_ids[pos + 1] if pos < len(all_ids) - 1 else None
    # Preserve the show param across prev/next links and the reject
    # advance target.
    nav_qs = "?show=all" if show_all else ""

    # Pull sections + ingredients
    sections = db.exec(
        select(IngredientSection).where(IngredientSection.recipe_id == r.id).order_by(IngredientSection.order)
    ).all()

    # Build the picker option sets used by the inline-edit dropdowns.
    household = db.exec(select(Household)).first()
    cat_opts = (household.grocery_categories if household else ["Pantry"]) or ["Pantry"]
    cat_opts_str = ",".join(cat_opts)
    # Unit dropdown — union of measure types in cooking use, sorted
    # alphabetically. Empty string stays first so the user can blank a
    # wrong unit.
    unit_opts = sorted([
        "tsp", "tbsp", "cup", "ml", "l", "floz",
        "g", "kg", "oz", "lb",
        "ea", "piece", "slice", "scoop", "pinch", "dash",
        "clove", "sprig", "stalk", "rib", "stick", "head", "bunch", "pod",
        "can", "jar", "bottle", "box", "bag", "package", "pkg",
    ])
    unit_opts = [""] + unit_opts
    unit_opts_str = ",".join(unit_opts)
    # Union of distinct canonicals across both the alias cache and live
    # Ingredient rows. Gives the user "what already exists" as picker
    # suggestions; they can still type something new.
    from mise.models import IngredientAlias as _IA
    canon_set: set[str] = set()
    for c in db.exec(select(_IA.canonical_name).distinct()).all():
        if c:
            canon_set.add(c)
    for c in db.exec(select(Ingredient.canonical_name).distinct()).all():
        if c:
            canon_set.add(c)
    canon_options_html = "".join(
        f"<option value='{_h(c)}'>" for c in sorted(canon_set)
    )

    # Inline hex colours — Tailwind v2 CDN ships its default palette
    # only, so 'bg-amber-500' silently renders no background. Inline
    # styles avoid any class-loading question.
    _DOT_GREEN = "#22c55e"  # green-500
    _DOT_AMBER = "#f59e0b"  # amber-500
    _DOT_RED = "#ef4444"   # red-500

    def _severity(t) -> tuple[str, str]:
        """Return (hex_colour, label). Worst-class wins:
        red = real loss / parse_failed; amber = warning; green = clean."""
        if t.parse_failed:
            return _DOT_RED, "parse failed"
        src_words = content_words(t.source_line)
        canon_words = content_words(t.final_canonical_name.replace("_", " "))
        lost = src_words - canon_words
        if lost:
            return _DOT_RED, f"lost from canonical: {', '.join(sorted(lost))}"
        warns = []
        if t.canonical_input == t.strangetom_foundation_food and t.strangetom_foundation_food:
            warns.append("canonical anchored on FDC name")
        if t.category_resolved == "Other":
            warns.append("category=Other")
        if (t.strangetom_name_confidence is not None
                and t.strangetom_name_confidence < 0.6):
            warns.append(f"low name confidence ({t.strangetom_name_confidence:.2f})")
        if t.size_likely_leaked:
            warns.append("size leaked into unit slot")
        if warns:
            return _DOT_AMBER, " · ".join(warns)
        return _DOT_GREEN, "clean"

    # Build side-by-side rows for each section
    section_blocks_html = []
    for s in sections:
        ings = db.exec(
            select(Ingredient).where(Ingredient.section_id == s.id).order_by(Ingredient.order)
        ).all()
        rows_html = []
        for i in ings:
            # Run trace on this row's source (or fall back to raw_text)
            src_text = i.source_line or i.raw_text or ""
            traces = _trace_line(src_text, db) if src_text else []
            t = traces[0] if traces else None
            dot_color, severity_note = _severity(t) if t else ("#9ca3af", "no trace")

            # Editable spans (reuse the existing inline-edit JS in _BASE_CSS)
            endpoint = f"/recipes/ingredient/{i.id}"
            # data-value uses :g format so 180.0 → '180' (no trailing
            # zero). The number-type input still parses cleanly, the
            # JS recolor pass uses parseFloat anyway, and there's no
            # '.0' leaking through to display.
            qty_span = (
                f"<span class='editable parsed-value' data-endpoint='{endpoint}' "
                f"data-field='qty' data-type='number' data-value='{i.qty:g}'>"
                f"{_fmt_qty(i.qty)}</span>"
            )
            unit_span = (
                f"<span class='editable parsed-value' data-endpoint='{endpoint}' "
                f"data-field='unit' data-type='select' "
                f"data-options='{_h(unit_opts_str)}' "
                f"data-value='{_h(i.unit)}'>{_h(i.unit)}</span>"
            )
            name_span = (
                f"<span class='editable parsed-value' data-endpoint='{endpoint}' "
                f"data-field='display_name' data-value='{_h(i.display_name)}'>"
                f"{_h(i.display_name)}</span>"
            )
            # prep_note — only rendered when set. Add/Edit happens
            # via the ⋮ menu → inline form pattern (below the row).
            prep_html = ""
            if i.prep_note:
                prep_html = (
                    f"<div class='text-xs italic text-stone-600 mt-0.5 ml-5'>"
                    f"  prep: <span class='editable parsed-value' data-endpoint='{endpoint}' "
                    f"  data-field='prep_note' data-value='{_h(i.prep_note)}'>"
                    f"  {_h(i.prep_note)}</span>"
                    f"</div>"
                )
            # Tail rescue — separate row when source had a comma but
            # the parser dropped the tail.
            tail_html = ""
            if not i.prep_note and src_text and "," in src_text:
                tail = src_text.split(",", 1)[1].strip()
                if tail:
                    tail_html = (
                        f"<div class='text-xs text-amber-700 mt-0.5 ml-5'>"
                        f"  <span class='italic'>prep tail not captured:</span> "
                        f"  <span class='italic'>{_h(tail)}</span>"
                        f"  <button type='button' class='tail-rescue ml-2 underline "
                        f"          hover:text-amber-900' "
                        f"          data-endpoint='{endpoint}' data-field='prep_note' "
                        f"          data-value='{_h(tail)}'>→ prep</button>"
                        f"  <button type='button' class='tail-rescue ml-1 underline "
                        f"          hover:text-amber-900' "
                        f"          data-endpoint='{endpoint}' data-field='alternatives_note' "
                        f"          data-value='{_h(tail)}'>→ alt</button>"
                        f"</div>"
                    )
            size_html = ""
            if i.size_descriptor:
                size_html = (
                    f"<div class='text-xs italic text-stone-600 mt-0.5 ml-5'>"
                    f"  size: <span class='editable parsed-value' data-endpoint='{endpoint}' "
                    f"  data-field='size_descriptor' data-value='{_h(i.size_descriptor)}'>"
                    f"  {_h(i.size_descriptor)}</span>"
                    f"</div>"
                )
            # One hidden inline form per row, populated dynamically by
            # the ⋮ menu trigger. Single form (vs one-per-field) keeps
            # the DOM lean.
            row_form_html = (
                f"<div class='row-edit-form hidden bg-amber-50 border border-blue-200 "
                f"     rounded p-2 mt-1 ml-5 text-xs' data-ingredient-id='{i.id}' "
                f"     data-endpoint='{endpoint}'>"
                f"  <label class='block text-stone-700 mb-1'>"
                f"    <span class='row-edit-label font-semibold'></span>:"
                f"  </label>"
                f"  <input type='text' class='row-edit-input border rounded px-2 py-1 "
                f"         w-full text-sm' autocomplete='off'>"
                f"  <div class='flex gap-2 mt-2 items-center'>"
                f"    <button type='button' class='row-edit-save bg-stone-900 text-white "
                f"            px-3 py-1 rounded'>Save</button>"
                f"    <button type='button' class='row-edit-cancel text-stone-500 "
                f"            hover:text-stone-700 px-2 py-1'>Cancel</button>"
                f"  </div>"
                f"</div>"
            )
            # Optional flag — only show inline when actually optional
            # (small amber pill). Toggle lives in the ⋮ dropdown so the
            # default state adds no visual noise.
            opt_pill_hidden = "" if i.optional else "hidden"
            opt_html = (
                f"<button type='button' class='optional-pill {opt_pill_hidden} ml-2 text-xs "
                f"        bg-amber-100 text-amber-800 hover:bg-amber-200 "
                f"        px-1.5 py-0.5 rounded whitespace-nowrap cursor-pointer' "
                f"        data-endpoint='{endpoint}' "
                f"        title='Tap to remove optional flag'>"
                f"  optional ✕"
                f"</button>"
            )
            opt_toggle_label = "Mark as required" if i.optional else "Mark as optional"
            size_action_label = "Edit size descriptor" if i.size_descriptor else "Add size descriptor"
            prep_action_label = "Edit prep note" if i.prep_note else "Add prep note"
            # Per-row action menu. Add/Edit size and prep open the
            # inline form below the row (vs a mobile-hostile prompt()).
            row_menu_html = (
                f"<span class='row-menu shrink-0 ml-1' style='position: relative'>"
                f"  <button type='button' class='row-menu-btn text-stone-400 "
                f"          hover:text-stone-700 text-lg leading-none px-1 py-0.5' "
                f"          title='Row actions' aria-label='Row actions'>⋮</button>"
                f"  <div class='row-menu-popup hidden absolute right-0 mt-1 bg-white border "
                f"             border-stone-300 rounded shadow-lg z-50 text-sm' "
                f"       style='min-width: 12rem'>"
                f"    <button type='button' class='optional-toggle block w-full text-left px-3 py-1.5 "
                f"            hover:bg-amber-50 text-stone-700' "
                f"            data-endpoint='{endpoint}' "
                f"            data-state='{1 if i.optional else 0}'>{opt_toggle_label}</button>"
                f"    <button type='button' class='open-row-edit block w-full text-left px-3 py-1.5 "
                f"            hover:bg-stone-50 text-stone-700' "
                f"            data-ingredient-id='{i.id}' data-field='size_descriptor' "
                f"            data-current='{_h(i.size_descriptor or '')}' "
                f"            data-label='size descriptor'>{size_action_label}</button>"
                f"    <button type='button' class='open-row-edit block w-full text-left px-3 py-1.5 "
                f"            hover:bg-stone-50 text-stone-700' "
                f"            data-ingredient-id='{i.id}' data-field='prep_note' "
                f"            data-current='{_h(i.prep_note or '')}' "
                f"            data-label='prep note'>{prep_action_label}</button>"
                f"    <button type='button' class='split-row block w-full text-left px-3 py-1.5 "
                f"            hover:bg-stone-50 text-stone-700' "
                f"            data-ingredient-id='{i.id}'>✂ Split row</button>"
                f"    <button type='button' class='delete-row block w-full text-left px-3 py-1.5 "
                f"            hover:bg-red-50 text-red-600' "
                f"            data-ingredient-id='{i.id}'>✕ Delete row</button>"
                f"  </div>"
                f"</span>"
            )
            # Trace toggle target
            trace_id = f"trace-{i.id}"
            # Build the "kept-stems" set: every word that ended up
            # somewhere in the stored row. Uses all_words (no stop
            # filter) so the highlighter shows shared 'tablespoon',
            # 'and', etc. Includes qty (as both int and decimal forms)
            # so '1' in source matches qty=1.0.
            qty_str_parts = []
            if i.qty:
                qty_str_parts.append(str(i.qty))
                if i.qty == int(i.qty):
                    qty_str_parts.append(str(int(i.qty)))
            kept_text = " ".join(filter(None, [
                *qty_str_parts,
                i.display_name,
                (i.canonical_name or "").replace("_", " "),
                i.prep_note or "",
                i.size_descriptor or "",
                i.unit or "",
                i.category or "",
            ]))
            kept_stems = all_words(kept_text)
            colored_src = _diff_color(src_text, kept_stems)
            rows_html.append(f"""
              <tr class="align-top border-b diff-row"
                  data-source-text="{_h(src_text)}"
                  data-qty="{i.qty}">
                <td class="py-1 pr-3 text-sm font-mono align-top w-1/2 break-words">
                  <span class="diff-source">{colored_src}</span>
                </td>
                <td class="py-1 align-top">
                  <div class="flex items-baseline gap-2">
                    <button type="button" class="trace-toggle w-3 h-3 rounded-full shrink-0 cursor-help"
                            style="background-color: {dot_color}"
                            title="{_h(severity_note)}"
                            data-target="{trace_id}"></button>
                    <span class="text-stone-500 text-sm whitespace-nowrap">{qty_span} {unit_span}</span>
                    <span class="text-sm flex-1">{name_span}{opt_html}</span>
                    <span class="text-xs text-stone-400 whitespace-nowrap">
                      <span class="editable parsed-value" data-endpoint='{endpoint}'
                            data-field='category' data-type='select'
                            data-options='{_h(cat_opts_str)}'
                            data-value='{_h(i.category)}'>{_h(i.category)}</span>
                    </span>
                    {row_menu_html}
                  </div>
                  {size_html}
                  {prep_html}
                  {tail_html}
                  {row_form_html}
                  <div class="text-xs text-stone-400 font-mono mt-0.5 ml-5">
                    canonical:
                    <span class="editable parsed-value" data-endpoint='{endpoint}'
                          data-field='canonical_name' data-type='datalist'
                          data-list='canon-options'
                          data-value='{_h(i.canonical_name)}'>{_h(i.canonical_name)}</span>
                  </div>
                  <div id="{trace_id}" class="hidden mt-2 ml-5 p-2 bg-stone-50 border rounded text-xs">
                    {_render_inline_trace(t) if t else "<i>no trace</i>"}
                  </div>
                </td>
              </tr>
            """)
        # Reconstruct the source text for "↻ reprocess". Prefer existing
        # rows' source_line; fall back to the section name (handles the
        # empty-section case like "TOPPINGS: chopped fresh chives, …"
        # where the parser swallowed the body into the header).
        src_lines = [
            (ing.source_line or ing.raw_text) for ing in ings
            if (ing.source_line or ing.raw_text)
        ]
        if src_lines:
            prefill_text = "\n".join(src_lines)
        elif s.name and ":" in s.name:
            _, _, tail = s.name.partition(":")
            prefill_text = tail.strip() if tail.strip() else (s.name or "")
        else:
            prefill_text = s.name or ""

        name_html = (
            f"<h3 class='font-semibold text-sm uppercase tracking-wide "
            f"        text-stone-500'>{_h(s.name)}</h3>"
            if s.name else "<span class='text-xs text-stone-400 italic'>(no section name)</span>"
        )
        # Empty section hint — without rows the user can't see what's
        # there; surface a one-line nudge so the reprocess button isn't
        # mysterious.
        empty_hint = (
            "<div class='text-xs text-amber-600 italic mb-1'>"
            "Empty section — no ingredient rows parsed. Edit the text "
            "below and reparse.</div>"
            if not ings else ""
        )
        section_label = (
            f"<div class='flex items-center justify-between gap-2 mt-4 mb-2'>"
            f"  {name_html}"
            f"  <button type='button' class='reprocess-toggle text-xs text-amber-700 "
            f"          hover:text-amber-900 whitespace-nowrap' "
            f"          data-section-id='{s.id}'>↻ reprocess</button>"
            f"</div>"
            f"<form class='reprocess-form hidden bg-amber-50 border border-blue-200 "
            f"      rounded p-2 mb-2' data-section-id='{s.id}'>"
            f"  {empty_hint}"
            f"  <textarea class='w-full border rounded p-1 text-sm font-mono' "
            f"            rows='{max(3, min(8, prefill_text.count(chr(10)) + 2))}'>"
            f"{_h(prefill_text)}</textarea>"
            f"  <div class='flex gap-2 mt-2 items-center'>"
            f"    <button type='button' class='reprocess-go bg-stone-900 text-white "
            f"            text-xs px-3 py-1 rounded'>Reparse → replace rows</button>"
            f"    <button type='button' class='reprocess-cancel text-xs text-stone-500 "
            f"            hover:text-stone-700 px-2 py-1'>Cancel</button>"
            f"  </div>"
            f"</form>"
        )
        section_blocks_html.append(
            section_label +
            f"<table class='w-full'><tbody>{''.join(rows_html)}</tbody></table>"
        )

    # Steps — simple side-by-side, no trace (parser doesn't touch them).
    step_blocks_html = []
    step_sects = db.exec(
        select(StepSection).where(StepSection.recipe_id == r.id).order_by(StepSection.order)
    ).all()
    for ss in step_sects:
        steps = db.exec(select(Step).where(Step.section_id == ss.id).order_by(Step.order)).all()
        # The parser doesn't transform step text — source IS stored.
        # Coloring both sides with the same kept_stems means: every
        # content word is green (matched). If the parser ever started
        # dropping anything, those words would flip red.
        step_rows_list = []
        for st in steps:
            kept_stems = all_words(st.text)
            colored = _diff_color(st.text, kept_stems)
            step_rows_list.append(
                f"<tr class='border-b'>"
                f"<td class='py-1 pr-3 text-sm font-mono align-top w-1/2'>{colored}</td>"
                f"<td class='py-1 text-sm align-top'>{colored}</td>"
                f"</tr>"
            )
        step_rows = "".join(step_rows_list)
        section_label = (
            f"<h3 class='font-semibold text-sm uppercase tracking-wide text-stone-500 mt-4 mb-2'>{_h(ss.name)}</h3>"
            if ss.name else ""
        )
        step_blocks_html.append(
            section_label + f"<table class='w-full'><tbody>{step_rows}</tbody></table>"
        )

    name = r.display_title()
    prev_btn = (
        f"<a href='{PREFIX}/preview/review/{prev_id}{nav_qs}' class='text-stone-500 hover:text-stone-900'>← prev</a>"
        if prev_id else "<span class='text-stone-300'>← prev</span>"
    )
    next_btn = (
        f"<a href='{PREFIX}/preview/review/{next_id}{nav_qs}' class='text-stone-500 hover:text-stone-900'>next →</a>"
        if next_id else "<span class='text-stone-300'>next →</span>"
    )
    # Toggle between unreviewed-only walk and all-recipes walk.
    if show_all:
        show_toggle_html = (
            f"<a href='{PREFIX}/preview/review/{r.id}' "
            f"   class='text-xs text-amber-700 hover:underline'>show unreviewed only</a>"
        )
    else:
        show_toggle_html = (
            f"<a href='{PREFIX}/preview/review/{r.id}?show=all' "
            f"   class='text-xs text-amber-700 hover:underline'>show all recipes</a>"
        )

    next_url_for_reject = (
        f"{PREFIX}/preview/review/{next_id}{nav_qs}" if next_id
        else f"{PREFIX}/preview/"
    )
    # Source URL slot: real link + tiny edit pencil when set, or a
    # plain "+ add link" button when not. Both paths PATCH the recipe.
    if r.source_url:
        source_link_html = (
            f"<span class='inline-flex items-center gap-1'>"
            f"  <a href='{_h(r.source_url)}' target='_blank' rel='noopener noreferrer' "
            f"     class='text-amber-700 hover:text-amber-900 hover:underline'>"
            f"    🔗 View original recipe ↗</a>"
            f"  <button type='button' class='edit-source-url text-stone-400 hover:text-stone-700 text-xs' "
            f"          data-recipe-id='{r.id}' data-current='{_h(r.source_url)}' "
            f"          title='Edit URL'>✎</button>"
            f"</span>"
        )
    else:
        source_link_html = (
            f"<button type='button' class='edit-source-url text-amber-700 hover:underline text-sm' "
            f"        data-recipe-id='{r.id}' data-current=''>"
            f"  🔗 + add link"
            f"</button>"
        )
    # Review-state badge — green pill when already done so a second
    # pass is obvious. Quietly hidden when unreviewed (the Complete
    # Review button at the bottom is the call-to-action instead).
    reviewed_badge = (
        f"<span class='text-xs bg-green-100 text-emerald-700 px-1.5 py-0.5 rounded "
        f"          whitespace-nowrap'>✓ reviewed</span>"
        if r.reviewed_at else ""
    )
    # Queue stats — count unreviewed actives so the user knows how
    # much is left without leaving the page. The current recipe counts
    # toward "remaining" until they hit Complete Review.
    total_active = len(all_ids)
    reviewed_count = db.exec(
        select(Recipe)
        .where(Recipe.archived == False)  # noqa: E712
        .where(Recipe.reviewed_at.is_not(None))
    ).all()
    reviewed_n = len(reviewed_count)
    remaining_n = total_active - reviewed_n
    complete_btn_label = "Re-mark as reviewed" if r.reviewed_at else "✓ Complete Review"
    # Two stacked rows so the buttons never inline with the links —
    # makes accidental Reject taps much harder when the toolbar wraps
    # on a narrow viewport.
    body = f"""
      <div class="mb-2 flex items-center gap-3 text-sm flex-wrap">
        <a href="{PREFIX}/preview/{r.id}" class="text-stone-500 hover:text-stone-900">View as recipe →</a>
        {source_link_html}
        {reviewed_badge}
        <span class="ml-auto text-xs text-stone-500">
          {reviewed_n} reviewed · {remaining_n} remaining ·
          <a href="{PREFIX}/preview/rejected" class="hover:underline">rejected backlog →</a>
        </span>
      </div>
      <div class="mb-3 flex items-center gap-3 text-sm flex-wrap">
        <button type="button" id="reject-recipe"
                data-recipe-id="{r.id}"
                data-next-url="{next_url_for_reject}"
                class="text-red-600 hover:text-red-800 border border-red-300 hover:border-red-500 px-2 py-0.5 rounded">
          Reject recipe (incomplete)
        </button>
        <button type="button" class="complete-review-btn bg-green-600 hover:bg-green-700 text-white font-semibold px-3 py-0.5 rounded"
                data-recipe-id="{r.id}">
          {complete_btn_label}
        </button>
      </div>
      <h1 class="text-2xl font-bold mb-1">
        <span class="editable" data-endpoint="/recipes/{r.id}" data-field="name_english"
              data-value="{_h(r.name_english or '')}">{_h(r.name_english or '(unnamed)')}</span>
      </h1>
      <div class="text-lg text-stone-600 mb-2">
        <span class="editable" data-endpoint="/recipes/{r.id}" data-field="name_original"
              data-value="{_h(r.name_original or '')}">{_h(r.name_original) if r.name_original else '<span class=text-stone-400>+ original name</span>'}</span>
      </div>

      <!-- Recipe-level metadata, all click-to-edit. Hits PATCH
           /recipes/{id} via the shared inline-edit JS in _BASE_CSS. -->
      <div class="text-sm text-stone-600 mb-4 flex flex-wrap gap-x-4 gap-y-1">
        <div>servings: <span class="editable" data-endpoint="/recipes/{r.id}"
              data-field="servings" data-type="number"
              data-value="{r.servings if r.servings is not None else ''}">{r.servings if r.servings is not None else '—'}</span></div>
        <div>prep: <span class="editable" data-endpoint="/recipes/{r.id}"
              data-field="prep_minutes" data-type="number"
              data-value="{r.prep_minutes if r.prep_minutes is not None else ''}">{r.prep_minutes if r.prep_minutes is not None else '—'}</span>m</div>
        <div>cook: <span class="editable" data-endpoint="/recipes/{r.id}"
              data-field="cook_minutes" data-type="number"
              data-value="{r.cook_minutes if r.cook_minutes is not None else ''}">{r.cook_minutes if r.cook_minutes is not None else '—'}</span>m</div>
        <div>total: <span class="editable" data-endpoint="/recipes/{r.id}"
              data-field="total_minutes" data-type="number"
              data-value="{r.total_minutes if r.total_minutes is not None else ''}">{r.total_minutes if r.total_minutes is not None else '—'}</span>m</div>
        <div>oven: <span class="editable" data-endpoint="/recipes/{r.id}"
              data-field="oven_temp_f" data-type="number"
              data-value="{r.oven_temp_f if r.oven_temp_f is not None else ''}">{r.oven_temp_f if r.oven_temp_f is not None else '—'}</span>°F</div>
        <div>lang: <span class="editable" data-endpoint="/recipes/{r.id}"
              data-field="language"
              data-value="{_h(r.language or '')}">{_h(r.language) if r.language else '—'}</span></div>
        <div>emoji: <span class="editable" data-endpoint="/recipes/{r.id}"
              data-field="emoji"
              data-value="{_h(r.emoji or '')}">{_h(r.emoji) if r.emoji else '—'}</span></div>
      </div>

      <!-- Tags — comma-separated text input. Server normalises (title-
           cases, dedupes) so client just sends the joined string. -->
      <div class="text-sm text-stone-600 mb-4">
        tags:
        <span class="editable" id="tags-edit" data-endpoint="/recipes/{r.id}"
              data-field="tags-csv"
              data-value="{_h(', '.join(r.tags or []))}">{_h(', '.join(r.tags or [])) if r.tags else '<span class=text-stone-400>+ tags (comma-separated)</span>'}</span>
      </div>

      <div class="text-sm text-stone-500 mb-4 space-y-1">
        <div>
          <span class="inline-flex items-center gap-1 mr-3"><span class="w-2.5 h-2.5 rounded-full" style="background-color: {_DOT_GREEN}"></span> clean</span>
          <span class="inline-flex items-center gap-1 mr-3"><span class="w-2.5 h-2.5 rounded-full" style="background-color: {_DOT_AMBER}"></span> warning (hover dot)</span>
          <span class="inline-flex items-center gap-1"><span class="w-2.5 h-2.5 rounded-full" style="background-color: {_DOT_RED}"></span> info lost (hover dot)</span>
        </div>
        <div class="text-xs">
          Source words:
          <mark class="bg-green-200 px-0.5 rounded-sm mx-1">shared</mark>
          <mark class="bg-red-200 px-0.5 rounded-sm mx-1">dropped</mark>
        </div>
      </div>
      <h2 class="text-lg font-semibold mb-2">Ingredients</h2>
      <div class="bg-white border rounded p-3 mb-6">
        <div class="grid grid-cols-2 gap-2 text-xs uppercase tracking-wide text-stone-500 mb-1 pb-1 border-b">
          <div>source</div>
          <div>parsed (click to edit · click dot for trace)</div>
        </div>
        {''.join(section_blocks_html)}
      </div>
      <h2 class="text-lg font-semibold mb-2">Steps</h2>
      <div class="bg-white border rounded p-3 mb-6">
        <div class="grid grid-cols-2 gap-2 text-xs uppercase tracking-wide text-stone-500 mb-1 pb-1 border-b">
          <div>source</div>
          <div>stored</div>
        </div>
        {''.join(step_blocks_html) or "<p class='italic text-stone-400'>No steps.</p>"}
      </div>
      <!-- prev / next + Complete Review at the page bottom — keeps the
           destructive Reject up top and the affirmative finish-and-
           advance action down here, after the user has looked at
           everything. -->
      <div class="mt-6 pt-4 border-t flex items-center justify-between text-base flex-wrap gap-2">
        <div class="min-w-[80px]">{prev_btn}</div>
        <button type="button"
                data-recipe-id="{r.id}"
                class="complete-review-btn bg-green-600 hover:bg-green-700 text-white text-sm font-semibold px-4 py-2 rounded">
          {complete_btn_label}
        </button>
        <div class="text-center">
          <div class="text-stone-500 text-sm">{pos + 1} of {len(all_ids)}{' (unreviewed)' if not show_all else ''}</div>
          <div class="mt-0.5">{show_toggle_html}</div>
        </div>
        <div class="min-w-[80px] text-right">{next_btn}</div>
      </div>
      <datalist id="canon-options">
        {canon_options_html}
      </datalist>
      <script>
        document.querySelectorAll('.trace-toggle').forEach(btn => {{
          btn.addEventListener('click', () => {{
            const id = btn.dataset.target;
            const el = document.getElementById(id);
            if (el) el.classList.toggle('hidden');
          }});
        }});

        // ----- Real-time word-diff coloring -----
        // Mirrors mise.services.parse_trace stem_word + all_words logic
        // in JS so we can re-color after inline-edit commits without a
        // page reload. Triggered on DOMContentLoaded + 'valueChanged'
        // (which bubbles up from the inline-edit JS in _BASE_CSS).
        const STEM_SYNONYMS = {{
          "lb":"lb","lbs":"lb","pound":"lb","pounds":"lb",
          "oz":"oz","ounce":"oz","ounces":"oz",
          "g":"g","gram":"g","grams":"g","kg":"kg","kilogram":"kg","kilograms":"kg",
          "ml":"ml","milliliter":"ml","millilitre":"ml","milliliters":"ml","millilitres":"ml",
          "l":"l","liter":"l","liters":"l","litre":"l","litres":"l",
          "tsp":"tsp","teaspoon":"tsp","teaspoons":"tsp",
          "tbsp":"tbsp","tablespoon":"tbsp","tablespoons":"tbsp",
          "cup":"cup","cups":"cup",
          "pinch":"pinch","pinches":"pinch","dash":"dash","dashes":"dash",
          "clove":"clove","cloves":"clove",
          "stick":"stick","sticks":"stick","stalk":"stalk","stalks":"stalk",
          "rib":"rib","ribs":"rib","sprig":"sprig","sprigs":"sprig",
          "head":"head","heads":"head","bunch":"bunch","bunches":"bunch",
          "can":"can","cans":"can","jar":"jar","jars":"jar",
          "bottle":"bottle","bottles":"bottle","slice":"slice","slices":"slice",
        }};

        function stemWord(w) {{
          if (w in STEM_SYNONYMS) return STEM_SYNONYMS[w];
          if (w.length <= 3) return STEM_SYNONYMS[w] || w;
          let s = w;
          if (w.endsWith('ies')) s = w.slice(0, -3) + 'y';
          else if (w.endsWith('ves') && w.length > 4) s = w.slice(0, -3) + 'f';
          else if (/(ches|shes|sses|xes|zes)$/.test(w)) s = w.slice(0, -2);
          else if (w.endsWith('oes') && w.length > 4) s = w.slice(0, -2);
          else if (w.endsWith('s') && !/(ss|us|is)$/.test(w)) s = w.slice(0, -1);
          return STEM_SYNONYMS[s] || s;
        }}

        // Split at digit↔letter boundaries so '180g' becomes ['180','g']
        // — otherwise it never matches a separately-stored qty + unit.
        function splitNumAlpha(s) {{
          return s.split(/(?<=\\d)(?=\\D)|(?<=\\D)(?=\\d)/);
        }}

        function allWords(text) {{
          const out = new Set();
          // ASCII word tokens via \w+
          const matches = (text || '').toLowerCase().match(/\\w+/gu) || [];
          for (const m of matches) {{
            for (const p of splitNumAlpha(m)) {{
              if (p) out.add(stemWord(p));
            }}
          }}
          // Vulgar-fraction glyphs anywhere in text — '¾' contributes
          // '3' and '4'. Use a non-regex scan so we don't need a
          // separate Unicode-aware regex.
          for (const ch of (text || '')) {{
            const digits = "⅛¼⅓⅜½⅝⅔¾⅞".includes(ch)
              ? {{ "⅛":["1","8"],"¼":["1","4"],"⅓":["1","3"],"⅜":["3","8"],
                   "½":["1","2"],"⅝":["5","8"],"⅔":["2","3"],"¾":["3","4"],
                   "⅞":["7","8"] }}[ch]
              : null;
            if (digits) digits.forEach(d => out.add(d));
          }}
          return out;
        }}

        function esc(s) {{
          return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        }}

        // Vulgar-fraction glyph → [numerator, denominator] strings. Lets
        // a parsed '¾' match source '3/4' (and vice-versa: a source '¾'
        // match a stored qty of 0.75).
        const GLYPH_TO_DIGITS = {{
          "⅛": ["1","8"], "¼": ["1","4"], "⅓": ["1","3"], "⅜": ["3","8"],
          "½": ["1","2"], "⅝": ["5","8"], "⅔": ["2","3"], "¾": ["3","4"],
          "⅞": ["7","8"],
        }};
        function fractionGlyphDigits(tok) {{
          // Token may be the bare glyph ("¾") OR an integer-prefixed
          // glyph rendered by fmtQty ("1½"). Strip a leading run of
          // digits, return the digits of the trailing glyph if any.
          const m = (tok || '').match(/^(\\d*)([⅛¼⅓⅜½⅝⅔¾⅞])$/);
          if (m && GLYPH_TO_DIGITS[m[2]]) return GLYPH_TO_DIGITS[m[2]];
          return null;
        }}

        function colorText(text, keptStems) {{
          if (!text) return '';
          // Treat _ as a separator too (for canonical_name like 'meat_and_cheese')
          const rawTokens = text.split(/([\\W_]+)/u);
          // Sub-split word tokens at digit↔letter boundaries so '180g'
          // becomes ['180','g'] — each piece classified separately,
          // then merged back together by the same-class run logic if
          // they're both green / both red.
          const tokens = [];
          for (const tok of rawTokens) {{
            if (!tok) continue;
            if (/^[\\p{{L}}\\p{{N}}]+$/u.test(tok)) {{
              const parts = splitNumAlpha(tok);
              for (const p of parts) if (p) tokens.push(p);
            }} else {{
              tokens.push(tok);
            }}
          }}
          const classes = tokens.map(tok => {{
            if (!tok) return 'neutral';
            if (/^[\\p{{L}}\\p{{N}}]+$/u.test(tok)) {{
              // Vulgar-fraction glyph: green if BOTH digit equivalents
              // are present in keptStems (so '¾' matches source '3/4').
              const digits = fractionGlyphDigits(tok);
              if (digits) {{
                const allIn = digits.every(d => keptStems.has(d));
                return allIn ? 'green' : 'red';
              }}
              const stem = stemWord(tok.toLowerCase());
              return keptStems.has(stem) ? 'green' : 'red';
            }}
            return 'neutral';
          }});
          // Promote ANY neutral separator (whitespace, punctuation,
          // underscore, quotes) between two same-class word tokens
          // into that class — keeps green runs unbroken across commas,
          // periods, hyphens, parens, quotes, etc.
          for (let i = 0; i < tokens.length; i++) {{
            if (classes[i] !== 'neutral' || !tokens[i]) continue;
            const prev = i > 0 ? classes[i-1] : 'neutral';
            const next = i < tokens.length-1 ? classes[i+1] : 'neutral';
            if (prev === next && prev !== 'neutral') classes[i] = prev;
          }}
          // Merge contiguous same-class runs into one mark
          let out = '';
          let i = 0;
          while (i < tokens.length) {{
            const cls = classes[i];
            let chunk = tokens[i];
            let j = i + 1;
            while (j < tokens.length && classes[j] === cls) {{
              chunk += tokens[j];
              j++;
            }}
            if (cls === 'green') out += `<mark class='bg-green-200 rounded-sm'>${{esc(chunk)}}</mark>`;
            else if (cls === 'red') out += `<mark class='bg-red-200 rounded-sm'>${{esc(chunk)}}</mark>`;
            else out += esc(chunk);
            i = j;
          }}
          return out;
        }}

        // Mirror Python _fmt_qty: 0.75 → "¾", 1.5 → "1½", 2 → "2"
        const FRAC_GLYPHS = [
          [0.125, "⅛"], [0.25, "¼"], [1/3, "⅓"], [0.375, "⅜"],
          [0.5, "½"], [0.625, "⅝"], [2/3, "⅔"], [0.75, "¾"], [0.875, "⅞"],
        ];
        function fmtQty(q) {{
          if (q === 0) return "0";
          const intPart = Math.floor(q);
          const frac = q - intPart;
          const tol = 0.02;
          for (const [v, g] of FRAC_GLYPHS) {{
            if (Math.abs(frac - v) <= tol) {{
              return (intPart ? String(intPart) : "") + g;
            }}
          }}
          if (Math.abs(frac) <= tol || Math.abs(frac - 1) <= tol) {{
            return String(Math.round(q));
          }}
          return String(parseFloat(q.toFixed(2)));
        }}

        // Fraction → (numerator, denominator) digit pair. Lets source
        // '3/4 tsp' match a stored qty of 0.75 because we include '3'
        // and '4' in the kept-stems set.
        const FRAC_DIGITS = {{
          "0.125": ["1","8"], "0.25": ["1","4"], "0.333": ["1","3"],
          "0.375": ["3","8"], "0.5": ["1","2"], "0.625": ["5","8"],
          "0.667": ["2","3"], "0.75": ["3","4"], "0.875": ["7","8"],
        }};
        function fractionDigits(q) {{
          const frac = q - Math.floor(q);
          for (const [v, pair] of Object.entries(FRAC_DIGITS)) {{
            if (Math.abs(frac - parseFloat(v)) <= 0.02) return pair;
          }}
          return [];
        }}

        function colorRow(row) {{
          const sourceEl = row.querySelector('.diff-source');
          if (!sourceEl) return;
          const sourceText = row.dataset.sourceText || sourceEl.textContent;
          // Gather parsed values
          const parsed = [];
          row.querySelectorAll('.parsed-value').forEach(el => {{
            const v = el.dataset.value || el.textContent.trim();
            if (v) parsed.push(v);
          }});
          // Add qty + fraction-equivalent digits so '3/4' in source
          // can match a stored qty of 0.75.
          if (row.dataset.qty) {{
            const qty = parseFloat(row.dataset.qty);
            if (!isNaN(qty)) {{
              parsed.push(String(qty));
              if (Math.floor(qty) === qty) parsed.push(String(Math.floor(qty)));
              const frac = fractionDigits(qty);
              if (frac.length) parsed.push(frac.join(' '));
            }}
          }}

          const keptStems = allWords(parsed.join(' '));
          const srcStems = allWords(sourceText);

          sourceEl.innerHTML = colorText(sourceText, keptStems);
          row.querySelectorAll('.parsed-value').forEach(el => {{
            let v = el.dataset.value || el.textContent.trim();
            // Format qty fields as fractions for display.
            if (el.dataset.field === 'qty') {{
              const n = parseFloat(v);
              if (!isNaN(n)) v = fmtQty(n);
            }}
            el.innerHTML = colorText(v, srcStems);
          }});
        }}

        // Initial colouring
        document.querySelectorAll('.diff-row').forEach(colorRow);
        // Re-colour after any inline-edit commit
        document.addEventListener('valueChanged', (e) => {{
          const row = e.target.closest('.diff-row');
          if (row) colorRow(row);
        }});

        // Edit / add source URL — prompt, PATCH, reload. Empty input
        // clears the URL (chip flips back to "+ add link").
        document.querySelectorAll('.edit-source-url').forEach(btn => {{
          btn.addEventListener('click', async () => {{
            const current = btn.dataset.current || '';
            const v = prompt(
              'Source URL for this recipe (leave empty to clear):',
              current
            );
            if (v === null) return;
            const newVal = v.trim();
            try {{
              const r = await fetch(
                '{PREFIX}/recipes/' + btn.dataset.recipeId,
                {{
                  method: 'PATCH',
                  headers: {{ 'Content-Type': 'application/json' }},
                  body: JSON.stringify({{ source_url: newVal || null }}),
                }}
              );
              if (!r.ok) throw new Error('HTTP ' + r.status);
              window.location.reload();
            }} catch (e) {{
              alert('Save failed: ' + e.message);
            }}
          }});
        }});

        // Complete Review — stamp reviewed_at=now, then jump to the
        // next un-reviewed recipe (server returns the target id) or
        // back to the library when the queue is empty. There are two
        // buttons (top + bottom of page); share the handler.
        document.querySelectorAll('.complete-review-btn').forEach(btn => {{
          btn.addEventListener('click', async () => {{
            const originalLabel = btn.textContent.trim();
            // Disable every twin so a slow network doesn't allow double-tap.
            const all = document.querySelectorAll('.complete-review-btn');
            all.forEach(b => {{ b.disabled = true; }});
            btn.textContent = 'Marking…';
            try {{
              const r = await fetch(
                '{PREFIX}/recipes/' + btn.dataset.recipeId + '/review/complete',
                {{ method: 'POST' }}
              );
              if (!r.ok) throw new Error('HTTP ' + r.status);
              const out = await r.json();
              window.location = out.next_unreviewed_id
                ? '{PREFIX}/preview/review/' + out.next_unreviewed_id
                : '{PREFIX}/preview/';
            }} catch (e) {{
              all.forEach(b => {{ b.disabled = false; }});
              btn.textContent = originalLabel;
              alert('Mark reviewed failed: ' + e.message);
            }}
          }});
        }});

        // Reject recipe — soft-delete via archived=true and jump to
        // the next recipe in the review queue.
        const rejectBtn = document.getElementById('reject-recipe');
        if (rejectBtn) {{
          rejectBtn.addEventListener('click', async () => {{
            const reason = prompt(
              'Why reject this recipe? (e.g. incomplete, duplicate, stub)',
              'incomplete'
            );
            if (reason === null) return;
            try {{
              const r = await fetch(
                '{PREFIX}/recipes/' + rejectBtn.dataset.recipeId,
                {{
                  method: 'PATCH',
                  headers: {{ 'Content-Type': 'application/json' }},
                  body: JSON.stringify({{
                    archived: true,
                    archived_reason: reason || 'rejected from review',
                  }}),
                }}
              );
              if (!r.ok) throw new Error('HTTP ' + r.status);
              window.location = rejectBtn.dataset.nextUrl;
            }} catch (e) {{
              alert('Reject failed: ' + e.message);
            }}
          }});
        }}

        // ↻ Reprocess section — toggle the inline textarea, POST on submit.
        document.querySelectorAll('.reprocess-toggle').forEach(btn => {{
          btn.addEventListener('click', () => {{
            const sid = btn.dataset.sectionId;
            const form = document.querySelector(
              'form.reprocess-form[data-section-id="' + sid + '"]'
            );
            if (form) form.classList.toggle('hidden');
          }});
        }});
        document.querySelectorAll('.reprocess-cancel').forEach(btn => {{
          btn.addEventListener('click', () => {{
            btn.closest('form').classList.add('hidden');
          }});
        }});
        document.querySelectorAll('.reprocess-go').forEach(btn => {{
          btn.addEventListener('click', async () => {{
            const form = btn.closest('form');
            const sid = form.dataset.sectionId;
            const text = form.querySelector('textarea').value;
            btn.disabled = true;
            btn.textContent = 'Reparsing…';
            try {{
              const r = await fetch(
                '{PREFIX}/recipes/section/' + sid + '/reprocess',
                {{
                  method: 'POST',
                  headers: {{'Content-Type': 'application/json'}},
                  body: JSON.stringify({{ text: text }}),
                }}
              );
              if (!r.ok) throw new Error('HTTP ' + r.status);
              window.location.reload();
            }} catch (e) {{
              btn.disabled = false;
              btn.textContent = 'Reparse → replace rows';
              alert('Reparse failed: ' + e.message);
            }}
          }});
        }});

        // ⋯ row menu — toggle the popup on click, close on outside click
        // OR after a menu-item is tapped (the menu items do their own work).
        document.querySelectorAll('.row-menu-btn').forEach(btn => {{
          btn.addEventListener('click', (e) => {{
            e.stopPropagation();
            const popup = btn.parentElement.querySelector('.row-menu-popup');
            const wasHidden = popup.classList.contains('hidden');
            // Close any other open popup
            document.querySelectorAll('.row-menu-popup').forEach(p => p.classList.add('hidden'));
            if (wasHidden) popup.classList.remove('hidden');
          }});
        }});
        document.addEventListener('click', (e) => {{
          if (!e.target.closest('.row-menu')) {{
            document.querySelectorAll('.row-menu-popup').forEach(p => p.classList.add('hidden'));
          }}
        }});

        // Rescue a "prep tail not captured" warning — one click stashes
        // the tail text into prep_note or alternatives_note. Reload so
        // the warning disappears and the new field renders correctly.
        document.querySelectorAll('.tail-rescue').forEach(btn => {{
          btn.addEventListener('click', async (e) => {{
            e.stopPropagation();
            const field = btn.dataset.field;
            const value = btn.dataset.value;
            btn.disabled = true;
            try {{
              const r = await fetch('{PREFIX}' + btn.dataset.endpoint, {{
                method: 'PATCH',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ [field]: value }}),
              }});
              if (!r.ok) throw new Error('HTTP ' + r.status);
              window.location.reload();
            }} catch (err) {{
              btn.disabled = false;
              alert('Save failed: ' + err.message);
            }}
          }});
        }});

        // Open the row's inline edit form, populated for the chosen
        // field (size_descriptor / prep_note). Reuses one form per
        // row — label + input value get set when the trigger fires.
        document.querySelectorAll('.open-row-edit').forEach(btn => {{
          btn.addEventListener('click', () => {{
            const id = btn.dataset.ingredientId;
            const form = document.querySelector(
              '.row-edit-form[data-ingredient-id="' + id + '"]'
            );
            if (!form) return;
            // Close any other open form first.
            document.querySelectorAll('.row-edit-form').forEach(f => {{
              if (f !== form) f.classList.add('hidden');
            }});
            form.dataset.field = btn.dataset.field;
            form.querySelector('.row-edit-label').textContent = btn.dataset.label;
            const input = form.querySelector('.row-edit-input');
            input.value = btn.dataset.current || '';
            input.placeholder = '(leave empty to clear)';
            form.classList.remove('hidden');
            input.focus();
          }});
        }});
        document.querySelectorAll('.row-edit-cancel').forEach(btn => {{
          btn.addEventListener('click', () => {{
            btn.closest('.row-edit-form').classList.add('hidden');
          }});
        }});
        document.querySelectorAll('.row-edit-save').forEach(btn => {{
          btn.addEventListener('click', async () => {{
            const form = btn.closest('.row-edit-form');
            const field = form.dataset.field;
            const v = form.querySelector('.row-edit-input').value.trim();
            btn.disabled = true;
            btn.textContent = 'Saving…';
            try {{
              const r = await fetch('{PREFIX}' + form.dataset.endpoint, {{
                method: 'PATCH',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ [field]: v || null }}),
              }});
              if (!r.ok) throw new Error('HTTP ' + r.status);
              window.location.reload();
            }} catch (e) {{
              btn.disabled = false;
              btn.textContent = 'Save';
              alert('Save failed: ' + e.message);
            }}
          }});
        }});
        // Enter submits the form input, Escape cancels — matches the
        // inline-edit affordances elsewhere.
        document.querySelectorAll('.row-edit-input').forEach(input => {{
          input.addEventListener('keydown', (e) => {{
            if (e.key === 'Enter') {{
              e.preventDefault();
              input.closest('.row-edit-form').querySelector('.row-edit-save').click();
            }} else if (e.key === 'Escape') {{
              input.closest('.row-edit-form').classList.add('hidden');
            }}
          }});
        }});

        // Delete row — confirm, DELETE, splice out of the table.
        document.querySelectorAll('.delete-row').forEach(btn => {{
          btn.addEventListener('click', async () => {{
            if (!confirm('Delete this ingredient row?')) return;
            btn.disabled = true;
            btn.textContent = '…';
            try {{
              const r = await fetch(
                '{PREFIX}/recipes/ingredient/' + btn.dataset.ingredientId,
                {{ method: 'DELETE' }}
              );
              if (!r.ok) throw new Error('HTTP ' + r.status);
              const row = btn.closest('tr');
              if (row) row.remove();
            }} catch (e) {{
              btn.disabled = false;
              btn.textContent = '✕ delete';
              alert('Delete failed: ' + e.message);
            }}
          }});
        }});

        // Split row — POST to /recipes/ingredient/{id}/split which
        // clones the row inline. Reload after to pick up the new row
        // (cheaper than splicing the DOM by hand).
        document.querySelectorAll('.split-row').forEach(btn => {{
          btn.addEventListener('click', async () => {{
            btn.disabled = true;
            btn.textContent = '…';
            try {{
              const r = await fetch(
                '{PREFIX}/recipes/ingredient/' + btn.dataset.ingredientId + '/split',
                {{ method: 'POST' }}
              );
              if (!r.ok) throw new Error('HTTP ' + r.status);
              window.location.reload();
            }} catch (e) {{
              btn.disabled = false;
              btn.textContent = '✂ split';
              alert('Split failed: ' + e.message);
            }}
          }});
        }});

        // Pill click — quick "unmark optional". Same PATCH as the menu
        // toggle, just inline so the user doesn't have to dig.
        document.querySelectorAll('.optional-pill').forEach(pill => {{
          pill.addEventListener('click', async (e) => {{
            e.stopPropagation();
            pill.disabled = true;
            try {{
              const r = await fetch('{PREFIX}' + pill.dataset.endpoint, {{
                method: 'PATCH',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ optional: false }}),
              }});
              if (!r.ok) throw new Error('HTTP ' + r.status);
              pill.classList.add('hidden');
              // Sync the dropdown toggle so its label + state stay correct.
              const row = pill.closest('tr');
              const toggle = row && row.querySelector('.optional-toggle');
              if (toggle) {{
                toggle.dataset.state = '0';
                toggle.textContent = 'Mark as optional';
              }}
            }} catch (err) {{
              alert('Save failed: ' + err.message);
            }} finally {{
              pill.disabled = false;
            }}
          }});
        }});

        // Optional toggle (inside the ⋮ menu) — PATCH, flip the row's
        // amber pill visibility, update the menu label, close popup.
        document.querySelectorAll('.optional-toggle').forEach(btn => {{
          btn.addEventListener('click', async () => {{
            const currentlyOptional = btn.dataset.state === '1';
            const newVal = !currentlyOptional;
            btn.disabled = true;
            try {{
              const r = await fetch('{PREFIX}' + btn.dataset.endpoint, {{
                method: 'PATCH',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ optional: newVal }}),
              }});
              if (!r.ok) throw new Error('HTTP ' + r.status);
              btn.dataset.state = newVal ? '1' : '0';
              btn.textContent = newVal ? 'Mark as required' : 'Mark as optional';
              const row = btn.closest('tr');
              const pill = row && row.querySelector('.optional-pill');
              if (pill) pill.classList.toggle('hidden', !newVal);
              btn.closest('.row-menu-popup').classList.add('hidden');
              btn.dispatchEvent(new CustomEvent('valueChanged', {{ bubbles: true }}));
            }} catch (e) {{
              alert('Save failed: ' + e.message);
            }} finally {{
              btn.disabled = false;
            }}
          }});
        }});
      </script>
    """
    return HTMLResponse(_shell(f"Review · {name}", body))


def _render_inline_trace(t) -> str:
    """Compact trace summary for an in-row expansion. Mirrors the cards
    on /preview/parse-trace but tighter."""
    bits = []
    bits.append(
        f"<div><span class='text-stone-500'>chosen name:</span> "
        f"<span class='font-mono'>{html.escape(t.chosen_name)}</span> "
        f"<span class='text-stone-400'>(via {t.name_chosen_via})</span></div>"
    )
    if t.strangetom_foundation_food:
        bits.append(
            f"<div><span class='text-stone-500'>FDC match:</span> "
            f"<span class='font-mono'>{html.escape(t.strangetom_foundation_food)}</span></div>"
        )
    bits.append(
        f"<div><span class='text-stone-500'>canonical input:</span> "
        f"<span class='font-mono'>{html.escape(t.canonical_input)}</span> "
        f"<span class='text-stone-400'>(alias {'hit' if t.canonical_alias_existed else 'miss'}"
        f"{f', source={t.canonical_alias_source}' if t.canonical_alias_source else ''})</span></div>"
    )
    if t.strangetom_preparation:
        bits.append(
            f"<div><span class='text-stone-500'>prep tagged:</span> "
            f"<span class='font-mono'>{html.escape(t.strangetom_preparation)}</span></div>"
        )
    if t.parser_cache_hit:
        bits.append(
            f"<div class='text-emerald-700'>live cache hit at v{html.escape(t.parser_cache_version or '')}</div>"
        )
    elif t.parser_cache_version:
        bits.append(
            f"<div class='text-stone-500'>cache stale at v{html.escape(t.parser_cache_version)}</div>"
        )
    bits.append(
        f"<div class='mt-1'><a href='{PREFIX}/preview/parse-trace?blob={html.escape(t.source_line)}' "
        f"class='text-amber-700 hover:underline'>full trace →</a></div>"
    )
    return "".join(bits)


_DEFAULT_TRACE_BLOB = """1 12 ounce jar roasted red bell peppers, drained and roughly chopped
1/2 cup mini bocconcini
1/4 teaspoon crushed red pepper
1 15 ounce can garbanzo beans, drained and rinsed
1 Tablespoon red wine vinegar
Salt and pepper to taste
1 inch ginger, grated"""


@router.get("/preview/parse-trace", response_class=HTMLResponse)
def parse_trace_page(
    blob: str | None = None, db: Session = Depends(get_session),
) -> HTMLResponse:
    """Debug view: paste an ingredients blob, see every intermediate
    state the parser produces. Read-only — doesn't write to ParserCache
    or IngredientAlias."""
    from mise.services.parse_trace import content_words, trace_blob

    blob_text = blob if blob is not None else _DEFAULT_TRACE_BLOB
    sections = trace_blob(blob_text, db) if blob_text.strip() else []

    # Use the shared content_words helper from parse_trace.
    def _src_content_words(text: str) -> set[str]:
        return content_words(text)

    def _final_content_words(t) -> set[str]:
        return content_words(t.final_canonical_name.replace("_", " "))

    def _conf_chip(conf: float | None) -> str:
        """Small chip showing strangetom's confidence in a field.
        Amber below 0.6, gray otherwise. Empty when None."""
        if conf is None:
            return ""
        c = float(conf)
        cls = ("bg-amber-100 text-amber-800" if c < 0.6
               else "bg-stone-100 text-stone-600")
        return (
            f"<span class='inline-block ml-2 px-1.5 py-0 text-xs rounded {cls}'>"
            f"conf {c:.2f}</span>"
        )

    def _option(label: str, value, picked: bool, dim: bool = False) -> str:
        """Render one row of a decision: ✓ if picked, · if not. Dim
        rows that didn't apply at all (e.g. cache miss when no alias)."""
        if value is None or value == "":
            v_html = "<span class='text-stone-400 italic'>—</span>"
        else:
            v_html = f"<span class='font-mono text-sm'>{_h(str(value))}</span>"
        marker_cls = "text-amber-700 font-bold" if picked else "text-stone-300"
        marker = "✓" if picked else "·"
        bg = "bg-amber-50" if picked else ""
        text_cls = "text-stone-400" if dim else "text-stone-700"
        return (
            f"<div class='flex items-baseline gap-2 px-2 py-1 rounded {bg}'>"
            f"  <span class='{marker_cls} w-4'>{marker}</span>"
            f"  <span class='text-xs {text_cls} w-44 shrink-0'>{_h(label)}</span>"
            f"  <div class='flex-1 min-w-0 break-all'>{v_html}</div>"
            f"</div>"
        )

    def _decision_card(title: str, options_html: str, note: str = "", warn: bool = False) -> str:
        border = "border-amber-300" if warn else "border-stone-200"
        note_html = (
            f"<div class='text-xs text-amber-700 mt-1 px-2'>⚠ {_h(note)}</div>"
            if note and warn else (
                f"<div class='text-xs text-stone-500 mt-1 px-2'>{_h(note)}</div>"
                if note else ""
            )
        )
        return (
            f"<div class='border {border} rounded mb-2'>"
            f"  <div class='text-xs uppercase tracking-wide text-stone-600 font-semibold "
            f"             px-2 pt-2 pb-1'>{_h(title)}</div>"
            f"  <div class='pb-2'>{options_html}</div>"
            f"  {note_html}"
            f"</div>"
        )

    def _render_trace(t) -> str:
        src_words = _src_content_words(t.source_line)
        final_words = _final_content_words(t)
        lost = src_words - final_words
        lost_str = ", ".join(sorted(lost)) if lost else ""

        # Decision 1: name selection
        st_name_val = (
            f"{t.strangetom_name}{_conf_chip(t.strangetom_name_confidence)}"
            if t.strangetom_name else None
        )
        ff_val = (
            f"{t.strangetom_foundation_food}{_conf_chip(t.strangetom_ff_confidence)}"
            if t.strangetom_foundation_food else None
        )
        # _option escapes the value, but our chips contain HTML — render
        # via a small custom row when chips are present.
        def _opt_html(label, html_value, picked, dim=False):
            marker_cls = "text-amber-700 font-bold" if picked else "text-stone-300"
            marker = "✓" if picked else "·"
            bg = "bg-amber-50" if picked else ""
            text_cls = "text-stone-400" if dim else "text-stone-700"
            return (
                f"<div class='flex items-baseline gap-2 px-2 py-1 rounded {bg}'>"
                f"  <span class='{marker_cls} w-4'>{marker}</span>"
                f"  <span class='text-xs {text_cls} w-44 shrink-0'>{_h(label)}</span>"
                f"  <div class='flex-1 min-w-0 break-all'>{html_value}</div>"
                f"</div>"
            )

        def _wrap(s, is_mono=True):
            if s is None:
                return "<span class='text-stone-400 italic'>—</span>"
            cls = "font-mono text-sm" if is_mono else "text-sm"
            return f"<span class='{cls}'>{s}</span>"

        name_opts = (
            _opt_html("derive_from_source",
                     _wrap(_h(t.derived_name_from_source) if t.derived_name_from_source else None),
                     t.name_chosen_via == "derive",
                     dim=t.derived_name_from_source is None) +
            _opt_html("strangetom.name",
                     _wrap(_h(t.strangetom_name) + (_conf_chip(t.strangetom_name_confidence) if t.strangetom_name else "") if t.strangetom_name else None),
                     t.name_chosen_via == "strangetom",
                     dim=t.strangetom_name is None) +
            _opt_html("foundation_food",
                     _wrap(_h(t.strangetom_foundation_food) + (_conf_chip(t.strangetom_ff_confidence) if t.strangetom_foundation_food else "") if t.strangetom_foundation_food else None),
                     t.name_chosen_via == "ff_text",
                     dim=t.strangetom_foundation_food is None) +
            _option("(fallback) unknown", "unknown",
                    t.name_chosen_via == "unknown", dim=True)
        )
        # Warn if strangetom dropped words and the picked source did too
        chosen_words = _src_content_words(t.chosen_name)
        name_warn_words = src_words - chosen_words
        name_card = _decision_card(
            "name selection",
            name_opts,
            note=(f"chosen name dropped: {', '.join(sorted(name_warn_words))}"
                  if name_warn_words else ""),
            warn=bool(name_warn_words),
        )

        # Decision 2: canonical resolver input
        canon_opts = (
            _option("foundation_food (preferred)", t.strangetom_foundation_food,
                    t.canonical_input == t.strangetom_foundation_food,
                    dim=not t.strangetom_foundation_food) +
            _option("chosen_name (fallback)", t.chosen_name,
                    t.canonical_input == t.chosen_name)
        )
        canon_lookup = (
            _option(f"alias hit (source={t.canonical_alias_source})",
                    t.canonical_name, True) if t.canonical_alias_existed
            else _option("alias miss → snake_case", t.canonical_name, True)
        )
        canon_card = _decision_card(
            "canonical resolution",
            canon_opts +
            f"<div class='border-t mt-1 pt-1 px-2 text-xs text-stone-500'>then →</div>" +
            canon_lookup,
            note=(
                f"resolver used FDC name (loses recipe-specific modifiers)"
                if t.canonical_input == t.strangetom_foundation_food
                and t.strangetom_foundation_food else ""
            ),
            warn=(t.canonical_input == t.strangetom_foundation_food
                  and t.strangetom_foundation_food is not None
                  and bool(name_warn_words)),
        )

        # Decision 3: qty/unit transformation
        qty_opts = ""
        if t.qty_from_leading_fallback:
            qty_opts += _option(
                "_extract_leading_qty (regex fallback)",
                f"{t.qty_raw} (strangetom missed it)", True,
            )
        qty_opts += (
            _option(f"strangetom raw", f"{t.qty_raw} {t.unit_raw}", False) +
            _option(f"after Pint canonical", f"{t.qty_after_canonical} {t.unit_after_canonical}",
                    t.qty_after_canonical != t.qty_after_preference) +
            _option(f"after preference ({t.preference_rule})",
                    f"{t.qty_after_preference} {t.unit_after_preference}", True)
        )
        qty_card = _decision_card(
            "qty + unit",
            qty_opts,
            note=t.size_leak_detail if t.size_likely_leaked else "",
            warn=t.size_likely_leaked,
        )

        # Decision 3.25: LLM parse fallback (deterministic give-up).
        # Distinct from LLM verify — fires when strangetom + our regex
        # fallbacks can't structure the line at all. Only show this
        # card when something actually triggered it.
        fb_card = ""
        if t.llm_fallback_triggered:
            fb_card = _decision_card(
                "LLM parse fallback (deterministic give-up)",
                _option("triggered?", "yes", True) +
                _option("reason", t.llm_fallback_reason or "—", True) +
                _option("would call API?",
                        "yes (ANTHROPIC_API_KEY set)" if t.llm_fallback_called
                        else "no (no API key — falls through to parse_failed)",
                        True),
                note=(
                    "Different from LLM verify (which refines canonical/category). "
                    "This fallback REPLACES qty/unit/name when deterministic gives up."
                ),
                warn=True,
            )

        # Decision 3.5: prep / size / comment — these flow through
        # strangetom and land on the Ingredient row unchanged. Showing
        # them so the user can see what got tagged as prep vs name.
        prep_opts = (
            _option("preparation", t.strangetom_preparation,
                    bool(t.strangetom_preparation),
                    dim=not t.strangetom_preparation) +
            _option("size", t.strangetom_size,
                    bool(t.strangetom_size),
                    dim=not t.strangetom_size) +
            _option("comment", t.strangetom_comment,
                    bool(t.strangetom_comment),
                    dim=not t.strangetom_comment)
        )
        prep_card = _decision_card(
            "prep / size / comment (strangetom-tagged)",
            prep_opts,
            note=("'optional' / 'to taste' / 'for garnish' in comment "
                  "flips the final row's optional flag"
                  if t.strangetom_comment and any(
                      kw in t.strangetom_comment.lower()
                      for kw in ("optional", "to taste", "garnish", "as needed")
                  ) else ""),
        )

        # Decision 4: category
        cat_card = _decision_card(
            "category",
            _option(f"prefix match", t.category_resolved, True),
            note="'Other' = no prefix match in CATEGORY_BY_PREFIX (LLM verify will be asked to refine)" if t.category_resolved == "Other" else "",
            warn=t.category_resolved == "Other",
        )

        # Decision 5: LLM verify
        verify_rows = ""
        verify_rows += _option(
            f"fire? mode=first-seen",
            "yes" if t.llm_verify_fired else f"no — {t.llm_verify_skip_reason}",
            True,
        )
        if t.llm_verify_raw_output:
            llm_canon = t.llm_verify_raw_output.get("canonical_name", "—")
            verify_rows += _option("LLM canonical", llm_canon, True)
            verify_rows += _option("grounded against source?",
                                   "✓ yes" if t.llm_verify_grounded else "✗ no — REJECTED",
                                   True)
            verify_rows += _option("applied?",
                                   "yes" if t.llm_verify_applied else "no (fell back to deterministic)",
                                   True)
        verify_card = _decision_card(
            "LLM verify",
            verify_rows,
            note=(
                "grounding check passed but lost source words — false negative"
                if (t.llm_verify_grounded and t.llm_verify_applied and
                    t.llm_verify_raw_output and lost) else ""
            ),
            warn=(
                t.llm_verify_grounded and t.llm_verify_applied and
                t.llm_verify_raw_output is not None and bool(lost)
            ),
        )

        # Final summary
        prep_tail = ""
        if t.final_prep_note or t.final_size_descriptor:
            bits = []
            if t.final_size_descriptor:
                bits.append(f"size: {t.final_size_descriptor}")
            if t.final_prep_note:
                bits.append(f"prep: {t.final_prep_note}")
            prep_tail = (
                f"<div class='text-xs text-stone-600 mt-1 font-mono'>"
                f"  {_h(' · '.join(bits))}"
                f"</div>"
            )
        final_html = (
            f"<div class='bg-green-50 border border-green-200 rounded p-2'>"
            f"  <div class='text-xs uppercase tracking-wide text-emerald-700 font-semibold mb-1'>final stored</div>"
            f"  <div class='font-mono text-sm'>"
            f"    {t.final_qty} {_h(t.final_unit)} · "
            f"    <span class='font-semibold'>{_h(t.final_display_name)}</span>"
            f"    <span class='text-stone-500'>(canonical: {_h(t.final_canonical_name)} · {_h(t.final_category)})</span>"
            f"  </div>"
            f"  {prep_tail}"
            f"</div>"
        )

        # Header banner — overall status + lost words
        lost_banner = ""
        if lost:
            lost_banner = (
                f"<div class='bg-amber-50 border border-amber-300 rounded px-3 py-2 mb-3 text-sm'>"
                f"  <span class='font-semibold text-amber-800'>⚠ Information lost:</span> "
                f"  <span class='font-mono text-amber-700'>{_h(lost_str)}</span>"
                f"</div>"
            )

        cache_chip = ""
        if t.parser_cache_hit:
            cache_chip = (
                f"<span class='inline-block ml-2 px-2 py-0.5 text-xs rounded "
                f"           bg-green-100 text-green-800'>"
                f"  cache hit · v{_h(t.parser_cache_version or '')}"
                f"</span>"
            )
        elif t.parser_cache_version:
            cache_chip = (
                f"<span class='inline-block ml-2 px-2 py-0.5 text-xs rounded "
                f"           bg-stone-100 text-stone-600'>"
                f"  cache stale (was v{_h(t.parser_cache_version)})"
                f"</span>"
            )

        return (
            f"<div class='bg-white border rounded-lg p-4 mb-6 shadow-sm'>"
            f"  <div class='text-xs uppercase tracking-wide text-stone-500 mb-1'>source{cache_chip}</div>"
            f"  <div class='font-mono text-sm mb-3 break-all'>{_h(t.source_line)}</div>"
            f"  {lost_banner}"
            f"  <div class='pl-1 border-l-2 border-stone-200'>"
            f"    {name_card}"
            f"    {qty_card}"
            f"    {fb_card}"
            f"    {prep_card}"
            f"    {canon_card}"
            f"    {cat_card}"
            f"    {verify_card}"
            f"  </div>"
            f"  {final_html}"
            f"</div>"
        )

    sections_html = ""
    for section_name, traces_per_line in sections:
        if section_name:
            sections_html += f"<h2 class='text-lg font-semibold mt-6 mb-2'>{_h(section_name)}</h2>"
        for line_traces in traces_per_line:
            # line_traces is a list — usually 1, more for compound lines.
            if len(line_traces) == 1:
                sections_html += _render_trace(line_traces[0])
            else:
                # Compound: shared source line + N stacked traces with
                # a header for each.
                src_line = line_traces[0].source_line
                sections_html += (
                    f"<div class='bg-amber-50 border border-amber-300 rounded px-3 py-2 mb-2 text-sm'>"
                    f"  <span class='font-semibold text-amber-800'>Compound line:</span> "
                    f"  <span class='font-mono'>{_h(src_line)}</span> "
                    f"  <span class='text-stone-600'>→ {len(line_traces)} ingredients</span>"
                    f"</div>"
                )
                for t in line_traces:
                    sections_html += (
                        f"<div class='text-xs text-amber-700 ml-2 mb-1'>"
                        f"  ↳ #{t.compound_index + 1} of {t.compound_total}"
                        f"</div>"
                    ) + _render_trace(t)

    body = f"""
      <div class="mb-2">
        <a href="{PREFIX}/preview/" class="text-sm text-stone-500 hover:text-stone-900">← Library</a>
      </div>
      <h1 class="text-2xl font-bold mb-3">Parse trace</h1>
      <p class="text-sm text-stone-500 mb-4">
        Read-only debug view of the ingredient parser. Shows every
        intermediate value at every step. No writes — does not
        pollute ParserCache or IngredientAlias.
      </p>
      <form method="get" action="{PREFIX}/preview/parse-trace" class="mb-6">
        <textarea name="blob" rows="8"
                  class="w-full border rounded px-3 py-2 font-mono text-sm">{_h(blob_text)}</textarea>
        <button type="submit" class="mt-2 bg-stone-900 hover:bg-stone-700 text-white text-sm font-semibold px-4 py-2 rounded">
          Trace
        </button>
      </form>
      {sections_html}
    """
    return HTMLResponse(_shell("Parse trace", body))


@router.get("/preview/new", response_class=HTMLResponse)
def new_recipe_form() -> HTMLResponse:
    """Migrated to /app/add."""
    from fastapi.responses import RedirectResponse as _RR
    return _RR(url=f"{PREFIX}/app/add", status_code=307)


@router.get("/preview/plan", response_class=HTMLResponse)
def preview_plan(
    start: str | None = None, days: int = 7,
    placing: int | None = None,
    move_date: str | None = None,
    move_meal: str | None = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """Migrated to /app/plan."""
    from fastapi.responses import RedirectResponse as _RR
    qs = f"?start={start}" if start else ""
    return _RR(url=f"{PREFIX}/app/plan{qs}", status_code=307)


def _add_to_plan_modal_html(r: Recipe, occupied: dict[tuple[str, str], str]) -> str:
    """Tap-to-pick slot grid — household ask: pick a slot, not fill a form.
    Shows next 7 days × {Lunch, Dinner}. Occupied cells are dimmed +
    labeled with the existing recipe. Tap an empty cell → POST place →
    close + land on plan. Single +/- stepper for servings (defaults to
    recipe.servings)."""
    from datetime import date as _d, timedelta as _td
    today = _d.today()
    default_servings = r.servings or 4

    day_rows = []
    for i in range(7):
        d = today + _td(days=i)
        d_iso = d.isoformat()
        if i == 0:
            label = "Today"
            sub = d.strftime("%a %b %-d")
        elif i == 1:
            label = "Tomorrow"
            sub = d.strftime("%a %b %-d")
        else:
            label = d.strftime("%A")
            sub = d.strftime("%b %-d")
        cells = []
        for meal in ("lunch", "dinner"):
            taken = occupied.get((d_iso, meal))
            if taken:
                cells.append(
                    f"<button type='button' class='atp-slot atp-taken' disabled>"
                    f"<span class='atp-meal-label'>{meal.title()}</span>"
                    f"<span class='atp-taken-name'>{_h(taken)}</span>"
                    f"</button>"
                )
            else:
                cells.append(
                    f"<button type='button' class='atp-slot atp-open' "
                    f"data-date='{d_iso}' data-meal='{meal}'>"
                    f"<span class='atp-meal-label'>{meal.title()}</span>"
                    f"<span class='atp-meal-sub'>tap to add</span>"
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
<div id="atp-modal" class="atp-modal" role="dialog" aria-modal="true" aria-labelledby="atp-title">
  <div class="atp-sheet">
    <div class="flex items-baseline justify-between mb-2">
      <h2 id="atp-title" class="serif text-xl font-medium tracking-tight">Add to plan</h2>
      <button type="button" id="atp-close" aria-label="Close"
              class="text-stone-400 hover:text-stone-900 text-3xl leading-none p-2 -mt-2 -mr-2">×</button>
    </div>
    <div class="flex items-center justify-between gap-3 mb-3">
      <span class="serif italic font-medium text-stone-900 truncate">{_h(r.display_title())}</span>
      <div class="inline-flex items-center border border-stone-200 rounded-lg overflow-hidden bg-white shrink-0">
        <button type="button" id="atp-srv-dec" class="px-3 py-1.5 text-stone-700 hover:bg-stone-100 border-r border-stone-200">−</button>
        <span class="px-3 py-1.5 min-w-[3rem] text-center text-sm">
          <span id="atp-srv-val" class="font-semibold tabular-nums">{default_servings}</span>
          <span class="text-stone-400"> srv</span>
        </span>
        <button type="button" id="atp-srv-inc" class="px-3 py-1.5 text-stone-700 hover:bg-stone-100 border-l border-stone-200">+</button>
      </div>
    </div>
    <div class="text-[11px] font-semibold text-stone-700 uppercase tracking-wider mb-2">Pick a slot</div>
    <div class="atp-days">
      {''.join(day_rows)}
    </div>
    <p id="atp-status" class="text-sm text-center text-stone-500 mt-3 min-h-[1.25em]"></p>
  </div>
</div>
<style>
  #atp-modal {{
    /* z-100 — above the feedback FAB (z-90). FAB additionally hidden
       via JS when the modal opens; :has() isn't reliable on older iOS. */
    display: none; position: fixed; inset: 0; z-index: 100;
    background: rgba(28,25,23,0.45);
    align-items: flex-end; justify-content: center;
  }}
  #atp-modal.is-open {{ display: flex; }}
  #atp-modal .atp-sheet {{
    background: white;
    border-radius: 1.25rem 1.25rem 0 0;
    width: 100%; max-width: 28rem; max-height: 90vh; overflow-y: auto;
    padding: 1.25rem 1.25rem calc(1.5rem + env(safe-area-inset-bottom));
    box-shadow: 0 -16px 48px -8px rgba(0,0,0,0.22);
  }}
  #atp-modal .atp-days {{ display: flex; flex-direction: column; gap: 0.5rem; }}
  #atp-modal .atp-day-row {{
    display: flex; align-items: stretch; gap: 0.6rem;
  }}
  #atp-modal .atp-day-label {{
    width: 5.5rem; flex-shrink: 0; display: flex; flex-direction: column;
    justify-content: center;
  }}
  #atp-modal .atp-day-name {{ font-family: 'Fraunces', Georgia, serif; font-weight: 500; font-size: 0.95rem; line-height: 1.1; }}
  #atp-modal .atp-day-sub {{ font-size: 0.7rem; color: #a8a29e; margin-top: 0.05rem; }}
  #atp-modal .atp-day-slots {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.4rem; flex: 1; min-width: 0; }}
  #atp-modal .atp-slot {{
    text-align: left; padding: 0.5rem 0.65rem; border-radius: 0.55rem;
    border: 1px solid #e7e5e4; background: white; cursor: pointer;
    display: flex; flex-direction: column; gap: 0.05rem;
    transition: background 0.12s, border-color 0.12s;
    font-size: 14px;
  }}
  #atp-modal .atp-slot.atp-open:hover,
  #atp-modal .atp-slot.atp-open:active {{
    background: #fffbeb; border-color: #fcd34d;
  }}
  #atp-modal .atp-meal-label {{ font-weight: 600; color: #1c1917; }}
  #atp-modal .atp-meal-sub {{ font-size: 0.65rem; color: #a8a29e; }}
  #atp-modal .atp-taken {{ background: #f5f5f4; cursor: not-allowed; opacity: 0.65; }}
  #atp-modal .atp-taken .atp-meal-label {{ color: #78716c; }}
  #atp-modal .atp-taken-name {{
    font-family: 'Fraunces', Georgia, serif; font-style: italic;
    font-size: 0.75rem; color: #57534e; line-height: 1.15;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }}
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
  const fab = document.getElementById('fb-root');
  let servings = {default_servings};

  function show() {{
    modal.classList.add('is-open');
    if (fab) fab.style.display = 'none';     // JS fallback for older iOS
    status.textContent = '';
  }}
  function hide() {{
    modal.classList.remove('is-open');
    if (fab) fab.style.display = '';
  }}

  openBtn.addEventListener('click', show);
  closeBtn.addEventListener('click', hide);
  modal.addEventListener('click', (e) => {{ if (e.target === modal) hide(); }});
  document.addEventListener('keydown', (e) => {{ if (e.key === 'Escape') hide(); }});

  function setSrv(v) {{
    servings = Math.max(1, Math.min(40, v));
    srvVal.textContent = servings;
  }}
  srvDec.addEventListener('click', () => setSrv(servings - 1));
  srvInc.addEventListener('click', () => setSrv(servings + 1));

  document.querySelectorAll('#atp-modal .atp-slot.atp-open').forEach(btn => {{
    btn.addEventListener('click', async () => {{
      const date = btn.dataset.date;
      const meal = btn.dataset.meal;
      btn.disabled = true;
      status.textContent = 'Adding…';
      try {{
        const r = await fetch('{PREFIX}/plan/place', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ recipe_id: RECIPE_ID, date, meal, servings }}),
        }});
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
        status.textContent = 'Added!';
        setTimeout(() => {{ location.href = '{PREFIX}/preview/plan?start=' + date; }}, 500);
      }} catch (e) {{
        status.textContent = 'Failed: ' + e.message;
        btn.disabled = false;
      }}
    }});
  }});
}})();
</script>
"""


@router.get("/preview/{recipe_id}", response_class=HTMLResponse)
def preview_recipe(recipe_id: int, db: Session = Depends(get_session)) -> HTMLResponse:
    """Migrated to /app/recipe/{id}. Redirect for legacy URLs."""
    from fastapi.responses import RedirectResponse as _RR
    return _RR(url=f"{PREFIX}/app/recipe/{recipe_id}", status_code=307)


@router.get("/recipes/{recipe_id}/json")
def recipe_json(recipe_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    """Raw recipe payload — same shape as the on-disk recipes-export/
    files. Mirroring the export schema means anything that consumes the
    git-tracked JSON can also pull live data."""
    r = db.get(Recipe, recipe_id)
    if r is None:
        raise HTTPException(status_code=404, detail="recipe not found")
    return JSONResponse(_serialise_recipe(r, db))


@router.get("/shopping", response_class=HTMLResponse)
def shopping_page(ids: str = "", db: Session = Depends(get_session)) -> HTMLResponse:
    """Render the aggregated shopping list. `ids` is a comma-separated
    list of recipe ids (set by the library page's checkbox+button)."""
    from collections import defaultdict
    from mise.services.shopping_aggregator import (
        ShoppingItem,
        aggregate as _aggregate,
    )

    try:
        recipe_ids = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        recipe_ids = []

    if not recipe_ids:
        body = f"""
          <div class="mb-4">
            <a href="{PREFIX}/preview/" class="text-sm text-stone-500 hover:text-stone-900">← Library</a>
          </div>
          <p class="text-stone-500">Pick some recipes from the library first.</p>
        """
        return HTMLResponse(_shell("Shopping list", body))

    items = _aggregate(recipe_ids, db)
    recipes = db.exec(
        select(Recipe).where(Recipe.id.in_(recipe_ids))
    ).all()

    by_category: dict[str, list] = defaultdict(list)
    for it in items:
        by_category[it.category].append(it)

    # Render each contributing recipe as a chip linking back to its
    # detail page. Easier to scan than a comma-joined run-on.
    recipe_chips = "".join(
        f"""<a href="{PREFIX}/preview/{r.id}"
           class="inline-block bg-stone-100 hover:bg-stone-200 text-sm
                  text-stone-700 px-3 py-1 rounded-full">{_h(r.display_title())}</a>"""
        for r in recipes
    )

    # Stable per-item key for checkbox state. (canonical, unit) is what
    # the aggregator deduplicates on, so it's the natural identity.
    def _row_key(it: ShoppingItem) -> str:
        return f"{it.canonical_name}|{it.unit}"

    # Build a sub-line showing the original contributing rows so the
    # math is auditable. Only render when >1 source — single-source
    # rows are trivially correct and adding the line just adds noise.
    def _sources_line(it: ShoppingItem) -> str:
        if it.source_count <= 1:
            return ""
        parts = [
            f"{_fmt_qty(s.qty)} {_h(s.unit)} ({_h(s.recipe_name)})"
            for s in it.sources
        ]
        return (
            f"<div class='text-xs text-stone-400 pl-8 -mt-1 mb-1'>"
            f"  ← {' + '.join(parts)}"
            f"</div>"
        )

    sections = []
    for cat, cat_items in by_category.items():
        rows_html = "".join(
            # Whole row is a <label> so tapping anywhere toggles. The
            # checkbox is visually small but the tap target is the full
            # row — `p-2 -mx-2` reclaims the row padding so visual
            # spacing matches the unchecked design.
            f"""<li>
                <label class="shop-row flex items-baseline gap-3 py-2 px-2 -mx-2
                              cursor-pointer hover:bg-stone-50 rounded select-none"
                       data-key="{_h(_row_key(it))}">
                  <input type="checkbox" class="shop-check h-5 w-5 accent-amber-700 cursor-pointer flex-shrink-0">
                  <span class="row-qty text-stone-500 text-sm whitespace-nowrap w-24">
                    {_fmt_qty(it.qty)} {_h(it.unit)}
                  </span>
                  <span class="row-name flex-1">{_h(it.display_name)}</span>
                  {f'<span class="text-xs text-stone-400">×{it.source_count}</span>' if it.source_count > 1 else ''}
                </label>
                {_sources_line(it)}
              </li>"""
            for it in cat_items
        )
        sections.append(f"""
          <div class="mb-6">
            <h3 class="serif text-xl font-medium tracking-tight mb-1">{_h(cat)}</h3>
            <ul>{rows_html}</ul>
          </div>
        """)

    # Stable storage key — same recipe set ⇒ same list ⇒ same checks.
    # When the user builds a different list (different ids), they get
    # a fresh slate, which matches "this is one shopping trip" semantics.
    storage_key = "mise:shopping:" + ",".join(str(i) for i in sorted(recipe_ids))

    body = f"""
      <style>
        /* Checked rows: greyed + struck through. Checked rows also
           reorder to the bottom of their section (see JS), so the
           active list always stays at the top. */
        .shop-row.checked .row-qty,
        .shop-row.checked .row-name {{
          color: #9ca3af;
          text-decoration: line-through;
        }}
        .crossed-divider {{
          font-size: 0.7rem;
          color: #9ca3af;
          text-transform: uppercase;
          letter-spacing: 0.06em;
          padding-top: 0.6rem;
          margin-top: 0.4rem;
          border-top: 1px dashed #e5e7eb;
          list-style: none;
        }}
        /* Quick-add extras section — render distinct from aggregator
           rows so they're spottable as ad-hoc additions. */
        .extra-row {{ font-style: italic; }}
        .extra-del {{ color: #ef4444; cursor: pointer; padding: 0 0.4rem; }}
      </style>
      <div class="mb-3 flex items-center justify-between">
        <a href="{PREFIX}/preview/" class="text-sm text-stone-500 hover:text-stone-900">← Library</a>
        <button type="button" id="shop-reset"
                class="text-sm text-stone-500 hover:text-stone-900 hidden">
          Uncheck all
        </button>
      </div>
      <div class="card p-6">
        <div class="text-[10px] uppercase tracking-[0.25em] text-stone-400 mb-1">Marketing list</div>
        <h1 class="serif text-4xl font-medium tracking-tight leading-none">The <em class="text-stone-500">shop</em>.</h1>
        <div class="mt-4 mb-6">
          <div class="text-sm text-stone-500 mb-2">
            From {len(recipes)} recipe{'s' if len(recipes) != 1 else ''}:
          </div>
          <div class="flex flex-wrap gap-2">{recipe_chips}</div>
        </div>
        {''.join(sections)}

        <!-- Quick-add ad-hoc items — stored in localStorage, scoped to
             this exact list (same recipe-id set ⇒ same extras). -->
        <div class="mt-6 mb-4">
          <h3 class="serif text-xl font-medium tracking-tight mb-2">Extras</h3>
          <ul id="extras-list"></ul>
          <form id="extras-form" class="mt-3 flex items-center gap-2">
            <input id="extras-input" type="text" placeholder="Add an item…"
                   class="flex-1 border border-stone-200 rounded-lg px-3 py-2 bg-white"
                   autocomplete="off">
            <button type="submit"
                    class="bg-stone-900 hover:bg-stone-700 text-white text-sm font-medium px-4 py-2 rounded-lg">
              Add
            </button>
          </form>
        </div>

        <p class="text-xs text-stone-400 mt-6 serif italic">
          ×N means N recipes contributed to this row. Optional /
          to-taste ingredients are not aggregated.
        </p>
      </div>
      <script>
        // localStorage-backed checkbox state. Key includes the sorted
        // recipe-id set so a different shopping list gets a fresh slate.
        // Per-device only — fine for v0; shared state can come later.
        // Household ask: checked rows slide to the bottom of their section
        // so the active list stays at the top. AnyList pattern.
        (() => {{
          const STORAGE_KEY = {storage_key!r};
          const resetBtn = document.getElementById('shop-reset');
          const rows = [...document.querySelectorAll('.shop-row')];
          const sections = [...document.querySelectorAll('ul')]
            .filter(ul => ul.querySelector('.shop-row'));

          const load = () => {{
            try {{
              return new Set(JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]'));
            }} catch {{ return new Set(); }}
          }};
          const save = (set) => {{
            localStorage.setItem(STORAGE_KEY, JSON.stringify([...set]));
          }};
          const refreshReset = (set) => {{
            resetBtn.classList.toggle('hidden', set.size === 0);
          }};

          // Move checked <li>s to the bottom of the <ul>, insert/update
          // a "Crossed off · N" divider between unchecked and checked.
          function reorderSection(ul) {{
            const lis = [...ul.children].filter(el =>
              el.tagName === 'LI' && !el.classList.contains('crossed-divider')
            );
            const unchecked = lis.filter(li => !li.querySelector('.shop-row.checked'));
            const checkedRows = lis.filter(li => li.querySelector('.shop-row.checked'));
            ul.innerHTML = '';
            unchecked.forEach(li => ul.appendChild(li));
            if (checkedRows.length > 0) {{
              const divider = document.createElement('li');
              divider.className = 'crossed-divider';
              divider.textContent = `Crossed off · ${{checkedRows.length}}`;
              ul.appendChild(divider);
              checkedRows.forEach(li => ul.appendChild(li));
            }}
          }}

          let checked = load();
          rows.forEach(row => {{
            const key = row.dataset.key;
            const cb = row.querySelector('.shop-check');
            if (checked.has(key)) {{
              cb.checked = true;
              row.classList.add('checked');
            }}
            cb.addEventListener('change', () => {{
              if (cb.checked) {{ checked.add(key); row.classList.add('checked'); }}
              else {{ checked.delete(key); row.classList.remove('checked'); }}
              save(checked);
              refreshReset(checked);
              // Reorder this row's section. The row's parent is the
              // <label>'s parent's parent: label > li > ul.
              const ul = row.closest('ul');
              if (ul) reorderSection(ul);
            }});
          }});
          // Initial sort so reloads land with the right order.
          sections.forEach(reorderSection);
          refreshReset(checked);

          resetBtn.addEventListener('click', () => {{
            checked = new Set();
            save(checked);
            rows.forEach(row => {{
              row.querySelector('.shop-check').checked = false;
              row.classList.remove('checked');
            }});
            sections.forEach(reorderSection);
            refreshReset(checked);
          }});

          /* Ad-hoc "extras" — localStorage-backed for now, scoped to this
             list. Household want #4: add items at the store without picking
             a recipe. Cross-device sync is a future server-side concern. */
          const EXTRAS_KEY = 'mise:extras:' + ({storage_key!r}).split(':').pop();
          const EXTRAS_CHK_KEY = EXTRAS_KEY + ':checked';
          const list = document.getElementById('extras-list');
          const form = document.getElementById('extras-form');
          const input = document.getElementById('extras-input');

          const loadExtras = () => {{
            try {{ return JSON.parse(localStorage.getItem(EXTRAS_KEY) || '[]'); }}
            catch {{ return []; }}
          }};
          const saveExtras = (arr) => localStorage.setItem(EXTRAS_KEY, JSON.stringify(arr));
          const loadExtraChk = () => {{
            try {{ return new Set(JSON.parse(localStorage.getItem(EXTRAS_CHK_KEY) || '[]')); }}
            catch {{ return new Set(); }}
          }};
          const saveExtraChk = (set) => localStorage.setItem(EXTRAS_CHK_KEY, JSON.stringify([...set]));

          function renderExtras() {{
            const arr = loadExtras();
            const chk = loadExtraChk();
            list.innerHTML = arr.map((name, i) => `
              <li class="extra-row flex items-center gap-3 py-2 border-b border-stone-100 last:border-0" data-idx="${{i}}">
                <input type="checkbox" class="ex-check h-5 w-5 accent-amber-700 cursor-pointer flex-shrink-0" ${{chk.has(name) ? 'checked' : ''}}>
                <span class="flex-1 ex-name ${{chk.has(name) ? 'line-through text-stone-400' : ''}}">${{name.replace(/[&<>"']/g, c => ({{ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }})[c])}}</span>
                <button type="button" class="ex-del text-stone-300 hover:text-rose-500 px-2" aria-label="Remove">×</button>
              </li>`).join('') || '<li class="text-xs text-stone-400 italic py-2">No extras yet — add things you spot at the store.</li>';
            list.querySelectorAll('.ex-check').forEach((cb, i) => {{
              cb.addEventListener('change', () => {{
                const arr = loadExtras();
                const set = loadExtraChk();
                if (cb.checked) set.add(arr[i]); else set.delete(arr[i]);
                saveExtraChk(set); renderExtras();
              }});
            }});
            list.querySelectorAll('.ex-del').forEach((btn, i) => {{
              btn.addEventListener('click', () => {{
                const arr = loadExtras();
                const set = loadExtraChk();
                set.delete(arr[i]);
                arr.splice(i, 1);
                saveExtras(arr); saveExtraChk(set); renderExtras();
              }});
            }});
          }}
          form.addEventListener('submit', (e) => {{
            e.preventDefault();
            const v = input.value.trim();
            if (!v) return;
            const arr = loadExtras();
            arr.push(v);
            saveExtras(arr);
            input.value = '';
            renderExtras();
            input.focus();
          }});
          renderExtras();
        }})();
      </script>
    """
    return HTMLResponse(_shell("Shopping list", body))
