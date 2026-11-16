// Mise measurements — the browser half of services/measure.py.
// Every <span class="measure" data-qty data-unit> on any page:
//   - rescales when something sets data-scale (the serving stepper) and
//     calls Measure.render(el);
//   - if its unit converts (volume / mass), tapping it opens a dropdown
//     of the other units. The pick is remembered per unit on this phone,
//     so choosing "g" for cups once shows grams wherever cups appear.
//     The dropdown's Calculator button opens a keypad with the amount in.
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
        if (e.target.closest('[data-calc]') && target) { const t = target; close(); calc(t); return; }
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
    ).join('') + `<button type="button" data-calc class="measure-calc-open">${CALC_ICON}Calculator</button>`;
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

  // Calculator, opened from the unit dropdown with the amount already in.
  // Tokens are numbers ({v, s}: value and how it's shown) or operators.
  // The starting amount is one token, so ⅓ cup stays exactly a third.
  const CALC_ICON = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" '
    + 'stroke-linecap="round" aria-hidden="true"><rect x="5" y="3" width="14" height="18" rx="2.5"/><rect x="8" y="6" width="8" height="3.5" rx="0.8"/>'
    + '<path d="M8.5 13h.01M12 13h.01M15.5 13h.01M8.5 16.5h.01M12 16.5h.01M15.5 16.5h.01"/></svg>';
  const OPS = { '+': 1, '−': 1, '×': 2, '÷': 2 };
  function evaluate(toks) {
    let i = 0;
    const peek = () => toks[i];
    function atom() {
      const t = toks[i++];
      if (t === '−') { const v = atom(); return v == null ? null : -v; }
      if (t === '(') { const v = expr(0); if (peek() === ')') i++; return v; }
      return t && typeof t === 'object' ? t.v : null;
    }
    function expr(min) {
      let left = atom();
      while (left != null && OPS[peek()] && OPS[peek()] > min) {
        const op = toks[i++], right = expr(OPS[op]);
        if (right == null) return null;
        left = op === '+' ? left + right : op === '−' ? left - right : op === '×' ? left * right : left / right;
      }
      return left;
    }
    // While typing, ignore a trailing operator or open bracket.
    let n = toks.length;
    while (n && (OPS[toks[n - 1]] || toks[n - 1] === '(')) n--;
    toks = toks.slice(0, n);
    const v = toks.length ? expr(0) : null;
    return v == null || !isFinite(v) ? null : v;
  }

  // Fraction keys; after a whole number they make a mixed number (1 then ½ = 1½).
  const FRACS = [[0.125, '⅛'], [0.25, '¼'], [1 / 3, '⅓'], [0.5, '½'], [2 / 3, '⅔'], [0.75, '¾']];
  const FRAC = Object.fromEntries(FRACS.map(([f, g]) => [g, f]));
  // Cups and spoons read best as the nearest kitchen fraction (eighths or
  // thirds); the exact value shows underneath when it was rounded.
  const IMPERIAL = new Set(['tsp', 'tbsp', 'cup', 'fl oz', 'oz', 'lb']);
  function kitchen(v, unit) {
    if (!IMPERIAL.has(unit) || Math.abs(v) < 0.1) return v;
    const w = Math.floor(v), f = v - w;
    let best = 0;
    for (const c of [0, 0.125, 0.25, 1 / 3, 0.375, 0.5, 0.625, 2 / 3, 0.75, 0.875, 1])
      if (Math.abs(f - c) < Math.abs(f - best)) best = c;
    return w + best;
  }

  let pad = null, st = null;
  function calc(el) {
    const s = shown(el), from = el.dataset.unit, d = dens(el), dim = T.units[from][0];
    st = { toks: [{ v: s.qty, s: fmt(s.qty), fixed: true }], unit: s.unit, from, d,
           units: T.choices[dim].concat(d ? T.choices[dim === 'volume' ? 'mass' : 'volume'] : []) };
    if (!pad) {
      pad = document.createElement('div');
      pad.className = 'measure-calc';
      pad.hidden = true;
      const keys = ['C', '(', ')', '÷', '7', '8', '9', '×', '4', '5', '6', '−', '1', '2', '3', '+', '0', '.', '⌫', '='];
      pad.innerHTML = `<div class="mc-panel" role="dialog" aria-label="Calculator">
        <div class="mc-head"><span>${CALC_ICON} Calculator</span><button type="button" data-k="close" aria-label="Close">✕</button></div>
        <div class="mc-expr"></div><div class="mc-out"></div><div class="mc-exact"></div>
        <div class="mc-units"></div>
        <div class="mc-fracs">${FRACS.map(([f, g]) => `<button type="button" data-k="${g}">${g}</button>`).join('')}</div>
        <div class="mc-keys">${keys.map(k => `<button type="button" data-k="${k}" class="${OPS[k] || k === '=' ? 'is-op' : ''}">${k}</button>`).join('')}</div>
      </div>`;
      document.body.appendChild(pad);
      pad.addEventListener('click', e => {
        if (e.target === pad) { pad.hidden = true; return; }
        const b = e.target.closest('button');
        if (!b) return;
        if (b.dataset.u) setUnit(b.dataset.u); else key(b.dataset.k);
      });
      document.addEventListener('keydown', e => {
        if (!pad || pad.hidden) return;
        const map = { '*': '×', 'x': '×', '/': '÷', '-': '−', Enter: '=', '=': '=', Backspace: '⌫', Escape: 'close', Delete: 'C' };
        const k = map[e.key] || (/^[0-9.+()]$/.test(e.key) ? e.key : null);
        if (k) { e.preventDefault(); key(k); }
      });
    }
    paint();
    pad.hidden = false;
  }
  function collapse() {
    const v = evaluate(st.toks);
    if (v != null) st.toks = [{ v, s: fmt(v), fixed: true }];
    return v;
  }
  function key(k) {
    const t = st.toks, last = t[t.length - 1];
    const typing = last && typeof last === 'object' && !last.fixed && !last.frac;
    if (k === 'close') { pad.hidden = true; return; }
    if (k === 'C') st.toks = [];
    else if (k === '=') collapse();
    else if (FRAC[k] != null) {
      const whole = typing && /^\d+$/.test(last.s);
      if (whole) { last.v += FRAC[k]; last.s += k; last.frac = true; }
      else {
        if (last && (typeof last === 'object' || last === ')')) { if (last.fixed || last.frac) t.pop(); else t.push('×'); }
        t.push({ v: FRAC[k], s: k, frac: true });
      }
    } else if (k === '⌫') {
      if (last && last.frac && last.s.length > 1) { last.s = last.s.slice(0, -1); last.v = parseFloat(last.s); delete last.frac; }
      else if (typing && last.s.length > 1) { last.s = last.s.slice(0, -1); last.v = parseFloat(last.s) || 0; }
      else t.pop();
    } else if (/[0-9.]/.test(k)) {
      if (typing) { if (k === '.' && last.s.includes('.')) return; last.s += k; last.v = parseFloat(last.s) || 0; }
      else {
        if (last && (typeof last === 'object' || last === ')')) { if (last.fixed) t.pop(); else t.push('×'); }
        t.push({ v: parseFloat(k === '.' ? '0' : k), s: k === '.' ? '0.' : k });
      }
    } else if (k === '(') {
      if (last && (typeof last === 'object' || last === ')')) t.push('×');
      t.push('(');
    } else if (k === ')') {
      const open = t.filter(x => x === '(').length - t.filter(x => x === ')').length;
      if (open > 0 && last && (typeof last === 'object' || last === ')')) t.push(')');
    } else if (OPS[k]) {
      if (OPS[last]) t[t.length - 1] = k;
      else if (last || k === '−') t.push(k);
    }
    paint();
  }
  function setUnit(u) {
    const v = collapse();
    if (v != null && u !== st.unit) {
      const c = convert(v, st.unit, u, st.d);
      if (c != null) st.toks = [{ v: c, s: fmt(c), fixed: true }];
    }
    st.unit = u;
    paint();
  }
  function paint() {
    const v = evaluate(st.toks);
    const single = st.toks.length === 1 && typeof st.toks[0] === 'object';
    pad.querySelector('.mc-expr').textContent = single ? '' : st.toks.map(x => typeof x === 'object' ? x.s : x).join(' ');
    const k = v == null ? null : kitchen(v, st.unit);
    pad.querySelector('.mc-out').textContent = v == null ? (st.toks.length ? '…' : '0') : label(k, st.unit, st.from);
    pad.querySelector('.mc-exact').textContent = k != null && Math.abs(k - v) > 0.004 ? `exactly ${+v.toFixed(3)} ${st.unit}` : '';
    pad.querySelector('.mc-units').innerHTML = st.units.map(u =>
      `<button type="button" data-u="${u}" class="${u === st.unit ? 'is-current' : ''}">${u}</button>`).join('');
  }

  window.Measure = { fmt, convert, render: renderAt, renderAll, evaluate };
  document.addEventListener('DOMContentLoaded', () =>
    document.querySelectorAll('.measure').forEach(m => { if (prefs[m.dataset.unit]) renderAt(m); }));
})();
