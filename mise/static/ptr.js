// Pull to refresh, on every app page. The installed app has no browser
// reload, and the service worker shows the saved copy of a page first,
// so this drops that copy before reloading.
//
// Pulling heats a little pot (steam fades in, the spoon swings in);
// letting go past the line stirs it while the fresh page loads.
(function () {
  const LINE = 140;      // px of finger travel needed
  const MIN_STIR = 900;  // ms the stir shows even on a fast reload

  const css = document.createElement('style');
  css.textContent = `
    .ptr { position: fixed; left: 50%; top: 0; z-index: 200; pointer-events: none;
      transform: translate(-50%, var(--ptr-off)); opacity: 0; display: flex; align-items: center; gap: 10px;
      padding: 8px 18px 8px 10px; margin-top: calc(env(safe-area-inset-top) + 10px);
      /* Fully off screen: its own height plus the status-bar area and shadow. */
      --ptr-off: calc(-100% - env(safe-area-inset-top) - 40px);
      background: #fff; color: #78350F; border: 1px solid #FCD34D; border-radius: 999px;
      box-shadow: 0 10px 28px -10px rgba(120,80,40,0.45); font: 600 14px Inter, system-ui, sans-serif;
      white-space: nowrap; }
    .ptr.settle { transition: transform 0.25s cubic-bezier(.2,.9,.3,1.2), opacity 0.2s; }
    .ptr svg { width: 38px; height: 38px; overflow: visible; }
    .ptr .spoon { transform-box: view-box; transform-origin: 21px 25px; }
    .ptr .steam path { opacity: 0; }
    .ptr.ready { border-color: #F59E0B; }
    .ptr.stirring .spoon { animation: ptr-stir 0.7s ease-in-out infinite alternate; }
    .ptr.stirring .steam path { animation: ptr-steam 1.2s ease-out infinite; }
    .ptr.stirring .steam path:nth-child(2) { animation-delay: 0.4s; }
    .ptr.stirring .steam path:nth-child(3) { animation-delay: 0.8s; }
    @keyframes ptr-stir { from { transform: translateX(-4px) rotate(-16deg); } to { transform: translateX(4px) rotate(16deg); } }
    @keyframes ptr-steam { 0% { opacity: 0; transform: translateY(4px); } 30% { opacity: 1; } 100% { opacity: 0; transform: translateY(-9px); } }
  `;
  document.head.appendChild(css);

  const el = document.createElement('div');
  el.className = 'ptr';
  el.setAttribute('aria-hidden', 'true');
  el.innerHTML = `
    <svg viewBox="0 0 40 40">
      <g class="steam" fill="none" stroke="#D97706" stroke-width="1.8" stroke-linecap="round">
        <path d="M13 14 q-2.5 -3 0 -6 q2.5 -3 0 -6"/>
        <path d="M20 13 q-2.5 -3 0 -6 q2.5 -3 0 -6"/>
        <path d="M27 14 q-2.5 -3 0 -6 q2.5 -3 0 -6"/>
      </g>
      <g class="spoon">
        <line x1="21" y1="25" x2="31" y2="6" stroke="#57534E" stroke-width="2.4" stroke-linecap="round"/>
      </g>
      <rect x="2.5" y="23" width="5" height="3" rx="1.5" fill="#92400E"/>
      <rect x="32.5" y="23" width="5" height="3" rx="1.5" fill="#92400E"/>
      <path d="M7 21 h26 v8 a7 7 0 0 1 -7 7 h-12 a7 7 0 0 1 -7 -7 z" fill="#B45309"/>
      <rect x="5" y="18.5" width="30" height="3.5" rx="1.75" fill="#78350F"/>
    </svg>
    <span class="ptr-text">Pull to refresh</span>`;
  document.body.appendChild(el);
  // Haptic tick on letting go past the line. iPhones have no vibrate API,
  // but toggling a native switch control gives a light tap (iOS 18+), and
  // only from a finger-up event: mid-drag it does nothing.
  const tick = document.createElement('label');
  tick.setAttribute('aria-hidden', 'true');
  tick.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0;pointer-events:none;';
  tick.innerHTML = '<input type="checkbox" switch tabindex="-1">';
  document.body.appendChild(tick);
  function nudge() {
    try { if (navigator.vibrate) navigator.vibrate(12); } catch (e) {}
    try { tick.click(); } catch (e) {}
  }
  const text = el.querySelector('.ptr-text');
  const spoon = el.querySelector('.spoon');
  const steam = el.querySelectorAll('.steam path');

  // p: 0 = hidden above the screen, 1 = fully in view.
  function place(p, settle) {
    el.classList.toggle('settle', !!settle);
    el.style.transform = `translate(-50%, calc(var(--ptr-off) * ${(1 - p).toFixed(3)}))`;
    el.style.opacity = p > 0 ? '1' : '0';
  }
  const hide = () => { place(0, true); el.classList.remove('ready'); };

  // Not while a sheet has locked the page, or inside a fixed or scrolled panel.
  function blocked(t) {
    if (document.body.style.position === 'fixed') return true;
    for (let n = t; n && n !== document.body; n = n.parentElement) {
      if (n.scrollTop > 0 || getComputedStyle(n).position === 'fixed') return true;
    }
    return false;
  }

  let startY = 0, pulling = false, busy = false;
  document.addEventListener('touchstart', e => {
    pulling = !busy && window.scrollY <= 0 && !blocked(e.target);
    startY = e.touches[0].clientY;
  }, { passive: true });

  document.addEventListener('touchmove', e => {
    if (!pulling) return;
    const dy = e.touches[0].clientY - startY;
    if (dy <= 0) return hide();
    const p = Math.min(1, dy / LINE);
    // Follows the finger with some weight: fully in view at the line.
    place(p, false);
    spoon.style.transform = `rotate(${(1 - p) * 40}deg)`;
    steam.forEach((s, i) => { s.style.opacity = Math.max(0, p * 1.4 - i * 0.2).toFixed(2); });
    const ready = p >= 1;
    if (ready !== el.classList.contains('ready')) {
      el.classList.toggle('ready', ready);
      text.textContent = ready ? 'Let go to refresh' : 'Pull to refresh';
    }
  }, { passive: true });

  document.addEventListener('touchend', async e => {
    if (!pulling) return;
    pulling = false;
    const dy = (e.changedTouches[0]?.clientY ?? startY) - startY;
    if (dy < LINE || window.scrollY > 0) return hide();
    busy = true;
    nudge();
    spoon.style.transform = '';
    steam.forEach(s => { s.style.opacity = ''; });
    el.classList.remove('ready');
    el.classList.add('stirring');
    text.textContent = 'Refreshing…';
    place(1, true);
    const wait = new Promise(r => setTimeout(r, MIN_STIR));
    try {
      for (const k of await caches.keys()) await (await caches.open(k)).delete(location.href.split('#')[0]);
    } catch (err) {}
    await wait;
    location.reload();
  }, { passive: true });
})();
