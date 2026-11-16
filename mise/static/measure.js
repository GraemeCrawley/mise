// Mise measurements — the browser half of services/measure.py.
// Every <span class="measure" data-qty data-unit> on any page:
//   - rescales when something sets data-scale (the serving stepper) and
//     calls Measure.render(el);
//   - if its unit converts (volume / mass), tapping it opens a dropdown
//     of the other units. The pick is remembered per unit on this phone,
//     so choosing "g" for cups once shows grams wherever cups appear.
(function () {
  const T = window.MISE_UNITS || { units: {}, choices: {} };
  const GLYPHS = [[0.125, '⅛'], [0.25, '¼'], [1 / 3, '⅓'], [0.375, '⅜'], [0.5, '½'],
                  [0.625, '⅝'], [2 / 3, '⅔'], [0.75, '¾'], [0.875, '⅞']];
  const KEY = 'mise:unit-pref';
  let prefs = {};
  try { prefs = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) {}

  function fmt(q) {
    if (q == null || isNaN(q)) return '';
    if (Math.abs(q - Math.round(q)) < 0.01) return String(Math.round(q));
    const whole = Math.floor(q), frac = q - whole;
    if (q < 20) for (const [f, g] of GLYPHS) if (Math.abs(frac - f) < 0.02) return (whole || '') + g;
    if (q >= 100) return String(Math.round(q));
    return (q >= 10 ? q.toFixed(1) : q.toFixed(2)).replace(/\.?0+$/, '');
  }
  // density (g per ml) bridges volume and weight; without it they don't mix.
  function convert(qty, from, to, density) {
    const a = T.units[from], b = T.units[to];
    if (!a || !b) return null;
    let base = qty * a[1];
    if (a[0] !== b[0]) {
      if (!density) return null;
      base = a[0] === 'volume' ? base * density : base / density;
    }
    return base / b[1];
  }
  const dens = el => parseFloat(el.dataset.density || '0') || 0;
  // "~" marks a weight worked out from a volume (or back): an estimate.
  const label = (q, u, from) => (T.units[u] && T.units[from] && T.units[u][0] !== T.units[from][0] ? '~' : '')
    + (fmt(q) + (u ? ' ' + u : '')).trim();
  function shown(el) {
    const unit = el.dataset.unit || '';
    const show = el.dataset.show || prefs[unit] || unit;
    const qty = parseFloat(el.dataset.qty || '0') * parseFloat(el.dataset.scale || '1');
    const q = show !== unit ? convert(qty, unit, show, dens(el)) : qty;
    return q == null ? { qty, unit } : { qty: q, unit: show };
  }
  function renderAll(root) { (root || document).querySelectorAll('.measure').forEach(renderAt); }

  // One shared dropdown, moved to whichever quantity was tapped.
  let menu = null, target = null;
  function close() { if (menu) menu.hidden = true; target = null; }
  function open(el) {
    if (!menu) {
      menu = document.createElement('div');
      menu.className = 'measure-menu';
      menu.hidden = true;
      document.body.appendChild(menu);
      menu.addEventListener('click', e => {
        const b = e.target.closest('button[data-unit]');
        e.preventDefault(); e.stopPropagation();
        if (!b || !target) return;
        const unit = target.dataset.unit;
        prefs[unit] = b.dataset.unit === unit ? '' : b.dataset.unit;
        if (!prefs[unit]) delete prefs[unit];
        try { localStorage.setItem(KEY, JSON.stringify(prefs)); } catch (e2) {}
        document.querySelectorAll('.measure').forEach(m => {
          if (m.dataset.unit === unit) { delete m.dataset.show; renderAt(m); }
        });
        close();
      });
    }
    target = el;
    const unit = el.dataset.unit, dim = T.units[unit][0], d = dens(el);
    const qty = parseFloat(el.dataset.qty) * parseFloat(el.dataset.scale || '1');
    const cur = shown(el).unit;
    const units = T.choices[dim].concat(d ? T.choices[dim === 'volume' ? 'mass' : 'volume'] : []);
    menu.innerHTML = units.map(u =>
      `<button type="button" data-unit="${u}" class="${u === cur ? 'is-current' : ''}">${label(convert(qty, unit, u, d), u, unit)}</button>`
    ).join('');
    menu.hidden = false;
    const r = el.getBoundingClientRect(), mh = menu.offsetHeight, mw = menu.offsetWidth;
    const below = r.bottom + 4;
    menu.style.top = Math.max(8, below + mh > window.innerHeight - 8 ? r.top - 4 - mh : below) + 'px';
    menu.style.left = Math.max(8, Math.min(r.left, window.innerWidth - mw - 8)) + 'px';
  }
  function renderAt(el) {
    const s = shown(el);
    el.textContent = label(s.qty, s.unit, el.dataset.unit || '');
  }

  // Capture phase: a quantity inside a <label> (shopping list) must
  // not tick the row's checkbox.
  document.addEventListener('click', e => {
    const el = e.target.closest && e.target.closest('.measure-tap');
    if (el && T.units[el.dataset.unit]) {
      e.preventDefault(); e.stopPropagation();
      if (target === el && menu && !menu.hidden) close(); else open(el);
      return;
    }
    if (menu && !menu.hidden && !e.target.closest('.measure-menu')) close();
  }, true);
  window.addEventListener('scroll', close, { passive: true });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });

  window.Measure = { fmt, convert, render: renderAt, renderAll };
  document.addEventListener('DOMContentLoaded', () =>
    document.querySelectorAll('.measure').forEach(m => { if (prefs[m.dataset.unit]) renderAt(m); }));
})();
