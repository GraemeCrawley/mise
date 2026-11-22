// The one recipe search + category filter, shared by the Library and the
// plan pickers. app.py's _recipe_filter() renders the search box and tag
// chips; they point (data-rf) at a list whose rows carry data-q (name
// text, lowercase) and data-tags (tags joined by |).
(function () {
  function setup(root) {
    const list = document.getElementById(root.dataset.rf);
    const input = root.querySelector('.rf-q');
    const chips = root.querySelectorAll('.rf-chip');
    const empty = root.dataset.rfEmpty && document.getElementById(root.dataset.rfEmpty);
    let tag = root.querySelector('.rf-chip[aria-pressed="true"]')?.dataset.tag || '';

    function apply() {
      const q = input.value.trim().toLowerCase();
      let shown = 0;
      list.querySelectorAll('[data-q]').forEach(r => {
        const ok = (!q || r.dataset.q.includes(q))
          && (!tag || (r.dataset.tags || '').split('|').includes(tag));
        r.style.display = ok ? '' : 'none';
        if (ok) shown++;
      });
      if (empty) empty.hidden = shown > 0;
    }
    function pick(t) {
      tag = t;
      chips.forEach(c => c.setAttribute('aria-pressed', String(c.dataset.tag === t)));
      apply();
    }
    // Tapping the chosen chip again goes back to All.
    chips.forEach(c => c.addEventListener('click', () => pick(tag === c.dataset.tag ? '' : c.dataset.tag)));
    input.addEventListener('input', apply);
    input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); input.blur(); } });
    root._rf = { reset() { input.value = ''; pick(''); } };
    apply();
  }

  const init = () => document.querySelectorAll('[data-rf]').forEach(r => r._rf || setup(r));
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.RecipeFilter = {
    // Clear the search and go back to All — used when a picker reopens.
    reset(listId) {
      init();
      const root = document.querySelector(`[data-rf="${listId}"]`);
      if (root) root._rf.reset();
    },
  };
})();
